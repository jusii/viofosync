#!/bin/bash
#
# bench_filmstrip.sh — measure filmstrip-sprite decode cost on THIS host.
#
# Why this exists: viofosync runs on many NAS models (Synology Celeron/Atom,
# Ryzen embedded, ARM, …). Whether hardware decode (-hwaccel) beats software
# for filmstrip generation depends entirely on the box's CPU and iGPU, so the
# only trustworthy data comes from running the real command on the real
# hardware with a real 4K dashcam clip. Apple-Silicon dev-machine numbers do
# not transfer.
#
# It runs the EXACT command web/services/filmstrip.py issues, in several decode
# configurations, and reports for each:
#   real      wall-clock seconds (lower = faster for one clip)
#   cpu       user+sys seconds   (lower = more decode offloaded off the CPU —
#                                 THIS is the number that matters on a weak NAS
#                                 that spikes under concurrent filmstrip jobs)
#   dims      sprite WxH, to confirm the output is correct (not garbage)
#
# Usage:
#   ./bench_filmstrip.sh /path/to/4k_clip.MP4 [output_dir]
#
# ffmpeg/ffprobe are taken from PATH; override with env vars if needed, e.g.
# on Synology where ffmpeg ships inside a package:
#   FFMPEG=/var/packages/ffmpeg/target/bin/ffmpeg \
#   FFPROBE=/var/packages/ffmpeg/target/bin/ffprobe \
#   ./bench_filmstrip.sh /volume1/dashcam/2024_0101_120000_0001F.MP4
#
set -u
export LC_ALL=C   # force '.' decimal separator for time/ffprobe parsing

# --- production constants (keep in sync with web/services/filmstrip.py) ---
INTERVAL_S=8
TILE_W=160
TILE_H=90
RUNS="${RUNS:-3}" # runs per config (keep best); set RUNS=1 for a fast first pass

FFMPEG="${FFMPEG:-$(command -v ffmpeg || true)}"
FFPROBE="${FFPROBE:-$(command -v ffprobe || true)}"

die() { echo "error: $*" >&2; exit 1; }

[ -n "$FFMPEG" ]  || die "ffmpeg not found (set FFMPEG=/path/to/ffmpeg)"
[ -n "$FFPROBE" ] || die "ffprobe not found (set FFPROBE=/path/to/ffprobe)"

CLIP="${1:-}"
[ -n "$CLIP" ] || die "usage: $0 /path/to/4k_clip.MP4 [output_dir]"
[ -f "$CLIP" ] || die "clip not found: $CLIP"

OUTDIR="${2:-$(dirname "$CLIP")/.bench_filmstrip}"
mkdir -p "$OUTDIR" || die "cannot create output dir: $OUTDIR"

# --- describe the clip and compute the tile count the real code would use ---
# Probe each field separately: with -show_entries, ffprobe emits values in the
# stream's own field order (codec_name often precedes width/height), so a
# single combined query would mislabel them.
_probe1() {
  "$FFPROBE" -v error -select_streams v:0 \
    -show_entries "stream=$1" -of default=noprint_wrappers=1:nokey=1 "$CLIP"
}
WIDTH=$(_probe1 width)
HEIGHT=$(_probe1 height)
CODEC=$(_probe1 codec_name)
DURATION=$("$FFPROBE" -v error -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 "$CLIP")
BITRATE=$("$FFPROBE" -v error -show_entries format=bit_rate \
  -of default=noprint_wrappers=1:nokey=1 "$CLIP" 2>/dev/null)

# tiles = max(1, ceil(duration / INTERVAL_S)) — same as filmstrip.frame_count
TILES=$(awk -v d="$DURATION" -v i="$INTERVAL_S" \
  'BEGIN{ n=int((d+i-1)/i); if(n<1)n=1; print n }')
VF="fps=1/${INTERVAL_S},scale=${TILE_W}:${TILE_H},tile=${TILES}x1"
EXPECT_W=$(( TILES * TILE_W ))

echo "host    : $(uname -srm)"
echo "ffmpeg  : $FFMPEG"
echo "clip    : $CLIP"
printf "video   : %sx%s %s  dur=%.0fs  bitrate=%sk  -> %d tiles (expect %dx%d sprite)\n" \
  "$WIDTH" "$HEIGHT" "$CODEC" "$DURATION" \
  "$(awk -v b="${BITRATE:-0}" 'BEGIN{printf "%.0f", b/1000}')" \
  "$TILES" "$EXPECT_W" "$TILE_H"
echo

# --- which hwaccels did this ffmpeg build advertise? ---
HWACCELS=$("$FFMPEG" -hide_banner -hwaccels 2>/dev/null | tail -n +2 | tr -d ' ')
echo "advertised hwaccels: $(echo "$HWACCELS" | paste -sd',' -)"
[ -e /dev/dri/renderD128 ] && echo "found /dev/dri/renderD128 (iGPU render node present)"
echo

printf "%-26s  %8s  %8s  %-10s  %s\n" "config" "real(s)" "cpu(s)" "dims" "status"
printf "%-26s  %8s  %8s  %-10s  %s\n" "--------------------------" "-------" "------" "----------" "------"

# run_cfg LABEL  <pre-input args...>
# Times the production command RUNS times, keeps the best wall-clock, and
# verifies the sprite dimensions.
run_cfg() {
  label="$1"; shift
  out="$OUTDIR/${label//[^A-Za-z0-9]/_}.jpg"
  errlog="$OUTDIR/${label//[^A-Za-z0-9]/_}.err"

  best_real=""; best_cpu=""; rc=1
  for _ in $(seq 1 "$RUNS"); do
    rm -f "$out"
    TIMEFORMAT='%R %U %S'
    # ffmpeg's own stderr -> errlog, stdout -> /dev/null; the `time` builtin's
    # report is the only thing left on the compound's stderr, captured here.
    t=$( { time "$FFMPEG" -loglevel error -y "$@" -i "$CLIP" -an \
                 -vf "$VF" -frames:v 1 "$out" 2>"$errlog" 1>/dev/null ; } 2>&1 )
    rc=$?
    [ $rc -eq 0 ] || break
    real=$(echo "$t" | awk '{print $1}')
    cpu=$(echo "$t" | awk '{printf "%.3f", $2+$3}')
    if [ -z "$best_real" ] || awk -v a="$real" -v b="$best_real" 'BEGIN{exit !(a<b)}'; then
      best_real="$real"; best_cpu="$cpu"
    fi
  done

  if [ $rc -ne 0 ]; then
    msg=$(head -n1 "$errlog" 2>/dev/null | cut -c1-40)
    printf "%-26s  %8s  %8s  %-10s  FAIL %s\n" "$label" "-" "-" "-" "$msg"
    return 1
  fi

  dims=$("$FFPROBE" -v error -select_streams v:0 \
    -show_entries stream=width,height -of csv=p=0 "$out" 2>/dev/null)
  status="ok"
  case "$dims" in
    "${EXPECT_W},${TILE_H}") status="ok" ;;
    *) status="DIMS_MISMATCH" ;;
  esac
  printf "%-26s  %8s  %8s  %-10s  %s\n" "$label" "$best_real" "$best_cpu" "$dims" "$status"
}

# Baseline + software keyframe-skip (the no-GPU path the code uses).
run_cfg "software (no skip)"
run_cfg "software+skip"

# Every advertised hwaccel that's relevant to decode, with and without the
# keyframe skip — so you can see both the offload AND the surface-transfer
# cliff (hwaccel without skip downloads every decoded frame; can be ~25x).
for hw in videotoolbox cuda qsv vaapi; do
  echo "$HWACCELS" | grep -qx "$hw" || continue

  run_cfg "hw:${hw}+skip"        -hwaccel "$hw" -skip_frame nokey
  run_cfg "hw:${hw} (no skip)"   -hwaccel "$hw"

  # vaapi/qsv often need the render node named explicitly inside a container
  # (the Synology /dev/dri passthrough case). If the bare form failed and the
  # node exists, try again with the device so we learn what production needs.
  if { [ "$hw" = "vaapi" ] || [ "$hw" = "qsv" ]; } && [ -e /dev/dri/renderD128 ]; then
    run_cfg "hw:${hw}+skip+device" -hwaccel "$hw" \
            -hwaccel_device /dev/dri/renderD128 -skip_frame nokey
  fi
done

echo
echo "Read it like this:"
echo "  * 'cpu(s)' is the number to watch on a NAS — lower means decode is"
echo "    offloaded and the box stays responsive under concurrent jobs."
echo "  * a big gap between a hwaccel's '+skip' and 'no skip' rows confirms the"
echo "    surface-transfer cliff (skip_frame nokey is essential on every host)."
echo "  * any DIMS_MISMATCH / FAIL row means that decode path is not usable here."
echo "  * sprites left in: $OUTDIR  (delete when done)"
