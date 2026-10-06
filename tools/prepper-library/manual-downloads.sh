#!/usr/bin/env bash
# Open each by-hand source (a `legacy` entry in sources.yaml whose comment says
# "by hand: <url>") that isn't on the NAS yet, wait while you save the PDF in
# the browser, then push the newest PDF from the downloads folder to the NAS
# (rsync over ssh as root on the PVE host, like fetch.py --push: the SMB mount
# is read-only for new folders).
#
#   tools/prepper-library/manual-downloads.sh
#   REF=/mnt/fileserver/media/reference DOWNLOADS=~/Downloads tools/prepper-library/manual-downloads.sh
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REF=${REF:-/mnt/fileserver/media/reference}   # NAS media/reference, mounted
DOWNLOADS=${DOWNLOADS:-$HOME/Downloads}
NAS=root@192.168.1.16
NAS_REF=/Tank/media_root/media/reference
SSH_KEY=${SSH_KEY:-$HOME/.ssh/ansible-on-nest}

[ -d "$REF/sources" ] || { echo "NAS not mounted at $REF" >&2; exit 1; }

# id <TAB> category <TAB> url, for by-hand entries
mapfile -t todo < <(python3 - "$HERE/sources.yaml" <<'EOF'
import re, sys
text = open(sys.argv[1]).read()
for m in re.finditer(r"- id: (\S+)\n    type: legacy  # by hand: (\S+).*?\n    category: (\S+)", text, re.S):
    print(f"{m[1]}\t{m[3]}\t{m[2]}")
EOF
)

missing=0
for row in "${todo[@]}"; do
  IFS=$'\t' read -r id category url <<<"$row"
  dest="$REF/sources/$category/$id"
  if compgen -G "$dest/*.pdf" >/dev/null; then
    echo "have    $id"
    continue
  fi
  missing=1
  echo
  echo "== $id"
  echo "   $url"
  marker=$(mktemp)
  xdg-open "$url" >/dev/null 2>&1 || echo "   (open it yourself: xdg-open failed)"
  read -rp "   Save the PDF to $DOWNLOADS, then press Enter (s = skip) " answer
  if [ "$answer" = s ]; then rm -f "$marker"; continue; fi
  pdf=$(find "$DOWNLOADS" -maxdepth 1 -iname '*.pdf' -newer "$marker" -printf '%T@ %p\n' \
        | sort -n | tail -n1 | cut -d' ' -f2-)
  rm -f "$marker"
  if [ -z "$pdf" ]; then
    echo "   no new PDF in $DOWNLOADS; skipped"
    continue
  fi
  pages=$(pdfinfo "$pdf" 2>/dev/null | awk '/^Pages/{print $2}')
  read -rp "   Use $(basename "$pdf") (${pages:-?} pages)? [Y/n] " ok
  [ "${ok:-y}" = n ] && continue
  target="$NAS_REF/sources/$category/$id"
  ssh -i "$SSH_KEY" "$NAS" "mkdir -p '$target' && chown 1000:1000 '$target'"
  rsync -a --chown=1000:1000 -e "ssh -i $SSH_KEY" "$pdf" "$NAS:$target/"
  rm -f "$pdf"
  echo "   -> $dest/"
done

[ "$missing" = 0 ] && echo "All by-hand sources are on the NAS."
exit 0
