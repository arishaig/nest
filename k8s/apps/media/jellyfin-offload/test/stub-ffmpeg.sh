#!/usr/bin/env bash
# Stand-in for ffmpeg in test-offload.sh. Prints which side ran it and its
# args, then emits progress lines until it reads "q" on stdin.
#   FAILFAST in args  -> exit 1 immediately, no progress
#   FAILLATE in args  -> emit progress, then exit 3
echo "[stub:${STUB_TAG:-?}] args: $*" >&2
# Trickplay-shaped call (numbered .jpg output): write three thumbnails and exit.
#   FAILTP in args -> exit 4 without writing any
last=${!#}
if [[ $last == *%08d.jpg ]]; then
  for a in "$@"; do [[ $a == FAILTP ]] && { echo "stub: trickplay failed" >&2; exit 4; }; done
  # Like the real image2 muxer: the output directory must already exist.
  [[ -d ${last%/*} ]] || { echo "stub: ${last%/*}: No such file or directory" >&2; exit 254; }
  for i in 1 2 3; do printf img >"${last%/*}/0000000$i.jpg"; done
  exit 0
fi
for a in "$@"; do
  [[ $a == FAILFAST ]] && { echo "stub: cannot init" >&2; exit 1; }
done
n=0
while true; do
  n=$((n + 1))
  printf 'frame=%5d fps=24.0 q=18.0 time=00:00:%02d.00\r' "$n" "$n" >&2
  for a in "$@"; do
    [[ $a == FAILLATE && $n -ge 3 ]] && { echo "stub: late failure" >&2; exit 3; }
  done
  IFS= read -r -t 0.2 -n1 ch
  rc=$?
  if [[ $rc -eq 0 && $ch == q ]]; then
    echo "stub: got q" >&2
    exit 0
  fi
  # EOF on stdin (rc 1): the parent is gone, don't linger as an orphan.
  [[ $rc -eq 1 ]] && exit 0
done
