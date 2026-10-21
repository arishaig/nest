#!/usr/bin/perl
# Remote ffmpeg runner for Jellyfin transcode offload. Runs on omega (GPU)
# inside the stock Jellyfin image, which has perl but no python/ssh/nc, so the
# transport is a small TCP protocol rather than SSH.
#
# Client (ffmpeg-wrapper.pl on the Jellyfin pod) -> runner:
#   "FFRUN1 <token>\n<argc>\n" then argc times "<len>\n<bytes>\n"
#   then raw bytes, forwarded to ffmpeg's stdin (Jellyfin sends "q" to stop).
# Runner -> client:
#   "OK\n" (ffmpeg started) or "ERR <reason>\n", then frames of
#   <1-byte type><4-byte big-endian length><payload>:
#     E ffmpeg stderr   H heartbeat (empty)   X exit code (ASCII)
#
# The runner kills ffmpeg if the client disconnects or goes silent, so a
# killed Jellyfin or a vanished alpha never leaves a transcode running on the
# GPU. ffmpeg writes its output to the shared NFS volume, not over this socket.
#
# Env: RUNNER_TOKEN (required), RUNNER_PORT (9876), RUNNER_MAX (concurrent
# transcodes, 3), FFMPEG_REAL (/usr/lib/jellyfin-ffmpeg/ffmpeg).
use strict;
use warnings;
use IO::Socket::INET;
use IO::Select;
use POSIX qw(:sys_wait_h setsid);
use Socket qw(SOL_SOCKET SO_KEEPALIVE IPPROTO_TCP TCP_KEEPIDLE TCP_KEEPINTVL TCP_KEEPCNT);

my $token  = $ENV{RUNNER_TOKEN} // die "RUNNER_TOKEN is required\n";
my $port   = $ENV{RUNNER_PORT}  // 9876;
my $max    = $ENV{RUNNER_MAX}   // 3;
my $ffmpeg = $ENV{FFMPEG_REAL} // '/usr/lib/jellyfin-ffmpeg/ffmpeg';
my $hb_secs = 2;
my $max_args = 4096;
my $max_arg_len = 1 << 20;

$SIG{PIPE} = 'IGNORE';

my $listen = IO::Socket::INET->new(
  Listen => 16, LocalPort => $port, ReuseAddr => 1, Proto => 'tcp',
) or die "listen on $port: $!\n";
$| = 1;
logmsg("listening on :$port (max $max concurrent, ffmpeg=$ffmpeg)");

my %children;    # pid => 1, only used to enforce the concurrency cap
$SIG{CHLD} = sub {
  while ((my $pid = waitpid(-1, WNOHANG)) > 0) { delete $children{$pid}; }
};

while (1) {
  my $client = $listen->accept or next;
  if (keys(%children) >= $max) {
    syswrite($client, "ERR busy\n");
    close $client;
    next;
  }
  my $pid = fork();
  if (!defined $pid) { close $client; next; }
  if ($pid) { $children{$pid} = 1; close $client; next; }
  close $listen;
  $SIG{CHLD} = 'DEFAULT';
  handle($client);
  exit 0;
}

sub logmsg { my $t = scalar localtime; print "[$t] $_[0]\n"; }

sub read_line {
  my ($fh) = @_;
  my $line = '';
  my $sel = IO::Select->new($fh);
  while (length($line) < 4096) {
    return unless $sel->can_read(10);
    my $n = sysread($fh, my $c, 1);
    return unless $n;
    return $line if $c eq "\n";
    $line .= $c;
  }
  return;
}

sub read_exact {
  my ($fh, $len) = @_;
  my $buf = '';
  my $sel = IO::Select->new($fh);
  while (length($buf) < $len) {
    return unless $sel->can_read(10);
    my $n = sysread($fh, my $c, $len - length($buf));
    return unless $n;
    $buf .= $c;
  }
  return $buf;
}

# Constant-time comparison so response timing doesn't leak the token.
sub safe_eq {
  my ($a, $b) = @_;
  return 0 unless length($a) == length($b);
  my $d = 0;
  $d |= ord(substr($a, $_, 1)) ^ ord(substr($b, $_, 1)) for 0 .. length($a) - 1;
  return $d == 0;
}

sub frame {
  my ($type, $payload) = @_;
  return $type . pack('N', length $payload) . $payload;
}

sub handle {
  my ($c) = @_;
  my $peer = $c->peerhost // '?';
  $c->autoflush(1);
  setsockopt($c, SOL_SOCKET, SO_KEEPALIVE, 1);
  setsockopt($c, IPPROTO_TCP, TCP_KEEPIDLE, 10);
  setsockopt($c, IPPROTO_TCP, TCP_KEEPINTVL, 5);
  setsockopt($c, IPPROTO_TCP, TCP_KEEPCNT, 3);

  my $hello = read_line($c);
  my ($magic, $given) = split / /, ($hello // ''), 2;
  unless (($magic // '') eq 'FFRUN1' && safe_eq($given // '', $token)) {
    syswrite($c, "ERR auth\n");
    logmsg("$peer: rejected (bad hello/token)");
    return;
  }
  my $argc = read_line($c);
  unless (defined $argc && $argc =~ /^\d+$/ && $argc > 0 && $argc <= $max_args) {
    syswrite($c, "ERR argc\n");
    return;
  }
  my @args;
  for (1 .. $argc) {
    my $len = read_line($c);
    unless (defined $len && $len =~ /^\d+$/ && $len <= $max_arg_len) {
      syswrite($c, "ERR arglen\n");
      return;
    }
    my $arg = read_exact($c, $len + 1);    # bytes + trailing newline
    unless (defined $arg) { syswrite($c, "ERR short\n"); return; }
    chop $arg;
    push @args, $arg;
  }

  pipe(my $err_r, my $err_w) or die "pipe: $!";
  pipe(my $in_r, my $in_w)   or die "pipe: $!";
  my $ff = fork();
  if (!defined $ff) { syswrite($c, "ERR fork\n"); return; }
  if (!$ff) {
    setsid();    # own process group so we can kill ffmpeg and any children
    close $err_r; close $in_w; close $c;
    open(STDIN,  '<&', $in_r)  or die;
    open(STDERR, '>&', $err_w) or die;
    open(STDOUT, '>', '/dev/null') or die;
    exec { $ffmpeg } $ffmpeg, @args or die "exec $ffmpeg: $!";
  }
  close $err_w; close $in_r;
  # Never let a full stdin pipe (ffmpeg not reading it) block the loop: we
  # must keep noticing a dead client. Dropping stdin bytes is harmless; the
  # only thing Jellyfin ever sends is a one-byte "q".
  $in_w->blocking(0);
  syswrite($c, "OK\n");
  logmsg("$peer: started pid $ff ($argc args)");

  my $sel = IO::Select->new($c, $err_r);
  my $last_hb = time;
  my $done = 0;
  my $client_gone = 0;
  my $status;
  while (!$done) {
    for my $fh ($sel->can_read(1)) {
      if ($fh == $err_r) {
        my $n = sysread($err_r, my $buf, 65536);
        if (!$n) { $sel->remove($err_r); $done = 1; next; }
        unless (defined syswrite($c, frame('E', $buf))) { $client_gone = 1; }
      } else {
        my $n = sysread($c, my $buf, 65536);
        if (!$n) { $client_gone = 1; next; }
        syswrite($in_w, $buf);
      }
    }
    if (time - $last_hb >= $hb_secs) {
      $last_hb = time;
      $client_gone = 1 unless defined syswrite($c, frame('H', ''));
    }
    last if $client_gone;
  }

  if ($client_gone) {
    logmsg("$peer: client gone, killing pid $ff");
    kill_group($ff);
    waitpid($ff, 0);
    return;
  }
  waitpid($ff, 0);
  my $code = $? & 127 ? 128 + ($? & 127) : $? >> 8;
  syswrite($c, frame('X', $code));
  logmsg("$peer: pid $ff exited $code");
}

sub kill_group {
  my ($pid) = @_;
  kill 'TERM', -$pid;
  for (1 .. 50) {
    return if waitpid($pid, WNOHANG) == $pid;
    select(undef, undef, undef, 0.1);
  }
  kill 'KILL', -$pid;
}
