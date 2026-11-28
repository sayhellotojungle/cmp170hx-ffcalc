import argparse
import os
import sys

from . import ffrows, hardrows, ingest, rir
from .analyze import SS_THROTTLED, analyze
from .dump import (BadSnapshot, atomic_write_json, load_dump, load_plan,
                   update_rows)
from .generate import plan
from .optionmap import (BY_KEY, DEVID_KEYS, FF_ROW_MAX, FF_ROW_MIN,
                        TOTAL_SYS_PIPE)
from .simulate import simulate


def _apply_ffrows_option(dump, path):
    """Merge an external FF rows file into a dump (shared by --ffrows)."""
    try:
        rows = ffrows.parse_file(path)
    except ValueError as e:
        raise SystemExit("bad FF rows file %s: %s" % (path, e))
    return ffrows.merge_into(dump, rows)


def _write_dump_ffrows(dump_path, rows):
    """Merge an externally-read FF rows file into a saved card JSON in place.

    Whatever the snapshot already holds is re-checked and re-normalised on
    the way, not carried over unexamined: the probe channel writes ints and
    the capture channel writes `"0x%08x"` strings (both are legal snapshots),
    while an external rows file usually covers only the FF region, so rows
    from outside it have to survive the round trip in whatever shape they
    were written in. Passing them through blind is what crashed mergeff on a
    capture snapshot (`"0x%08x" % "0x00000000"`, real card 2026-11-24); not
    checking them is what would write a row 999 or a 33-bit value back into
    the file and then crash `gen`, far from the cause. BadSnapshot comes out
    of here and the file is left untouched.

    Returns (state, missing, conflicts).
    """
    merged, conflicts = update_rows(dump_path, rows)
    return ffrows.coverage({"ff_rows": merged}) + (conflicts,)


def _fmt_conflicts(conflicts, limit=6):
    parts = ["%d 0x%08x->0x%08x" % (row, old, new)
             for row, old, new in conflicts[:limit]]
    if len(conflicts) > limit:
        parts.append("...(+%d)" % (len(conflicts) - limit))
    return ",".join(parts)


def _parse_sets(pairs):
    out = {}
    for p in pairs or ():
        if "=" not in p:
            raise SystemExit("--set expects key=hex, got %r" % p)
        k, v = p.split("=", 1)
        if k not in BY_KEY:
            raise SystemExit("unknown option key %r; known: %s"
                             % (k, ",".join(sorted(BY_KEY))))
        out[k] = int(v, 0)
    return out


def _print_analysis(rep):
    w = rep["words"]
    if rep.get("uuid"):
        print("Card UUID: %s   fingerprint: %s"
              % (rep["uuid"], rep.get("fingerprint")))
    if rep.get("ff_rows_state") == "complete":
        rows_desc = "%d used, %d free (rows %d-%d)" % (
            rep["ff_rows_used"], rep["ff_rows_free"], FF_ROW_MIN, FF_ROW_MAX)
    else:
        rows_desc = ("NOT READ (%s) -- no burn plan until FF rows are "
                     "captured and merged (ffcalc mergeff)"
                     % rep.get("ff_rows_state"))
    print("SKU: %s   FF rows: %s" % (rep["sku"], rows_desc))
    _print_hard(rep)
    if rep["usable_fbp"] is not None:
        print("\n[Framebuffer potential]")
        print("  fbp_disable  current=0x%x  target=0x%x"
              % (w.get("fbp_disable", 0), rep["targets"].get("fbp_disable", 0)))
        print("  usable FBP: %d / 12" % rep["usable_fbp"])
        print("  potential framebuffer: %d GiB" % rep["fb_gib"])
        hard = rep.get("hard") or {}
        if hard.get("read") and hard["ceiling_gib"] is not None:
            if rep["fb_gib"] == hard["ceiling_gib"]:
                print("  (= the hard ceiling: *_DEFECTIVE, not the target, is what "
                      "bounds this number)")
            else:
                print("  (hard ceiling %d GiB: this target leaves %d GiB that an FF "
                      "record could still take back)"
                      % (hard["ceiling_gib"], hard["ceiling_gib"] - rep["fb_gib"]))
    if rep["sm_count"] is not None:
        print("\n[SM potential]")
        print("  gpc_disable target=0x%x" % rep["targets"].get("gpc_disable", 0))
        print("  SM per GPC: %s" % ",".join(map(str, rep["sm_per_gpc"])))
        print("  potential SMs: %d" % rep["sm_count"])
    print("\n[Other unlocks]")
    nv = rep["nvlink_ok"]
    print("  NVLink: %s%s" % (
        "unlockable" if nv else "stays disabled",
        "  (needs U11 bridge mod + ROM check bypass)" if nv else ""))
    print("  PCIe Gen3/4: %s" % ("bits clearable" if rep["pcie_gen4_ok"]
                                 else "stays restricted"))
    _print_media(rep)
    _print_runtime(rep)
    _print_devid(rep)
    for msg in rep["warnings"]:
        print("  WARN: %s" % msg)


def _print_runtime(rep):
    """What this boot is running on, as opposed to what the fuses allow.

    The FF pool sets the resource set (SMs, memory, NVLink, link speed). Rate,
    ECC and geometry are runtime registers the driver-side unlocker rewrites
    every boot, so the two layers do not imply each other: our cards have
    OPT_ECC_EN fused to 0 while ECC_CTRL reports DRAM ECC enabled for this boot,
    and the speed selects read their factory setting while the blocks run
    unthrottled. Every word here is read-only, read off two unlocked cards with
    the driver attached; a snapshot taken before the readout map carried them
    says so instead of inventing a value.
    """
    rt = rep.get("runtime") or {}
    print("\n[Runtime state (BAR0, this boot)]")
    c = rt.get("compute")
    if c:
        if c["read"]:
            print("  compute: FEAT_READOUT_1=0x%08x -> %s"
                  % (c["readout"], "no throttle bit set"
                     if c["full_rate_now"] else "throttle bits set"))
        else:
            print("  compute: FEAT_READOUT_1 not read")
        if None not in (c["override"][0], c["override"][1]):
            print("    feature-override SS0/SS1 = 0x%08x / 0x%08x: volatile, "
                  "rewritten every boot by the driver-side unlocker"
                  % (c["override"][0], c["override"][1]))
        if c["fused_throttled"]:
            print("    speed selects reading 0x%x (%s) while the blocks above "
                  "are unthrottled: the FF pool does not carry these words, so "
                  "the effective rate comes from the feature-override island, "
                  "not from a burnt record"
                  % (SS_THROTTLED, ", ".join("%s=0x%x" % (k, v) for k, v in
                                             sorted(c["fused"].items())
                                             if k in c["fused_throttled"])))
    e = rt.get("ecc")
    if e:
        print("  ECC: fused OPT_ECC_EN=0x%08x (%s); DRAM ECC_CTRL=0x%08x "
              "(master=%s sideband=%s); SRAM overrides 0x%08x/0x%08x/0x%08x"
              % (e["fused"], "fused on" if e["fused"] else "fused off",
                 e["ctrl"], e["dram_master"], e["dram_sideband"],
                 e["sram"][0], e["sram"][1], e["sram"][2]))
        if not e["fused"] and (e["dram_sideband"] or e["dram_master"]):
            print("    on for this boot only: DRAM/SRAM ECC here is runtime "
                  "state, not a burnt column fuse")
    g = rt.get("geometry")
    if g:
        print("  geometry: FBPA_CFG1=0x%08x MMU_LMR=0x%08x RAMAMOUNT=0x%08x "
              "(volatile: the unlocker rewrites the framebuffer map each boot, "
              "an FLR puts the stock value back)"
              % (g["fbpa_cfg1"], g["mmu_lmr"], g["cstatus_ramamount"]))
    b = rt.get("board") or {}
    if b.get("gfw_boot") is not None or b.get("lane_disable") is not None:
        print("  board: OPT_PCIE_LANE_DISABLE=0x%x/_DEFECTIVE=0x%x %s; "
              "GFW_BOOT=0x%08x (%s)"
              % (b.get("lane_disable", 0), b.get("lane_defective", 0),
                 "-> nothing fused off: the width the card trains is the "
                 "slot's doing (check lspci LnkSta), not a fuse"
                 if not b.get("lane_disable") and not b.get("lane_defective")
                 else "-> lanes are limited in fuses/silicon",
                 b.get("gfw_boot", 0),
                 "POSTed clean" if b.get("gfw_clean") else "boot steps missing"))
    if b.get("hbm_mhz") is not None:
        print("  HBM PLL: coeff 0x%08x -> %d MHz (%s)"
              % (b["hbm_pll_coeff"], b["hbm_mhz"], b["hbm_state"]))
    m = rt.get("macro")
    if m:
        def word(v, fmt="0x%08x"):
            return "not read" if v is None else (fmt % v)
        print("  fuse macro: CRC=%s DSOV=%s DEBUGCTRL=%s (PLM shadow %s) "
              "DIS_PROGRAM=%s PRIV_MASK=%s FUSEWDATA_PLM=%s"
              % (word(m["crc"], "0x%02x"), word(m["dsov"], "%d"),
                 word(m["debugctrl"], "0x%x"), word(m["debugctrl_plm"], "0x%x"),
                 word(m["dis_program"], "%d"),
                 word(m["priv_mask"]), word(m["plm_fuse_wdata"])))
        if m.get("rwl"):
            print("    DEBUGCTRL bit1 (RWL) set: the read multiplexer points at "
                  "the RIR macro, so rows read in this state are the macro's 8 "
                  "rows, not the main array. Nothing here sets that bit (it is a "
                  "write), so this snapshot came from a tool that did")
        if m["iff_window"]:
            print("    IFF window rows %d-%d (from IFF_RECORD): the FF pool ends "
                  "at row %d" % (m["iff_window"][0], m["iff_window"][1],
                                 m["iff_window"][0] - 1))


def _print_hard(rep):
    """The hard-row side of the capacity question: what the silicon allows.

    Printed apart from `[Framebuffer potential]` on purpose: that section is
    about what a plan can do with the FF pool, this one is about the limit the FF
    pool cannot move. A card can show headroom in one and none in the other, and
    conflating them is how an unreachable number ends up in a burn order.
    """
    hard = rep.get("hard") or {}
    print("\n[Hard rows (what the silicon allows, not the FF pool)]")
    if not hard.get("read"):
        print("  fbp_defective not in this snapshot: the ceiling is unknown, "
              "not absent (re-capture: the readout map reads it at 0x8205cc)")
        return
    dead = hard["dead"]
    print("  dead FBP: 0x%x (%s) from fbp_defective at %s"
          % (dead["mask"],
             ",".join(str(i) for i in dead["list"]) or "none",
             hard["located"]))
    if dead.get("anomalous"):
        print("    ... of which 0x%x sits above the 12 partition bits: GA100 has "
              "12 FBPs, so that part of the word is not a partition. The ceiling "
              "here ignores it; re-check the readout before trusting the word"
              % dead["anomalous"])
    if dead["consistent"] is None:
        print("    fbpa/fbio/rop_l2 defective not folded in: the same dead set is "
              "marked in those three too, and this snapshot did not read them")
    elif dead["consistent"]:
        print("    fbpa/fbio/rop_l2 defective fold to the same set (the factory "
              "marks one judgement in four words): snapshot is self-consistent")
    else:
        print("    CROSS-CHECK FAILS: %s fold to a different FBP set than "
              "fbp_defective 0x%x. On this bench's three dies all four words mark "
              "one judgement (fbpa/fbio/rop_l2 use two bits per FBP), so a "
              "snapshot where they differ is either two cards mixed together or a "
              "register that is not what the table says: read capacity off "
              "fbp_defective alone and check the readout"
              % (", ".join("%s -> 0x%x" % (k, v) for k, v in dead["mismatch"]),
                 dead["mask"]))
    if hard["ceiling_gib"] is not None:
        print("  hard ceiling: %d GiB (%d FBP) -- a hard-row mask, so no FF "
              "record moves it" % (hard["ceiling_gib"], hard["ceiling_fbp"]))
    if hard["ff_headroom_gib"] is not None:
        print("  buyable with FF records: %d GiB%s"
              % (hard["ff_headroom_gib"],
                 "" if hard["ff_headroom_gib"] else
                 "  (nothing left to hand back: every partition this card still "
                 "switches off is one the factory marked dead, so a burn here "
                 "buys compute and capability, not memory)"))
    if hard["records"]:
        print("  the one lever that reaches a hard row is an RIR record: %d "
              "needed (%s), one per dead FBP"
              % (len(hard["records"]),
                 " ".join("0x%04x" % r["record"] for r in hard["records"])))
        print("    %s" % hard["rir"]["text"])


def _fmt_values(values, limit=4):
    """['a',0x840],... -> '0x840|0x840|0x2d', with a nod at which card is which."""
    seen = []
    for _name, value in values:
        if value not in seen:
            seen.append(value)
    text = "|".join("0x%x" % v for v in seen[:limit])
    if len(seen) > limit:
        text += "|..."
    if len(seen) == 1:
        text += " (all samples)"
    return text


def _print_defmap(dumps, results):
    """The solving report: the sample, the bit string, then one line a field."""
    print("sample: %d snapshot(s): %s"
          % (len(dumps), ", ".join(d.get("uuid") or d["source"]
                                   for d in dumps)))
    print("hard rows = factory rows 0-%d: %d logical words, each stored twice = "
          "%d bits; a field may straddle a word (the 24-bit ones only solve that "
          "way)" % (hardrows.FACTORY_ROW_MAX, hardrows.LOGICAL_WORDS,
                    hardrows.FLAT_BITS))
    same = None
    silent = []
    for d in dumps:
        name = d.get("uuid") or d["source"]
        if not any(0 <= row <= hardrows.FACTORY_ROW_MAX
                   for row in d["ff_rows"]):
            silent.append(name)
            print("  %-40s no factory rows: a words-only readout, so it carries "
                  "the option words and nothing to read them out of" % name)
            continue
        bad = hardrows.copy_mismatches(d["ff_rows"])
        print("  %-40s %d redundancy pair(s) disagree (rows %s)"
              % (name, len(bad), _fmt_rows(bad)))
        if same is None:
            same = set(bad)
        elif same != set(bad):
            same = None
    if silent:
        print("  %d of the above read no factory rows and take no part in the "
              "solve: %s" % (len(silent), ", ".join(silent)))
    if same:
        print("  the disagreeing rows are the same rows on every die: those words "
              "are simply not checked against their copy (CRC_STATUS is still "
              "0x55). No field solved below lands in one")
    print("")
    print("%-24s %3s %9s %-22s %s"
          % ("field", "w", "candidates", "shadow values", "reading"))
    off_table = 0
    ff_pool = 0
    for r in results:
        print("%-24s %3d %9d %-22s %s"
              % (r["key"], r["width"], len(r["candidates"]),
                 _fmt_values(r["values"]) if r["values"] else "not read",
                 hardrows.verdict(r)))
        if r["status"] == hardrows.STATUS_LOCATED and not r["in_hard_rows"]:
            share = sorted(set(key for _off, keys in (r.get("overlaps") or ())
                               for key in keys))
            print("%snot a hard-row field. One offset still fits because every "
                  "snapshot here reads the same value in this word as in %s; what "
                  "separates them is a --pin of a value one die had when the two "
                  "words differed"
                  % (" " * 24, ", ".join(share)
                     or "the word the table locates at that offset"))
            ff_pool += 1
        elif r["status"] == hardrows.STATUS_LOCATED and not r["reproduces"]:
            print("%s this run disagrees with the built-in table (%s): more "
                  "samples are needed to say which is right"
                  % (" " * 24, ", ".join(hardrows.coord(o, r["width"])
                                         for o in r["known"]) or "not listed"))
            off_table += 1
        if not r["in_hard_rows"]:
            continue                       # already explained above
        for off, shared in r.get("overlaps") or ():
            print("%s%s would share %s with %s: two option words do not read one "
                  "fuse bit, so this candidate is the least likely of the list"
                  % (" " * 24, hardrows.coord(off, r["width"]),
                     "a bit" if len(shared) == 1 else "bits",
                     ", ".join(shared)))
    if off_table:
        print("\n%d field(s) solved differently from the table above" % off_table)
    if ff_pool:
        print("%d field(s) above are FF-pool words: the only offset that fits them "
              "is one the table already assigns to a *_defective word, because on "
              "these dies the two words read the same value" % ff_pool)
    print("\nwhat this means: a *_defective word that solves to one position is a "
          "hard-row mask, and the capacity ceiling is read from it; a *_disable "
          "word with zero candidates is carried by the FF pool instead, which is "
          "why burning one leaves the defective masks untouched")
    return 0


def _fmt_rows(rows, limit=12):
    """A row list for printing, e.g. '18,48,50,...(+18)'."""
    if not rows:
        return "none"
    text = ",".join(str(r) for r in rows[:limit])
    return text + (",...(+%d)" % (len(rows) - limit) if len(rows) > limit else "")


def _print_rir(inv, dump, allow_partial=False):
    """Decode an RIR readout people brought us; no card access happens here."""
    print("RIR macro: %d rows x two 16-bit records = %d slots"
          % (rir.ROW_COUNT, rir.SLOT_COUNT))
    by_row = {}
    for s in inv["slots"]:
        by_row.setdefault(s["row"], []).append(s)
    for row in sorted(by_row):
        cells = "  ".join("slot%-3d 0x%04x %s%s"
                          % (s["slot"], s["value"], s["state"],
                             ("  force %d @ row%d bit%d"
                              % (s["record"]["value"], s["record"]["row"],
                                 s["record"]["bit"]))
                          if s["record"] else "")
                          for s in sorted(by_row[row], key=lambda s: s["half"]))
        print("  row %d  0x%08x  %s" % (row, inv["words"][row], cells))
    for row in inv["missing"]:
        print("  row %d  not read" % row)
    read = 2 * (rir.ROW_COUNT - len(inv["missing"]))
    print("free (blank) slots: %d%s; spent (no 0 cell left): %d; live records: %d"
          % (inv["free"],
             " of %d" % rir.SLOT_COUNT if inv["complete"] else
             " counted, but only %d of %d slots were read (%d rows missing)"
             % (read, rir.SLOT_COUNT, len(inv["missing"])),
             inv["spent"], len(inv["live"])))
    print("slot numbers assume the low half of a row is the even slot; the "
          "external dump calls the same live record slot 15, so the half order "
          "is not settled -- the raw words above are the evidence either way")
    if dump is not None:
        hard = analyze(dump)["hard"]
        if not hard["read"]:
            print("this snapshot has no fbp_defective: nothing to price")
        else:
            print("this card (%s): dead FBP %s, hard ceiling %s, and %d RIR "
                  "record(s) (%s) would be needed to lift it"
                  % (dump["sku"],
                     "0x%x" % hard["dead"]["mask"],
                     ("%d GiB" % hard["ceiling_gib"])
                     if hard["ceiling_gib"] is not None else "unknown",
                     len(hard["records"]),
                     " ".join("0x%04x" % r["record"]
                              for r in hard["records"])))
            print("  %s" % hard["rir"]["text"])
    if not inv["complete"] and not allow_partial:
        print("FAIL: rows %s are not in this readout, and an unread row is not a "
              "blank one: 'how many slots are free' has no answer. Pass "
              "--partial if a partial picture is what you want to look at"
              % _fmt_rows(inv["missing"]))
        return 2
    return 0


def _print_media(rep):
    """MIG and the media engines: what is live now, what this plan makes it,
    and what the readout can and cannot prove about it.

    Kept separate from the other unlocks because the evidence is different:
    these engines have no ``*_DEFECTIVE`` word, so ``STATUS_OPT_*`` (which
    hardware computes as OPT | DEFECTIVE) is what says whether a row spent on
    them unlocks real silicon, and without that readout the plan skips them.
    """
    media = rep.get("media") or {}
    mig, engines = media.get("mig", {}), media.get("engines", [])
    print("\n[MIG / media engines]")
    if mig.get("read") and mig.get("after") is not None:
        print("  MIG SYS_PIPE: %d/%d live now -> %d/%d after this plan "
              "(sys_pipe_defective=0x%02x; 1 pipe = MIG offers one "
              "non-partitionable 1g profile)"
              % (mig["now"], TOTAL_SYS_PIPE, mig["after"], TOTAL_SYS_PIPE,
                 mig["defective"]))
    else:
        print("  MIG SYS_PIPE: not read")
    for e in engines:
        if not e["read"]:
            print("  %-6s not read by this snapshot (the readout map grew): "
                  "cannot prove the units are defect-free -> no row planned"
                  % e["label"])
            continue
        verdict = ("no defects: STATUS == OPT_DISABLE" if not e["defective"]
                   else "DEFECTIVE 0x%x: those units no fuse can create"
                        % e["defective"])
        print("  %-6s %d/%d live now -> %s after this plan "
              "(OPT=0x%x STATUS=0x%x: %s)%s"
              % (e["label"], e["now"], e["total"],
                 ("%d/%d" % (e["after"], e["total"])) if e["planned"]
                 else "unchanged",
                 e["opt"], e["status"], verdict,
                 "" if e["planned"] else "  [not in this plan: --mig]"))
    if any(not e["planned"] for e in engines):
        print("  gates: NVDEC = chain1 blk8[15:11], NVJPG/OFA pair = blk9[2] + "
              "blk23[3] (which bit is which engine is not separated, so both "
              "go together). Row cost depends on whether this plan is already "
              "rewriting those blocks -- on a fresh card taking the default "
              "plan they ride along and only blk9 is new; on a card whose "
              "capability records are burnt already each of the three carries "
              "its own row. `gen --mig` prints the exact count for this card")
    disp = media.get("display") or {}
    if disp.get("read"):
        print("  chain1 bits 78..82 (DISPLAY/DISP_HEAD, STATUS=0x%x/0x%x): left "
              "fused on purpose, GA100 has no display outputs"
              % (disp["status"], disp["disp_head"]))
    sw = media.get("sw_override") or {}
    if sw.get("read"):
        # Scoped to the floorsweep route on purpose: what is inert here is the
        # CTRL_OPT_* path guarded by FUSE_EN_SW_OVERRIDE. The 0x8238xx feature
        # override island (compute rate, ECC, geometry) is a different
        # mechanism, is written by the host unlocker every boot, and is
        # emphatically not inert -- see the [Runtime state] section.
        print("  floorsweep override (CTRL_OPT_*): %s (FUSE_EN_SW_OVERRIDE=0x%x,"
              " DSOV/FUSE_DIS_SW_OVR 0x820084=0x%x) -> %s"
              % ("INERT" if sw["dead"] else "available", sw["en"], sw["dsov"],
                 "FF records are the only lever for these bits (the 0x8238xx "
                 "feature-override island is a separate mechanism and is live)"
                 if sw["dead"]
                 else "volatile CTRL_OPT_* writes may work, but they do not "
                      "survive a reset"))


def _print_devid(rep):
    """What the chip is fused as, and what a plan would make it claim.

    Read from the device-id fuse registers (`option_words`), not from the FF
    replay: an unburnt card keeps its id in the initial fuse array, where the
    FF snapshot cannot see it, so the FF-replayed value would read 0x0 and
    invite somebody to "fix" a number that was never wrong.
    """
    w, t = rep["words"], rep["targets"]
    label = dict((k, k.replace("pcie_", "")) for k in DEVID_KEYS)
    now = "  ".join("%s=%s" % (label[k], "0x%04x" % w[k]) for k in DEVID_KEYS
                    if w.get(k) is not None)
    tgt = "  ".join("%s=0x%04x" % (label[k], t[k]) for k in DEVID_KEYS
                    if k in t)
    print("\n[Fused device id]")
    print("  fused as: %s (this is what lspci reports)" % (now or "not read"))
    if "magic_d" in t:
        # --a100 (or --set) takes the capability word along with the id, and
        # that value is the one part of it nobody has read off a real A100.
        print("  capability word: MAGIC_BITS_D -> 0x%08x, EXPERIMENTAL "
              "community guess (--skip magic_d to leave it alone)"
              % t["magic_d"])
    if not tgt:
        print("  no id change planned: the card stays what it is fused as, "
              "which is all full capability + Gen4 need")
    else:
        print("  target: %s" % tgt)
        print("  WARN: the card only comes up under a vBIOS that carries that "
              "id, and a platform that stops POSTing leaves only CH341A "
              "offline recovery of the SPI rom")


def _dispatch(argv=None):
    ap = argparse.ArgumentParser(prog="ffcalc",
                                 description="GA100 CMP 170HX FF fuse tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("report", help="unlock potential of one card dump")
    p.add_argument("dump")
    p.add_argument("--set", action="append", metavar="KEY=HEX",
                   help="override a target option value")

    p = sub.add_parser(
        "defmap", help="re-derive, from one or more card snapshots, where the "
                       "*_DEFECTIVE masks sit inside the factory (hard) rows")
    p.add_argument("dumps", nargs="+", help="card snapshot json files")
    p.add_argument("--pin", action="append", metavar="[CARD:]FIELD=HEX",
                   help="assert that one die read FIELD=HEX in the hard rows at "
                        "some point in its life, typically the value a field had "
                        "before this repo burnt it (fbp_disable=0x852). Hard rows "
                        "are static, so a die that would have to read two values "
                        "at one offset leaves no offset standing: that is how a "
                        "*_disable field is shown NOT to live here. CARD is a "
                        "substring of the snapshot path or the UUID")
    p.add_argument("--keys", help="comma-separated fields to report, in this "
                                  "order (default: every *_defective the table "
                                  "covers). A *_disable field named here is "
                                  "reported too, which is how it is shown to have "
                                  "no home in the hard rows; any --pin names a "
                                  "field, it is added either way)")

    p = sub.add_parser(
        "rir", help="decode a captured RIR readout and count free slots "
                    "(pure arithmetic: this command never touches the card)")
    p.add_argument("readout", nargs="*", metavar="WORD",
                   help="the 8 RIR row words in order, e.g. 0xffffffff x7 "
                        "0xffff0923")
    p.add_argument("--file", help="or a file of 'row 0xword' lines, as external "
                                  "tools print them")
    p.add_argument("--partial", action="store_true",
                   help="show a readout that does not carry all 8 rows (the "
                        "free-slot count then says how many rows were read: an "
                        "unread row is not a blank row)")
    p.add_argument("--dump", help="also show what this card's ceiling would cost "
                                  "in RIR records")

    p = sub.add_parser("gen", help="generate FF burn records from a dump "
                       "(refuses if current FF records were not read)")
    p.add_argument("dump")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--set", action="append", metavar="KEY=HEX")
    p.add_argument("--only", help="comma-separated keys to consider")
    p.add_argument("--skip", help="comma-separated keys to ignore")
    p.add_argument("--ffrows", metavar="FILE",
                   help="FF rows file to merge before planning "
                        "(see ffcalc mergeff)")
    p.add_argument("--mig", action="store_true",
                   help="also burn the Chain-1 floorsweep bits that hold off "
                        "the media engines: nvdec_disable (chain1 blk8[15:11]) "
                        "and the NVJPG/OFA pair (blk9[2] + blk23[3]). Those "
                        "engines are physically on the die (5x NVDEC decode of "
                        "H264/HEVC/VP9/MPEG2/JPEG -- no AV1, GA100's NVDEC "
                        "predates the GA10x AV1 block; NVJPG0 JPEG decode; OFA0 "
                        "optical flow) and only these fuses hold them off, so "
                        "this is an irreversible unlock, not a settings change. "
                        "With sys_pipe_disable=0 (already in the default plan) "
                        "it also makes MIG usable beyond one 1g instance. Row "
                        "cost depends on this plan: burn it together with the "
                        "capability records and the bits ride in blocks that "
                        "are being rewritten anyway (1 row, blk9), while a card "
                        "whose unlock is burnt already pays one row per bit (3 "
                        "rows, measured) -- gen prints the number for this card. "
                        "Requires the OPT_*/STATUS_OPT_* readout (the map grew): "
                        "a snapshot taken before it cannot prove the units are "
                        "defect-free, and those fields are then skipped with a "
                        "warning instead of burned blind")
    p.add_argument("--a100", action="store_true",
                   help="HIGH RISK (community-reported, not reproduced on our "
                        "cards): rewrite the FUSED device id 0x20c2 -> 0x20b5 "
                        "(chain2 blk37/38) so an A100 80GB PCIe vBIOS boots, "
                        "and take the whole MAGIC_BITS_D capability word with "
                        "it (--skip magic_d for the conversion report's "
                        "minimum). You then have to flash that vBIOS "
                        "yourself, and a platform that fails to POST leaves "
                        "only CH341A recovery. Not needed for full capability "
                        "+ Gen4: the default plan covers that")
    p.add_argument("--allow-hard-floor", action="store_true",
                   help="with --set: let a target clear a bit that *_DEFECTIVE "
                        "sets. Default is to refuse, because that state is not "
                        "an ambitious plan but a different failure: the shadow "
                        "registers follow OPT_DISABLE, so the readout agrees "
                        "with the plan while the block stays dead -- on this "
                        "bench's 8 GB card (fbp_disable == fbp_defective == "
                        "0x2d) the un-disable left GSP-RM stuck at "
                        "PTOP_GFW_BOOT=0x1. *_DEFECTIVE is a hard-row mask: no "
                        "FF record can overrule it, and an 8 GB SKU card reads "
                        "0 of 16 free RIR slots, the only lever that could")

    p = sub.add_parser("mergeff", help="merge an externally-read FF rows "
                       "dump (e.g. fuse macro CMD_READ) into a card dump")
    p.add_argument("dump", help="card dump json to update in place")
    p.add_argument("rows", help="FF rows file: one '<row> <value>' per line")

    p = sub.add_parser("sim", help="replay dump + burn plan and verify")
    p.add_argument("dump")
    p.add_argument("burn")
    p.add_argument("--ffrows", metavar="FILE",
                   help="FF rows file to merge before replaying")

    p = sub.add_parser("scan", help="analyze every card in a collected dir; "
                       "print table, save UUID-keyed dumps and burn plans")
    p.add_argument("--dir", required=True,
                   help="collected dir (bar0map.tsv + probe_*.txt)")
    p.add_argument("--outdir", default="cards")
    p.add_argument("--plans", default="plans")
    p.add_argument("--sku", default="8gb", choices=("8gb", "10gb"))
    p.add_argument("--no-gen", action="store_true")
    p.add_argument("--skip", help="comma-separated keys to ignore in plans")
    p.add_argument("--mig", action="store_true",
                   help="plans also clear the media-engine floorsweep bits "
                        "(5x NVDEC / NVJPG0 / OFA0, and multi-instance MIG "
                        "beyond one 1g slice). Irreversible. Cost is 1 extra FF "
                        "row when these bits ride the same round as the "
                        "capability records, 3 rows on a card already unlocked "
                        "(both measured); `gen --mig` prints the real number "
                        "and what is and is not proven")
    p.add_argument("--a100", action="store_true",
                   help="HIGH RISK: plans rewrite the fused device id to "
                        "0x20b5 and the whole MAGIC_BITS_D word, which "
                        "commits you to an A100 vBIOS flash (CH341A is the "
                        "only way back). Default plans need neither")

    p = sub.add_parser("verify", help="check that a burn plan belongs to "
                       "the card currently in the machine")
    p.add_argument("burn")
    p.add_argument("--dir", help="fresh collected dir")
    p.add_argument("--dump", help="fresh single-card dump json")
    p.add_argument("--ffrows", metavar="FILE",
                   help="FF rows file to merge into the fresh read before "
                        "replaying the plan")

    args = ap.parse_args(argv)

    if args.cmd == "report":
        dump = load_dump(args.dump)
        _print_analysis(analyze(dump, _parse_sets(args.set)))
        return 0

    if args.cmd == "defmap":
        dumps = [load_dump(p) for p in args.dumps]
        try:
            pins = [hardrows.Pin.parse(p) for p in args.pin or ()]
            results = hardrows.solve(dumps,
                                     keys=args.keys.split(",") if args.keys
                                     else None, pins=pins)
        except ValueError as e:
            print("FAIL: %s" % e)
            return 2
        return _print_defmap(dumps, results)

    if args.cmd == "rir":
        text = " ".join(args.readout)
        if args.file:
            try:
                with open(args.file) as f:
                    text += "\n" + f.read()
            except IOError as e:
                print("FAIL: cannot read %s (%s)" % (args.file, e))
                return 2
        try:
            words = rir.parse_words(text)
        except (rir.BadRirRecord, ValueError) as e:
            print("FAIL: %s" % e)
            print("want the 8 RIR row words, e.g. `ffcalc rir %s` or --file"
                  % " ".join("0x%08x" % w for w in rir.MEASURED_8GB))
            return 2
        return _print_rir(rir.inventory(words),
                          load_dump(args.dump) if args.dump else None,
                          allow_partial=args.partial)

    if args.cmd == "mergeff":
        try:
            rows = ffrows.parse_file(args.rows)
        except ValueError as e:
            print("FAIL: bad FF rows file %s: %s" % (args.rows, e))
            return 2
        state, missing, conflicts = _write_dump_ffrows(args.dump, rows)
        print("merged %d FF row(s) into %s" % (len(rows), args.dump))
        if conflicts:
            print("note: %d row(s) already in the snapshot held a different "
                  "value and were overwritten by this readout: %s"
                  % (len(conflicts), _fmt_conflicts(conflicts)))
            print("note: unless this card was burnt since the snapshot, "
                  "check this rows file really came from this card")
        if state == "complete":
            print("FF rows %d-%d fully covered; burn plan available: "
                  "ffcalc gen %s -o plans/<uuid>.burn.json"
                  % (FF_ROW_MIN, FF_ROW_MAX, args.dump))
            return 0
        print("STILL %s: %d of %d FF rows unknown (rows %d-%d; e.g. %s)"
              % (state, len(missing), ffrows.N_FF_ROWS, FF_ROW_MIN,
                 FF_ROW_MAX, ffrows.fmt_rows(missing)))
        print("unburnt rows must be listed as 0; how to fix: %s"
              % ffrows.GUIDANCE)
        return 1

    if args.cmd == "gen":
        dump = load_dump(args.dump)
        if args.ffrows:
            _apply_ffrows_option(dump, args.ffrows)
        only = args.only.split(",") if args.only else None
        skip = args.skip.split(",") if args.skip else []
        try:
            obj = plan(dump, only=only, skip=skip,
                       overrides=_parse_sets(args.set), a100=args.a100,
                       mig=args.mig,
                       allow_hard_floor=args.allow_hard_floor)
        except ffrows.FFRowsNotRead as e:
            print("REFUSED: %s" % e.detail)
            print("how to fix: %s" % ffrows.GUIDANCE)
            print("no burn plan was written to %s" % args.out)
            return 2
        except hardrows.BeyondHardFloor as e:
            print("REFUSED: %s" % e)
            print("no burn plan was written to %s" % args.out)
            return 2
        # Atomic: a half-written plan is worse than no plan, because whoever
        # reads that filename next takes it for a burn order.
        atomic_write_json(args.out, obj)
        print("card: %s  fingerprint: %s"
              % (dump.get("uuid") or "none (no nvidia-smi data)",
                 dump.get("fingerprint")))
        print("planned %d FF record(s):" % len(obj["burn"]))
        for rec in obj["burn"]:
            print("  row %-4d rec %s  chain%d blk%-3d %s -> %s  [%s]"
                  % (rec["row"], rec["record"], rec["chain"], rec["block"],
                     rec["old"], rec["new"], ",".join(rec["fields"])))
        a = obj["analysis"]
        print("potential: %s GiB FB, %s SMs" % (a["fb_gib"], a["sm_count"]))
        hard = a.get("hard") or {}
        if hard.get("read") and hard["ceiling_gib"] is not None:
            print("hard ceiling: %d GiB (%d dead FBP at %s); FF headroom on this "
                  "card: %s GiB"
                  % (hard["ceiling_gib"], len(hard["dead"]["list"]),
                     hard["located"], hard["ff_headroom_gib"]))
        _print_media({"media": a["media"]})   # same shape as `report`
        for msg in obj["warnings"]:
            print("WARN: %s" % msg)
        return 0

    if args.cmd == "sim":
        dump = load_dump(args.dump)
        if args.ffrows:
            _apply_ffrows_option(dump, args.ffrows)
        burn_obj = load_plan(args.burn)
        res = simulate(dump, burn_obj)
        print("%-24s %-9s %-8s %-10s %-10s" %
              ("key", "chain/bit", "before", "after", "status"))
        for row in res["table"]:
            st = "CHANGED" if row["changed"] else "same"
            print("%-24s c%d b%-6s %-10s %-10s %s"
                  % (row["key"], row["chain"], row["bit"],
                     row["before"], row["after"], st))
        for msg in res["warnings"]:
            print("WARN: %s" % msg)
        if res["pending"]:
            print("PENDING (targeted but not in this burn set): %s"
                  % ",".join(res["pending"]))
        for msg in res["errors"]:
            print("FAIL: %s" % msg)
        if res["errors"]:
            print("SIMULATION FAILED (%d issue(s))" % len(res["errors"]))
            return 1
        print("SIMULATION OK")
        return 0

    if args.cmd == "scan":
        m = ingest.load_map()
        rows = []
        no_ffrows = []
        for dump in ingest.read_collected(args.dir, m, sku=args.sku):
            saved = os.path.join(args.outdir,
                                 ingest.card_id(dump) + ".json")
            if not dump["ff_rows"] and os.path.exists(saved):
                try:
                    prev = load_dump(saved)
                except ValueError:
                    prev = None
                if prev and prev.get("ff_rows"):
                    dump["ff_rows"] = prev["ff_rows"]
            rep = analyze(dump)
            dpath = ingest.save_dump(args.outdir, dump)
            pinfo = "-" if args.no_gen else "SKIPPED (FF rows not read)"
            pwarns = []
            if not args.no_gen:
                try:
                    obj = plan(dump, a100=args.a100, mig=args.mig,
                               skip=args.skip.split(",") if args.skip else [])
                except ffrows.FFRowsNotRead:
                    no_ffrows.append(dpath)
                else:
                    if not os.path.isdir(args.plans):
                        os.makedirs(args.plans)
                    ppath = os.path.join(args.plans,
                                         ingest.card_id(dump) + ".burn.json")
                    atomic_write_json(ppath, obj)
                    pinfo = "%s (%d records)" % (ppath, len(obj["burn"]))
                    pwarns = obj["warnings"]
            rows.append((dump, rep, pinfo, pwarns))
        if not rows:
            print("no cards found under %s/bar0map.tsv" % args.dir)
            return 1
        print("%-38s  %-12s  %-13s  %4s  %-9s  plan"
              % ("card UUID", "BDF", "FB now->max", "SM", "FP"))
        for dump, rep, pinfo, _ in rows:
            fb = ("%s->%s GiB" % (rep["fb_now_gib"], rep["fb_gib"])
                  if rep["fb_now_gib"] is not None else "?")
            print("%-38s  %-12s  %-13s  %4s  %-9s  %s"
                  % (dump.get("uuid") or "GPU-" + dump["fingerprint"],
                     dump.get("bdf", "?"), fb,
                     rep["sm_count"] if rep["sm_count"] is not None else "?",
                     dump["fingerprint"], pinfo))
        for dump, rep, _, pwarns in rows:
            # The plan repeats the analysis warnings and adds its own (what a
            # fused-device-id row really costs), so print the plan's set once.
            for wmsg in (pwarns or rep["warnings"]):
                print("WARN %s: %s" % (dump.get("uuid") or
                                       dump.get("fingerprint"), wmsg))
        if no_ffrows:
            print("\nNO BURN PLAN for %d card(s): their current FF records "
                  "were never read (read-only probe cannot see FF rows; "
                  "burning blind could reuse occupied rows)."
                  % len(no_ffrows))
            for dpath in no_ffrows:
                print("  %s: ffcalc mergeff %s <ffrows.txt>" % (dpath, dpath))
            print("how to fix: %s" % ffrows.GUIDANCE)
            return 1
        return 0

    if args.cmd == "verify":
        burn_obj = load_plan(args.burn)
        puuid, pfp = burn_obj.get("uuid"), burn_obj.get("fingerprint")
        if not puuid and not pfp:
            print("FAIL: plan has no uuid/fingerprint; re-generate it")
            return 1
        if args.dir:
            dumps = list(ingest.read_collected(args.dir))
        elif args.dump:
            dumps = [load_dump(args.dump)]
        else:
            print("FAIL: verify needs --dir <collected> or --dump <json>")
            return 2
        if args.ffrows:
            for d in dumps:
                _apply_ffrows_option(d, args.ffrows)
        match = None
        for d in dumps:
            if (puuid and d.get("uuid") == puuid) or \
                    (pfp and d.get("fingerprint") == pfp):
                match = d
                break
        if match is None:
            print("FAIL: plan for %s (%s) not present among %d card(s)"
                  % (puuid or "?", pfp or "?", len(dumps)))
            for d in dumps:
                print("  present: %s fp=%s"
                      % (d.get("uuid") or "?", d.get("fingerprint")))
            return 1
        if pfp and match.get("fingerprint") != pfp:
            print("FAIL: UUID matches but fingerprint differs "
                  "(%s vs %s): defective words changed?"
                  % (pfp, match.get("fingerprint")))
            return 1
        print("OK: plan %s belongs to card %s (bdf %s, fingerprint %s)"
              % (args.burn, match.get("uuid") or "GPU-" + pfp,
                 match.get("bdf", "?"), pfp))
        res = simulate(match, burn_obj)
        for msg in res["errors"]:
            print("FAIL: %s" % msg)
        return 1 if res["errors"] else 0

    return 2


def main(argv=None):
    """Run one command.

    A snapshot that cannot be trusted prints ``FAIL: <what and where>`` and
    exits 2 instead of raising: these files are read by hand, edited by hand
    and are the only thing a burn plan is derived from, so the answer is
    either "believed" or "refused out loud", never a Python traceback.
    """
    try:
        return _dispatch(argv)
    except BadSnapshot as e:
        print("FAIL: %s" % e)
        return 2
    except hardrows.BeyondHardFloor as e:
        # A plan asked for a state the FF pool cannot deliver (`scan` reaches it
        # too, since it plans every card it collects): say what and stop, rather
        # than let a caller read a potential number off a traceback.
        print("REFUSED: %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
