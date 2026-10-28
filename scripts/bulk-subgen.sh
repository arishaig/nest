#!/usr/bin/env bash
# Bulk-trigger subgen (k8s, pinned to talos-omega's GPU) to generate subtitles
# for all movies, TV and/or filler. Subgen skips files that already have an
# English subtitle, so re-running is safe.
#
# /batch holds the connection open while it walks the library, so the call is
# fired from inside the pod in the background and its exit status is ignored;
# follow progress with the logs command printed at the end.
#
# Usage:
#   ./bulk-subgen.sh            # process movies, TV and filler
#   ./bulk-subgen.sh movies     # movies only
#   ./bulk-subgen.sh tv         # TV only
#   ./bulk-subgen.sh filler     # filler only

set -euo pipefail

NAMESPACE="media"
DEPLOY="deploy/subgen"
MEDIA_ROOT="/data/media"

batch() {
    local label="$1"
    local path="$2"
    echo "Queuing $label ($path)..."
    nohup kubectl -n "$NAMESPACE" exec "$DEPLOY" -- \
        curl -s -X POST "localhost:9000/batch?directory=${path}" > /dev/null 2>&1 &
    echo "  Scanning in background (PID $!). Subgen will process as it walks the library."
}

case "${1:-all}" in
    movies) batch "movies" "$MEDIA_ROOT/movies" ;;
    tv)     batch "TV"     "$MEDIA_ROOT/tv"     ;;
    filler) batch "filler" "$MEDIA_ROOT/filler" ;;
    all)
        batch "movies" "$MEDIA_ROOT/movies"
        batch "TV"     "$MEDIA_ROOT/tv"
        batch "filler" "$MEDIA_ROOT/filler"
        ;;
    *)
        echo "Usage: $0 [movies|tv|filler|all]"
        exit 1
        ;;
esac

echo "Done. Watch subgen logs with: kubectl -n $NAMESPACE logs -f $DEPLOY"
