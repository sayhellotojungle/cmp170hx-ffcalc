from . import ffrecord, ffrows, hardrows
from .analyze import analyze, replay_blocks, used_ff_rows
from .optionmap import (A100_DEVID, A100_MAGIC_D, BY_KEY, CMP_DEVID,
                        DEVID_KEYS, ENGINE_KEYS, ENGINE_PAIR_KEYS, FIELDS,
                        FF_ROW_MAX, FF_ROW_MIN, block_mask, get_bits, set_bits)

# What the fused-device-id rewrite means, said out loud in every plan that
# carries it: it is the one burn that changes what the card *is*, and it drags
# an invasive vBIOS flash in with it. The two records come from a community
# conversion report -- this repo has never walked the path on its own card, so
# the warning says "reported", not "verified".
A100_ID_WARNINGS = (
    "pcie_devida/pcie_devidb: rewrites the FUSED device id 0x%04x -> 0x%04x "
    "(chain2 blk37/38 -> 0x%04x, the records the community conversion burned: "
    "0x416A0942/0x416A0982, not yet reproduced on our own card). From the next "
    "cold boot the chip itself is 10de:%04x: lspci, the driver and the vBIOS "
    "boot gate all see an A100, so the card needs an A100 80GB PCIe vBIOS in "
    "its SPI rom to come up (nvflash --save the CMP rom first, only your own "
    "dump counts). Full capability + Gen4 do NOT need this: the default plan "
    "already gives them"
    % (CMP_DEVID, A100_DEVID, A100_DEVID << 1, A100_DEVID),
    "pcie_devida/pcie_devidb: this plan adds an invasive step on top of the "
    "fuse burn -- a vBIOS flash. Fused as an A100 while the rom is still the "
    "CMP one, the id mismatch keeps the card from initialising, and if the "
    "platform then fails to POST there is no in-band way back (no driver, no "
    "nvflash): CH341A + SOP8 clip on the SPI rom is the only recovery, so do "
    "not burn these rows without it. The platform BIOS must map a 128 GiB "
    "64-bit prefetchable BAR1 (Above 4G Decoding on, CSM off, Re-Size BAR on, "
    "MMIO High Size >= 128G); the judgement is the MMIO window, not how new "
    "the board is",
    "pcie_devida/pcie_devidb: the id can be pointed at another value by a "
    "later chain-2 record (later records win), but only while the card still "
    "boots into a burn window",
)

# --a100 rewrites the whole PCIe capability declaration word along with the id:
# someone paying the vBIOS price to look like an A100 wants the full A100
# declaration too, not a half-converted word that still carries CMP-era bits.
# That is our reasoning, not the community report's -- the conversion they
# describe leaves MAGIC_BITS_D alone, and nobody has read the word off a real
# A100 -- so it warns, and says how to check the value. It is not part of
# A100_ID_WARNINGS because --set magic_d=HEX can reach it without --a100.
A100_MAGIC_WARNING = (
    "magic_d -> 0x%08x: MAGIC_BITS_D is a PCIe capability declaration word "
    "(bit25 = 'no Gen4'), so rewriting it changes what the link may claim, "
    "not anything measured. Clearing bit25 alone is already enough to run "
    "Gen4, and the community conversion report does not touch this word at "
    "all; it is rewritten here because a card that takes an A100 id should "
    "not go on declaring CMP-era capability next to it. Any whole-word value, "
    "0x%08x included, is an EXPERIMENTAL guess nobody has read off a real "
    "A100: check it against one (BAR0 0x820520) first, or --skip magic_d to "
    "burn only what the report actually did"
)


# The Chain-1 floorsweep bits on the media engines are part of the default plan;
# `--no-mig` leaves them fused. Everything the operator needs in order to agree to
# those rows, in the order the questions come: what the bits are, what comes out of
# burning them, and what this does not prove. The row cost is not asserted here --
# it depends on which blocks this card's plan already rewrites, so it is computed
# per plan and appended below. The long version is the "媒体引擎与 MIG" section of
# README.
MIG_WARNINGS = (
    "nvdec_disable/nvjpg_ofa_disable_bit146/bit371: these are Chain-1 (TOP_FS) "
    "floorsweep bits, not a driver check for 10de:20c2 and not a VBIOS lock. "
    "That the units are really here is read out, not assumed: this snapshot has "
    "STATUS_OPT_* == OPT_DISABLE for them, and hardware computes STATUS_OPT = "
    "OPT_DISABLE | OPT_DEFECTIVE, so that equality means every unit exists with "
    "no defects (on the community card the same clear made every NVDEC/NVJPG/"
    "OFA slot show up in NV_PTOP_DEVICE_INFO2). The volatile route for these "
    "floorsweep bits is inert on datacenter GA100: FUSE_EN_SW_OVERRIDE (0x820040) "
    "= 0, and writes to the override/option words "
    "(CTRL_OPT_NVJPG 0x82080c, CTRL_OPT_NVDEC 0x820824, CTRL_OPT_SYS_PIPE "
    "0x820a30, OPT_NVDEC_DISABLE 0x820378, OPT_SYS_PIPE_DISABLE 0x8203a4) never "
    "moved STATUS_OPT_*, even inside the burn window. (That is this route only: "
    "the 0x8238xx feature-override island -- compute rate, ECC, FB geometry -- is "
    "a separate mechanism and is written every boot.) Burning an FF record is "
    "the only lever for these bits",
    "what this buys: 5x NVDEC hardware decode (H264/HEVC/VP9/MPEG2/JPEG; no "
    "AV1, GA100's NVDEC predates the GA10x AV1 block), NVJPG0 hardware JPEG "
    "decode, OFA0 optical flow (NvOF API 5.0), and real multi-instance MIG -- "
    "with sys_pipe_disable=0, which the default plan already burns, the 8 "
    "SYS_PIPEs give 8g/4g/2g/1g profiles instead of the single non-"
    "partitionable 1g that un-burnt cards show",
    "what this does NOT prove: which of bit146/bit371 is NVJPG and which is "
    "OFA (both are cleared, as with the two unresolved DISABLE_GEN3_SPEED "
    "bits; separating them takes a bus reset between the two rows on a second "
    "card), and nothing here touches ECC. AV1 is architecturally absent, not "
    "fuse-gated. The functional side (5/5 concurrent CUVID sessions, "
    "nvjpegDecode HARDWARE, NvOF API 5.0, 8-way `mig -lgip`) comes from an "
    "external card report -- nothing on THIS card has run it yet. After "
    "toggling MIG mode (`nvidia-smi -mig 1/-mig 0`) REBOOT before using MIG: "
    "the in-driver FLR resets the FB geometry back to 8192 MiB until "
    "gpu_unlock restores it from initramfs, and live slices silently shrink to "
    "1g.1gb",
)


def plan(dump, only=None, skip=(), overrides=None, a100=False, mig=True,
         allow_hard_floor=False):
    # Without a complete FF-row snapshot we cannot know which rows are
    # occupied or what the current block values are; refuse rather than
    # append records onto already-burnt rows.
    ffrows.require(dump, "generate a burn plan")
    only = set(only) if only else None
    skip = set(skip)
    # --a100 adds targets below; copy first so a caller's dict (and a re-used
    # overrides dict) is never rewritten by planning a card.
    overrides = dict(overrides or {})
    mig_keys = ()
    if a100:
        # Community-reported path: the boot gate that refuses an A100 vBIOS
        # compares the *fused* device id with the rom's, so the conversion
        # burns DEVIDA/DEVIDB to 0x20b5 and then flashes an A100 80GB PCIe
        # vBIOS -- the rom itself stays byte-identical to stock. We have not
        # walked it on our own card, hence the warnings below.
        #
        # MAGIC_BITS_D is added on top: the word states what the link may
        # claim, and a card that goes as far as a new identity should not keep
        # declaring CMP-only capability next to an A100 id. Not in the
        # conversion report, hence its own warning.
        #
        # --skip/--only are respected here, not just downstream: a target named
        # without any record behind it would leave `sim` reporting an unfilled
        # target for a field this plan was told to leave alone.
        for key in DEVID_KEYS + ("magic_d",):
            if key in skip or (only is not None and key not in only):
                continue
            overrides.setdefault(key, A100_MAGIC_D if key == "magic_d"
                                 else A100_DEVID)
    if mig:
        # The media-engine floorsweep bits (chain1 blk8[15:11], blk9[2],
        # blk23[3]) are in the plan unless this is `--no-mig`. Nothing else in
        # this tool touches them, and without a STATUS_OPT_* readout
        # resolve_targets() refuses them outright: see MIG_WARNINGS and the
        # field comments in optionmap.py. Honored here rather than as an
        # override, so --skip/--only still decide: a target with no record
        # behind it would leave `sim` reporting an unfilled field this plan was
        # told to leave alone.
        mig_keys = tuple(key for key in ENGINE_KEYS
                         if key not in skip and (only is None or key in only))
    rep = analyze(dump, overrides, mig_keys=mig_keys)
    targets = rep["targets"]
    warnings = list(rep["warnings"])
    words = dump["option_words"]

    # Clearing the disable bit of a block the factory marked dead is not an
    # ambitious plan, it is a different kind of failure: the shadow registers
    # follow OPT_DISABLE, so the readout agrees with the plan, while the block
    # stays dead and (measured on this bench's 8 GB card, where fbp_disable
    # equals fbp_defective at 0x2d) GSP-RM never gets past the booter's
    # instruction fetch. *_DEFECTIVE sits in the hard rows, outside the FF
    # address space, so no record in this plan can overrule it -- an RIR record
    # could, and an 8 GB SKU card was read out with no free RIR slot to put one
    # in. Refusing beats emitting a plan whose own analysis section then reports
    # a capacity the burn cannot deliver.
    hard = rep["hard"]
    below = [k for k in hard["beyond"]
             if targets.get(k) != words.get(k)]
    if below and not allow_hard_floor:
        def why(k):
            """One line per offending field: the mask, and where it lives."""
            defect = BY_KEY[k].defect_key or k
            field = hardrows.HARD_BY_KEY.get(defect)
            where = (", ".join(hardrows.coord(o, hardrows.WIDTHS[defect])
                               for o in field.offsets)
                     if field and field.offsets else
                     "a position no sample here pins down")
            return "%s -> 0x%x while %s reads 0x%x (%s)" % (
                k, targets[k], defect, words.get(defect, 0), where)
        raise hardrows.BeyondHardFloor(
            "the plan asks for a state the hard rows forbid (%s). *_DEFECTIVE is "
            "a hard-row mask, outside the FF address space, so no record here "
            "reaches it: the burn would move the shadow registers and not the "
            "silicon -- measured on fbp_disable, STATUS_OPT_FBP followed the "
            "target while the card kept its ceiling, and GSP-RM stopped at "
            "PTOP_GFW_BOOT=0x1 with gsp_riscv_status=0. Hard ceiling: %s. Only an "
            "RIR record overrules a hard row, and an 8 GB SKU card of this bench "
            "reads 0 of 16 free slots; --allow-hard-floor says this is a "
            "deliberate experiment"
            % ("; ".join(why(k) for k in below),
               ("%d GiB (%d FBP)" % (hard["ceiling_gib"], hard["ceiling_fbp"])
                if hard["ceiling_gib"] is not None else "unknown")))

    base = {c: dict(b) for c, b in replay_blocks(dump).items()}
    pending = {c: dict(b) for c, b in base.items()}
    # The same replay with the media-engine bits left alone: what this plan
    # would have written anyway. Those bits cost a row only where they change a
    # block this baseline does not already touch, which is the difference
    # between "clearing NVDEC is free" (chain1 blk8 is being rewritten for
    # gpc/fbpa regardless) and "clearing NVDEC costs a row".
    baseline = {c: dict(b) for c, b in base.items()}
    selected = []

    for f in FIELDS:
        if not f.burnable or f.key in skip or f.key not in targets:
            continue
        if only is not None and f.key not in only:
            continue
        cur = get_bits(pending, f.chain, f.bit, f.width)
        tgt = targets[f.key]
        if cur == tgt:
            continue
        set_bits(pending.setdefault(f.chain, {}), f.bit, f.width, tgt)
        if f.key not in ENGINE_KEYS:
            set_bits(baseline.setdefault(f.chain, {}), f.bit, f.width, tgt)
        selected.append(f)

    def touched(state):
        return set((chain, blk) for chain, blocks in state.items()
                   for blk, value in blocks.items()
                   if value != base.get(chain, {}).get(blk, 0))

    extra_rows = sorted(touched(pending) - touched(baseline))

    used = set(used_ff_rows(dump))
    next_row = (max(used) + 1) if used else FF_ROW_MIN
    records = []
    for chain in sorted(pending):
        for blk in sorted(pending[chain]):
            old = base.get(chain, {}).get(blk, 0)
            new = pending[chain][blk]
            if old == new:
                continue
            if next_row > FF_ROW_MAX:
                raise RuntimeError("ran out of free FF rows")
            why = [f.key for f in selected
                   if f.chain == chain and
                   (old ^ new) & block_mask(f, blk)]
            records.append({
                "row": next_row, "chain": chain, "block": blk,
                "old": "0x%04x" % old, "new": "0x%04x" % new,
                "record": "0x%08x" % ffrecord.encode(chain, blk, new),
                "fields": why,
            })
            next_row += 1

    # Say what the identity rows in this plan actually do -- a plan file is a
    # burn order, and whoever executes it must not have to know the history.
    in_plan = {key for rec in records for key in rec["fields"]}
    ids_in_plan = [key for key in DEVID_KEYS if key in in_plan]
    if ids_in_plan:
        warnings.extend(A100_ID_WARNINGS)
        for key in ids_in_plan:
            # A card that already answers to 0x20c2 replays it from its FF
            # records. When it does not, `old` is not the id in use, and the
            # operator should hear that from the plan rather than conclude the
            # fuse register was lying.
            live = words.get(key)
            if live is not None and rep["current"].get(key) != live:
                replayed = rep["current"].get(key, 0)
                warnings.append(
                    "%s: this snapshot replays 0x%04x while the fuse register "
                    "reads 0x%04x, so the planned old value is not the id the "
                    "card answers to today (the id can live in rows this "
                    "readout does not cover); the record replaces the whole "
                    "block either way" % (key, replayed, live))
        if len(ids_in_plan) == 1:
            warnings.append(
                "only %s is rewritten here: the two halves of the id are meant "
                "to agree, and which one the boot gate reads is not documented. "
                "Opening a burn window is the expensive part of this procedure "
                "(power sequence and timing, not the record count), so splitting "
                "the two halves pays that cost twice and leaves the card in an "
                "id-inconsistent state in between: burn both in the same window "
                "unless you know why you are not" % ids_in_plan[0])
    if "magic_d" in in_plan:
        # Whoever burns this word, from --a100 or an explicit --set, has to
        # know the value is a guess and what the cheaper alternative was.
        warnings.append(A100_MAGIC_WARNING % (targets["magic_d"],
                                             A100_MAGIC_D))

    # Same reasoning for the media-engine rows: the plan file is a burn order,
    # and these three bits are the only ones in here whose payoff is not
    # already visible in the FB/SM numbers above.
    engines_in_plan = [key for key in ENGINE_KEYS if key in in_plan]
    if engines_in_plan:
        warnings.extend(MIG_WARNINGS)
        # "Free" only means "rides in a block this plan already rewrites", and
        # whether that happens is a property of THIS plan: on a card whose
        # capability records are burnt already, chain1 blk8/blk23 hold nothing
        # else and every engine bit has to carry its own row. Measured on our
        # own two cards, both already unlocked: 3 rows, not the 1 a fresh card
        # taking the whole default plan in one round pays.
        engine_blocks = set((f.chain, f.bit // 16) for f in FIELDS
                            if f.key in ENGINE_KEYS and f.key in in_plan)
        free = sorted(engine_blocks & touched(baseline))
        if extra_rows:
            msg = ("row cost of the media-engine bits on this card (--no-mig "
                   "leaves them fused): %d extra FF row(s) (%s)"
                   % (len(extra_rows),
                      ", ".join("chain%d blk%d" % t for t in extra_rows)))
            if free:
                warnings.append(
                    msg + "; the rest rode along in %s, blocks this plan "
                    "rewrites anyway" % ", ".join(
                        "chain%d blk%d" % t for t in free))
            else:
                warnings.append(
                    msg + ": nothing else in this plan touches those blocks, so "
                    "each engine bit carries its own row. That is the price of "
                    "a later add-on round -- FF rows are finite and every "
                    "already-written block has to be rewritten, which is why "
                    "clearing these bits in the same round as the capability "
                    "records is the cheap moment")
        else:
            warnings.append(
                "row cost of the media-engine bits on this card: 0 extra rows, "
                "every engine bit here sits in a block the plan already rewrites "
                "(the cheap moment; --no-mig would skip them entirely)")
        pair = [key for key in ENGINE_PAIR_KEYS if key in in_plan]
        if len(pair) == 1:
            warnings.append(
                "only %s of the NVJPG/OFA pair is being cleared: which engine "
                "comes out depends on which one that bit belongs to, and that "
                "mapping is exactly the part nobody has separated yet -- "
                "clearing both together is what the community card did and the "
                "only combination with a functional proof behind it"
                % pair[0])
        if targets.get("sys_pipe_disable"):
            warnings.append(
                "8-way MIG also needs sys_pipe_disable=0, which this plan "
                "leaves at 0x%x: with one SYS_PIPE, MIG stays a single "
                "non-partitionable 1g instance, so the DEC/JPEG/OFA units "
                "cleared here stay unreachable from any slice"
                % targets["sys_pipe_disable"])

    return {
        "sku": dump["sku"],
        "source": dump["source"],
        "uuid": dump.get("uuid"),
        "fingerprint": dump.get("fingerprint"),
        "burn": records,
        "targets": {k: "0x%x" % v for k, v in targets.items()},
        "analysis": {
            "usable_fbp": rep["usable_fbp"], "fb_gib": rep["fb_gib"],
            "sm_count": rep["sm_count"], "sm_per_gpc": rep["sm_per_gpc"],
            # Whole media/MIG block, same shape `report` prints: which engines
            # are live now and after this plan, what the OPT/STATUS readout
            # proved, and whether the software override route was even alive.
            "media": rep["media"],
            "nvlink_ok": rep["nvlink_ok"], "pcie_gen4_ok": rep["pcie_gen4_ok"],
            # The hard-row side of the same question: what the silicon allows
            # regardless of this plan, what the FF pool can still buy, and what
            # an RIR record would cost. A plan file is a burn order, and "these
            # rows buy 16 GiB" has to be checkable against the ceiling from it.
            "hard": rep["hard"],
        },
        "warnings": warnings,
    }
