"""The hard rows: where ``*_DEFECTIVE`` actually lives, and what that caps.

GA100's floorsweep data sits in two physically separate places with completely
different writability:

==========================  ==========================  ======================
what                        where                       how to change it
==========================  ==========================  ======================
``*_DISABLE`` option words  FF pool, rows 222-487       append a record
``*_DEFECTIVE`` masks       main array, rows 0-221      RIR record only
==========================  ==========================  ======================

The hard rows are not merely read-only, they are **outside the FF address
space**: an FF record names ``(chain, block)``, and no ``(chain, block)`` covers
a hard row, so there is no block number still to be found -- the records cannot
address these bits at all. That is the physical reason a burn that rewrites
``fbp_disable`` leaves ``fbp_defective`` exactly where it was, and why the only
thing that can change how a hard-row bit reads is an RIR record
(`ffcalc/rir.py`), which substitutes the read instead of touching the cell.

The consequence for capacity, and the one that cost somebody a dead card:

    usable FBP = 12 - popcount(target ``*_DISABLE`` | ``*_DEFECTIVE``)

The ceiling is set by the defective mask, *not* by what ``disable`` currently
says. Clearing the disable bit of a dead FBP does make the shadow registers
agree (``STATUS_OPT_FBP`` follows ``OPT_DISABLE``; defective does not force it
back), and the card still does not come up: on this bench's 8 GB card, whose
``fbp_disable`` equals its ``fbp_defective`` at 0x2d, the un-disable was tried
and GSP-RM died in the booter's instruction fetch, ``PTOP_GFW_BOOT`` at 0x1 and
``gsp_riscv_status`` at 0. "The shadow words read what I asked for" is therefore
not a success criterion for anything on the memory side, and `gen` refuses a
target that clears a defective bit (``--allow-hard-floor`` for the experiment).

How the coordinates below were obtained, and how to re-derive them here
(``python -m ffcalc defmap cards/*.json``): factory rows store every logical
word twice (row 2k and 2k+1), so rows 0-221 read as 111 logical words, one
3552-bit string. A candidate is an offset at which every snapshot in the sample
reads exactly the value the card's own shadow register reports, and a field must
be allowed to straddle a word boundary or none of the 24-bit fields solves. The
negative half of the argument matters as much as the positive: a field this repo
has burnt (so its shadow value is no longer the factory one) must *not* read its
current value anywhere in the hard rows, and that is what tells the two address
spaces apart. ``defmap --pin`` feeds such a value in as a constraint.
"""
from dataclasses import dataclass

from .optionmap import BY_KEY, FF_ROW_MIN, FIELDS

# Factory OTP: every row below the FF pool, and the redundancy pairs that hold
# each logical word twice.
FACTORY_ROW_MAX = FF_ROW_MIN - 1               # 221
LOGICAL_WORDS = (FACTORY_ROW_MAX + 1) // 2     # 111
FLAT_BITS = LOGICAL_WORDS * 32                 # 3552

# Rows whose two copies do NOT hold the same word, measured on three different
# dies -- all three: the same row numbers, every time. "Every factory row is
# stored twice" is true except here, and CRC_STATUS reads 0x55 on all three, so
# these pairs are simply not checked against each other (per-card-unique data or
# separate semantics; not proven which). None of the fields solved below lands
# in one, so the solving is unaffected -- but a solver that assumed perfect
# pairing would read these rows as corruption, and the factory RIR record on this
# bench's 8 GB card patches row18:bit8, one of exactly these rows.
COPY_MISMATCH_ROWS = (18, 48, 50, 52, 54, 56, 58, 60, 62, 64, 66, 68, 70, 72,
                      74, 76, 78, 80, 84, 86, 94)

# ``fbpa``/``fbio``/``rop_l2``/``fbp`` mark the same dead FBP set in four
# independent shadow words; the 24-bit ones use two bits per FBP (FBP i is bits
# 2i and 2i+1). Checking the three against ``fbp_defective`` costs nothing and
# catches a snapshot that mixed two cards.
FOLD_TWO_PER_FBP = ("fbpa_defective", "fbio_defective", "rop_l2_defective")
TOTAL_FBP = 12

# ``fbp_defective``: the solved position, and the field a capacity ceiling is
# read from. Bit i of the field is FBP i.
FBP_OFFSET = 432
FBP_ROW, FBP_BIT0 = 26, 16

STATUS_LOCATED = "located"             # one position, discriminated by samples
STATUS_SINGLE = "single-sample"        # one snapshot: consistent, not proven
STATUS_ALIAS = "alias"                 # a handful: the samples cannot split them
STATUS_NO_SAMPLE = "no-sample"         # thousands: nothing discriminates
STATUS_ABSENT = "absent"               # zero: the field is not in the hard rows

_NOT_SEPARATED = ("three positions, and all three fields carry the same value "
                  "in every sample: neither the coordinate nor which of the "
                  "three words is which has been separated. Needs a die where "
                  "these three fields differ from one another. The positions are "
                  "not three rival guesses at one field: the first two are "
                  "back-to-back 24-bit slots, and on every die here they account "
                  "for every set bit of row28, while row26's other set bits "
                  "belong to fbp_defective and (on the die whose GPC5 is dead) to "
                  "the gpc_defective alias at row26 bit0..7. What is missing is "
                  "the assignment of registers to slots, not the layout")

#: Shadow word / defect word -> its width, one source of truth (optionmap).
WIDTHS = {}
for _f in FIELDS:
    WIDTHS[_f.key] = _f.width
    if _f.defect_key:
        WIDTHS[_f.defect_key] = _f.width


@dataclass(frozen=True)
class HardField:
    """One hard-row mask and the position(s) the available samples leave open.

    ``offsets`` are bit offsets in the 3552-bit factory string (see
    :func:`coord`). An empty tuple means nothing was solved: no sample
    discriminated, or the field is not in the hard rows at all.
    """
    key: str
    offsets: tuple
    status: str
    note: str = ""

    @property
    def width(self):
        return WIDTHS[self.key]


def _tpc_hard():
    located = {2: (363,), 4: (387,), 5: (405,)}
    alias = {1: (355, 425, 495), 7: (416, 737)}
    notes = {
        1: "three offsets fit every sample; what is missing is a die whose "
           "GPC1 has two or more dead TPCs, which no sample here has",
        7: "carries the same values as gpc_defective in every sample, so the "
           "two fields cannot be told apart; one of the two offsets shares "
           "word 13 (row26) with fbp_defective, the other is row46 bit1",
    }
    out = []
    for i in range(8):
        key = "tpc_gpc%d_defective" % i
        if i in located:
            out.append(HardField(key, located[i], STATUS_LOCATED,
                                 "solved: the three dies' values differ here"))
        elif i in alias:
            out.append(HardField(key, alias[i], STATUS_ALIAS, notes[i]))
        else:
            out.append(HardField(key, (), STATUS_NO_SAMPLE,
                                 "0 in every sample: nothing discriminates"))
    return out


HARD_FIELDS = tuple([
    HardField("fbp_defective", (FBP_OFFSET,), STATUS_LOCATED,
              "the field a capacity ceiling is read from; bit i = FBP i"),
    HardField("pes_defective", (564,), STATUS_LOCATED,
              "straddles word 17 into word 18: a solver restricted to one 32-bit "
              "word would never find it"),
] + _tpc_hard() + [
    HardField("gpc_defective", (416, 737), STATUS_ALIAS,
              "same values as tpc_gpc7_defective in every sample"),
    HardField("fbpa_defective", (444, 468, 502), STATUS_ALIAS, _NOT_SEPARATED),
    HardField("fbio_defective", (444, 468, 502), STATUS_ALIAS, _NOT_SEPARATED),
    HardField("rop_l2_defective", (444, 468, 502), STATUS_ALIAS,
              _NOT_SEPARATED + ". Their first four bits are row26 bit28..31, "
              "which is also what explains the 0x3 versus 0x0 high nibble of "
              "row26 on an 8-partition and a 10-partition card: not a separate "
              "mystery, the low nibble of this very field"),
    HardField("nvlink_defective", (), STATUS_NO_SAMPLE, "0 in every sample"),
    HardField("sys_pipe_defective", (), STATUS_NO_SAMPLE, "0 in every sample"),
])

HARD_BY_KEY = dict((f.key, f) for f in HARD_FIELDS)


class BeyondHardFloor(ValueError):
    """A plan targets a block whose defect that plan cannot overrule.

    Raised by ``gen`` for a target that clears a bit ``*_DEFECTIVE`` sets while
    the plan carries no RIR record (this tool reads RIR and cannot yet burn one).
    See the module docstring for what that state does to a card.
    """


def coord(offset, width=1):
    """A factory bit offset -> the physical rows and bits it names."""
    first, last = offset, offset + width - 1
    row1, bit1 = 2 * (first // 32), first % 32
    row2, bit2 = 2 * (last // 32), last % 32
    if row1 == row2:
        return ("row%d bit%d" % (row1, bit1) if bit1 == bit2
                else "row%d bit%d..%d" % (row1, bit1, bit2))
    return "row%d bit%d..row%d bit%d" % (row1, bit1, row2, bit2)


def factory_words(rows):
    """Physical factory rows -> {logical word: value}, taking the even copy.

    Which copy is authoritative is not proven. The even one is preferred because
    on this bench's 8 GB card the pair row18/row19 differs in bit8 alone, which is
    the bit that card's factory RIR record forces to 1: the stored copy and the
    patched copy differ there, and the even copy is the one whose value the shadow
    registers do *not* report, so it is the array and not the patch. It is also a
    choice no answer here depends on: every field the solver locates covers only
    rows whose two copies agree, which `test_the_copy_choice_changes_no_located_
    answer` pins rather than leaves to luck.
    """
    return dict((row // 2, value) for row, value in rows.items()
                if 0 <= row <= FACTORY_ROW_MAX and row % 2 == 0)


def factory_flat(rows):
    """Factory rows -> one 3552-bit int, word 0's bit 0 at offset 0."""
    flat = 0
    for word, value in factory_words(rows).items():
        flat |= (value & 0xffffffff) << (32 * word)
    return flat


def copy_mismatches(rows):
    """Factory rows whose redundancy pair disagrees (see COPY_MISMATCH_ROWS)."""
    return sorted(2 * w for w in range(LOGICAL_WORDS)
                  if rows.get(2 * w) is not None
                  and rows.get(2 * w + 1) is not None
                  and rows[2 * w] != rows[2 * w + 1])


def fold_two_per_fbp(value):
    """A 24-bit ``*_DEFECTIVE`` word -> the 12-bit set of FBP it marks dead."""
    mask = 0
    for fbp in range(TOTAL_FBP):
        if (value >> (2 * fbp)) & 3 == 3:
            mask |= 1 << fbp
    return mask


def expand_per_fbp(mask):
    """The inverse of :func:`fold_two_per_fbp`: one FBP bit -> that pair of bits.

    Used to build test snapshots and to show what the 24-bit words would have to
    read if a given FBP set were the real dead set. A half-pair is not a shape the
    factory uses, which is why folding requires both bits.
    """
    value = 0
    for fbp in range(TOTAL_FBP):
        if (mask >> fbp) & 1:
            value |= 3 << (2 * fbp)
    return value


def dead_fbps(words):
    """The dead-FBP mask, cross-checked against the three 24-bit shadows.

    The factory writes the same judgement into four independent registers, so the
    three foldings agreeing is a free integrity check on the snapshot, and a
    mismatch says the snapshot mixed two cards or a register is not what this
    table says it is. ``consistent`` stays None when the 24-bit words were not
    read: unknown, not "agreed".

    The comparison is by :func:`fold_two_per_fbp` only, because that is the
    encoding three real dies use: 0xcf3 and 0xc03000 fold to their card's 0x2d and
    0x840 exactly, and on all three the four words carry one judgement. A word that
    merely has the same *flat* low 12 bits does not count as agreeing: under this
    encoding that is a half-set pair, a shape the factory does not use, and saying
    so is more useful than hiding it. (The vendored sample's fbio/rop_l2 words are
    like that, which is a further reason to read that file's capacity off
    fbp_defective alone.)
    """
    mask = words.get("fbp_defective")
    out = {"mask": mask, "list": [], "sources": {}, "consistent": None,
           "mismatch": [], "anomalous": None}
    if mask is not None:
        # 12 partitions; anything else in the word is not a partition, so it is
        # reported rather than folded into the ceiling.
        out["anomalous"] = mask & ~((1 << TOTAL_FBP) - 1) or None
        mask = out["mask"] = mask & ((1 << TOTAL_FBP) - 1)
        out["list"] = [i for i in range(TOTAL_FBP) if (mask >> i) & 1]
    for key in FOLD_TWO_PER_FBP:
        value = words.get(key)
        if value is None:
            continue
        folded = fold_two_per_fbp(value)
        out["sources"][key] = {"raw": value, "fbp_set": folded}
        if mask is not None and folded != mask:
            out["mismatch"].append((key, folded))
    if mask is not None and len(out["sources"]) == len(FOLD_TWO_PER_FBP):
        out["consistent"] = not out["mismatch"]
    return out


def fbp_bit(fbp):
    """Where FBP `fbp`'s dead bit sits in the main array."""
    if not 0 <= fbp < TOTAL_FBP:
        raise ValueError("FBP %d does not exist on GA100 (0-%d)"
                         % (fbp, TOTAL_FBP - 1))
    return (FBP_ROW, FBP_BIT0 + fbp)


def rir_to_free(fbp_mask):
    """The RIR records that would hold every FBP in `fbp_mask` at 0."""
    from . import rir
    return rir.records_for_bits([(FBP_ROW, FBP_BIT0 + i, 0)
                                 for i in range(TOTAL_FBP)
                                 if (fbp_mask >> i) & 1])


# ---------------------------------------------------------------- solver ----
@dataclass(frozen=True)
class Pin:
    """A hard-row value one die had at some point (typically: before a burn).

    Hard rows are static, so requiring one die to read two different values at
    one offset is a contradiction -- and that is the entire content of the
    statement "this field is not in the hard rows".
    """
    key: str
    value: int
    card: str = ""

    def matches(self, source, dump):
        if not self.card:
            return True
        for probe in (source, dump.get("uuid") or ""):
            if self.card in probe:
                return True
        return False

    @classmethod
    def parse(cls, text):
        """``[card:]field=hex``, the form ``defmap --pin`` takes."""
        spec, sep, value = text.partition("=")
        if not sep:
            raise ValueError("--pin expects [card:]field=hex, got %r" % text)
        card, colon, field = spec.rpartition(":")
        if not colon:
            card, field = "", spec
        field = field.strip()
        if field not in WIDTHS:
            raise ValueError("--pin %s: not a floorsweep shadow word this tool "
                             "knows (try a *_disable or *_defective name)"
                             % field)
        return cls(field, int(value, 0), card.strip())


def candidates(requirements, width):
    """Every offset in the factory string where all `requirements` hold.

    `requirements` is [(3552-bit factory string, expected value), ...]. The same
    die may appear twice with two different values (see :class:`Pin`), which is
    what drives such a field's candidate list to empty.
    """
    mask = (1 << width) - 1
    return [off for off in range(FLAT_BITS - width + 1)
            if all(((flat >> off) & mask) == value
                   for flat, value in requirements)]


def solve(dumps, keys=None, pins=()):
    """Re-derive the hard-row positions from snapshots.

    `dumps` are loaded snapshots (``ff_rows`` + ``option_words``). Every
    ``*_defective`` word the table covers is solved for, plus every field a
    ``--pin`` names -- which is how a ``*_disable`` field, whose home is the FF
    pool, gets its expected-empty result. The status is computed from *this run's*
    samples rather than copied from the table, so a run with one snapshot says
    "undetermined" instead of repeating an answer it cannot reproduce;
    `table_status` and `reproduces` show the relation between the two. Fields the
    readout did not supply are reported as not read rather than skipped: a
    missing word is a different answer from "no position matches".

    A snapshot with no factory rows -- a words-only readout, which is what
    ``ffscan.sh FFROWS=0`` writes for a busy card -- cannot discriminate anything,
    and its all-zero array would eliminate every offset for a field whose word is
    nonzero, turning "not read" into "not in the hard rows". Such snapshots are
    left out of the solve and named in `no_rows`.
    """
    flats, skipped = [], []
    for d in dumps:
        rows = d.get("ff_rows") or {}
        name = d.get("source") or "?"
        if any(0 <= row <= FACTORY_ROW_MAX for row in rows):
            flats.append((name, factory_flat(rows), d))
        else:
            skipped.append(name)
    if not flats:
        raise ValueError(
            "no snapshot in this run carries factory rows 0-%d, so nothing can "
            "be discriminated (a words-only readout has the option words and not "
            "the array: re-capture with FFROWS=1, or `ffcalc mergeff` the rows "
            "into the snapshot: %s)"
            % (FACTORY_ROW_MAX, ", ".join(skipped) or "none given"))
    pinned = {}
    for pin in pins:
        pinned.setdefault(pin.key, []).append(pin)
    wanted = list(keys) if keys else [f.key for f in HARD_FIELDS]
    for key in sorted(pinned):
        if key not in wanted:
            wanted.append(key)
    unknown = [key for key in wanted if key not in WIDTHS]
    if unknown:
        raise ValueError("unknown field(s): %s"
                         % ", ".join(sorted(set(unknown))))
    out = []
    for key in wanted:
        field = HARD_BY_KEY.get(key)
        width = WIDTHS[key]
        reqs, values, missing = [], [], []
        for name, flat, dump in flats:
            value = dump["option_words"].get(key)
            if value is None:
                missing.append(name)
                continue
            reqs.append((flat, value))
            values.append((name, value))
        for pin in pinned.get(key, ()):
            for name, flat, dump in flats:
                if pin.matches(name, dump):
                    reqs.append((flat, pin.value))
                    break
            else:
                raise ValueError(
                    "--pin %s=%s names no card whose factory rows are in this "
                    "sample (read and used: %s%s)"
                    % (pin.key, hex(pin.value),
                       ", ".join(n for n, _f, _d in flats) or "none",
                       "; no factory rows: " + ", ".join(skipped)
                       if skipped else ""))
        hits = candidates(reqs, width) if reqs else []
        distinct = len(set(value for _flat, value in reqs))
        expected = field.offsets if field else ()
        out.append({
            "key": key, "width": width, "values": values, "missing": missing,
            "no_rows": skipped,
            "candidates": hits, "status": _status(len(hits), distinct),
            "distinct_values": distinct,
            "table_status": field.status if field else "ff-pool",
            "known": list(expected), "in_hard_rows": bool(field),
            "note": field.note if field else
                    "carried by the FF pool, not by the hard rows",
            "reproduces": set(expected) == set(hits),
            "discriminating": len(set(v for _n, v in values)) > 1,
        })
    _flag_overlaps(out)
    return out


def _flag_overlaps(results):
    """Flag a candidate that would have to share a bit with a field solved here.

    Two option words do not read the same fuse bit, so a window overlapping a
    uniquely located field's window is not equally plausible with the rest: it
    survives only because the samples agree on the shared bits. ``defmap`` prints
    that instead of listing such a candidate as if it were on equal footing.

    Only fields the hard-row table knows can serve as the reference. An FF-pool word
    does tie onto the defective window -- `fbp_disable` reads the same value as
    `fbp_defective` until something is burnt -- and the report explains that; it is
    not a reason to doubt the defective window.
    """
    fixed = [(r["key"], r["candidates"][0], r["width"]) for r in results
             if r["in_hard_rows"] and len(r["candidates"]) == 1]
    asked = {r["key"] for r in results}
    fixed += [(f.key, f.offsets[0], f.width) for f in HARD_FIELDS
              if f.status == STATUS_LOCATED and len(f.offsets) == 1
              and f.key not in asked]
    for r in results:
        hits = []
        for off in r["candidates"]:
            share = sorted(key for key, other, width in fixed
                           if key != r["key"]
                           and off < other + width and other < off + r["width"])
            if share:
                hits.append((off, share))
        r["overlaps"] = hits


def _status(count, distinct_values=2):
    """What a candidate list proves, given how many different values forced it.

    One snapshot can leave exactly one offset standing -- the pattern simply does
    not repeat anywhere else in the array -- and that is luck, not evidence. Only
    two *different* required values cut offsets in a way that says something, so
    below that the honest status is "single-sample" even when the list is short.
    """
    if count == 0:
        return STATUS_ABSENT
    if count == 1:
        return STATUS_LOCATED if distinct_values >= 2 else STATUS_SINGLE
    return STATUS_ALIAS if count <= 8 else STATUS_NO_SAMPLE


def verdict(report_entry):
    """One-line reading of a :func:`solve` entry, for ``defmap`` output."""
    r = report_entry
    partial = (" (not read on %d snapshot(s))" % len(r["missing"])
               if r["missing"] and r["values"] else "")
    if not r["values"]:
        if len(r["missing"]) == 1:
            return "not read by this snapshot"
        return ("not read by any of the %d snapshot(s) in this run"
                % (len(r["missing"]) or 1))
    if r["status"] == STATUS_ABSENT:
        if r["known"]:
            return ("the table puts it at %s, but nothing in this sample reads "
                    "that value at that offset either: option_words and ff_rows "
                    "here do not correspond (a hand-made snapshot, or a table "
                    "position that no longer holds)%s"
                    % (", ".join(coord(o, r["width"]) for o in r["known"]),
                       partial))
        return ("not in the hard rows at any offset"
                + (" (its home is the FF pool)" if not r["in_hard_rows"] else "")
                + partial)
    if r["status"] == STATUS_NO_SAMPLE:
        return ("undetermined: %d offsets survive, no sample in this run "
                "discriminates%s" % (len(r["candidates"]), partial))
    where = ", ".join(coord(o, r["width"]) for o in r["candidates"][:8])
    if r["status"] == STATUS_SINGLE:
        return ("%s is the only offset left, but every sample here reads the same "
                "value, so nothing was discriminated%s" % (where, partial))
    if r["status"] == STATUS_LOCATED:
        return ("at %s (%d candidate%s)%s"
                % (where, len(r["candidates"]),
                   "" if len(r["candidates"]) == 1 else "s", partial))
    return ("candidates %s -- %s%s"
            % (where, r["note"] or "samples cannot split", partial))
