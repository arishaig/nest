#!/usr/bin/env bash
# Exercises ffmpeg-wrapper.pl against the real runner script with a stub ffmpeg
# (no GPU, no network beyond loopback). Run: bash test-wrapper.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
runner="$here/../../jellyfin-offload/ffmpeg-runner.pl"
wrapper="$here/../ffmpeg-wrapper.pl"
tmp=$(mktemp -d); trap 'kill $RPID 2>/dev/null; rm -rf "$tmp"' EXIT
pass=0; fail=0
ok() { if [ "$2" = 0 ]; then pass=$((pass+1)); echo "ok - $1"; else fail=$((fail+1)); echo "not ok - $1"; fi; }

# Stub ffmpeg: records its args and who ran it, writes the first segment.
cat >"$tmp/ffmpeg" <<'STUB'
#!/usr/bin/env bash
echo "$STUB_NAME $*" >>"$STUB_LOG"
for a in "$@"; do case $a in */stream.m3u8) d=$(dirname "$a"); mkdir -p "$d"; : >"$d/data000000.ts";; esac; done
[ -n "${STUB_EXIT:-}" ] && exit "$STUB_EXIT"
exit 0
STUB
chmod +x "$tmp/ffmpeg"
echo -n tok >"$tmp/token"
port=$((20000 + RANDOM % 10000))
export STUB_LOG="$tmp/log"
RUNNER_TOKEN=tok RUNNER_PORT=$port FFMPEG_REAL="$tmp/ffmpeg" STUB_NAME=remote perl "$runner" >"$tmp/runner.log" 2>&1 &
RPID=$!
sleep 1

W() { # extra env via env(1), then args
  env FFMPEG_REAL="$tmp/ffmpeg" STUB_NAME=local FFMPEG_RUNNER_HOST=127.0.0.1 FFMPEG_RUNNER_PORT=$port \
    FFMPEG_RUNNER_TOKEN_FILE="$tmp/token" "$@"
}
TR=(-nostdin -c:v hevc -readrate 1 -i http://jellyfin:8096/Videos/x/stream?static=true -c:v libx264 -preset:v veryfast -sc_threshold 1 -f hls -hls_time 4 "$tmp/out/stream.m3u8")

: >"$tmp/log"; W perl "$wrapper" "${TR[@]}" 2>/dev/null
grep -q '^remote .*-hwaccel cuda.*h264_nvenc' "$tmp/log"; ok 'offloadable command runs rewritten on the runner' $?
! grep -q '^local' "$tmp/log"; ok '...and not locally' $?

: >"$tmp/log"; W perl "$wrapper" -nostdin -i http://localhost:8000/images/x.png -f hls "$tmp/o2/stream.m3u8" 2>/dev/null
grep -q '^local .*localhost:8000' "$tmp/log" && ! grep -q '^remote' "$tmp/log"; ok 'localhost input stays local, arguments untouched' $?

: >"$tmp/log"; W perl "$wrapper" -version >/dev/null 2>&1
grep -q '^local -version' "$tmp/log"; ok 'probe stays local' $?

: >"$tmp/log"; W env OFFLOAD_DISABLED=1 perl "$wrapper" "${TR[@]}" 2>/dev/null
grep -q '^local .*libx264' "$tmp/log" && ! grep -q 'nvenc' "$tmp/log"; ok 'kill switch runs the original locally' $?

: >"$tmp/log"; W env FFMPEG_RUNNER_PORT=1 perl "$wrapper" "${TR[@]}" 2>"$tmp/err"
grep -q '^local .*libx264' "$tmp/log" && grep -q 'omega unreachable' "$tmp/err"; ok 'runner unreachable -> original arguments locally' $?

echo -n wrong >"$tmp/badtoken"; : >"$tmp/log"
W env FFMPEG_RUNNER_TOKEN_FILE="$tmp/badtoken" perl "$wrapper" "${TR[@]}" 2>"$tmp/err"
grep -q '^local .*libx264' "$tmp/log" && grep -q 'declined' "$tmp/err"; ok 'bad token -> original arguments locally' $?

# Remote fails before any output: stub exits 3 without writing a segment.
cat >"$tmp/ffmpeg-fail" <<'STUB'
#!/usr/bin/env bash
echo "$STUB_NAME $*" >>"$STUB_LOG"
if [ "$STUB_NAME" = remote ]; then exit 3; fi
exit 0
STUB
chmod +x "$tmp/ffmpeg-fail"
kill $RPID; wait $RPID 2>/dev/null
RUNNER_TOKEN=tok RUNNER_PORT=$port FFMPEG_REAL="$tmp/ffmpeg-fail" STUB_NAME=remote perl "$runner" >>"$tmp/runner.log" 2>&1 &
RPID=$!; sleep 1
rm -rf "$tmp/out"; : >"$tmp/log"
FFMPEG_REAL="$tmp/ffmpeg-fail" STUB_NAME=local FFMPEG_RUNNER_HOST=127.0.0.1 FFMPEG_RUNNER_PORT=$port \
  FFMPEG_RUNNER_TOKEN_FILE="$tmp/token" perl "$wrapper" "${TR[@]}" 2>"$tmp/err"
grep -q '^remote' "$tmp/log" && grep -q '^local .*libx264' "$tmp/log" && grep -q 'before producing output' "$tmp/err"
ok 'remote fails before output -> falls back to original locally' $?

echo "$pass passed, $fail failed"; [ "$fail" = 0 ]
