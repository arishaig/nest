#!/usr/bin/perl
# Tests nvenc-rewrite.pl against command lines Tunarr 1.3.15 really ran
# (captured from its logs on 2026-10-22; X-Emby-Token redacted there).
# Run: perl test-rewrite.pl   (no GPU needed)
use strict;
use warnings;
use Test::More;
use Text::ParseWords qw(shellwords);
use File::Basename qw(dirname);
use Cwd qw(abs_path);

require(dirname(abs_path($0)) . '/../nvenc-rewrite.pl');

my $out_tail = '-muxdelay 0 -muxpreload 0 -flags cgop -movflags +faststart -t 2733472ms '
  . '-video_track_timescale 90000 -b:v 4000k -maxrate:v 4000k -bufsize:v 8000k -c:v libx264 '
  . '-preset:v veryfast -sc_threshold 1000000000 -c:a aac -ac 2 -b:a 192k -maxrate:a 192k '
  . '-bufsize:a 384k -ar 48k -g 96 -keyint_min 96 -force_key_frames expr:gte(t,n_forced*4) '
  . '-f hls -hls_time 4 -hls_list_size 0 -segment_list_flags +live -hls_segment_type mpegts '
  . '-hls_segment_filename /config/tunarr/streams/stream_x/data%06d.ts '
  . '-hls_base_url /stream/channels/x/hls/ -master_pl_name playlist.m3u8 '
  . '-hls_flags program_date_time+append_list+independent_segments+omit_endlist '
  . '/config/tunarr/streams/stream_x/stream.m3u8';
my $head = "-nostdin -hide_banner -nostats -loglevel warning -fflags +genpts+discardcorrupt+igndts "
  . "-headers 'X-Emby-Token: REDACTED' -ss 972638ms -reconnect 1 -reconnect_on_network_error 1 "
  . "-reconnect_streamed 1 -multiple_requests 1";

# 1. 4K HEVC remux scaled to 1080p (what Cat TV / Planet Earth runs).
my $scaled = "$head -c:v hevc -readrate 1 -readrate_initial_burst 60 "
  . '-i http://jellyfin:8096/Videos/abc/stream?static=true '
  . '-filter_complex [0:0]scale=1920:1080:flags=bicubic:force_original_aspect_ratio=decrease,setsar=1,pad=1920:1080:-1:-1:color=black[v];[0:1]aresample=async=1,apad[a] '
  . "-map [v] -map [a] $out_tail";
# 2. Already-1080p source: no video filter, video goes straight to the encoder.
my $direct = "$head -c:v hevc -readrate 1 -readrate_initial_burst 60 "
  . '-i http://jellyfin:8096/Videos/def/stream?static=true '
  . "-filter_complex [0:1]aresample=async=1,apad[a] -map 0:0 -map [a] $out_tail";
# 3. Error screen: still image served by Tunarr itself.
my $error = '-nostdin -hide_banner -nostats -loglevel warning -reconnect 1 -readrate 1 '
  . '-i http://localhost:8000/images/generic-error-screen.png -f lavfi -i anullsrc '
  . "-filter_complex [1:0]aresample=async=1,apad[a] -map 0:0 -map [a] -t 1324251ms -r 24 -c:v libx264 "
  . '-f hls -hls_time 4 /config/tunarr/streams/stream_x/stream.m3u8';
# 4. The concat process reading Tunarr's own playlist.
my $concat = '-nostdin -hide_banner -threads 1 -loglevel error -nostats -fflags +genpts+discardcorrupt+igndts '
  . '-reconnect 1 -reconnect_at_eof 1 -rtbufsize 15M -readrate 1 '
  . '-i http://localhost:8000/stream/channels/x.m3u8?mode=hls -map 0 -c copy -f mpegts pipe:1';

my @scaled = shellwords($scaled);
my @direct = shellwords($direct);

ok(NvencRewrite::should_offload(@scaled), 'remote file -> HLS transcode is offloaded');
ok(NvencRewrite::should_offload(@direct), 'filterless variant is offloaded');
ok(!NvencRewrite::should_offload(shellwords($error)), 'error screen (localhost input) stays local');
ok(!NvencRewrite::should_offload(shellwords($concat)), 'concat (localhost playlist) stays local');
ok(!NvencRewrite::should_offload('-version'), 'capability probe stays local');
ok(!NvencRewrite::should_offload(grep { $_ ne 'libx264' } @scaled), 'no libx264 encode -> nothing to move');

my @r = NvencRewrite::rewrite(@scaled);
my $line = join ' ', @r;
like($line, qr/-hwaccel cuda -readrate 1/, 'input-side hevc decoder becomes NVDEC');
unlike($line, qr/-c:v hevc/, 'software decoder flag is gone');
like($line, qr/-pix_fmt yuv420p -c:v h264_nvenc/, 'encoder becomes NVENC, forced to 8-bit');
unlike($line, qr/libx264/, 'no x264 left');
like($line, qr/-preset:v p3/, 'veryfast maps to p3');
unlike($line, qr/sc_threshold/, 'x264-only flag dropped');
like($line, qr/-force_key_frames expr:gte\(t,n_forced\*4\) -forced-idr 1/, 'segment keyframes are IDR');
like($line, qr/-ss 972638ms/, 'seek untouched');
like($line, qr/-headers X-Emby-Token: REDACTED|-headers 'X-Emby-Token: REDACTED'/, 'auth header kept');

# The result is the original with exactly the intended substitutions, in order.
my $expect = join ' ', @scaled;
$expect =~ s/-c:v hevc/-hwaccel cuda/;
$expect =~ s/-c:v libx264/-pix_fmt yuv420p -c:v h264_nvenc/;
$expect =~ s/-preset:v veryfast/-preset:v p3/;
$expect =~ s/ -sc_threshold 1000000000//;
$expect =~ s/(-force_key_frames expr:gte\(t,n_forced\*4\))/$1 -forced-idr 1/;
is($line, $expect, 'only the intended arguments changed, order preserved');

my @rd = NvencRewrite::rewrite(@direct);
like(join(' ', @rd), qr/-hwaccel cuda .*-map 0:0 -map \[a\] .*-c:v h264_nvenc/, 'filterless variant rewritten too');

# Decoders NVDEC cannot do keep CPU decode (the encoder still moves).
my @wmv = map { $_ eq 'hevc' ? 'msmpeg4v3' : $_ } @scaled;
my $wl = join ' ', NvencRewrite::rewrite(@wmv);
like($wl, qr/-c:v msmpeg4v3/, 'unsupported decoder is left alone');
unlike($wl, qr/-hwaccel/, '...and no hwaccel is added');
like($wl, qr/-c:v h264_nvenc/, '...but encode still moves to NVENC');

# Tunarr forces libaom-av1 for AV1 input; the runner's build may lack it.
my @av1 = map { $_ eq 'hevc' ? 'libaom-av1' : $_ } @scaled;
my $av = join ' ', NvencRewrite::rewrite(@av1);
like($av, qr/-hwaccel cuda -readrate 1/, 'libaom-av1 input becomes NVDEC');
unlike($av, qr/libaom/, '...and the software decoder name is gone');
my @unk = map { $_ eq 'hevc' ? 'libfoo' : $_ } @scaled;
my $uk = join ' ', NvencRewrite::rewrite(@unk);
unlike($uk, qr/libfoo|-hwaccel/, 'unknown lib* decoder is dropped, ffmpeg chooses');

# Rewriting is pure and leaves the input list alone.
my @copy = @scaled;
NvencRewrite::rewrite(@scaled);
is_deeply(\@scaled, \@copy, 'input arguments not modified');

done_testing;
