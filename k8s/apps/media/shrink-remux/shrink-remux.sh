#!/usr/bin/env bash
# shrink-remux: re-encode oversized 1080p-and-below SDR TV remuxes to HEVC on
# the GPU, with a before/after report and a reversible swap.
#
# Modes (first argument):
#   scan              read-only survey of tv/; writes reports/scan-<ts>.{tsv,md}
#   plan <list>       projected before/after for the files in <list>; read-only
#   test <file>       encode ONE file to $WORK/test/, measured before/after; no swap
#   run  <list>       encode + verify + swap every file in <list>
# <list> is a text file with one absolute path per line (user-approved batch).
#
# HDR is never touched. A file is "protected" (never encoded, even if listed)
# when ffprobe sees PQ/HLG transfer, BT.2020 primaries/matrix, a Dolby Vision
# record, HDR10+ dynamic metadata, static HDR metadata, or 10-bit with
# untagged colour (ambiguous). Interlaced, non-mkv and non-legacy-codec files
# are skipped too. Originals are moved to the arr Recycle Bin, never deleted.
# shellcheck disable=SC2016  # jq programs contain literal $vars
set -euo pipefail

ROOT=${MEDIA_ROOT:-/data/media}
LIB=${LIB_DIR:-$ROOT/tv}
WORK=${WORK_DIR:-$ROOT/.shrink}
RECYCLE=${RECYCLE_DIR:-$ROOT/.arr-recycle}
REPORTS=$WORK/reports
MIN_SIZE_MB=${MIN_SIZE_MB:-1000}    # below this a file is not even probed
MIN_KBPS=${MIN_KBPS:-12000}         # overall bitrate floor for a candidate
PROJ_RATIO=${PROJ_RATIO:-0.30}      # rough size-after estimate until a test measures it
CQ=${CQ:-22}
PRESET=${PRESET:-p7}
VENC=${VENC:-hevc_nvenc}            # libx265 only for off-cluster testing
TS=$(date -u +%Y%m%dT%H%M%SZ)
GIB=1073741824

mkdir -p "$REPORTS" "$WORK/tmp" "$WORK/test"

# --- jq programs -----------------------------------------------------------

JQ_DEFS='
def n: if . == null or . == "" then "-" else (. | tostring) end;
def vstream: (.streams | map(select(.codec_type == "video" and (.disposition.attached_pic != 1))) | .[0]);
def sd: [((vstream.side_data_list // [])[]), ((.frame0.side_data_list // [])[])] | map(.side_data_type // "");
def unk: . == null or . == "" or . == "unknown" or . == "unspecified";
def hdr:
  vstream as $v | sd as $sd
  | [ (if (($v.color_transfer // "") | test("smpte2084|arib-std-b67")) then "transfer=\($v.color_transfer)" else empty end),
      (if ($v.color_primaries // "") == "bt2020" then "primaries=bt2020" else empty end),
      (if (($v.color_space // "") | test("^bt2020")) then "matrix=\($v.color_space)" else empty end),
      (if any($sd[]; test("DOVI|Dolby Vision"; "i")) then "dolby-vision" else empty end),
      (if any($sd[]; test("2094-40|Dynamic Metadata"; "i")) then "hdr10+" else empty end),
      (if any($sd[]; test("Mastering display|Content light"; "i")) then "static-hdr-metadata" else empty end),
      (if (($v.pix_fmt // "") | test("10")) and (($v.color_transfer | unk) or ($v.color_primaries | unk)) then "ambiguous-10bit-untagged" else empty end)
    ] | join(",");
def fps: vstream.avg_frame_rate // "0/0" | split("/") | if (.[1] | tonumber) > 0 then ((.[0] | tonumber) / (.[1] | tonumber) * 100 | round / 100) else null end;
def kbps: ((.format.bit_rate // "0") | tonumber / 1000 | floor);
def audio: [.streams[] | select(.codec_type == "audio")];
def subs: [.streams[] | select(.codec_type == "subtitle")];
'

JQ_SUM="$JQ_DEFS"'
vstream as $v
| [ (.format.duration // "0"), ($v.codec_name | n), ($v.profile | n), ($v.width | n), ($v.height | n),
    ($v.pix_fmt | n), (fps | n), kbps, (hdr | n), ($v.field_order | n),
    ($v.color_transfer | n), ($v.color_primaries | n), ($v.color_space | n), ($v.color_range | n) ]
| map(tostring) | @tsv'

JQ_CMP="$JQ_DEFS"'
def summ:
  vstream as $v
  | { "size (GiB)": ((.format.size | tonumber) / 1073741824 * 100 | round / 100),
      "duration (s)": ((.format.duration | tonumber) * 10 | round / 10),
      "overall bitrate (kbps)": kbps,
      "video bitrate (kbps)": (($v.tags.BPS // $v.bit_rate // null) | if . == null then "-" else (tonumber / 1000 | round) end),
      "video codec / profile": "\($v.codec_name) / \($v.profile // "-")",
      "resolution @ fps": "\($v.width)x\($v.height) @ \(fps)",
      "pixel format": $v.pix_fmt,
      "colour prim/trc/matrix/range": "\($v.color_primaries // "-")/\($v.color_transfer // "-")/\($v.color_space // "-")/\($v.color_range // "-")",
      "HDR flags": (hdr | if . == "" then "none" else . end),
      "audio streams": (audio | map("\(.codec_name)/\(.channels)ch/\(.tags.language // "und")") | join(", ")),
      "subtitle streams": (subs | length),
      "chapters": (.chapters | length),
      "attachments": ([.streams[] | select(.codec_type == "attachment")] | length) };
($b[0] | summ) as $B | ($a[0] | summ) as $A
| $B | keys_unsorted[] | . as $k | "| \($k) | \($B[$k] | n) | \($A[$k] | n) |"'

# --- probing / classification ---------------------------------------------

probe_json() { # file -> merged ffprobe JSON (streams, format, chapters, frame 0)
    local f=$1 s fr
    s=$(ffprobe -v error -print_format json -show_format -show_streams -show_chapters -- "$f" </dev/null) || return 1
    fr=$(ffprobe -v error -print_format json -select_streams v:0 -show_frames \
        -read_intervals '%+#1' -- "$f" </dev/null 2>/dev/null) || fr=
    [[ -n $fr ]] || fr='{}'
    jq -n --argjson s "$s" --argjson f "$fr" '$s + {frame0: ($f.frames[0] // {})}'
}

row() { local IFS=$'\t'; printf '%s\n' "$*"; }

HEADER=$(row status path bytes duration_s vcodec profile width height pix_fmt fps kbps hdr field_order trc primaries matrix range ext)

classify() { # file -> one TSV row (columns as HEADER)
    local f=$1 size ext j
    size=$(stat -c %s -- "$f")
    ext=${f##*.}; ext=${ext,,}
    if ((size < MIN_SIZE_MB * 1048576)); then
        row SKIP-small "$f" "$size" - - - - - - - - - - - - - - "$ext"; return 0
    fi
    if ! j=$(probe_json "$f"); then
        row SKIP-probe-failed "$f" "$size" - - - - - - - - - - - - - - "$ext"; return 0
    fi
    local dur vcodec profile w h pix fps kbps hdr field trc prim matrix range
    IFS=$'\t' read -r dur vcodec profile w h pix fps kbps hdr field trc prim matrix range \
        < <(jq -r "$JQ_SUM" <<<"$j")
    [[ $h =~ ^[0-9]+$ ]] || h=0
    [[ $kbps =~ ^[0-9]+$ ]] || kbps=0
    local st=CANDIDATE
    if [[ $ext != mkv ]]; then st=SKIP-container
    elif [[ $vcodec == - ]]; then st=SKIP-novideo
    elif [[ $field != progressive && $field != - && $field != unknown ]]; then st=SKIP-interlaced
    elif ! [[ $vcodec =~ ^(h264|vc1|mpeg2video)$ ]]; then st=SKIP-codec
    elif ((h > 1088)); then st=SKIP-over1080p
    elif ((kbps < MIN_KBPS)); then st=SKIP-lowbitrate
    fi
    if [[ $st == CANDIDATE && $hdr != - ]]; then st=SKIP-hdr-protected; fi
    row "$st" "$f" "$size" "$dur" "$vcodec" "$profile" "$w" "$h" "$pix" "$fps" "$kbps" "$hdr" \
        "$field" "$trc" "$prim" "$matrix" "$range" "$ext"
}

# --- reporting --------------------------------------------------------------

summarize() { # scan tsv -> markdown
    local tsv=$1
    awk -F'\t' -v ratio="$PROJ_RATIO" -v gib="$GIB" '
        NR == 1 { next }
        { n[$1]++; b[$1] += $3 }
        $12 != "-" { anyhdr++ }
        END {
            print "## Scan summary\n";
            print "| status | files | GiB |\n|---|---:|---:|";
            for (k in n) printf "| %s | %d | %.1f |\n", k, n[k], b[k] / gib;
            printf "\nFiles with any HDR flag (all sizes): %d\n", anyhdr;
            c = b["CANDIDATE"];
            printf "\n**Candidates: %d files, %.1f GiB now, ~%.1f GiB after (estimate at %.0f%% of size), ~%.1f GiB saved.**\n",
                n["CANDIDATE"], c / gib, c * ratio / gib, ratio * 100, c * (1 - ratio) / gib;
        }' "$tsv"
    # series table sorted by size (awk above prints a sortable prefix)
    awk -F'\t' -v ratio="$PROJ_RATIO" -v gib="$GIB" -v lib="$LIB/" '
        NR == 1 { next }
        $1 == "CANDIDATE" { s = substr($2, length(lib) + 1); sub("/.*", "", s); sn[s]++; sb[s] += $3 }
        END { for (s in sn) printf "%.4f\t| %s | %d | %.1f | %.1f |\n", sb[s] / gib, s, sn[s], sb[s] / gib, sb[s] * ratio / gib }' "$tsv" \
        | sort -rn | cut -f2- | { echo; echo "### Candidates by series"; echo; echo "| series | files | GiB now | ~GiB after |"; echo "|---|---:|---:|---:|"; cat; }
    echo
    echo "### Protected: would be candidates but are HDR / ambiguous (never encoded)"
    echo
    echo "| file | reason | res | pix_fmt | GiB |"
    echo "|---|---|---|---|---:|"
    awk -F'\t' -v gib="$GIB" -v lib="$LIB/" '$1 == "SKIP-hdr-protected" { printf "| %s | %s | %sx%s | %s | %.1f |\n", substr($2, length(lib) + 1), $12, $7, $8, $9, $3 / gib }' "$tsv"
}

plan_table() { # tsv of classified rows -> markdown
    local tsv=$1
    echo "| file | status | codec | res | pix_fmt | kbps | GiB now | ~GiB after |"
    echo "|---|---|---|---|---|---:|---:|---:|"
    awk -F'\t' -v ratio="$PROJ_RATIO" -v gib="$GIB" -v lib="$LIB/" '
        NR == 1 { next }
        { est = ($1 == "CANDIDATE") ? $3 * ratio : $3;
          printf "| %s | %s | %s | %sx%s | %s | %s | %.2f | %.2f |\n", substr($2, length(lib) + 1), $1, $5, $7, $8, $9, $11, $3 / gib, est / gib;
          now += $3; after += est }
        END { printf "| **total** | | | | | | **%.2f** | **%.2f** |\n", now / gib, after / gib }' "$tsv"
}

# --- encoding ---------------------------------------------------------------

encode_one() { # src dst trc prim matrix range
    local src=$1 dst=$2 trc=$3 prim=$4 matrix=$5 range=$6
    local -a color=() venc
    [[ $trc != - && $trc != unknown && $trc != unspecified ]] && color+=(-color_trc:v:0 "$trc")
    [[ $prim != - && $prim != unknown && $prim != unspecified ]] && color+=(-color_primaries:v:0 "$prim")
    [[ $matrix != - && $matrix != unknown && $matrix != unspecified ]] && color+=(-colorspace:v:0 "$matrix")
    [[ $range != - && $range != unknown && $range != unspecified ]] && color+=(-color_range:v:0 "$range")
    if [[ $VENC == hevc_nvenc ]]; then
        venc=(-c:v:0 hevc_nvenc -preset "$PRESET" -tune hq -rc vbr -cq "$CQ" -b:v 0
            -profile:v main10 -pix_fmt p010le -rc-lookahead 32 -spatial_aq 1 -temporal_aq 1)
    else
        venc=(-c:v:0 libx265 -preset veryfast -crf "$CQ" -pix_fmt yuv420p10le)
    fi
    # The explicit set of copies keeps every audio/subtitle/attachment/chapter
    # track bit-exact; only the first real video stream is re-encoded.
    ffmpeg -nostdin -hide_banner -loglevel error -nostats -progress "$WORK/tmp/progress.txt" -y -i "$src" \
        -map 0 -map_metadata 0 -map_chapters 0 -c copy "${venc[@]}" "${color[@]}" \
        -max_muxing_queue_size 4096 -- "$dst"
}

verify() { # src_json dst_json dst -> prints check lines, returns 0 if all pass
    local sj=$1 dj=$2 dst=$3 ok=0 v err
    v=$(jq -rn --slurpfile b "$sj" --slurpfile a "$dj" "$JQ_DEFS"'
        def cnt(t): [.streams[] | select(.codec_type == t)] | length;
        ($b[0]) as $B | ($a[0]) as $A
        | [ (((($A.format.duration | tonumber) - ($B.format.duration | tonumber)) | fabs) < 1.0),
            (($A | cnt("audio")) == ($B | cnt("audio"))),
            (($A | cnt("subtitle")) == ($B | cnt("subtitle"))),
            (($A | cnt("attachment")) == ($B | cnt("attachment"))),
            (($A.chapters | length) == ($B.chapters | length)),
            ((($A | vstream.codec_name) == "hevc")),
            (($A.format.size | tonumber) < ($B.format.size | tonumber)) ] | @tsv')
    local names=(duration-within-1s audio-count subtitle-count attachment-count chapter-count video-is-hevc smaller)
    local -a r; IFS=$'\t' read -r -a r <<<"$v"
    local i
    for i in "${!names[@]}"; do
        printf '%s=%s ' "${names[$i]}" "$([[ ${r[$i]} == true ]] && echo pass || echo FAIL)"
        [[ ${r[$i]} == true ]] || ok=1
    done
    err=$(ffmpeg -nostdin -v error -i "$dst" -map 0:v:0 -map '0:a?' -f null - 2>&1 || true)
    if [[ -z $err ]]; then printf 'full-decode=pass'; else printf 'full-decode=FAIL(%s)' "${err:0:120}"; ok=1; fi
    echo
    return $ok
}

QUALITY_SSIM=-; QUALITY_PSNR=-
quality_metrics() { # src dst duration
    local src=$1 dst=$2 dur=$3 pct t out s p sums=0 sump=0 n=0
    for pct in 20 50 80; do
        t=$(awk -v d="$dur" -v p="$pct" 'BEGIN { printf "%.0f", d * p / 100 }')
        out=$(ffmpeg -nostdin -hide_banner -ss "$t" -t 20 -i "$src" -ss "$t" -t 20 -i "$dst" \
            -filter_complex '[0:v:0]format=yuv420p10le,split[a1][a2];[1:v:0]format=yuv420p10le,split[b1][b2];[a1][b1]ssim;[a2][b2]psnr' \
            -f null - 2>&1 || true)
        s=$(sed -n 's/.*SSIM.* All:\([0-9.]*\).*/\1/p' <<<"$out" | tail -1)
        p=$(sed -n 's/.*PSNR.*average:\([0-9.]*\).*/\1/p' <<<"$out" | tail -1)
        if [[ -n $s && -n $p ]]; then
            sums=$(awk -v a="$sums" -v b="$s" 'BEGIN { print a + b }')
            sump=$(awk -v a="$sump" -v b="$p" 'BEGIN { print a + b }')
            n=$((n + 1))
        fi
    done
    if ((n > 0)); then
        QUALITY_SSIM=$(awk -v a="$sums" -v n="$n" 'BEGIN { printf "%.4f", a / n }')
        QUALITY_PSNR=$(awk -v a="$sump" -v n="$n" 'BEGIN { printf "%.1f", a / n }')
    else
        QUALITY_SSIM=-; QUALITY_PSNR=-
    fi
}

swap_in() { # original encoded -> moves original to recycle, encoded into place
    local src=$1 new=$2 rel dest
    rel=${src#"$ROOT"/}
    dest=$RECYCLE/shrink-$(date +%Y%m%d)/$rel
    mkdir -p -- "$(dirname -- "$dest")"
    mv -- "$src" "$dest"
    if ! mv -- "$new" "$src"; then
        mv -- "$dest" "$src"
        return 1
    fi
    printf '%s\n' "$dest"
}

process_file() { # file apply(0|1) -> prints markdown; returns 0 ok
    local f=$1 apply=$2 line st dur trc prim matrix range base tmp dst sj dj before after
    line=$(classify "$f")
    IFS=$'\t' read -r st _ before dur _ _ _ _ _ _ _ _ _ trc prim matrix range _ <<<"$line"
    echo "### ${f#"$LIB"/}"
    if [[ $st != CANDIDATE ]]; then echo "Skipped: **$st** (not encoded)"; echo; return 1; fi
    base=$(basename -- "$f")
    if ((apply)); then dst=$WORK/tmp/$base; else dst=$WORK/test/$base; fi
    tmp=$(mktemp -d "$WORK/tmp/probe.XXXXXX"); sj=$tmp/before.json; dj=$tmp/after.json
    probe_json "$f" >"$sj"
    local avail; avail=$(df -B1 --output=avail -- "$WORK" | tail -1)
    if ((avail < before)); then echo "Skipped: **low free space**"; rm -rf "$tmp"; return 1; fi
    rm -f -- "$dst"
    local t0=$SECONDS
    if ! encode_one "$f" "$dst" "$trc" "$prim" "$matrix" "$range"; then
        echo "Encode **FAILED**; original untouched."; rm -f -- "$dst"; rm -rf "$tmp"; return 1
    fi
    probe_json "$dst" >"$dj"
    quality_metrics "$f" "$dst" "$dur"
    echo
    echo "| metric | before | after |"
    echo "|---|---|---|"
    jq -rn --slurpfile b "$sj" --slurpfile a "$dj" "$JQ_CMP"
    echo "| SSIM / PSNR (3 sampled 20 s windows) | - | $QUALITY_SSIM / ${QUALITY_PSNR} dB |"
    echo "| encode wall time (s) | - | $((SECONDS - t0)) |"
    echo
    echo "_Note: \`ambiguous-10bit-untagged\` on the after side is expected when an 8-bit untagged SDR source is written as 10-bit; the flag only ever blocks encoding of a source._"
    echo
    local checks vok=1
    checks=$(verify "$sj" "$dj" "$dst") || vok=0
    echo "Integrity: $checks"
    if ((vok == 0)); then echo "**Verification failed; original untouched; output removed.**"; rm -f -- "$dst"; rm -rf "$tmp"; return 1; fi
    if ((apply)); then
        local rec; rec=$(swap_in "$f" "$dst") || { echo "**Swap failed; original restored.**"; rm -rf "$tmp"; return 1; }
        after=$(stat -c %s -- "$f")
        printf '%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$f" "$before" "$after" >>"$WORK/done.log"
        echo "Replaced. Original moved to \`$rec\`."
    else
        after=$(stat -c %s -- "$dst")
        echo "Test output kept at \`$dst\` (original untouched)."
    fi
    echo "$before $after" >"$WORK/tmp/last-result"
    echo
    rm -rf "$tmp"
}

# --- modes ------------------------------------------------------------------

list_files() { # list-file -> validated absolute paths
    local p
    while IFS= read -r p || [[ -n $p ]]; do
        [[ -z $p || $p == \#* ]] && continue
        if [[ $p != "$LIB"/* || ! -f $p ]]; then echo "ignoring (outside $LIB or missing): $p" >&2; continue; fi
        printf '%s\n' "$p"
    done <"$1"
}

mode_scan() {
    local out=$REPORTS/scan-$TS.tsv n=0 f
    row "$HEADER" >"$out"
    # shellcheck disable=SC2094
    while IFS= read -r -d '' f; do
        classify "$f" >>"$out"
        n=$((n + 1)); ((n % 250)) || echo "scanned $n files..." >&2
    done < <(find "$LIB" -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.avi' -o -iname '*.m2ts' -o -iname '*.ts' -o -iname '*.wmv' \) -print0 | sort -z)
    summarize "$out" | tee "$REPORTS/scan-$TS.md"
    echo; echo "TSV: $out"
    awk -F'\t' '$1 == "CANDIDATE" { print $2 }' "$out" >"$WORK/candidates.txt"
    echo "Candidate list: $WORK/candidates.txt"
}

mode_plan() {
    local list=$1 out=$WORK/tmp/plan-$TS.tsv f
    row "$HEADER" >"$out"
    while IFS= read -r f; do classify "$f" >>"$out"; done < <(list_files "$list")
    plan_table "$out" | tee "$REPORTS/plan-$TS.md"
}

mode_batch() { # list apply
    local list=$1 apply=$2 f ok=0 bad=0 sb=0 sa=0 b a
    while IFS= read -r f; do
        rm -f "$WORK/tmp/last-result"
        if process_file "$f" "$apply"; then
            read -r b a <"$WORK/tmp/last-result"; sb=$((sb + b)); sa=$((sa + a)); ok=$((ok + 1))
        else
            bad=$((bad + 1))
        fi
    done < <(list_files "$list")
    echo "## Batch total"
    echo "files ok: $ok, failed/skipped: $bad"
    awk -v b="$sb" -v a="$sa" -v g="$GIB" 'BEGIN { printf "before %.2f GiB, after %.2f GiB, saved %.2f GiB (%.0f%%)\n", b / g, a / g, (b - a) / g, (b > 0 ? (b - a) / b * 100 : 0) }'
}

main() {
    local mode=${1:-scan}
    exec > >(tee -a "$REPORTS/$mode-$TS.md") 2>&1
    case $mode in
        scan) mode_scan ;;
        plan) mode_plan "${2:?usage: plan <list>}" ;;
        test) printf '%s\n' "${2:?usage: test <file>}" >"$WORK/tmp/test-list"; mode_batch "$WORK/tmp/test-list" 0 ;;
        run) mode_batch "${2:?usage: run <list>}" 1 ;;
        *) echo "usage: $0 scan | plan <list> | test <file> | run <list>" >&2; exit 2 ;;
    esac
}
main "$@"
