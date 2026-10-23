# Rewrites Tunarr's software ffmpeg command lines for NVENC/NVDEC on omega
# (docs/tunarr-offload.md). Pure functions, no I/O, so they can be tested
# without a GPU. Not deployed yet.
#
# Tunarr stays in CPU mode (transcode config hardwareAccelerationMode=none), so
# the original arguments are always a valid fallback: if the rewrite is not
# applicable, or omega fails before producing output, the wrapper runs them
# unchanged, which is exactly today's behaviour.
package NvencRewrite;
use strict;
use warnings;

# Decoders ffmpeg's cuda hwaccel can drive on Ampere. Anything else (and
# 10-bit H.264, which NVDEC cannot do) either keeps CPU decode or fails before
# output and falls back.
my %NVDEC = map { $_ => 1 } qw(h264 hevc mpeg1video mpeg2video mpeg4 vc1 vp8 vp9 av1 mjpeg);

# x264 preset -> NVENC preset (p1 fastest .. p7 slowest).
my %PRESET = (
  ultrafast => 'p1', superfast => 'p2', veryfast => 'p3', faster => 'p4',
  fast => 'p4', medium => 'p5', slow => 'p6', slower => 'p7', veryslow => 'p7',
);

sub _value_after {
  my ($flag, @a) = @_;
  for my $i (0 .. $#a - 1) { return $a[$i + 1] if $a[$i] eq $flag }
  return undef;
}

# Only the transcode that reads a remote file and writes HLS is worth sending
# to omega. The error-screen command (still image from localhost), the concat
# process (reads Tunarr's own playlist from localhost) and capability probes
# (-version) must stay local: localhost does not resolve on omega.
sub should_offload {
  my @a = @_;
  my $in = _value_after('-i', @a);
  return 0 unless defined $in && $in =~ m{^https?://(?!localhost\b|127\.|\[::1\])}i;
  return 0 unless (_value_after('-f', @a) // '') eq 'hls';
  my $seen_input = 0;
  for my $i (0 .. $#a - 1) {
    $seen_input = 1 if $a[$i] eq '-i';
    return 1 if $seen_input && $a[$i] eq '-c:v' && $a[$i + 1] eq 'libx264';
  }
  return 0;
}

sub rewrite {
  my @a = @_;
  my (@out, $seen_input);
  for (my $i = 0; $i < @a; $i++) {
    my $x = $a[$i];
    if ($x eq '-i') { $seen_input = 1; push @out, $x, $a[++$i]; next }
    # Input-side "-c:v hevc" forces CPU decode; use NVDEC instead. Frames come
    # back to system memory, so Tunarr's CPU filters (scale, pad, setsar) are
    # untouched.
    if (!$seen_input && $x eq '-c:v' && defined $a[$i + 1] && $NVDEC{ $a[$i + 1] }) {
      push @out, '-hwaccel', 'cuda'; $i++; next;
    }
    if ($seen_input && $x eq '-c:v' && ($a[$i + 1] // '') eq 'libx264') {
      # Ampere NVENC cannot encode 10-bit H.264 ("Function not implemented"),
      # and the CPU path emits High 10 for 10-bit sources; force 8-bit.
      push @out, '-pix_fmt', 'yuv420p', '-c:v', 'h264_nvenc'; $i++; next;
    }
    if ($seen_input && $x eq '-preset:v') {
      push @out, '-preset:v', $PRESET{ $a[$i + 1] // '' } // 'p4'; $i++; next;
    }
    # x264-only; NVENC has no equivalent flag by this name.
    if ($x eq '-sc_threshold') { $i++; next }
    # Segment boundaries must be IDR frames for HLS; NVENC needs asking.
    if ($x eq '-force_key_frames') { push @out, $x, $a[++$i], '-forced-idr', '1'; next }
    push @out, $x;
  }
  return @out;
}

1;
