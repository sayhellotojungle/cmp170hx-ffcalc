"""RIR (Redundancy Information Rows): what they are, and why they are closed to us.

This module is arithmetic and recorded facts. Nothing in this repo reads, writes
or burns the RIR macro: it exists to decode what somebody else read out, and to
say what lifting a given ceiling would cost. See docs/BURNING.md ("硬行与 RIR")
for the reasoning; the short version:

* The ``*_DEFECTIVE`` masks live in the main array's factory rows (0-221), which
  are outside the FF address space -- an FF record names ``(chain, block)`` and
  no ``(chain, block)`` covers a hard row, so records cannot reach those bits at
  all (`ffcalc/hardrows.py`: where they are, how that was solved).
* An RIR record substitutes the **read**: at boot, while a record is enabled, the
  bit at absolute address ``row*32 + bit`` of the main array reads as the
  record's value instead of as its own fuse. The cell is untouched, and the
  substitution works in both directions -- a record can hold a fused-1 bit at 0
  just as well as force a 0 up to 1. "RIR can only turn a 0 into a 1" is a
  statement about burning OTP cells, not about what a record does.
* The macro is 8 rows x two 16-bit records = **16 slots**, and an 8 GB SKU card
  was read out with every slot spent (rows 0-6 ``0xffffffff``, row 7
  ``0xffff0923``). One record per hard-row bit, so on such a card there is no
  route to a memory ceiling any FF record can reach: this repo does not plan to
  burn RIR, and on an 8 GB SKU card there is nothing left to burn into anyway.

Record layout (16 bit) and the two anchors that fix it:

    bit0 = enabled   bit1 = the value to force   bits[15:2] = row*32 + bit

``0x00950015`` (two records: force 0 at row0:bit5 and row1:bit5, i.e.
``disable_sw_override`` cleared, effect confirmed over a cold boot) is an
external burn report. ``0x0923`` (force 1 at row18:bit8) is a *factory* record,
read off this bench's 8 GB card b6b9f653 by an external tool; it cross-checks
against the array, because on that card the redundancy pair row18/row19 differs
in bit8 alone, so the main array really does have a dead bit there and the
record patches it.

Slot *numbering* is not settled -- the low half of a row is numbered as the even
slot here, while the external dump calls the same live record slot 15, so the
half-word order is unproven. Everything user-facing prints the raw row word next
to the slot for that reason.
"""
import re

#: A number inside a captured RIR dump: hex with 0x, or a run of digits. The
#: alternation order matters -- 0x... must be taken whole, or the digits inside
#: 0xffff0923 would come back as a row number.
NUMBER = re.compile(r"0[xX][0-9a-fA-F]+|\d+")

ROW_COUNT = 8
SLOT_COUNT = ROW_COUNT * 2
MAX_ROW = 511           # abs_bit is 14 bits: 512 rows x 32 bits = 16384 exactly
MAX_BIT = 31
BLANK = 0x0000          # nothing burnt: a whole record can still be burnt here
SPENT = 0xFFFF          # every bit burnt: no 0 cell left, so nothing can be added

#: The 8 GB SKU card of this bench, as read out (rows 0..7). Not read by this
#: repo: reaching these rows means switching the read mux with DEBUGCTRL bit 1,
#: which is a write, and RIR is not on our to-do list. Recorded here because it
#: is the measurement that closes the 64 GiB question.
MEASURED_8GB = (0xFFFFFFFF,) * 7 + (0xFFFF0923,)

#: Per SKU, what the slot count is and how firmly it is known. An 8 GB SKU card
#: of ours was read: 0 of 16 free. For a 10 GB SKU card the free-slot count is an
#: external claim ("10de:2082 ships with all 16 free") that this bench has never
#: read, so it is reported as unproven rather than as an available route.
SKU_ROUTE = {
    "8gb": {"free": 0, "known": True,
            "text": "read on this bench's 8 GB SKU card: 0 of 16 slots free "
                    "(rows 0-6 all 0xffffffff, row 7 0xffff0923) -- no RIR "
                    "record can be issued, so the hard ceiling is permanent"},
    "10gb": {"free": None, "known": False,
             "text": "external reports say a 10 GB SKU card ships with all 16 "
                     "slots free; this bench has never read one, so treat the "
                     "RIR route as possible and unproven, not as available"},
}


class BadRirRecord(ValueError):
    pass


def abs_bit(row, bit):
    """A main-array bit's absolute address, the form a RIR record stores."""
    if not 0 <= row <= MAX_ROW or not 0 <= bit <= MAX_BIT:
        raise BadRirRecord("row %d bit %d is not a main-array bit (rows 0-%d, "
                           "bits 0-%d)" % (row, bit, MAX_ROW, MAX_BIT))
    return row * 32 + bit


def split_abs(value):
    """The inverse of :func:`abs_bit`: abs_bit -> (row, bit)."""
    if not 0 <= value <= MAX_ROW * 32 + MAX_BIT:
        raise BadRirRecord("abs_bit %d is outside 0-%d"
                           % (value, MAX_ROW * 32 + MAX_BIT))
    return value // 32, value % 32


def encode(row, bit, value):
    """One 16-bit RIR record forcing `value` at main-array `row`:`bit`."""
    if value not in (0, 1):
        raise BadRirRecord("a RIR record forces 0 or 1, got %r" % (value,))
    return (abs_bit(row, bit) << 2) | (value << 1) | 1


def decode(record):
    """A 16-bit RIR record -> {enabled, row, bit, value, abs_bit}.

    `row`/`bit`/`value` are what the record says even when it is disabled
    (`enabled` False, i.e. bit0 is 0 and the record is inert).
    """
    if not 0 <= record <= 0xffff:
        raise BadRirRecord("0x%x is not a 16-bit RIR record" % record)
    return {"enabled": bool(record & 1), "row": (record >> 2) // 32,
            "bit": (record >> 2) % 32, "value": (record >> 1) & 1,
            "abs_bit": record >> 2}


def live_record(record):
    """decode(record), or None when the record is not enabled."""
    d = decode(record)
    return d if d["enabled"] else None


def slot(row, half):
    """Slot number of one 16-bit half of an RIR row (half 0 = low half).

    The even/odd assignment of the two halves is a convention, not a measurement
    (see the module docstring); the row and the raw word are what are solid.
    """
    if not 0 <= row < ROW_COUNT or half not in (0, 1):
        raise BadRirRecord("RIR row %d half %d does not exist" % (row, half))
    return row * 2 + half


def halves(word):
    """A 32-bit RIR row word -> (low half, high half)."""
    if not 0 <= word <= 0xffffffff:
        raise BadRirRecord("0x%x is not a 32-bit RIR row" % word)
    return word & 0xffff, (word >> 16) & 0xffff


def slots(words):
    """{row: word} or a sequence of 8 words -> one dict per slot, slot order.

    States, from the fuse cell's point of view rather than the record's:

    ``blank``   reads 0x0000: nothing burnt, any record can still be burnt.
    ``spent``   reads 0xffff: no 0 cell left, so the slot cannot take a record.
                This is what "factory-filled" means physically.
    ``live``    bit0 is burnt, so a record is in effect at boot.
    ``partial`` some bits burnt, none of them the enable bit: a record can still
                be burnt, but only one whose bits are a superset of these.
    """
    pairs = normalise_words(words)
    out = []
    for row, word in pairs:
        for half, value in enumerate(halves(word)):
            if value == BLANK:
                state = "blank"
            elif value == SPENT:
                state = "spent"
            elif value & 1:
                state = "live"
            else:
                state = "partial"
            out.append({"slot": slot(row, half), "row": row, "half": half,
                        "value": value, "state": state,
                        # Only a live slot's bits are a record. 0xffff decodes to
                        # "force 1 at row511 bit31", which is what an all-burnt
                        # word has to decode to, not a claim that the factory
                        # addressed row511 fifteen times.
                        "record": decode(value) if state == "live" else None})
    return out


def inventory(words):
    """A slot inventory of an RIR readout: raw words, per-slot state, counts.

    ``words`` is keyed by row, and ``missing`` names the rows the readout did not
    carry: an absent row is not a blank one, so ``free`` counts nothing about it
    and ``complete`` says whether the count can be read as a total.
    """
    pairs = normalise_words(words)
    table = slots(pairs)
    read = dict(pairs)
    return {"words": read, "missing": [r for r in range(ROW_COUNT)
                                       if r not in read],
            "complete": len(read) == ROW_COUNT,
            "slots": table,
            "live": [s for s in table if s["state"] == "live"],
            "free": sum(1 for s in table if s["state"] == "blank"),
            "spent": sum(1 for s in table if s["state"] == "spent")}


def burnable(words, record):
    """Slots a given 16-bit record could still be burnt into.

    A fuse cell only goes 0 -> 1, so a slot accepts the record when the record
    already contains every bit the slot holds: ``record | burnt == record``.
    """
    record = int(record, 0) if isinstance(record, str) else int(record)
    decode(record)
    return [s["slot"] for s in slots(words)
            if (s["value"] | record) == record and s["value"] != record]


def records_for_bits(bits):
    """[(row, bit, value), ...] -> the RIR records that express them, one each."""
    out = []
    for row, bit, value in bits:
        rec = encode(row, bit, value)
        out.append({"record": rec, "row": row, "bit": bit, "value": value,
                    "abs_bit": decode(rec)["abs_bit"]})
    return out


def route(sku):
    """What the slot situation is for a SKU, with how firmly it is known."""
    return SKU_ROUTE.get(sku, {"free": None, "known": False,
                               "text": "unknown SKU %r: no RIR readout" % sku})


def normalise_words(words):
    """{row: word} / [word, ...] / [(row, word), ...] -> [(row, word)] sorted.

    Accepts our own output as well as an input, so `slots(inventory(w)["rows"])`
    and a double normalise behave: a slot count that depended on the shape it was
    handed would be a fragile thing to put in front of a burn decision.
    """
    if isinstance(words, str):
        words = parse_words(words)
    if isinstance(words, dict):
        items = [(int(k), _word(v)) for k, v in words.items()]
    else:
        items = [(i, _word(v)) if not isinstance(v, (tuple, list))
                 else (int(v[0]), _word(v[1])) for i, v in enumerate(words)]
    for row, value in items:
        if not 0 <= row < ROW_COUNT:
            raise BadRirRecord("%d is not an RIR row (the macro is 8 rows, 0-%d)"
                               % (row, ROW_COUNT - 1))
        if not 0 <= value <= 0xffffffff:
            raise BadRirRecord("RIR row %d: 0x%x is not a 32-bit word"
                               % (row, value))
    return sorted(items)


def _word(value):
    """A row word from a dump: int, or a hex string as a captured file holds it."""
    return int(value, 0) if isinstance(value, str) else int(value)


def parse_words(text):
    """A captured RIR readout -> {row: word}.

    Accepts what people actually have: the eight row words in order (one per line
    or all on one line), or lines of ``7 0xffff0923`` / ``RIR row 7: 0xffff0923``
    / ``rir7=0xffff0923`` as external tools print them. The rules, so a file
    cannot be half-read: a line whose numbers are all ``0x`` hex is positional
    words; any other line must hold exactly two numbers, the row and its word.
    Anything else is refused, because an RIR slot count is a load-bearing claim
    and a partially parsed dump would report a free-slot number that the file
    never said.
    """
    out = {}
    positional = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].split(";", 1)[0]
        toks = NUMBER.findall(line)
        if not toks:
            continue
        if all(t.lower().startswith("0x") for t in toks):
            positional.extend((lineno, t) for t in toks)
            continue
        if len(toks) != 2:
            raise BadRirRecord(
                "line %d (%r): expected %d row words, or '<row> <0xword>'. A "
                "line with several decimal numbers is ambiguous, and guessing "
                "here would turn a half-read dump into a slot count"
                % (lineno, line, ROW_COUNT))
        row_tok = next(i for i, t in enumerate(toks)
                       if not t.lower().startswith("0x"))
        row, value = int(toks[row_tok]), int(toks[1 - row_tok], 0)
        if not 0 <= row < ROW_COUNT:
            raise BadRirRecord("line %d: row %d is not an RIR row (0-%d). Note "
                               "that 0-15 numbers there are slot numbers, and the "
                               "macro has 8 rows of two records"
                               % (lineno, row, ROW_COUNT - 1))
        if not 0 <= value <= 0xffffffff:
            raise BadRirRecord("line %d: 0x%x is not a 32-bit word"
                               % (lineno, value))
        if row in out and out[row] != value:
            raise BadRirRecord("line %d: row %d already read as 0x%08x"
                               % (lineno, row, out[row]))
        out[row] = value
    for i, (lineno, tok) in enumerate(positional):
        if not tok.lower().startswith("0x"):
            raise BadRirRecord("line %d: %r is not an RIR word; write bare words "
                               "as hex (0x...) so they are not confused with a "
                               "row number" % (lineno, tok))
        if i >= ROW_COUNT:
            raise BadRirRecord("more than %d bare words: an RIR readout is the "
                               "%d rows of one macro" % (ROW_COUNT, ROW_COUNT))
        if i in out and out[i] != int(tok, 0):
            raise BadRirRecord("row %d given twice with different values" % i)
        out[i] = int(tok, 0)
    if not out:
        raise BadRirRecord("no RIR rows found (want %d words, e.g. 0xffffffff "
                           "x7 + 0xffff0923)" % ROW_COUNT)
    return out
