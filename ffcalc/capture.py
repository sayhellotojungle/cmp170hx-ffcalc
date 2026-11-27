#!/usr/bin/env python3
"""Capture option words (and optionally raw fuse rows) from a live CMP 170HX.

The word capture is strictly read-only: pure reads against BAR0 via
/sys/.../resource0. Address table is vendored at
ffcalc/data/ga100-fuses.json.

--read-ff-rows additionally captures all 512 physical rows through the
fuse macro's CMD_READ mechanism (ffcalc/ffmacro.py, procedure from
kate-land/ga100vfi). CMD_READ is a command WRITE (FUSEADDR + FUSECTRL),
so run it on an idle card; it is skipped on a bare capture. If you read
the rows with another setup instead, pass the file with --ffrows. Without
FF records in the dump, ffcalc refuses to emit or verify a burn plan (see
ffcalc mergeff).

Run on the card's host as root:
    python3 -m ffcalc.capture --bdf 0000:05:00.0 --sku 8gb \
        --read-ff-rows -o dump.json
"""
import argparse
import io
import json
import mmap
import operator
import os
import struct
import sys

from . import dump as snapshot
from . import ffmacro, ffrows
from .optionmap import A100_DEVID, CMP_DEVID

BAR0_LEN = 0x1000000

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MAP = os.path.join(_HERE, "data", "ga100-fuses.json")

# Same GA100 die and the same fuse map, before and after a --a100 conversion:
# burning the fused device id to 0x20b5 changes what the card calls itself,
# not what it is, so capture and burn have to keep finding it either way.
DEVICE_IDS = tuple("0x%04x" % v for v in (CMP_DEVID, A100_DEVID))


def open_bar0(bdf=None, writable=False):
    """Map a card's BAR0, read-only unless the caller really writes.

    `writable` is the difference between "look at the card" and "change the
    card": the word capture maps ACCESS_READ, so a stray pointer bug cannot
    reach a card that is serving traffic. Only the fuse-macro commands (the
    burn path, and CMD_READ, which writes FUSEADDR/FUSECTRL) ask for
    ACCESS_WRITE, and both want the card idle.

    This is also the access pattern the community unlocker uses against a live
    driver: the kernel refuses sysfs resource0 mmap/read for a bound device, so
    it has to be O_RDWR/O_SYNC with the driver attached.
    """
    base = "/sys/bus/pci/devices"
    cands = []
    for dev in sorted(os.listdir(base)):
        vp = os.path.join(base, dev, "device")
        try:
            with io.open(vp) as f:
                dev_id = f.read().strip()
        except IOError:
            continue
        if dev_id not in DEVICE_IDS:
            continue
        cands.append(dev)
    if bdf:
        want = bdf if len(bdf.split(":")) == 3 else "0000:" + bdf
        cands = [c for c in cands if c == want] or [want]
    if not cands:
        raise SystemExit("no device %s found" % " or ".join(DEVICE_IDS))
    path = os.path.join(base, cands[0], "resource0")
    fd = os.open(path, os.O_SYNC | (os.O_RDWR if writable else os.O_RDONLY))
    try:
        length = os.path.getsize(path) or BAR0_LEN
        mm = mmap.mmap(fd, length,
                       access=mmap.ACCESS_WRITE if writable else mmap.ACCESS_READ)
    finally:
        os.close(fd)
    return cands[0], mm


def _off(addr):
    """BAR0 offset as an int, from either address style we pass around.

    The JSON map stores hex strings ("0x0082036c"); ffmacro hands us plain
    ints (PMC_BOOT_0, FUSECTRL, ...). int(addr, 0) alone raises TypeError on
    an int under Python 3 ("int() can't convert non-string with explicit
    base"), which would kill every fuse macro command before the first read.
    """
    if isinstance(addr, str):
        return int(addr, 0)
    return operator.index(addr)


def rd(mm, addr):
    """One 32-bit BAR0 word.

    The bounds check is not decoration: reading past the mapping is what took a
    host down once (a register table entry beyond the window the probe had
    mapped). A clean error here, an OOPS in a kernel module.
    """
    off = _off(addr)
    if off < 0 or off + 4 > len(mm):
        raise ValueError("BAR0 offset 0x%x outside the mapping (0x%x bytes)"
                         % (off & 0xffffffff, len(mm)))
    mm.seek(off)
    return struct.unpack("<I", mm.read(4))[0]


def wr(mm, addr, value):
    off = _off(addr)
    if off < 0 or off + 4 > len(mm):
        raise ValueError("BAR0 offset 0x%x outside the mapping (0x%x bytes)"
                         % (off & 0xffffffff, len(mm)))
    mm.seek(off)
    mm.write(struct.pack("<I", value & 0xffffffff))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bdf")
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--sku", default="8gb", choices=("8gb", "10gb"))
    ap.add_argument("-o", "--out", default="dump.json")
    ap.add_argument("--ffrows", metavar="FILE",
                    help="FF rows file read out elsewhere; merged into the "
                         "dump (see also --read-ff-rows)")
    ap.add_argument("--read-ff-rows", action="store_true",
                    help="capture all 512 physical rows via fuse macro "
                         "CMD_READ (writes FUSEADDR/FUSECTRL; idle card "
                         "only)")
    args = ap.parse_args()

    try:
        with io.open(args.map, encoding="utf-8") as f:
            m = json.load(f)
    except ValueError as e:
        raise SystemExit("bad option map %s: %s" % (args.map, e))

    # Only the CMD_READ pass writes (FUSEADDR/FUSECTRL); the word capture maps
    # the BAR read-only so a bug in this process cannot reach a live card.
    bdf, mm = open_bar0(args.bdf, writable=bool(args.read_ff_rows))
    words = {}
    for key, loc in m["fields"].items():
        fused = rd(mm, loc["fused"]) if loc.get("fused") else None
        status = rd(mm, loc["status"]) if loc.get("status") else None
        cur = status if status is not None else fused
        words[key] = "0x%x" % cur
        if fused is not None and status is not None and fused != status:
            words[key + "__fused"] = "0x%x" % fused

    for key, spec in m["bit_fields"].items():
        w = rd(mm, spec["reg"])
        words[key] = "0x%x" % ((w >> spec["bit"]) & 1)

    for key, addr in m["defective"].items():
        words[key] = "0x%x" % rd(mm, addr)

    info = {}
    for name, addr in m["info"].items():
        info[name] = "0x%x" % rd(mm, addr)

    ff_rows = {}
    if args.ffrows:
        try:
            rows = ffrows.parse_file(args.ffrows)
        except ValueError as e:
            raise SystemExit("bad FF rows file %s: %s" % (args.ffrows, e))
        ff_rows.update((r, v) for r, v in rows.items())
    wrote_rows = bool(args.read_ff_rows)
    if args.read_ff_rows:
        try:
            ffmacro.capture_rows(lambda a: rd(mm, a),
                                 lambda a, v: wr(mm, a, v), rows=ff_rows)
        except ffmacro.FuseControllerError as e:
            print("WARN: CMD_READ pass aborted: %s" % e)
            print("WARN: keeping the %d row(s) read so far" % len(ff_rows))
    dump = {"sku": args.sku, "bdf": bdf, "ff_rows": ff_rows,
            "option_words": words, "info": info,
            "read_only": not wrote_rows}
    try:
        # One codec for every snapshot on disk: values checked, rows written
        # as fixed-width "0x%08x", file renamed into place only after it has
        # been read back -- a capture is the evidence a plan is built from,
        # it must not be half-written by an interrupted run.
        dump = snapshot.to_json_form(dump, args.out)
        snapshot.atomic_write_json(args.out, dump)
    except snapshot.BadSnapshot as e:
        raise SystemExit("cannot write %s: %s" % (args.out, e))
    state, missing = ffrows.coverage(dump)
    if state != "complete":
        print("note: FF rows %s (%d of %d unknown); ffcalc will not emit a "
              "burn plan until a full readout is merged (ffcalc mergeff)"
              % (state, len(missing), ffrows.N_FF_ROWS))
    print("wrote %s (%s)" % (args.out, bdf))


if __name__ == "__main__":
    sys.exit(main())
