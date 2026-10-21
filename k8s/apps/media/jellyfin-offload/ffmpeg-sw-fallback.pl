#!/usr/bin/perl
# Rewrite the ffmpeg arguments Jellyfin generates for NVENC/CUDA so they run
# on a CPU-only host (alpha). Used by the Jellyfin ffmpeg wrapper when omega
# is unreachable: Jellyfin is configured for NVENC, so every command line it
# builds assumes a GPU.
#
# Input : ffmpeg arguments (without the program name) as argv.
# Output: the rewritten arguments, NUL-terminated, on stdout.
# Exit 97 (message on stderr) if a hardware construct remains that this script
# does not know how to translate. Failing loudly beats guessing: a wrong
# rewrite produces a silently broken stream.
#
# Translations are derived from command lines captured from a real
# Jellyfin 12.1 / jellyfin-ffmpeg 8.1.2 session (see test/fixtures).
use strict;
use warnings;

my @in = @ARGV;

# Options that only exist to set up the GPU. Each takes one value.
my %hw_opt = map { $_ => 1 }
  qw(-init_hw_device -filter_hw_device -hwaccel -hwaccel_output_format -hwaccel_flags);

# GPU encoder -> CPU encoder (jellyfin-ffmpeg ships all of these).
my %encoder = (
  h264_nvenc => 'libx264',
  hevc_nvenc => 'libx265',
  av1_nvenc  => 'libsvtav1',
);
# NVENC presets p1..p7 mean nothing to the CPU encoders. Match the quality
# tier Jellyfin picks for software transcodes ("auto" -> veryfast).
my %preset = (libx264 => 'veryfast', libx265 => 'veryfast', libsvtav1 => '10');

my $had_hw = grep { $hw_opt{$_} } @in;

sub die_unsupported {
  my ($what) = @_;
  print STDERR "ffmpeg-sw-fallback: unsupported hardware construct: $what\n";
  exit 97;
}

sub fix_filter {
  my ($f) = @_;
  # Upload of the subtitle bitmap to the GPU; the software overlay takes
  # system-memory frames directly.
  $f =~ s/,\s*hwupload(?:_cuda)?=derive_device=cuda//g;
  # scale_cuda takes an output pixel format as an option; software scale
  # doesn't, so it moves to a trailing format filter.
  $f =~ s{\bscale_cuda=([^,;\[]*)}{
    my @opts = split /:/, $1;
    my ($fmt) = map { /^format=(.+)$/ ? $1 : () } @opts;
    my $rest = join ':', grep { !/^format=/ } @opts;
    ($rest ne '' ? "scale=$rest" : 'scale') . (defined $fmt ? ",format=$fmt" : '');
  }ge;
  $f =~ s/\btonemap_cuda=/tonemapx=/g;
  # Same option, different name in the software overlay.
  $f =~ s/\boverlay_cuda=([^,;\[]*)/'overlay=' . ($1 =~ s{\balpha_format=}{alpha=}gr)/ge;
  return $f;
}

# True if a hardware-only construct survives in an option or filter string.
# Single-quoted segments are file paths (subtitles f='...'), not filters.
sub still_hw {
  my ($s) = @_;
  $s =~ s/'[^']*'//g;
  return $s =~ /cuda|nvenc|nvdec|cuvid|_npp\b|hwupload|hwdownload|hwmap/i;
}

my @filter_opt = qw(-vf -filter_complex -lavfi);
my %is_filter_opt = map { $_ => 1 } @filter_opt;
my @out;
my $past_input = 0;
my $nvenc_seen = 0;
my $sw_encoder = '';

for (my $i = 0; $i < @in; $i++) {
  my $a = $in[$i];

  if ($hw_opt{$a}) { $i++; next; }

  # `-threads 1` before -i is the hardware-decode setting; software decode
  # of 4K needs ffmpeg's default threading.
  if (!$past_input && $had_hw && $a eq '-threads' && ($in[$i + 1] // '') eq '1') {
    $i++;
    next;
  }
  $past_input = 1 if $a eq '-i';

  if ($a =~ /^-(?:c|codec|vcodec)(?::v(?::\d+)?)?$/ && defined $in[$i + 1]
      && exists $encoder{ $in[$i + 1] }) {
    $sw_encoder = $encoder{ $in[$i + 1] };
    $nvenc_seen = 1;
    push @out, $a, $sw_encoder;
    $i++;
    next;
  }

  if ($a eq '-preset' && defined $in[$i + 1] && $in[$i + 1] =~ /^p[1-7]$/) {
    # The preset may precede the encoder option, so resolve it in a second pass.
    push @out, $a, "\0PRESET\0";
    $i++;
    next;
  }

  if ($is_filter_opt{$a} || $a =~ /^-filter(?::[a-z](?::\d+)?)?$/) {
    push @out, $a, fix_filter($in[$i + 1] // '');
    $i++;
    next;
  }

  push @out, $a;
}

# Resolve preset placeholders now that the encoder is known.
my $p = $preset{$sw_encoder} // 'veryfast';
for my $arg (@out) { $arg = $p if $arg eq "\0PRESET\0"; }

# Anything GPU-flavoured still left is something we don't understand.
# Check option names and filter strings only: file paths may legitimately
# contain words like "cuda".
my $skip_next = 0;
for (my $i = 0; $i < @out; $i++) {
  my $a = $out[$i];
  if ($skip_next) { $skip_next = 0; next; }
  if ($a eq '-i' || $a eq '-hls_segment_filename') { $skip_next = 1; next; }
  if ($a =~ /^-/) {
    die_unsupported($a) if still_hw($a);
    if ($is_filter_opt{$a} || $a =~ /^-filter(?::[a-z](?::\d+)?)?$/) {
      die_unsupported("$a $out[$i + 1]") if still_hw($out[$i + 1] // '');
      $skip_next = 1;
      next;
    }
    if ($a =~ /^-(?:c|codec|vcodec)(?::|$)/) {
      die_unsupported("$a $out[$i + 1]") if still_hw($out[$i + 1] // '');
    }
  }
}

print join("\0", @out), "\0";
