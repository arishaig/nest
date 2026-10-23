#!/usr/bin/env bash
# One-shot spike for docs/tunarr-offload.md: runs Tunarr-shaped HLS transcodes
# on omega, original (libx264) vs rewritten (NVDEC/NVENC), against one real 4K
# HDR HEVC file and one 1080p H.264 file, into an emptyDir. Reports time to two
# segments (Tunarr gives up at ~15 s), speed, VRAM and the output's pixel format.
# Read-only on the media share. Output is the Job log.
set -u
RUN=${RUN_SECONDS:-30}
SC=/scratch
export REWRITE=/scripts/nvenc-rewrite.pl

pick() { # kind -> first matching file path
  local kind=$1 f probe n=0 codec w pix tr pat='*1080p*'
  [ "$kind" = uhd_hdr ] && pat='*2160p*'
  while IFS= read -r f; do
    n=$((n + 1)); [ "$n" -gt 60 ] && break
    probe=$(ffprobe -v error -select_streams v:0 \
      -show_entries stream=codec_name,height,color_transfer,pix_fmt \
      -of csv=p=0 -- "$f" </dev/null 2>/dev/null) || continue
    # csv order is ffprobe's own: codec,width,height,pix_fmt,color_transfer.
    # Match on width: scope films are cropped (3840x1608, 1920x800).
    IFS=, read -r codec w _ pix tr <<<"$probe"
    case $kind in
      uhd_hdr) [ "$codec" = hevc ] && [ "${w:-0}" -ge 3800 ] && [ "$tr" = smpte2084 ] && { echo "$f"; return; } ;;
      hd_h264) [ "$codec" = h264 ] && [ "${w:-0}" -ge 1900 ] && [ "$pix" = yuv420p ] && { echo "$f"; return; } ;;
    esac
  done < <(find /data/media/movies /data/media/tv -type f \( -iname '*.mkv' -o -iname '*.mp4' \) \
             -iname "$pat" 2>/dev/null)
}

cmd() { # input -> Tunarr 1.3.15 shaped command (local file, no -headers)
  echo "-nostdin -hide_banner -nostats -loglevel warning -fflags +genpts+discardcorrupt+igndts \
-ss 600s -c:v $2 -readrate 1 -readrate_initial_burst 60 -i $1 \
-filter_complex [0:1]aresample=async=1,apad[a];[0:0]scale=1920:1080:flags=bicubic:force_original_aspect_ratio=decrease,setsar=1,pad=1920:1080:-1:-1:color=black[v] \
-map [v] -map [a] -muxdelay 0 -muxpreload 0 -flags cgop -movflags +faststart -t ${RUN}s \
-video_track_timescale 90000 -b:v 4000k -maxrate:v 4000k -bufsize:v 8000k -c:v libx264 \
-preset:v veryfast -sc_threshold 1000000000 -c:a aac -ac 2 -b:a 192k -ar 48k -g 96 -keyint_min 96 \
-force_key_frames expr:gte(t,n_forced*4) -f hls -hls_time 4 -hls_list_size 0 \
-hls_segment_type mpegts -hls_segment_filename $3/data%06d.ts \
-hls_flags program_date_time+append_list+independent_segments+omit_endlist $3/stream.m3u8"
}

run() { # label file codec rewrite(0|1)
  local label=$1 file=$2 codec=$3 rw=$4 dir="$SC/$1" start t2="n/a" line args
  mkdir -p "$dir"; line=$(cmd "$file" "$codec" "$dir")
  if [ "$rw" = 1 ]; then
    line=$(perl -e 'require $ENV{REWRITE}; use Text::ParseWords;
      print join(" ", NvencRewrite::rewrite(shellwords(shift)))' "$line")
  fi
  read -r -a args <<<"$line"
  echo "--- $label ---"; echo "cmd: ${line//$file/<file>}"
  start=$(date +%s.%N)
  ffmpeg "${args[@]}" 2>"$dir/err.txt" &
  local pid=$! vram=0 v
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$t2" = n/a ] && [ "$(find "$dir" -name 'data*.ts' | wc -l)" -ge 3 ]; then
      # segment N is complete once N+1 exists, so 3 files = 2 finished segments
      t2=$(echo "$(date +%s.%N) - $start" | bc)
    fi
    v=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1)
    [ -n "$v" ] && [ "$v" -gt "$vram" ] && vram=$v
    sleep 0.25
  done
  wait "$pid"; local rc=$?
  local total; total=$(echo "$(date +%s.%N) - $start" | bc)
  echo "exit=$rc wall=${total}s two_segments_at=${t2}s segments=$(find "$dir" -name 'data*.ts' | wc -l) gpu_mem_peak_mib=$vram"
  local seg; seg=$(find "$dir" -name 'data*.ts' | sort | head -1)
  [ -n "$seg" ] && ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,profile,pix_fmt,width,height -of csv=p=0 -- "$seg"
  [ -s "$dir/err.txt" ] && { echo "stderr:"; head -15 "$dir/err.txt"; }
}

nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
for kind in uhd_hdr hd_h264; do
  f=$(pick "$kind")
  if [ -z "$f" ]; then echo "no $kind file found"; continue; fi
  echo "=== $kind: ${f#/data/media/} ==="
  codec=$([ "$kind" = hd_h264 ] && echo h264 || echo hevc)
  run "$kind-cpu" "$f" "$codec" 0
  run "$kind-gpu" "$f" "$codec" 1
done
