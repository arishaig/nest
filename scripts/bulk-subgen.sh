#!/usr/bin/env bash
# Bulk-trigger subgen to generate subtitles for all movies and/or TV.
# Runs on the docker host. Subgen skips files it's already processed.
#
# Usage:
#   ./bulk-subgen.sh            # process both movies and TV
#   ./bulk-subgen.sh movies     # movies only
#   ./bulk-subgen.sh tv         # TV only

set -euo pipefail

SUBGEN="http://localhost:9000"
MOVIES_PATH="/data/media/movies"
TV_PATH="/data/media/tv"

batch() {
    local label="$1"
    local path="$2"
    echo "Queuing $label ($path)..."
    nohup curl -sf -X POST "${SUBGEN}/batch?directory=${path}" > /dev/null 2>&1 &
    echo "  Scanning in background (PID $!). Subgen will process as it walks the library."
}

case "${1:-all}" in
    movies) batch "movies" "$MOVIES_PATH" ;;
    tv)     batch "TV"     "$TV_PATH"     ;;
    all)
        batch "movies" "$MOVIES_PATH"
        batch "TV"     "$TV_PATH"
        ;;
    *)
        echo "Usage: $0 [movies|tv|all]"
        exit 1
        ;;
esac

echo "Done. Watch subgen logs with: docker logs -f subgenai"
