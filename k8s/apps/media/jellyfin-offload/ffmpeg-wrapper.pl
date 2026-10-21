#!/usr/bin/perl
# Jellyfin's ffmpeg (JELLYFIN_FFMPEG points here). Jellyfin is configured for
# NVENC but runs on a GPU-less host (alpha); the GPU lives on omega, which is
# routinely offline. Per invocation:
#
#   no GPU constructs  -> run the real ffmpeg locally, unchanged (version and
#                         capability probes, trickplay, subtitle extraction).
#   GPU constructs     -> try the runner on omega. If omega is unreachable,
#                         busy, or the remote ffmpeg fails before producing any
#                         output, run the CPU-rewritten command locally instead.
#
# A transcode already running on omega when it drops is not recoverable (the
# stream dies and the client's retry takes the local path); everything else is
# invisible to Jellyfin. Decisions are logged to stderr with an [offload]
# prefix, which ends up in Jellyfin's per-transcode ffmpeg log.
#
# Env: FFMPEG_RUNNER_HOST, FFMPEG_RUNNER_PORT (9876), FFMPEG_RUNNER_TOKEN_FILE,
# FFMPEG_REAL, OFFLOAD_DISABLED=1 (force local), OFFLOAD_CONNECT_TIMEOUT (2),
# OFFLOAD_SILENCE_TIMEOUT (10).
use strict;
use warnings;
use Cwd qw(abs_path);
use File::Basename qw(dirname);
use IO::Socket::INET;
use IO::Select;

my $real    = $ENV{FFMPEG_REAL} // '/usr/lib/jellyfin-ffmpeg/ffmpeg';
my $host    = $ENV{FFMPEG_RUNNER_HOST} // 'jellyfin-ffmpeg-runner';
my $port    = $ENV{FFMPEG_RUNNER_PORT} // 9876;
my $tokfile = $ENV{FFMPEG_RUNNER_TOKEN_FILE} // '/offload-secret/token';
my $conn_to = $ENV{OFFLOAD_CONNECT_TIMEOUT} // 2;
my $silence = $ENV{OFFLOAD_SILENCE_TIMEOUT} // 10;

require(dirname(abs_path($0)) . '/ffmpeg-sw-fallback.pl');

my @args = @ARGV;

sub note { print STDERR "[offload] $_[0]\n"; }

sub run_local {
  my ($reason) = @_;
  note($reason) if $reason;
  my @cpu = eval { SwFallback::rewrite(@args) };
  if ($@) {
    print STDERR "[offload] cannot run on CPU: $@";
    exit 97;
  }
  exec { $real } $real, @cpu or die "exec $real: $!";
}

# Commands that need no GPU (probes, trickplay, subtitle extraction) run as-is.
exec { $real } $real, @args or die "exec $real: $!"
  unless SwFallback::needs_gpu(@args);

run_local('offload disabled') if $ENV{OFFLOAD_DISABLED};

my $token = do {
  open(my $fh, '<', $tokfile) or run_local("no runner token ($tokfile)");
  my $t = <$fh>;
  chomp $t if defined $t;
  $t;
};
run_local('empty runner token') unless defined $token && length $token;

my $sock = IO::Socket::INET->new(
  PeerAddr => $host, PeerPort => $port, Proto => 'tcp', Timeout => $conn_to,
) or run_local("omega unreachable ($host:$port: $!); running on CPU");

$SIG{PIPE} = 'IGNORE';
$sock->autoflush(1);
my $hello = "FFRUN1 $token\n" . scalar(@args) . "\n";
$hello .= length($_) . "\n$_\n" for @args;
unless (defined syswrite($sock, $hello)) {
  close $sock;
  run_local('omega write failed; running on CPU');
}

my $reply = '';
{
  my $sel = IO::Select->new($sock);
  while ($reply !~ /\n/ && length($reply) < 200) {
    last unless $sel->can_read(5);
    my $n = sysread($sock, my $c, 1);
    last unless $n;
    $reply .= $c;
  }
}
chomp $reply;
if ($reply ne 'OK') {
  close $sock;
  run_local('omega declined (' . ($reply || 'no reply') . '); running on CPU');
}
note("running on omega ($host)");
# A stalled runner must not block our writes, or the silence watchdog below
# would never run.
$sock->blocking(0);

# From here on we are committed to the remote run unless it fails before
# producing any output.
$SIG{$_} = sub { close $sock; exit 143 } for qw(TERM INT HUP);

my $sel = IO::Select->new($sock, \*STDIN);
my $buf = '';
my $last_seen = time;
my $progress = 0;
my $exit;
my $stdin_open = 1;

while (!defined $exit) {
  for my $fh ($sel->can_read(1)) {
    if ($fh == $sock) {
      my $n = sysread($sock, my $chunk, 65536);
      if (!$n) { $exit = 255; last; }
      $buf .= $chunk;
      $last_seen = time;
      while (length($buf) >= 5) {
        my ($type, $len) = (substr($buf, 0, 1), unpack('N', substr($buf, 1, 4)));
        last if length($buf) < 5 + $len;
        my $payload = substr($buf, 5, $len);
        substr($buf, 0, 5 + $len) = '';
        if ($type eq 'E') {
          $progress = 1 if $payload =~ /frame=\s*[1-9]/;
          syswrite(STDERR, $payload);
        } elsif ($type eq 'X') {
          $exit = $payload + 0;
        }
      }
    } else {
      my $n = sysread(STDIN, my $in, 65536);
      if (!$n) { $sel->remove(\*STDIN); $stdin_open = 0; next; }
      syswrite($sock, $in);
    }
  }
  if (!defined $exit && time - $last_seen > $silence) {
    note("omega silent for ${silence}s; treating as lost");
    $exit = 255;
  }
}
close $sock;

exit 0 if $exit == 0;
run_local("omega failed before producing output (exit $exit); running on CPU")
  unless $progress;
note("omega transcode ended with exit $exit");
exit($exit > 255 ? 1 : $exit);
