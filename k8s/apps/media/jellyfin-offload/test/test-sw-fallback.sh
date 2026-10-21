#!/usr/bin/env bash
# Tests for ffmpeg-sw-fallback.pl against ffmpeg command lines captured from a
# real Jellyfin 12.1 NVENC session (fixtures/*.cmd, paths sanitised).
#
#   test-sw-fallback.sh                       offline checks only
#   FFMPEG=/usr/lib/jellyfin-ffmpeg/ffmpeg \
#   MEDIA_FILE=/path/to/4k-hdr.mkv SUB_FILE=/path/to/en.srt \
#     test-sw-fallback.sh                     also run each rewritten command
#                                             for $DURATION s (default 5) from
#                                             $SEEK s in, and report speed=
#                                             (needs HEVC HDR video, audio at
#                                             0:1 and 0:2, PGS at 0:8)
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
rewriter="$here/../ffmpeg-sw-fallback.pl"
fail=0

check() { # description, then a command that succeeds when the check passes
  local desc=$1
  shift
  if "$@"; then
    echo "ok   - $desc"
  else
    echo "FAIL - $desc"
    fail=1
  fi
}

has() { grep -qE -- "$2" <<<"$1"; }     # has <haystack> <regex>
lacks() { ! grep -qE -- "$2" <<<"$1"; } # lacks <haystack> <regex>

# Drop single-quoted segments (subtitle file paths) before looking for GPU words.
no_quoted() { sed "s/'[^']*'//g" <<<"$1"; }

rewrite() { # fixture -> rewritten args in REWRITTEN
  local line
  line=$(cat "$1")
  local -a args
  eval "args=($line)"
  mapfile -d '' REWRITTEN < <(perl "$rewriter" "${args[@]}")
}

exits_97() { # args... ; the rewriter must refuse them
  local rc=0
  perl "$rewriter" "$@" >/dev/null 2>&1 || rc=$?
  [[ $rc -eq 97 ]]
}

for fx in "$here"/fixtures/c*.cmd; do
  name=$(basename "$fx" .cmd)
  rewrite "$fx"
  joined=" ${REWRITTEN[*]} "

  check "$name: no GPU setup options remain" \
    lacks "$joined" ' -(init_hw_device|filter_hw_device|hwaccel|hwaccel_output_format|hwaccel_flags) '
  check "$name: encoder is libx264" has "$joined" ' -codec:v:0 libx264 '
  check "$name: preset is a CPU preset" has "$joined" ' -preset veryfast '
  check "$name: no -threads 1 before -i" lacks "$joined" ' -threads 1 .* -i '
  check "$name: no cuda/nvenc/hwupload outside file paths" \
    lacks "$(no_quoted "$joined")" 'cuda|nvenc|hwupload'
done

# Filter-specific expectations.
rewrite "$here/fixtures/c1_hdr_nosub.cmd"
joined="${REWRITTEN[*]}"
check "c1: tonemap_cuda -> tonemapx" has "$joined" 'tonemapx=format=yuv420p:p=bt709'
check "c1: scale_cuda -> scale" has "$joined" ',scale=w=1920:h=1080,tonemapx'

rewrite "$here/fixtures/c5_hdr_pgs_burn.cmd"
joined="${REWRITTEN[*]}"
check "c5: overlay_cuda -> overlay" has "$joined" '\[main\]\[sub\]overlay=eof_action=pass'
check "c5: hwupload dropped from subtitle chain" has "$joined" 'format=yuva420p\[sub\]'

rewrite "$here/fixtures/c2_hdr_extsrt_burn.cmd"
joined="${REWRITTEN[*]}"
check "c2: overlay alpha_format -> alpha" has "$joined" 'overlay=eof_action=pass:repeatlast=0:alpha=premultiplied'

rewrite "$here/fixtures/c3_sdr_nosub.cmd"
joined="${REWRITTEN[*]}"
check "c3: scale_cuda format moves to a format filter" has "$joined" 'scale=w=1920:h=1080,format=yuv420p'

# Anything it cannot translate must fail loudly (exit 97), never pass through.
check "unsupported scale_npp exits 97" exits_97 -i file:in.mkv -vf scale_npp=1920:1080 out.m3u8
check "unsupported h264_cuvid exits 97" exits_97 -i file:in.mkv -c:v h264_cuvid out.m3u8
check "unsupported hwdownload exits 97" exits_97 -i file:in.mkv -vf hwdownload,format=nv12 out.m3u8
check "unsupported cuda option exits 97" exits_97 -i file:in.mkv -foo_cuda 1 out.m3u8

# File paths containing GPU-ish words must not trip the check.
accepts() { perl "$rewriter" "$@" >/dev/null; }
check "path containing 'cuda' is not a false positive" \
  accepts -i "file:/data/Cuda (2020).mkv" -codec:v:0 h264_nvenc -preset p1 out.m3u8

# Optional: run each rewritten command for real, CPU only.
fixture_srt="/data/media/movies/Movie (2000)/Movie (2000) Remux-2160p Proper.en.srt"
if [[ -n ${FFMPEG:-} && -n ${MEDIA_FILE:-} ]]; then
  for fx in "$here"/fixtures/c*.cmd; do
    name=$(basename "$fx" .cmd)
    rewrite "$fx"
    run=()
    # Keep the real run small: swap the input file, cut everything from the
    # HLS output section on and write 5s to a null sink; shrink probing so we
    # don't read hundreds of MB from the NFS-backed media.
    for ((j = 0; j < ${#REWRITTEN[@]}; j++)); do
      a=${REWRITTEN[j]}
      if [[ $a == -f && ${REWRITTEN[j + 1]:-} == hls ]]; then break; fi
      case $a in
        -i) run+=(-ss "${SEEK:-0}" -i) ;;
        file:*) run+=("file:$MEDIA_FILE") ;;
        *"$fixture_srt"*) run+=("${a//"$fixture_srt"/${SUB_FILE:-$fixture_srt}}") ;;
        -probesize) run+=("$a" 20M); j=$((j + 1)) ;;
        -analyzeduration) run+=("$a" 10M); j=$((j + 1)) ;;
        *) run+=("$a") ;;
      esac
    done
    if "$FFMPEG" -hide_banner -nostdin -y "${run[@]}" -to "$(( ${SEEK:-0} + ${DURATION:-5} ))" -f null - >"/tmp/sw-fallback-$name.log" 2>&1; then
      # Exit 0 is not enough: a bad time window also exits 0 having encoded
      # nothing. Require frames, and report throughput.
      stats=$(tr '\r' '\n' <"/tmp/sw-fallback-$name.log" | grep -a '^frame=' | tail -1)
      frames=$(grep -a -o -E 'frame= *[0-9]+' <<<"$stats" | grep -o -E '[0-9]+' || true)
      fps=$(grep -a -o -E 'fps= *[0-9.]+' <<<"$stats" | grep -o -E '[0-9.]+' || true)
      if [[ ${frames:-0} -gt 0 ]]; then
        echo "ok   - $name: rewritten command runs on CPU (frames=$frames fps=${fps:-?})"
      else
        echo "FAIL - $name: exited 0 but encoded no frames (see /tmp/sw-fallback-$name.log)"
        fail=1
      fi
    else
      echo "FAIL - $name: rewritten command failed (see /tmp/sw-fallback-$name.log)"
      tail -n 5 "/tmp/sw-fallback-$name.log"
      fail=1
    fi
  done
fi

exit "$fail"
