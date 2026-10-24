#!/usr/bin/env bash
# Integration tests for ffmpeg-runner.pl + ffmpeg-wrapper.pl using a stub
# ffmpeg: no GPU, media or cluster needed. Covers the paths that matter when
# omega comes and goes: up, down, wrong token, busy, failing before/after
# output, stop via stdin, killed client, silent runner.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
off="$here/.."
tmp=$(mktemp -d)
fail=0
runner_pid=""
port=$((20000 + RANDOM % 20000))

cleanup() {
  [[ -n $runner_pid ]] && kill -9 "$runner_pid" 2>/dev/null || true
  ps -eo pid,args | awk -v t="$tmp" '$0 ~ "stub-ffmpeg[.]sh" && index($0, t) {print $1}' | xargs -r kill -9 2>/dev/null || true
  rm -rf "$tmp"
}
trap cleanup EXIT

printf 'sekrit' >"$tmp/token"
printf 'wrong' >"$tmp/badtoken"
GPU_ARGS=(-hwaccel cuda -i in.mkv -c:v h264_nvenc -preset p1 "$tmp/out.m3u8")

check() { # description, command...
  local desc=$1
  shift
  if "$@"; then echo "ok   - $desc"; else echo "FAIL - $desc"; fail=1; fi
}
has() { grep -qE -- "$2" <<<"$1"; }
lacks() { ! grep -qE -- "$2" <<<"$1"; }

remote_stubs() { ps -eo args | awk -v t="$tmp" '$0 ~ "stub-ffmpeg[.]sh -hwaccel" && index($0, t) {n++} END {print n + 0}'; }
# Poll up to ~6s for this run's remote stubs to disappear.
stubs_gone() {
  for _ in $(seq 30); do [[ $(remote_stubs) -eq 0 ]] && return 0; sleep 0.2; done
  echo "   leftover:"; ps -eo pid,ppid,pgid,stat,args | awk -v t="$tmp" '$0 ~ "stub-ffmpeg[.]sh" && index($0, t)' | cut -c1-140
  echo "   runner log:"; sed 's/^/   /' "$tmp/runner.log"
  return 1
}

start_runner() { # [max]
  RUNNER_TOKEN=sekrit RUNNER_PORT=$port RUNNER_MAX=${1:-3} STUB_TAG=remote \
    FFMPEG_REAL="$here/stub-ffmpeg.sh" perl "$off/ffmpeg-runner.pl" >"$tmp/runner.log" 2>&1 &
  runner_pid=$!
  for _ in $(seq 50); do
    (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && return 0
    sleep 0.1
  done
  echo "runner failed to start"; cat "$tmp/runner.log"; return 1
}
stop_runner() { kill -9 "$runner_pid" 2>/dev/null || true; wait "$runner_pid" 2>/dev/null || true; runner_pid=""; }

wrap() { # tokenfile, args... ; stdin from caller; combined output on stdout
  local tok=$1
  shift
  FFMPEG_RUNNER_HOST=127.0.0.1 FFMPEG_RUNNER_PORT=$port FFMPEG_RUNNER_TOKEN_FILE=$tok \
    OFFLOAD_CONNECT_TIMEOUT=1 OFFLOAD_SILENCE_TIMEOUT=${SILENCE:-10} STUB_TAG=local \
    FFMPEG_REAL="$here/stub-ffmpeg.sh" perl "$off/ffmpeg-wrapper.pl" "$@" 2>&1 | tr '\r' '\n'
}

# 1. No GPU constructs: run locally, unchanged, never touches the runner.
out=$(printf 'q' | wrap "$tmp/token" -i in.mkv -c:v libx264 out.mp4 || true)
check "non-GPU command runs locally unchanged" has "$out" 'stub:local\] args: -i in.mkv -c:v libx264 out.mp4'
check "non-GPU command does not use omega" lacks "$out" 'running on omega'

# 2. Runner down: fall back to CPU with rewritten args.
out=$(printf 'q' | wrap "$tmp/token" "${GPU_ARGS[@]}" || true)
check "omega down: falls back to local" has "$out" 'omega unreachable'
check "omega down: args rewritten for CPU" has "$out" 'stub:local\] args: -i in.mkv -c:v libx264 -preset veryfast'
check "omega down: no GPU args reach local ffmpeg" lacks "$out" 'stub:local\].*(cuda|nvenc)'

# 3. Runner up: runs remotely with the ORIGINAL GPU args; stdin q stops it.
start_runner
out=$( (sleep 1; printf 'q') | wrap "$tmp/token" "${GPU_ARGS[@]}" ) || true
check "omega up: runs on omega" has "$out" 'running on omega'
check "omega up: remote gets original GPU args" has "$out" 'stub:remote\] args: -hwaccel cuda -i in.mkv -c:v h264_nvenc -preset p1'
check "omega up: stdin q forwarded and stops ffmpeg" has "$out" 'stub: got q'
check "omega up: no local fallback" lacks "$out" 'stub:local'

# 4. Exit code propagates when the remote fails AFTER output (no fallback).
out=$(wrap "$tmp/token" "${GPU_ARGS[@]}" FAILLATE </dev/null; echo "rc=${PIPESTATUS[0]}") || true
check "late remote failure: not retried locally" lacks "$out" 'stub:local'
check "late remote failure: reported as ended" has "$out" 'omega transcode ended with exit 3'

# 5. Remote fails BEFORE output: fall back to CPU.
out=$(wrap "$tmp/token" "${GPU_ARGS[@]}" FAILFAST </dev/null || true)
check "early remote failure: falls back to local" has "$out" 'omega failed before producing output'

# 6. Wrong token: runner refuses, wrapper falls back.
out=$(printf 'q' | wrap "$tmp/badtoken" "${GPU_ARGS[@]}" || true)
check "bad token: declined, falls back" has "$out" 'omega declined \(ERR auth\)'
check "bad token: no remote run" lacks "$out" 'stub:remote'

# 7. Client killed mid-stream: runner must kill ffmpeg.
FFMPEG_RUNNER_HOST=127.0.0.1 FFMPEG_RUNNER_PORT=$port FFMPEG_RUNNER_TOKEN_FILE=$tmp/token \
  STUB_TAG=local FFMPEG_REAL="$here/stub-ffmpeg.sh" \
  perl "$off/ffmpeg-wrapper.pl" "${GPU_ARGS[@]}" >/dev/null 2>&1 < <(sleep 30) &
wpid=$!
sleep 1
remote_before=$(remote_stubs)
kill -TERM "$wpid" 2>/dev/null || true
wait "$wpid" 2>/dev/null || true
check "killed client: remote ffmpeg was running" test "$remote_before" -ge 1
check "killed client: runner killed remote ffmpeg" stubs_gone

# 7b. Regression: a stdin flood (ffmpeg never reads it) must not wedge the
# runner's loop, or it could no longer notice the client dying.
FFMPEG_RUNNER_HOST=127.0.0.1 FFMPEG_RUNNER_PORT=$port FFMPEG_RUNNER_TOKEN_FILE=$tmp/token \
  STUB_TAG=local FFMPEG_REAL="$here/stub-ffmpeg.sh" \
  perl "$off/ffmpeg-wrapper.pl" "${GPU_ARGS[@]}" >/dev/null 2>&1 </dev/zero &
wpid=$!
sleep 1.5
flood_before=$(remote_stubs)
kill -TERM "$wpid" 2>/dev/null || true
wait "$wpid" 2>/dev/null || true
check "stdin flood: remote ffmpeg was running" test "$flood_before" -ge 1
check "stdin flood: runner still killed ffmpeg after client died" stubs_gone

# 8. Concurrency cap: with RUNNER_MAX=1 a second client is told busy and falls back.
stop_runner
start_runner 1
( (sleep 4; printf 'q') | wrap "$tmp/token" "${GPU_ARGS[@]}" >/dev/null ) &
bgpid=$!
sleep 1
out=$(printf 'q' | wrap "$tmp/token" "${GPU_ARGS[@]}" || true)
check "runner at capacity: second client falls back" has "$out" 'omega declined \(ERR busy\)'
wait "$bgpid" 2>/dev/null || true

# 9. Runner stops answering (e.g. omega power-cut): detect silence, fall back.
stop_runner
start_runner
( SILENCE=3 wrap "$tmp/token" "${GPU_ARGS[@]}" < <(sleep 30) >"$tmp/silent.out" ) &
bgpid=$!
sleep 1
kill -STOP "$runner_pid"
# the forked per-connection child is what talks; freeze it too
pkill -STOP -P "$runner_pid" || true
for _ in $(seq 40); do kill -0 "$bgpid" 2>/dev/null || break; sleep 0.5; done
check "silent runner: wrapper gave up and ran locally" has "$(cat "$tmp/silent.out")" 'omega silent for 3s'
pkill -CONT -P "$runner_pid" 2>/dev/null || true
kill -CONT "$runner_pid" 2>/dev/null || true
wait "$bgpid" 2>/dev/null || true

# 10. Trickplay runs ONLY on omega: output goes via the shared scratch dir and is
# moved into place; with omega unavailable it fails fast (exit 98), never on CPU.
stop_runner
export TRICKPLAY_SCRATCH_ROOT="$tmp/scratch"
mkdir -p "$TRICKPLAY_SCRATCH_ROOT"
TP_ARGS=(-loglevel error -threads 4 -i in.mkv -map 0:0 -an -sn -vf fps=0.1 -c:v mjpeg -f image2 "$tmp/tp1/%08d.jpg")
out=$(wrap "$tmp/token" "${TP_ARGS[@]}" </dev/null; echo "rc=${PIPESTATUS[0]}") || true
check "trickplay, omega down: fails fast with exit 98" has "$out" 'rc=98'
check "trickplay, omega down: explains it runs only on omega" has "$out" 'trickplay runs only on omega'
check "trickplay, omega down: never ran locally" lacks "$out" 'stub:local'
check "trickplay, omega down: no thumbnails" test ! -e "$tmp/tp1"
check "trickplay, omega down: scratch cleaned" test -z "$(ls -A "$TRICKPLAY_SCRATCH_ROOT")"

start_runner
out=$(wrap "$tmp/token" "${TP_ARGS[@]}" </dev/null; echo "rc=${PIPESTATUS[0]}") || true
check "trickplay, omega up: succeeds" has "$out" 'rc=0'
check "trickplay, omega up: ran remotely with NVDEC" has "$out" 'stub:remote\] args: -loglevel error -threads 4 -hwaccel cuda -i in.mkv'
check "trickplay, omega up: output redirected to scratch" has "$out" "trickplay-[0-9]+-[0-9]+/%08d.jpg"
check "trickplay, omega up: thumbnails moved to the original dir" test "$(find "$tmp/tp1" -name "*.jpg" | wc -l)" -eq 3
check "trickplay, omega up: scratch cleaned" test -z "$(ls -A "$TRICKPLAY_SCRATCH_ROOT")"

TP_FAIL=(-i in.mkv -c:v mjpeg -f image2 FAILTP "$tmp/tp2/%08d.jpg")
out=$(wrap "$tmp/token" "${TP_FAIL[@]}" </dev/null; echo "rc=${PIPESTATUS[0]}") || true
check "trickplay, remote failure: exit code propagates, no CPU retry" has "$out" 'rc=4'
check "trickplay, remote failure: not retried locally" lacks "$out" 'stub:local'
check "trickplay, remote failure: nothing left behind" test ! -e "$tmp/tp2"

out=$(wrap "$tmp/badtoken" "${TP_ARGS[@]}" </dev/null; echo "rc=${PIPESTATUS[0]}") || true
check "trickplay, bad token: fails fast" has "$out" 'rc=98'

out=$(OFFLOAD_DISABLED=1 wrap "$tmp/token" "${TP_ARGS[@]}" </dev/null; echo "rc=${PIPESTATUS[0]}") || true
check "trickplay, kill switch: fails fast, does not run" has "$out" 'rc=98'
check "trickplay, kill switch: nothing ran" lacks "$out" 'stub:'

# Chapter images (single named .jpg) are not trickplay and keep running locally.
out=$(printf 'q' | wrap "$tmp/token" -i in.mkv -frames:v 1 -f image2 "$tmp/chap.jpg" || true)
check "single-image extraction is not treated as trickplay" lacks "$out" 'runs only on omega'

exit "$fail"
