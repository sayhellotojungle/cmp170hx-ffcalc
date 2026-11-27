#!/usr/bin/env python3
"""Execute a verified ffcalc burn plan on a live CMP 170HX.

    python3 -m ffcalc.burntool status --bdf 0000:05:00.0
    python3 -m ffcalc.burntool burn  plans/<uuid>.burn.json \
        --dump cards/<uuid>.json --bdf 0000:05:00.0 [--rows 315] [--yes]

burn refuses to touch the card unless:
  1. simulate() on the plan against the card dump is error-free (row
     occupancy, old values, re-encode, FF range all check out);
  2. the fuse controller sits in the write window (idle, FUSEDEBUG=0,
     CRC=0x55, EFP=1, DSOV=0) -- checked again after CMD_SENSE;
  3. every target row still reads blank (0x00000000) at burn time -- or
     already holds exactly its planned record (interrupted run: that row is
     skipped, re-running the same plan is safe).
Before the first write the plan's own warnings are echoed, and the YES prompt
comes after them and after the offline checks: a plan carrying the fused
device id rewrite (`ffcalc gen --a100`) says it here, at the moment of no
return -- the card will come up only under an A100 vBIOS, and a platform that
stops POSTing leaves only CH341A recovery of the SPI rom.
Each record is programmed one at a time and read back; a poisoned
controller (FUSECTRL=0xBADFxxxx) aborts the whole run and you must
cold-boot before any further access. See docs/BURNING.md.
"""
import argparse
import functools
import sys

from . import ffmacro
from .capture import open_bar0, rd as _rd, wr as _wr
from .dump import BadSnapshot, load_dump, load_plan
from .optionmap import DEVID_KEYS
from .simulate import simulate


def _load_pair(plan_path, dump_path, log):
    """(plan, dump) or None after a REFUSED line.

    Both files are load-bearing: the snapshot is what every row is re-read
    against before an irreversible burn, so an unreadable or truncated one
    means "do not touch the card" -- said out loud, not as a Python
    traceback in front of somebody holding a live card. BadPlan is a
    BadSnapshot, so one handler covers both files.
    """
    try:
        plan = load_plan(plan_path)
        dump = load_dump(dump_path)
    except BadSnapshot as e:
        log("REFUSED: %s" % e)
        return None
    return plan, dump


def cmd_status(rd, log=print):
    st = ffmacro.status(rd)
    for name in sorted(st):
        log("%-16s 0x%08x" % (name, st[name]))
    if ffmacro.is_poisoned(st["FUSECTRL"]):
        log("=> POISONED: stop reads, cold-boot the card")
        return 3
    log("=> write window: %s" % ("OPEN" if ffmacro.in_write_window(st)
                                 else "closed"))
    return 0


def _identity_rows(plan):
    """Fields of this plan that rewrite the fused id, plus the new id."""
    keys = {key for rec in plan["burn"] for key in rec.get("fields") or ()}
    ids = [key for key in DEVID_KEYS if key in keys]
    if not ids:
        return None
    return ids, (plan.get("targets") or {}).get(ids[0])


def cmd_burn(rd, wr, plan_path, dump_path, rows=None, log=print,
             sleep=None, clock=None, force=False, confirm=None):
    loaded = _load_pair(plan_path, dump_path, log)
    if loaded is None:
        return 1
    plan, dump = loaded
    # The plan is the only place the cost of each record was written down, so
    # read it back out here: whoever holds a live card and a `YES` prompt is
    # exactly where "this row also changes what the card is" belongs.
    for msg in plan.get("warnings") or ():
        log("plan warning: %s" % msg)
    ident = _identity_rows(plan)
    if ident:
        ids, target = ident
        log("NOTICE: %s rewrite the fused device id to %s. From the next cold "
            "boot the chip itself is that id, so the card comes up only under "
            "a vBIOS that carries it -- flash the A100 80GB PCIe rom (after "
            "backing this card's own rom up), and keep a CH341A + SOP8 clip "
            "within reach: if the platform stops POSTing there is no in-band "
            "way back. Full capability + Gen4 needed neither these rows nor "
            "that flash." % ("/".join(ids), target))
    res = simulate(dump, plan)
    for w in res["warnings"]:
        log("warn: %s" % w)
    if res["errors"]:
        for e in res["errors"]:
            log("REFUSED: %s" % e)
        return 1
    if confirm is not None and not confirm():
        log("aborted")
        return 2
    wanted = None
    if rows:
        wanted = {int(r, 0) for r in rows.split(",")}
        unknown = wanted - {rec["row"] for rec in plan["burn"]}
        if unknown:
            log("REFUSED: rows %s not in plan" % sorted(unknown))
            return 1
    kw = {}
    if sleep is not None:
        kw["sleep"] = sleep
    if clock is not None:
        kw["clock"] = clock

    st = ffmacro.status(rd)
    if ffmacro.is_poisoned(st["FUSECTRL"]):
        log("REFUSED: controller poisoned (FUSECTRL=0x%08x); cold-boot first"
            % st["FUSECTRL"])
        return 3
    if not force and not ffmacro.in_write_window(st):
        log("REFUSED: not in write window: %s"
            % (ffmacro.armed_problems(rd, wr) or "idle/CRC/EFP/DSOV check"))
        return 1

    log("CMD_SENSE ...")
    ffmacro.sense(rd, wr, **kw)
    log("CMD_SENSE ok (DSOV=0, inside window)")
    already = set()
    if not force:
        missing = {rec["row"] for rec in plan["burn"]} - \
                  {r for r in dump["ff_rows"]}
        if missing:
            log("REFUSED: dump does not cover rows %s" % sorted(missing))
            return 1
        for rec in plan["burn"]:
            live = ffmacro.read_row(rd, wr, rec["row"], **kw)
            snap = dump["ff_rows"].get(rec["row"])
            if live == snap:
                continue
            if live == int(rec["record"], 0):
                log("row %d already holds this record; will skip "
                    "(interrupted run? resuming is safe)" % rec["row"])
                already.add(rec["row"])
                continue
            log("REFUSED: row %d changed since snapshot "
                "(snapshot 0x%08x, live 0x%08x) -- regenerate the plan"
                % (rec["row"], snap, live))
            return 1
        log("snapshot matches card (%d rows re-read, %d already burned)"
            % (len(plan["burn"]), len(already)))

    try:
        log("arm (SBFE shadow + WDATA_WR_SECURE) ...")
        ffmacro.arm(rd, wr)
        done = 0
        for rec in plan["burn"]:
            if wanted is not None and rec["row"] not in wanted:
                continue
            if rec["row"] in already:
                done += 1
                log("row %3d: already burned, skipped (%s)"
                    % (rec["row"], ",".join(rec.get("fields", []))))
                continue
            record = int(rec["record"], 0)
            got = ffmacro.program_row(rd, wr, rec["row"], record, **kw)
            done += 1
            log("row %3d: burned 0x%08x ok (%s)"
                % (rec["row"], got, ",".join(rec.get("fields", []))))
    except ffmacro.FuseControllerError as exc:
        log("ABORT: %s" % exc)
        log("treat the card as suspect: stop all BAR0 reads, cold-boot, "
            "then re-check with `status` (docs/BURNING.md)")
        return 3
    st = ffmacro.status(rd)
    log("post: FUSECTRL=0x%08x FUSEDEBUG=0x%x CRC=0x%x DSOV=%d"
        % (st["FUSECTRL"], st["FUSEDEBUG"], st["CRC_STATUS"],
           st["DSOV_STATUS"]))
    log("done: %d/%d records. Cold boot now, then re-run ffscan.sh: "
        "fingerprint must be unchanged, plan must shrink"
        % (done, len(plan["burn"])))
    return 0


def cmd_swiff(rd, wr, plan_path, dump_path=None, rows=None, log=print,
              sleep=None, clock=None, confirm=None):
    """Rehearse a plan without destroying anything: apply its records as IFF
    fuses for this boot (see ffmacro.swiff_apply).

    Same records and the same window requirement as `burn`, but nothing is
    programmed: a cold boot puts the card back exactly as it was, which is the
    point of the rehearsal -- a mistaken plan costs a power cycle instead of a
    row. The IFF window holds one row per record (488-507 on a factory card,
    read from IFF_RECORD), so a plan longer than that cannot be rehearsed whole.

    Whether the card then *behaves* as if the records were burnt is what this
    command exists to find out: it has not been measured on the cards here, so
    the reading is the experiment -- run it, check deviceQuery / memory size /
    link width, then cold boot to undo.
    """
    try:
        plan = load_plan(plan_path)
    except BadSnapshot as exc:
        log("REFUSED: plan unreadable: %s" % exc)
        return 1
    recs = list(plan.get("burn") or ())
    if rows:
        wanted = {int(r, 0) for r in rows.split(",")}
        recs = [r for r in recs if r["row"] in wanted]
    if not recs:
        log("REFUSED: plan has no records to apply")
        return 1
    window_rows = None
    if dump_path:
        try:
            window_rows = load_dump(dump_path)["ff_rows"]
        except BadSnapshot as exc:
            log("REFUSED: dump unreadable: %s" % exc)
            return 1
    else:
        log("note: no --dump, so IFF window occupancy is not checked")
    if confirm is not None and not confirm():
        log("aborted")
        return 2
    kw = {}
    if sleep is not None:
        kw["sleep"] = sleep
    if clock is not None:
        kw["clock"] = clock
    try:
        applied = ffmacro.swiff_apply(
            rd, wr, [int(r["record"], 0) for r in recs],
            rows=window_rows, **kw)
    except ffmacro.FuseControllerError as exc:
        log("ABORT: %s" % exc)
        return 3
    for row, record in applied:
        log("iff row %3d <= 0x%08x (volatile)" % (row, record))
    log("done: %d record(s) live for this boot only. Check the card now "
        "(deviceQuery / memory size / link width); a cold boot removes all of "
        "it -- nothing here was burnt" % len(applied))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ffcalc.burntool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("status")
    sp.add_argument("--bdf")
    bp = sub.add_parser("burn")
    bp.add_argument("plan")
    bp.add_argument("--dump", required=True)
    bp.add_argument("--bdf")
    bp.add_argument("--rows", help="subset, e.g. '315' for a first test row")
    bp.add_argument("--force", action="store_true",
                    help="skip live-snapshot re-match (NOT recommended)")
    bp.add_argument("--yes", action="store_true")
    sw = sub.add_parser("swiff", help="apply a plan as volatile IFF fuses")
    sw.add_argument("plan")
    sw.add_argument("--dump", help="card snapshot, to skip occupied window rows")
    sw.add_argument("--bdf")
    sw.add_argument("--rows", help="subset of the plan's rows")
    sw.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)

    # `status` only ever reads, so it maps the BAR read-only: the burn and the
    # SW-IFF verbs are the only paths that ask the kernel for a writable
    # mapping, which is what makes "these two verbs can change the card" true
    # at the OS level and not only in the code.
    bdf, mm = open_bar0(args.bdf, writable=args.cmd in ("burn", "swiff"))
    r = functools.partial(_rd, mm)
    w = functools.partial(_wr, mm)
    print("card at %s" % bdf)
    if args.cmd == "status":
        return cmd_status(r)

    verb = "burn" if args.cmd == "burn" else "apply (IFF, volatile)"

    def confirm():
        ans = input("%s %s onto %s? type YES to continue: "
                    % (verb, args.plan, bdf))
        return ans.strip() == "YES"

    if args.cmd == "swiff":
        return cmd_swiff(r, w, args.plan, dump_path=args.dump, rows=args.rows,
                         confirm=None if args.yes else confirm)
    return cmd_burn(r, w, args.plan, args.dump, rows=args.rows,
                    force=args.force, confirm=None if args.yes else confirm)


if __name__ == "__main__":
    sys.exit(main())
