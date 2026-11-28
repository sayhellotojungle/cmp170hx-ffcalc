from . import ffrecord, ffrows, hardrows, rir
from .dump import int_value as _fuse_value
from .optionmap import (BY_KEY, ENGINES, FIELDS, FF_ROW_MAX, FF_ROW_MIN,
                        TOTAL_FBP, TOTAL_GPC, TPC_PER_GPC, SM_PER_TPC,
                        TOTAL_SYS_PIPE, BYTES_PER_FBP, popcount)

# Speed-select readout that the community register table labels the 1/32-rate
# slot. Our unlocked cards read 0x5 in these words while the feature-override
# island reports no throttle, which is the evidence that the FF pool does not
# govern the effective rate: it only holds the factory setting.
SS_THROTTLED = 0x5
# NV_PTIMER GSP/firmware boot progress: every step done reads 0x3ff.
GFW_CLEAN = 0x3FF
# Reference clock the HBM PLL multiplies (NV_PFB_FBM_PLL_CFG1 coeff N, [15:8]).
PLL_REF_MHZ = 27
# The two HBM PLL coefficients seen on our cards: the loaded one (27 MHz x 64 =
# 1728 MHz, and the same value the host's own cmp_hbm_pm driver prints as
# busy_coeff) and the idle one (27 MHz x 20 = 540 MHz). Both cards read the idle
# coefficient with nothing on them and the loaded one under traffic, so the word
# is power management, not a stock/modified distinction.
HBM_BUSY_COEFF = 0x00014001
HBM_IDLE_COEFF = 0x00011401


def ff_rows_only(dump):
    return {r: v for r, v in dump["ff_rows"].items()
            if FF_ROW_MIN <= r <= FF_ROW_MAX}


def used_ff_rows(dump):
    return sorted(r for r, v in ff_rows_only(dump).items() if v != 0)


def free_ff_rows(dump):
    used = set(used_ff_rows(dump))
    return [r for r in range(FF_ROW_MIN, FF_ROW_MAX + 1) if r not in used]


def replay_blocks(dump):
    return ffrecord.replay(ff_rows_only(dump))


def decode_current(blocks_by_chain):
    from .optionmap import decode_field
    return {f.key: decode_field(blocks_by_chain, f) for f in FIELDS}


def resolve_targets(dump, overrides=None, mig_keys=()):
    """Target value per field, plus the warnings and the hard-floor violations.

    The default for every burnable field is its own ``*_DEFECTIVE``: the floor
    the silicon allows, which is also what makes the default plan's capacity
    number the real ceiling rather than an aspiration. A target that clears one
    of those bits is recorded in `violations` -- see :func:`hard_state` for what
    that state actually does to a card.

    `mig_keys` are the Chain-1 media-engine floorsweep fields the caller's plan
    carries, which is the difference between the default plan and ``--no-mig``.
    They default to 0 like any other burnable field, and they are held to the
    same proof as an explicit ``--set``: no ``STATUS_OPT_*`` readout, no row.
    """
    words = dump["option_words"]
    info = _info_words(dump)
    overrides = dict(overrides or {})
    for _key in mig_keys:
        overrides.setdefault(_key, 0)
    targets = {}
    warnings = []
    violations = []
    for f in FIELDS:
        if not f.burnable:
            continue
        if f.opt_in and f.key not in overrides:
            continue
        if f.proof_keys:
            # Media-engine floorsweep (category "engine"): there is no
            # *_DEFECTIVE register for these, so the STATUS_OPT_* readout is
            # the only evidence that the engine exists on this die --
            # STATUS_OPT_* = OPT_DISABLE | OPT_DEFECTIVE, so the two words
            # having to agree is what says "no defects". If the snapshot did
            # not read them, the field is skipped rather than guessed at: an
            # FF row spent on an engine that turns out to be missing cannot be
            # given back. `--set` does not bypass this, because the value in a
            # `--set` says nothing about the silicon either.
            missing = [k for k in f.proof_keys if k not in info]
            if missing:
                warnings.append(
                    "%s: OPT_*/STATUS_OPT_* not in this snapshot (%s never "
                    "read), so this engine's defect state is unknown and no FF "
                    "row was planned for it -- re-capture the card (the "
                    "readout map grew) before asking for it"
                    % (f.key, "/".join(missing)))
                continue
            targets[f.key] = overrides.get(f.key, 0)
            continue
        if f.key in overrides:
            targets[f.key] = overrides[f.key]
        elif f.defect_key and f.defect_key in words:
            targets[f.key] = words[f.defect_key]
        elif f.key in words:
            targets[f.key] = words[f.key]
            if f.category != "pcie":
                warnings.append(
                    "%s: no %s in dump; assuming current value (no-op)"
                    % (f.key, f.defect_key))
        elif f.category == "pcie":
            targets[f.key] = 0
        else:
            warnings.append("%s: no value and no defect key; skipped" % f.key)
        if f.key in targets and f.defect_key and f.defect_key in words:
            defect = words[f.defect_key]
            if (targets[f.key] & defect) != defect:
                violations.append(f.key)
                warnings.append(
                    "%s: target 0x%x does not contain defective 0x%x, so %d of "
                    "those blocks are known bad. The shadow registers will still "
                    "read the target (STATUS_OPT_* follows OPT_DISABLE; the "
                    "defective mask does not force it back), and that is not "
                    "success: on this bench's 8 GB card, clearing a dead FBP's "
                    "disable bit left GSP-RM dead in the booter's instruction "
                    "fetch (PTOP_GFW_BOOT=0x1, gsp_riscv_status=0). Only an RIR "
                    "record overrules a hard row, and an 8 GB SKU card has no "
                    "free RIR slot to put one in"
                    % (f.key, targets[f.key], defect,
                       popcount(defect & ~targets[f.key])))
    return targets, warnings, violations


def _info_words(dump):
    """``dump["info"]`` with every value as an int.

    Info words arrive through two channels that write them differently -- the
    probe module through ``ingest`` writes ints, ``capture`` writes
    ``"0x..."`` strings -- so anything doing arithmetic on them has to
    normalise first, exactly like ``option_words``. Bad values are refused
    rather than guessed at.
    """
    return dict((k, _fuse_value(v, "info.%s" % k))
                for k, v in (dump.get("info") or {}).items())


def analyze_media(dump, targets, warnings, info=None):
    """What the media engines and MIG are now, and what the plan makes them.

    Per engine the caller gets two separate answers: ``planned`` (the gating
    fields are in this plan at all) and ``cleared`` (they are in it *at 0*, which
    is what actually switches the units on). ``after`` follows ``cleared``.

    There is no ``*_DEFECTIVE`` register for NVDEC/NVJPG/OFA, but the defect
    state is still readable: hardware computes ``STATUS_OPT_* = OPT_DISABLE |
    OPT_DEFECTIVE``, so the two words agreeing proves the engine exists on this
    die, and a STATUS bit outside OPT names a unit that no fuse can switch on.
    That comparison is the whole reason these three fields require the readout
    (``OptionField.proof_keys``) instead of trusting a ``--set``: a burnt row
    spent on silicon that is not there is the one mistake here that cannot be
    undone.
    """
    info = _info_words(dump) if info is None else info
    words = dump["option_words"]
    engines = []
    for label, opt_key, status_key, total, mask, gates in ENGINES:
        planned = all(k in targets for k in gates)
        # Being in the plan is not the same as being cleared: the media bits are
        # in the default plan at 0, but `--set nvdec_disable=0x1f` plans that
        # field at its factory value, and the honest number for that engine is
        # "still off". Reading `planned` as "cleared" is how a report ends up
        # promising five decoders a plan does not deliver.
        cleared = planned and not any(targets.get(k) for k in gates)
        e = {"label": label, "total": total, "keys": list(gates),
             "read": False, "planned": planned, "cleared": cleared,
             "now": None, "after": None, "defective": None}
        opt, status = info.get(opt_key), info.get(status_key)
        if opt is not None and status is not None:
            # Bits STATUS_OPT has that OPT_DISABLE does not = physical defects.
            defect = status & ~opt & mask
            e.update({"read": True, "opt": opt, "status": status,
                      "defective": defect,
                      "now": total - popcount(opt & mask),
                      "after": total - popcount(
                          (defect if cleared else opt) & mask)})
            if cleared and defect:
                warnings.append(
                    "%s: STATUS_OPT has 0x%x beyond OPT_DISABLE, so %d of the "
                    "%d units are physically missing -- clearing the fuse "
                    "unlocks the rest but cannot create those, and the FF rows "
                    "spent on them stay spent"
                    % (label, defect & mask, popcount(defect & mask), total))
        engines.append(e)

    # MIG is gated by SYS_PIPE, one pipe per GPC, and it is in the default plan
    # already; what the media-engine bits add on top (`--no-mig` leaves them) are
    # the engines the profiles report as DEC/JPEG/OFA. 1 pipe is what makes
    # `nvidia-smi mig -lgip` offer a single non-partitionable profile, which is
    # the "MIG works but only 1g" that the community kept running into.
    sp = words.get("sys_pipe_disable")
    sp_def = words.get("sys_pipe_defective", 0)
    sp_after = targets.get("sys_pipe_disable", sp)
    mig = {"read": sp is not None, "defective": sp_def,
           "now": (TOTAL_SYS_PIPE - popcount(sp | sp_def))
                  if sp is not None else None,
           "after": (TOTAL_SYS_PIPE - popcount(sp_after | sp_def))
                    if sp_after is not None else None}
    if mig["after"] == 1:
        why = ("sys_pipe_defective 0x%x leaves one" % sp_def if not sp_after
               else "sys_pipe_disable is that gate and this plan does not "
                    "clear it")
        warnings.append(
            "1 SYS_PIPE after this plan: MIG stays single-instance (`mig "
            "-lgip` shows one non-partitionable 1g profile); %s" % why)

    # Why an FF record is the only lever for these bits: the volatile route is
    # inert on datacenter GA100, so no write to CTRL_OPT_* moves STATUS_OPT_*.
    # (The 0x8238xx feature-override island is a different mechanism and is not
    # inert -- see runtime_state().)
    # Note the alias: 0x820084 is the word ffmacro reads as DSOV_STATUS and
    # demands be 0 inside the write window (and the floorsweep literature calls
    # it FUSE_DIS_SW_OVR, read as 1 under a running driver -- same register,
    # two names, two ends of the same fact). So the verdict rests on
    # EN_SW_OVERRIDE = 0, which says the override path is not enabled at all,
    # and DSOV is reported alongside so the reader can see both names.
    en, dsov = info.get("en_sw_override"), info.get("dsov_status")
    sw_override = {"read": en is not None and dsov is not None,
                   "en": en, "dsov": dsov,
                   "dead": en is not None and not en}
    # The other half of chain 1's unclaimed 1-bits, read out so the report can
    # say what is *not* being burned and why: DISPLAY (chain1 bits 78..82) and
    # DISP_HEAD stay fused on every GA100 because the part has no display
    # outputs, so those five bits are accounted for rather than "unknown".
    disp, heads = info.get("status_display"), info.get("status_disp_head")
    return {"mig": mig, "engines": engines, "sw_override": sw_override,
            "display": {"read": disp is not None and heads is not None,
                        "status": disp, "disp_head": heads}}


def hard_state(words, sku, violations):
    """The ceiling the hard rows impose, and what could still move it.

    Three different numbers get confused with each other, so they are computed
    separately here:

    ``ceiling_gib``   what the silicon allows: ``12 - popcount(*_DEFECTIVE)`` FBP,
                      times the per-FBP size of the SKU. ``*_DISABLE`` takes no
                      part in it -- the masks live in hard rows, which an FF
                      record cannot address at all (``ffcalc/hardrows.py``).
    ``ff_headroom_gib`` what the FF pool can still buy on this card right now:
                      the gap between today's ``fbp_disable`` and the floor. Zero
                      here means "this card's plan buys no memory", which is the
                      answer for a card whose ``disable`` equals its
                      ``defective``.
    ``records``       the RIR records that would lift the mask, one per dead FBP,
                      and ``rir`` says whether such a record could be placed at
                      all on this SKU.

    ``violations`` are the fields whose target clears a bit ``*_DEFECTIVE`` sets.
    The shadow registers do follow such a target (``STATUS_OPT_FBP`` tracks
    ``OPT_DISABLE``, defective does not force it back), so a readout agreeing is
    not success: on this bench's 8 GB card the un-disable was tried and GSP-RM
    died in the booter's instruction fetch (``PTOP_GFW_BOOT`` 0x1,
    ``gsp_riscv_status`` 0).
    """
    dead = hardrows.dead_fbps(words)
    per = BYTES_PER_FBP.get(sku)
    out = {"dead": dead, "ceiling_gib": None, "ceiling_fbp": None,
           "ff_headroom_gib": None, "beyond": sorted(violations),
           "located": hardrows.coord(hardrows.FBP_OFFSET, 12),
           "rir": rir.route(sku), "records": [], "read": dead["mask"] is not None}
    if not out["read"]:
        return out
    mask = dead["mask"]
    out["ceiling_fbp"] = TOTAL_FBP - popcount(mask)
    if per is not None:
        out["ceiling_gib"] = out["ceiling_fbp"] * per
    current = words.get("fbp_disable")
    if current is not None and per is not None:
        # What an FF record can hand back is exactly the set of partitions that
        # are switched off but not dead. Written as an intersection rather than a
        # subtraction: `disable` need not contain `defective` (a card left mid-
        # experiment by an un-disable has disable bits missing where defective
        # has them), and subtracting popcounts would go negative there.
        out["ff_headroom_gib"] = popcount((current & ~mask) & 0xfff) * per
    out["records"] = hardrows.rir_to_free(mask)
    return out


def runtime_state(info):
    """What the card is running on *this boot*, as read from BAR0.

    The fuses decide the resource set: SM count, memory size, NVLink, link
    speed. They do not decide what rate the compute blocks run at, whether ECC
    is on, or what geometry the framebuffer is mapped with: those are the
    feature-override island and the FB block registers, written every boot by
    the driver-side unlocker and taken back by a power cut. Reading them keeps
    "the FF records allow it" from being mistaken for "this boot is running
    unthrottled with ECC on", which are claims about two different things.

    Values are the ones two unlocked CMP 170HX cards under a running driver
    read; a snapshot taken before this readout map existed reports the section
    as not read rather than guessing at it.
    """
    selects = dict((k, info[k]) for k in
                   ("ss_dp", "ss_ffma", "ss_fmla16", "ss_fmla32", "ss_imla0",
                    "ss_imla1", "ss_imla2", "ss_imla3", "ss_imla4")
                   if k in info)
    out = {}

    if selects or "feat_readout_1" in info or "feat_ovr_sm_spd_0" in info:
        out["compute"] = {
            "read": "feat_readout_1" in info,
            "readout": info.get("feat_readout_1"),
            "full_rate_now": info.get("feat_readout_1") == 0,
            "override": [info.get("feat_ovr_sm_spd_0"),
                         info.get("feat_ovr_sm_spd_1")],
            "fused": selects,
            "fused_throttled": sorted(k for k, v in selects.items()
                                      if v == SS_THROTTLED),
        }

    ctrl, ecc_fused = info.get("ecc_ctrl"), info.get("opt_ecc_en")
    sram_keys = ("feat_ovr_ecc", "feat_ovr_ecc_1", "feat_ovr_ecc_2")
    if ctrl is not None or ecc_fused is not None:
        out["ecc"] = {
            "read": True,
            "fused": ecc_fused,
            "ctrl": ctrl,
            "dram_master": None if ctrl is None else bool(ctrl & 0x1),
            "dram_sideband": None if ctrl is None else bool(ctrl & 0x40),
            "sram": [info.get(k) for k in sram_keys],
            "sram_read": all(k in info for k in sram_keys),
        }

    geom = dict((k, info[k]) for k in ("fbpa_cfg1", "mmu_lmr",
                                       "cstatus_ramamount") if k in info)
    if geom:
        out["geometry"] = geom

    coeff = info.get("hbm_pll_coeff")
    hbm_state = None
    if coeff is not None:
        hbm_state = {HBM_BUSY_COEFF: "the busy coefficient the host unlocker "
                                      "programs",
                     HBM_IDLE_COEFF: "idle: HBM downclocked"}.get(
            coeff, "a coefficient the host unlocker does not use")
    out["board"] = {
        "lane_disable": info.get("opt_pcie_lane_disable"),
        "lane_defective": info.get("opt_pcie_lane_defective"),
        "gfw_boot": info.get("gfw_boot"),
        "gfw_clean": info.get("gfw_boot") == GFW_CLEAN,
        "hbm_pll_coeff": coeff,
        "hbm_mhz": None if coeff is None else PLL_REF_MHZ * ((coeff >> 8) & 0xff),
        "hbm_state": hbm_state,
    }

    iff = info.get("iff_record")
    debugctrl = info.get("fuse_debugctrl")
    macro = {
        "crc": info.get("crc_status"), "efp": info.get("efp_status"),
        "dsov": info.get("dsov_status"),
        "en_sw_override": info.get("en_sw_override"),
        "debugctrl": debugctrl,
        # bit1 (RWL) is the read multiplexer for the RIR macro: while it is set,
        # a row read is an RIR row and not a main-array row. Nothing here sets it
        # (that is a write, and RIR is not on our list), so it is reported only so
        # a snapshot taken by somebody who did set it is not read as the array.
        "rwl": None if debugctrl is None else bool(debugctrl & 0x2),
        "debugctrl_plm": info.get("fuse_debugctrl_plm"),
        "priv_mask": info.get("priv_mask"),
        "plm_fuse_wdata": info.get("plm_fuse_wdata"),
        "wdata_wr_secure": info.get("wdata_wr_secure"),
        "dis_program": info.get("dis_program"),
        "iff_window": None if iff is None else (iff & 0x1FF, (iff >> 16) & 0x1FF),
    }
    if any(v is not None for v in macro.values()):
        out["macro"] = macro
    return out


def analyze(dump, overrides=None, mig_keys=()):
    targets, warnings, violations = resolve_targets(dump, overrides, mig_keys)
    words = dump["option_words"]
    info = _info_words(dump)
    media = analyze_media(dump, targets, warnings, info)
    runtime = runtime_state(info)
    hard = hard_state(words, dump["sku"], violations)

    # A target can only be realised down to the defective floor: the mask is a
    # hard-row fact an FF record cannot overrule, so bits a target clears below it
    # stay dead and do not count as capacity. Flooring here (rather than trusting
    # the target) is what keeps `--set fbp_disable=0x0` from reporting 96 GiB on a
    # card whose silicon has 8 alive.
    fbp_floor = (hard["dead"]["mask"] or 0) if hard["read"] else 0
    fbp_cur = words.get("fbp_disable")
    fbp_tgt = targets.get("fbp_disable")
    usable_fbp = None
    fb_gib = None
    fb_now = None
    if fbp_cur is not None:
        fb_now = (TOTAL_FBP - popcount(fbp_cur & 0xfff)) * \
            BYTES_PER_FBP[dump["sku"]]
    if fbp_tgt is not None:
        usable_fbp = TOTAL_FBP - popcount((fbp_tgt | fbp_floor) & 0xfff)
        fb_gib = usable_fbp * BYTES_PER_FBP[dump["sku"]]
        if dump["sku"] == "8gb" and usable_fbp > 10:
            warnings.append(
                "%d FBP usable -> %d GiB; production A100 caps at 10 FBP/80 "
                "GiB, verify geometry encoding before trusting >80" %
                (usable_fbp, fb_gib))
        if fbp_floor & ~fbp_tgt:
            raw_fbp = TOTAL_FBP - popcount(fbp_tgt & 0xfff)
            warnings.append(
                "fbp_disable target 0x%x clears %d FBP that fbp_defective marks "
                "dead: the potential stays at the hard ceiling (%d GiB, %d FBP), "
                "not the %d GiB the raw target implies -- the shadow words will "
                "read the target, the memory will not be there"
                % (fbp_tgt, popcount(fbp_floor & ~fbp_tgt), fb_gib, usable_fbp,
                   raw_fbp * BYTES_PER_FBP[dump["sku"]]))

    # Same flooring on the compute side: a GPC or TPC the factory marked dead does
    # not come back because a disable bit was cleared.
    gpc_tgt = targets.get("gpc_disable", 0)
    gpc_def = words.get("gpc_defective", 0) or 0
    tpc_tgts = [targets.get("tpc_gpc%d_disable" % i, 0) for i in range(TOTAL_GPC)]
    tpc_defs = [words.get("tpc_gpc%d_defective" % i, 0) or 0
                for i in range(TOTAL_GPC)]
    sm_count = None
    per_gpc = []
    if "gpc_disable" in targets:
        sm_count = 0
        for i in range(TOTAL_GPC):
            if ((gpc_tgt | gpc_def) >> i) & 1:
                per_gpc.append(0)
                continue
            t = TPC_PER_GPC - popcount((tpc_tgts[i] | tpc_defs[i]) & 0xff)
            per_gpc.append(t * SM_PER_TPC)
            sm_count += t * SM_PER_TPC

    nvlink = targets.get("nvlink_disable")
    pcie_clear = all(targets.get(k) == 0 for k in (
        "disable_gen3_speed_bit1", "disable_gen3_speed_bit9",
        "pcie_magic_gen4_dis", "pcie_boot_gen23_dis", "pcie_boot_gen3_dis"))

    return {
        "sku": dump["sku"],
        "uuid": dump.get("uuid"),
        "fingerprint": dump.get("fingerprint"),
        "ff_rows_state": ffrows.coverage(dump)[0],
        "ff_rows_used": len(used_ff_rows(dump)),
        "ff_rows_free": len(free_ff_rows(dump)),
        "current": decode_current(replay_blocks(dump)),
        "words": words,
        "targets": targets,
        "usable_fbp": usable_fbp,
        "fb_gib": fb_gib,
        "fb_now_gib": fb_now,
        "sm_count": sm_count,
        "sm_per_gpc": per_gpc,
        "nvlink_ok": (nvlink == 0) if nvlink is not None else None,
        "pcie_gen4_ok": pcie_clear,
        "media": media,
        "runtime": runtime,
        "hard": hard,
        "warnings": warnings,
    }
