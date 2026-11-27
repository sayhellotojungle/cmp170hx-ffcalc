#!/bin/bash
# One-shot: collect fuse words (read-only MMIO) plus all 512 physical
# fuse rows (fuse-macro CMD_READ pass, command-register writes only)
# from every CMP 170HX (device 0x20c2, or 0x20b5 once a card has been
# converted to A100 ids), print the potential table, save
# cards/<uuid>.json and plans/<uuid>.burn.json. Pass FFROWS=0 for a
# words-only probe on a busy card; plans then need
# `ffcalc mergeff cards/<uuid>.json rows.txt` and a rerun. Exit code is
# nonzero while any card lacks FF rows. Run from the repo root on the card host.
#   sudo ./scripts/ffscan.sh [--mig] [--a100] [collect-dir]
# Without flags the plan is everything full capability + Gen4 needs.
# --mig (or MIG=1) adds the media-engine floorsweep bits (chain1 blk8[15:11],
# blk9[2], blk23[3]): 5x NVDEC decode, NVJPG0 JPEG decode, OFA0 optical flow,
# and MIG beyond a single 1g instance. Irreversible like every FF burn. Row
# cost: if the capability records are being burnt in this same round, the engine
# bits ride along in blk8/blk23 and only blk9[2] needs a row of its own; on a
# card whose unlock is burnt already, all three bits carry their own row
# (measured: 3). `ffcalc gen --mig` prints the real number for this card. Cards
# captured before this version lack the OPT_*/STATUS_OPT_* words that prove
# those units are physically present, so re-capture first: those fields are
# skipped with a warning, not burned blind.
# --a100 (or A100=1) burns the FUSED device id 0x20c2 -> 0x20b5 plus the whole
# MAGIC_BITS_D capability word: HIGH RISK, it commits you to flashing an A100
# 80GB PCIe vBIOS with nvflash, and a platform that stops POSTing is
# recoverable only with CH341A on the SPI rom. --no-magic-d (or
# A100_NO_MAGICD=1) leaves MAGIC_BITS_D alone, which is what the conversion
# report actually did. Combines with --mig: the engine bits are chain 1, the
# identity words are chain 2, and the plan emits chain 1 first, so a burn that
# stops halfway leaves a booting CMP 170HX with its engines unlocked rather
# than an A100-fused card waiting for its vBIOS flash.
set -e
cd "$(dirname "$0")/.."
A100=${A100:-0}
MIG=${MIG:-0}
NO_MAGIC=${A100_NO_MAGICD:-0}
DIR=""
for a in "$@"; do
	case "$a" in
	--a100) A100=1 ;;
	--mig) MIG=1 ;;
	--no-magic-d) NO_MAGIC=1 ;;
	# print the whole header comment, whatever length it grows to
	-h|--help) sed -n '2,/^set -e/p' "$0" | sed '$d'; exit 0 ;;
	-*) echo "unknown option: $a" >&2; exit 2 ;;
	*) DIR=$a ;;
	esac
done
DIR=${DIR:-collected}
KDIR=$DIR/kprobe
K=/lib/modules/$(uname -r)/build
PY=python3

mkdir -p "$DIR" "$KDIR"
if [ ! -f "$KDIR/ffprobe.c" ] ||
   [ ffcalc/data/ga100-fuses.json -nt "$KDIR/ffprobe.c" ] ||
   [ samples/gen_probe_c.py -nt "$KDIR/ffprobe.c" ] ||
   [ ffcalc/ffmacro.py -nt "$KDIR/ffprobe.c" ]; then
	$PY samples/gen_probe_c.py > "$KDIR/ffprobe.c"
	printf 'obj-m += ffprobe.o\n' > "$KDIR/Makefile"
fi
N=$(grep -c '{ 0x' "$KDIR/ffprobe.c")
FFROWS=${FFROWS:-1}
if [ "$FFROWS" = "1" ]; then N=$((N + 512 + 1)); fi
sudo make -C "$K" M="$PWD/$KDIR" modules >/dev/null

# Loading the probe touches the fuse macro while the nvidia driver is attached.
# Even a careful module is a bad idea under load: an insmod killed mid CMD_READ
# can leave the controller non-idle, and one out-of-range readl (a table entry
# past the ioremap length) has taken a host down. So the probe only goes in on
# an idle card, and FORCE=1 has to be a decision, not a reflex.
# Loading the probe touches the fuse macro while the nvidia driver is attached.
# Even a careful module is a bad idea under load: an insmod killed mid CMD_READ
# can leave the controller non-idle, and one out-of-range readl (a table entry
# past the ioremap length) has taken a host down. Reading words is harmless;
# the CMD_READ pass writes FUSEADDR/FUSECTRL, so it waits for a quiet card.
if [ "${FORCE:-0}" != "1" ]; then
	BUSY=$(nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory \
	      --format=csv,noheader 2>/dev/null | grep -c . || true)
	if [ "${BUSY:-0}" != "0" ]; then
		echo "refusing to run the probe: $BUSY compute process(es) on the GPUs" >&2
		nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory \
		        --format=csv,noheader >&2
		echo "stop the workload first; FORCE=1 overrides only if you own the risk" >&2
		exit 3
	fi
	# The host-side unlocker's keepers are not CUDA clients, so nvidia-smi shows
	# nothing while they poke the card (cmp_hbm_pm logs "the keeper reports work
	# on the card"). They do not block a word readout; they do block CMD_READ.
	if [ "$FFROWS" = "1" ] && pgrep -f 'hbm-lease|gpu_unlock|cmpunlocker' >/dev/null 2>&1; then
		echo "refusing the CMD_READ pass: an unlocker keeper is working on the card" >&2
		pgrep -af 'hbm-lease|gpu_unlock|cmpunlocker' >&2
		echo "FFROWS=0 to capture words only, or stop the keeper, or FORCE=1" >&2
		exit 3
	fi
fi

nvidia-smi --query-gpu=pci.bus_id,uuid --format=csv,noheader \
	| tr 'A-F' 'a-f' | tr -d ' ' | sed 's/^00000000:/0000:/' \
	> "$DIR/uuid.csv"
: > "$DIR/bar0map.tsv"
for dev in /sys/bus/pci/devices/*; do
	# 0x20c2 = CMP 170HX as fused; 0x20b5 = the same card after a --a100
	# fused-id rewrite, which is what ffscan must still find next round.
	case "$(cat "$dev/device" 2>/dev/null)" in
	0x20c2|0x20b5) ;;
	*) continue ;;
	esac
	bdf=$(basename "$dev")
	bar0=$(head -1 "$dev/resource" | cut -d' ' -f1)
	uuid=$(awk -F, -v b="$bdf" 'tolower($1)==b {print $2; exit}' "$DIR/uuid.csv")
	sudo insmod "$KDIR/ffprobe.ko" bar0="$bar0" ffrows="$FFROWS" || true
	sudo dmesg | grep FFPROBE | tail -n "$N" > "$DIR/probe_${bdf//[:.]/_}.txt"
	printf '%s\t%s\t%s\n' "$bdf" "$bar0" "$uuid" >> "$DIR/bar0map.tsv"
done
SCAN_FLAGS=""
if [ "$MIG" = "1" ]; then SCAN_FLAGS="$SCAN_FLAGS --mig"; fi
if [ "$A100" = "1" ]; then
	SCAN_FLAGS="$SCAN_FLAGS --a100"
	# the conversion report's minimum: fused id only, capability word untouched
	if [ "$NO_MAGIC" = "1" ]; then SCAN_FLAGS="$SCAN_FLAGS --skip magic_d"; fi
fi
$PY -m ffcalc scan --dir "$DIR" --outdir cards --plans plans $SCAN_FLAGS
