"""The hard rows (where ``*_DEFECTIVE`` lives, what it caps) and RIR records.

Two address spaces, one capacity number, and a lever that is closed on an 8 GB
SKU card: these tests pin the arithmetic, the reproduction of the coordinates from
snapshots, and the two CLI verbs that report them. Nothing here touches a card --
RIR is decoded from captured words, never read from the macro.
"""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ffcalc import cli, hardrows, rir
from ffcalc.analyze import analyze
from ffcalc.dump import load_dump

SAMPLE = os.path.join(os.path.dirname(__file__), "..",
                      "samples", "blog_card_example.json")


def quiet(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(argv))
    return rc, buf.getvalue()


def card(fbp_defective, fbp_disable=None, fbp1_defective=0, extra_words=None,
         broken_pairs=()):
    """A snapshot whose factory rows encode `fbp_defective` at row26 bit16..27.

    Everything else in the factory array is zero: with three of these carrying
    different masks, the only offset at which every one of them reads its own
    value is the planted one, which is the whole method in miniature.
    """
    rows = {}
    for word in range(hardrows.LOGICAL_WORDS):
        rows[2 * word] = (fbp_defective << 16) if word == 13 else 0
        rows[2 * word + 1] = rows[2 * word]
    for row in broken_pairs:
        rows[row + 1] = rows[row] ^ 0x1        # corrupt the ODD copy only
    words = {"fbp_defective": fbp_defective,
             "fbp_disable": fbp_disable if fbp_disable is not None
             else fbp_defective,
             "fbpa_defective": hardrows.expand_per_fbp(fbp1_defective),
             "fbio_defective": hardrows.expand_per_fbp(fbp1_defective),
             "rop_l2_defective": hardrows.expand_per_fbp(fbp1_defective)}
    words.update(extra_words or {})
    return {"sku": "8gb", "ff_rows": rows, "option_words": words,
            "source": "card-%x" % fbp_defective}


class TestHardFieldTable(unittest.TestCase):
    """The table is a claim about hardware, and it has to stay consistent with the
    widths optionmap uses and with the coordinates it quotes."""

    def test_every_table_entry_matches_the_readout_widths(self):
        for field in hardrows.HARD_FIELDS:
            self.assertEqual(field.width, hardrows.WIDTHS[field.key])
            self.assertTrue(field.note, "%s says nothing about its evidence"
                            % field.key)
            self.assertIn(field.status, (hardrows.STATUS_LOCATED,
                                         hardrows.STATUS_ALIAS,
                                         hardrows.STATUS_NO_SAMPLE))
            for off in field.offsets:
                self.assertLess(off + field.width, hardrows.FLAT_BITS)

    def test_the_words_the_table_covers_are_all_ones_the_readout_carries(self):
        """A mask nobody can read is not a floor."""
        from ffcalc.optionmap import FIELDS
        known = set(f.defect_key for f in FIELDS if f.defect_key)
        for field in hardrows.HARD_FIELDS:
            self.assertIn(field.key, known)

    def test_fbp_defective_coordinates_agree_with_the_offset(self):
        self.assertEqual(hardrows.coord(hardrows.FBP_OFFSET, 12),
                         "row26 bit16..27")
        self.assertEqual(hardrows.fbp_bit(5), (26, 21))

    def test_coordinates_name_both_rows_when_a_field_straddles(self):
        # pes_defective runs out of word 17 into word 18: a solver that stayed
        # inside one 32-bit word would report the field as unsolved.
        self.assertEqual(hardrows.coord(564, 24), "row34 bit20..row36 bit11")

    def test_the_copy_mismatch_rows_are_a_fixed_measured_set(self):
        """Measured on three dies, the same row numbers every time; none of them
        may carry a field the table solved, or the answer would depend on which
        copy of the pair happened to be read."""
        self.assertEqual(len(hardrows.COPY_MISMATCH_ROWS), 21)
        self.assertIn(18, hardrows.COPY_MISMATCH_ROWS)     # the row RIR patches
        self.assertNotIn(82, hardrows.COPY_MISMATCH_ROWS)
        for field in hardrows.HARD_FIELDS:
            for off in field.offsets:
                rows = set(2 * ((off + i) // 32) for i in range(field.width))
                self.assertFalse(rows & set(hardrows.COPY_MISMATCH_ROWS),
                                 "%s lands in %s" % (field.key, sorted(rows)))


class TestFolding(unittest.TestCase):
    """The 24-bit masks mark the same dead FBP set with two bits per FBP; that is
    the free cross-check on a snapshot."""

    def test_the_numbers_read_off_the_real_cards_fold_to_fbp_defective(self):
        self.assertEqual(hardrows.fold_two_per_fbp(0xc03000), 0x840)
        self.assertEqual(hardrows.fold_two_per_fbp(0x000cf3), 0x2d)
        self.assertEqual(hardrows.expand_per_fbp(0x840), 0xc03000)

    def test_a_half_set_pair_is_not_dead(self):
        """Both bits of the pair or neither: one of each means the factory did not
        mark that FBP, and reading a single bit as "dead" would invent a ceiling.
        (The vendored sample's rop_l2 word is exactly that shape.)"""
        self.assertEqual(hardrows.fold_two_per_fbp(0x840), 0x0)

    def test_the_inverse_round_trips(self):
        for mask in (0x0, 0x2d, 0x840, 0xfff):
            self.assertEqual(hardrows.fold_two_per_fbp(
                hardrows.expand_per_fbp(mask)), mask)

    def test_dead_fbps_agrees_and_disagrees_loudly(self):
        base = card(0x840, fbp1_defective=0x840)
        ok = hardrows.dead_fbps(base["option_words"])
        self.assertEqual(ok["mask"], 0x840)
        self.assertEqual(ok["list"], [6, 11])
        self.assertTrue(ok["consistent"])
        bad = hardrows.dead_fbps(dict(base["option_words"], fbio_defective=0))
        self.assertFalse(bad["consistent"])
        self.assertEqual([k for k, _set in bad["mismatch"]], ["fbio_defective"])
        unread = hardrows.dead_fbps({"fbp_defective": 0x2d})
        self.assertIsNone(unread["consistent"])       # unknown, not "agreed"


class TestSolver(unittest.TestCase):
    """`defmap` must reproduce the coordinates from snapshots, and reproduce the
    absence of the fields whose home is the FF pool."""

    def setUp(self):
        self.dumps = [card(0x2d), card(0x840), card(0x0a5)]

    def test_fbp_defective_solves_to_the_table_position(self):
        r = hardrows.solve(self.dumps, keys=["fbp_defective"])[0]
        self.assertEqual(r["candidates"], [hardrows.FBP_OFFSET])
        self.assertEqual(r["status"], hardrows.STATUS_LOCATED)
        self.assertEqual(r["distinct_values"], 3)
        self.assertTrue(r["reproduces"], hardrows.verdict(r))
        self.assertIn("row26 bit16..27", hardrows.verdict(r))

    def test_two_dies_with_the_same_mask_do_not_discriminate(self):
        """One offset can be left standing because the pattern simply does not
        repeat in the array, and that is luck, not evidence: only two *different*
        required values cut offsets in a way that says something."""
        r = hardrows.solve([card(0x840)], keys=["fbp_defective"])[0]
        self.assertEqual(r["status"], hardrows.STATUS_SINGLE)
        self.assertEqual(r["distinct_values"], 1)
        self.assertIn("nothing was discriminated", hardrows.verdict(r))
        three = hardrows.solve(self.dumps, keys=["fbp_defective"])[0]
        self.assertEqual(three["status"], hardrows.STATUS_LOCATED)

    def test_a_field_that_is_zero_everywhere_is_reported_undetermined(self):
        """Thousands of offsets read 0, so 'all samples agree' proves nothing; this
        is the honest answer for nvlink/sys_pipe on today's three dies."""
        r = hardrows.solve([card(0x0)], keys=["fbp_defective"])[0]
        self.assertGreater(len(r["candidates"]), 1000)
        self.assertEqual(r["status"], hardrows.STATUS_NO_SAMPLE)
        self.assertIn("undetermined", hardrows.verdict(r))

    def test_a_burnt_field_is_shown_not_to_live_here(self):
        """The factory value of a field we burnt must not be findable either: hard
        rows are static, so two required values on one die leaves no offset."""
        r = hardrows.solve(self.dumps[:1], keys=["fbp_disable"],
                           pins=[hardrows.Pin("fbp_disable", 0x852)])[0]
        self.assertEqual(r["candidates"], [])
        self.assertEqual(r["status"], hardrows.STATUS_ABSENT)
        self.assertIn("FF pool", hardrows.verdict(r))

    def test_a_candidate_sharing_bits_with_a_solved_field_is_flagged(self):
        """Two option words do not read the same fuse bit, so a candidate that
        overlaps a field solved on its own is not on equal footing with the rest
        of its list -- `defmap` says which one. The real snapshots give exactly
        this: tpc_gpc1_defective's row26 bit9..16 candidate shares bit 432 with the
        uniquely solved fbp_defective."""
        def row(key, hard, cands, width=12):
            return {"key": key, "width": width, "candidates": cands,
                    "in_hard_rows": hard}

        results = [row("fbp_defective", True, [hardrows.FBP_OFFSET]),
                   row("tpc_gpc1_defective", True,
                       [355, hardrows.FBP_OFFSET - 7, 495], 8)]
        hardrows._flag_overlaps(results)
        self.assertEqual(results[0]["overlaps"], [])
        self.assertEqual(results[1]["overlaps"],
                         [(hardrows.FBP_OFFSET - 7, ["fbp_defective"])])

    def test_an_ff_pool_word_tied_to_the_defective_window_explains_itself(self):
        """On these dies `fbp_disable` reads the same value as `fbp_defective`, so
        the hard-row window fits the FF-pool word too. That word is not a second
        claim on the offset -- it is the sample coincidence that `--pin` rules out --
        so it must not be used as the reference that doubts the defective window."""
        def row(key, hard):
            return {"key": key, "width": 12, "candidates": [hardrows.FBP_OFFSET],
                    "in_hard_rows": hard}

        results = [row("fbp_defective", True), row("fbp_disable", False)]
        hardrows._flag_overlaps(results)
        self.assertEqual(results[0]["overlaps"], [])   # the table stands
        # the FF-pool word does share the bits, and the report says so in its own
        # words ("not a hard-row field ... --pin separates them")
        self.assertEqual(results[1]["overlaps"],
                         [(hardrows.FBP_OFFSET, ["fbp_defective"])])

    def test_pin_names_the_card_it_cannot_match(self):
        with self.assertRaises(ValueError) as ctx:
            hardrows.solve(self.dumps, keys=["fbp_disable"],
                           pins=[hardrows.Pin.parse("nosuchcard:fbp_disable=1")])
        self.assertIn("names no card", str(ctx.exception))

    def test_a_snapshot_missing_the_word_says_not_read(self):
        one = dict(self.dumps[0], option_words={})
        r = hardrows.solve([one], keys=["fbp_defective"])[0]
        self.assertEqual(r["missing"], [self.dumps[0]["source"]])
        self.assertIn("not read by this snapshot", hardrows.verdict(r))
        r = hardrows.solve([one, dict(self.dumps[1], option_words={})],
                           keys=["fbp_defective"])[0]
        self.assertIn("not read by any of the 2 snapshot(s)",
                      hardrows.verdict(r))
        # and a field one snapshot did read says which one it is missing
        r = hardrows.solve([dict(self.dumps[0], option_words={}), self.dumps[1]],
                           keys=["fbp_defective"])[0]
        self.assertEqual(r["missing"], [self.dumps[0]["source"]])
        self.assertIn("not read on 1 snapshot", hardrows.verdict(r))

    def test_a_snapshot_without_factory_rows_cannot_discriminate_anything(self):
        """A words-only readout has an empty array, and "the array is all zeros"
        would otherwise be read as "this field's value is nowhere in the hard
        rows" -- which is the opposite conclusion from "not read"."""
        words_only = {"sku": "8gb", "ff_rows": {},
                      "option_words": {"fbp_defective": 0x2d},
                      "source": "words-only"}
        r = hardrows.solve([self.dumps[0], words_only],
                           keys=["fbp_defective"])[0]
        self.assertEqual(r["no_rows"], ["words-only"])
        self.assertEqual(r["values"], [(self.dumps[0]["source"], 0x2d)])
        r = hardrows.solve(self.dumps, keys=["fbp_defective"])[0]
        self.assertEqual(r["no_rows"], [])           # nothing skipped here
        with self.assertRaises(ValueError) as ctx:
            hardrows.solve([words_only], keys=["fbp_defective"])
        self.assertIn("no snapshot in this run carries factory rows",
                      str(ctx.exception))

    def test_rows_that_do_not_correspond_to_the_words_are_said_so(self):
        """The state of a hand-made or mis-transcribed snapshot: real rows, but no
        offset in them reads the value the word reports."""
        odd = dict(self.dumps[0],
                   option_words=dict(self.dumps[0]["option_words"],
                                     fbp_defective=0x123))
        r = hardrows.solve([odd], keys=["fbp_defective"])[0]
        self.assertEqual(r["status"], hardrows.STATUS_ABSENT)
        self.assertIn("do not correspond", hardrows.verdict(r))

    def test_copy_mismatches_are_reported_as_measured(self):
        broken = card(0x2d, broken_pairs=(18, 48))
        self.assertEqual(hardrows.copy_mismatches(broken["ff_rows"]), [18, 48])
        self.assertEqual(hardrows.copy_mismatches(card(0x2d)["ff_rows"]), [])

    def test_the_even_copy_is_the_one_read(self):
        """A mismatched pair is a real thing on these dies (row18/row19 differ in
        bit8 alone on this bench's 8 GB card, which is the bit that card's factory
        RIR record forces to 1). The solver reads the even copy, so a window that
        covers such a row sees the stored value and not the patched one."""
        broken = card(0x2d, broken_pairs=(26,))      # row27, the odd copy, only
        self.assertEqual(hardrows.copy_mismatches(broken["ff_rows"]), [26])
        r = hardrows.solve([broken], keys=["fbp_defective"])[0]
        self.assertEqual(r["candidates"], [hardrows.FBP_OFFSET])

    def test_the_copy_choice_changes_no_located_answer(self):
        """Which copy is authoritative is not proven, so no located position may
        depend on the choice. The fourth snapshot carries a mismatched pair, as
        real dies do."""
        dumps = self.dumps + [card(0x840, broken_pairs=(18,))]

        def located(results):
            return [(r["key"], r["candidates"]) for r in results
                    if r["status"] in (hardrows.STATUS_LOCATED,
                                       hardrows.STATUS_ALIAS)]

        even = located(hardrows.solve(dumps))
        real = hardrows.factory_words

        def odd(rows):
            return dict((row // 2, v) for row, v in rows.items()
                        if 0 <= row <= hardrows.FACTORY_ROW_MAX and row % 2)

        hardrows.factory_words = odd
        try:
            flipped = located(hardrows.solve(dumps))
        finally:
            hardrows.factory_words = real
        self.assertEqual(even, flipped)


class TestRirCodec(unittest.TestCase):
    def test_the_two_anchors_decode_as_the_burn_reports(self):
        self.assertEqual(rir.decode(0x0015), {"enabled": True, "row": 0, "bit": 5,
                                             "value": 0, "abs_bit": 5})
        self.assertEqual(rir.decode(0x0095), {"enabled": True, "row": 1, "bit": 5,
                                             "value": 0, "abs_bit": 37})
        # the factory record read off this bench's 8 GB card: force 1, row18 bit8
        self.assertEqual(rir.decode(0x0923), {"enabled": True, "row": 18,
                                             "bit": 8, "value": 1,
                                             "abs_bit": 584})

    def test_a_disabled_record_is_inert_but_still_readable(self):
        self.assertIsNone(rir.live_record(0x0922))
        d = rir.decode(0x0922)
        self.assertFalse(d["enabled"])
        self.assertEqual((d["row"], d["bit"], d["value"]), (18, 8, 1))

    def test_encoding_round_trips_and_the_address_space_fits(self):
        for row, bit, value in ((0, 0, 0), (26, 21, 0), (511, 31, 1)):
            rec = rir.encode(row, bit, value)
            self.assertLessEqual(rec, 0xffff)     # 14 address + 1 value + enable
            d = rir.decode(rec)
            self.assertEqual((d["row"], d["bit"], d["value"]), (row, bit, value))
            self.assertEqual(rir.split_abs(d["abs_bit"]), (row, bit))

    def test_an_address_beyond_the_array_is_refused(self):
        with self.assertRaises(rir.BadRirRecord):
            rir.encode(512, 0, 0)        # 14 bits hold 512 rows exactly
        with self.assertRaises(rir.BadRirRecord):
            rir.encode(26, 16, 2)        # a record forces 0 or 1

    def test_records_for_the_dead_fbps_of_a_64_gib_card(self):
        recs = hardrows.rir_to_free(0x2d)          # dead FBP 0, 2, 3, 5
        self.assertEqual([r["record"] for r in recs],
                         [0x0d41, 0x0d49, 0x0d4d, 0x0d55])
        self.assertEqual([(r["row"], r["bit"]) for r in recs],
                         [(26, 16), (26, 18), (26, 19), (26, 21)])


class TestRirSlots(unittest.TestCase):
    def setUp(self):
        self.inv = rir.inventory(rir.MEASURED_8GB)

    def test_the_measured_8gb_card_has_no_room_for_a_record(self):
        self.assertTrue(self.inv["complete"])
        self.assertEqual(self.inv["free"], 0)
        self.assertEqual(self.inv["spent"], 15)
        self.assertEqual(len(self.inv["live"]), 1)
        live = self.inv["live"][0]
        self.assertEqual(live["value"], 0x0923)
        self.assertEqual(live["record"]["row"], 18)     # the redundancy row

    def test_every_record_the_ceiling_wants_has_nowhere_to_go(self):
        for r in hardrows.rir_to_free(0x2d):
            self.assertEqual(rir.burnable(rir.MEASURED_8GB, r["record"]), [])
        self.assertEqual(rir.burnable([0] * 8, 0x0d41), list(range(16)))
        # row 0 half-burnt to 0x0001 (the enable bit, which the record has too):
        # still usable, and the blank high halves of rows 1-7 are too. Their spent
        # high halves are not, which is why the odd slots above 1 are absent.
        self.assertEqual(rir.burnable([0x0001] + [0xffff0000] * 7, 0x0d41),
                         [0, 1, 2, 4, 6, 8, 10, 12, 14])

    def test_a_partially_burnt_slot_only_takes_a_superset(self):
        self.assertIn(0, rir.burnable([0x0001], 0x0d41))
        # burnt 0x0002, the "force 1" bit, which 0x0d41 (force 0) does not carry:
        # that cell cannot be un-burnt, so slot 0 is out. Slot 1, the blank high
        # half of the same row, is the only place left in a one-row readout.
        self.assertEqual(rir.burnable([0x0002], 0x0d41), [1])

    def test_slot_numbering_is_documented_as_a_convention(self):
        self.assertEqual(rir.slot(7, 0), 14)       # low half = even slot
        self.assertEqual(rir.slot(7, 1), 15)       # the external dump's number
        with self.assertRaises(rir.BadRirRecord):
            rir.slot(8, 0)                         # the macro is 8 rows

    def test_an_unread_row_is_not_counted_as_a_blank_one(self):
        inv = rir.inventory([0xffffffff, 0xffff0923])
        self.assertFalse(inv["complete"])
        self.assertEqual(inv["missing"], [2, 3, 4, 5, 6, 7])
        self.assertEqual(inv["free"], 0)

    def test_per_sku_the_route_is_stated_with_how_firmly_it_is_known(self):
        self.assertEqual(rir.route("8gb")["free"], 0)
        self.assertTrue(rir.route("8gb")["known"])
        self.assertIsNone(rir.route("10gb")["free"])   # never read on this bench
        self.assertFalse(rir.route("10gb")["known"])
        self.assertFalse(rir.route("40gb")["known"])   # unknown SKU


class TestRirParsing(unittest.TestCase):
    def test_bare_words_in_order(self):
        self.assertEqual(rir.parse_words(" ".join("0x%08x" % w
                                                 for w in rir.MEASURED_8GB)),
                         dict((i, v) for i, v in enumerate(rir.MEASURED_8GB)))

    def test_row_word_lines_as_external_tools_print_them(self):
        text = "\n".join(["RIR row %d: 0x%08x" % (r, w)
                          for r, w in enumerate(rir.MEASURED_8GB) if r < 7]
                         + ["7 0xffff0923   # the factory patch"])
        self.assertEqual(rir.parse_words(text)[7], 0xffff0923)
        self.assertEqual(len(rir.parse_words(text)), 8)

    def test_rejects_an_incomplete_or_nonsense_readout(self):
        for text in ("", "RIR row 9: 0x1", "slot14 0x0923", "0 0x1 0x2",
                     "the macro is 8 rows long"):
            with self.assertRaises(rir.BadRirRecord, msg=text):
                rir.parse_words(text)

    def test_rejects_a_row_listed_twice_with_two_values(self):
        with self.assertRaises(rir.BadRirRecord):
            rir.parse_words("7 0xffff0923\nRIR row 7: 0x00000000")


class TestCeilingInAnalysis(unittest.TestCase):
    def setUp(self):
        self.dump = load_dump(SAMPLE)

    def test_the_ceiling_is_the_defective_mask_not_the_target(self):
        hard = analyze(self.dump)["hard"]
        self.assertEqual(hard["dead"]["mask"], 0x840)
        self.assertEqual(hard["ceiling_fbp"], 10)
        self.assertEqual(hard["ceiling_gib"], 80)
        # fbp_disable reads 0x852 today: two partitions are off that are not dead,
        # so those two are what an FF record can still give back to this card.
        self.assertEqual(hard["ff_headroom_gib"], 16)
        self.assertEqual(hard["located"], "row26 bit16..27")
        self.assertEqual([r["record"] for r in hard["records"]],
                         [0x0d59, 0x0d6d])          # FBP 6 and FBP 11

    def test_the_vendored_sample_does_not_cross_check(self):
        """Its fbio/rop_l2 words are not in the pair encoding the real cards carry
        (0x0, and a flat 0x840), so those two cannot corroborate the ceiling."""
        hard = analyze(self.dump)["hard"]
        self.assertFalse(hard["dead"]["consistent"])
        self.assertEqual([k for k, _set in hard["dead"]["mismatch"]],
                         ["fbio_defective", "rop_l2_defective"])
        rc, out = quiet(["report", SAMPLE])
        self.assertEqual(rc, 0)
        self.assertIn("CROSS-CHECK FAILS", out)

    def test_records_and_slot_situation_are_reported_together(self):
        rc, out = quiet(["report", SAMPLE])
        self.assertIn("[Hard rows", out)
        self.assertIn("hard ceiling: 80 GiB", out)
        self.assertIn("0 of 16 slots free", out)     # the reason, not just a no
        self.assertIn("0x0d59", out)                 # FBP 6's record

    def test_a_snapshot_without_the_word_says_unknown(self):
        dump = copy.deepcopy(self.dump)
        for key in list(dump["option_words"]):
            if key.endswith("_defective"):
                del dump["option_words"][key]
        hard = analyze(dump)["hard"]
        self.assertFalse(hard["read"])
        self.assertIsNone(hard["ceiling_gib"])
        path = os.path.join(tempfile.mkdtemp(prefix="ffcalc-hard-"), "no.json")
        with open(path, "w") as f:
            json.dump(dump, f)
        rc, out = quiet(["report", path])
        self.assertEqual(rc, 0)
        self.assertIn("the ceiling is unknown, not absent", out)


class TestCliVerbs(unittest.TestCase):
    def setUp(self):
        self.out = os.path.join(tempfile.mkdtemp(prefix="ffcalc-hard-"),
                                "plan.json")

    def test_gen_refuses_a_target_below_the_hard_floor(self):
        rc, out = quiet(["gen", SAMPLE, "-o", self.out,
                         "--set", "fbp_disable=0x0"])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", out)
        self.assertIn("--allow-hard-floor", out)
        self.assertFalse(os.path.exists(self.out))

    def test_the_flag_lets_it_through_with_the_true_capacity(self):
        rc, out = quiet(["gen", SAMPLE, "-o", self.out,
                         "--set", "fbp_disable=0x0", "--allow-hard-floor"])
        self.assertEqual(rc, 0)
        self.assertIn("hard ceiling: 80 GiB", out)   # not 96: two FBP are dead
        obj = json.load(open(self.out))
        self.assertEqual(obj["analysis"]["fb_gib"], 80)
        self.assertEqual(obj["analysis"]["hard"]["beyond"], ["fbp_disable"])

    def test_defmap_needs_a_snapshot_that_carries_the_hard_rows(self):
        """The vendored sample was hand-made from a blog post's register values: it
        has FF pool rows and no factory rows, so it cannot discriminate anything,
        and `defmap` says that rather than concluding the fields are absent."""
        rc, out = quiet(["defmap", SAMPLE])
        self.assertEqual(rc, 2)
        self.assertIn("no snapshot in this run carries factory rows", out)

    def test_defmap_solves_the_table_from_snapshots_that_do(self):
        out_dir = tempfile.mkdtemp(prefix="ffcalc-defmap-")
        paths = []
        for c in (card(0x2d, fbp_disable=0x2f), card(0x840, fbp_disable=0x852),
                  card(0x0a5, fbp_disable=0xfff)):
            path = os.path.join(out_dir, c["source"] + ".json")
            with open(path, "w") as f:
                json.dump(c, f)
            paths.append(path)
        rc, out = quiet(["defmap"] + paths)
        self.assertEqual(rc, 0)
        self.assertIn("3 snapshot(s)", out)
        self.assertIn("fbp_defective", out)
        self.assertIn("row26 bit16..27", out)
        self.assertIn("no sample in this run discriminates", out)
        # a *_disable field is only reported when asked for, and it has no home
        # here: three dies reading three different values leave no offset standing
        rc, out = quiet(["defmap"] + paths +
                        ["--keys", "fbp_defective,fbp_disable"])
        self.assertEqual(rc, 0)
        self.assertIn("not in the hard rows at any offset", out)
        self.assertIn("its home is the FF pool", out)
        self.assertIn("row26 bit16..27", out)
        # a words-only readout added to the run is reported and not used
        path = os.path.join(out_dir, "words-only.json")
        with open(path, "w") as f:
            json.dump(dict(card(0x2d), ff_rows={}, source="words-only"), f)
        rc, out = quiet(["defmap"] + paths + [path])
        self.assertEqual(rc, 0)
        self.assertIn("no factory rows", out)
        self.assertIn("take no part in the solve", out)
        self.assertIn("words-only", out)
        self.assertIn("row26 bit16..27", out)      # the other three still solve

    def test_a_stock_ff_word_tied_to_the_defective_window_is_explained(self):
        """Without a pin, a factory card's `fbp_disable` reads exactly what
        `fbp_defective` reads, so the hard-row window fits it too. `defmap` has to
        say that in words -- including the name of the word that owns the window,
        even when it was not asked for in this run -- rather than reporting the FF
        pool as if it were a second claim on the same bits."""
        out_dir = tempfile.mkdtemp(prefix="ffcalc-defmap-")
        paths = []
        for mask in (0x2d, 0x840, 0x0a5):
            path = os.path.join(out_dir, "card-%x.json" % mask)
            with open(path, "w") as f:
                json.dump(card(mask), f)       # fbp_disable defaults to defective
            paths.append(path)
        rc, out = quiet(["defmap"] + paths + ["--keys", "fbp_disable"])
        self.assertEqual(rc, 0)
        self.assertIn("not a hard-row field", out)
        self.assertIn("fbp_defective", out)
        # and the pin does separate them: the same run with a pre-burn value
        rc, out = quiet(["defmap"] + paths +
                        ["--keys", "fbp_disable", "--pin",
                         "card-840:fbp_disable=0x852"])
        self.assertEqual(rc, 0)
        self.assertIn("not in the hard rows at any offset", out)

    def test_defmap_reports_a_pin_naming_a_card_it_cannot_place(self):
        out_dir = tempfile.mkdtemp(prefix="ffcalc-defmap-")
        path = os.path.join(out_dir, "a.json")
        with open(path, "w") as f:
            json.dump(card(0x840), f)
        rc, out = quiet(["defmap", path, "--pin", "nosuch:fbp_disable=0x852"])
        self.assertEqual(rc, 2)
        self.assertIn("FAIL:", out)
        self.assertIn("no card whose factory rows are in this sample", out)

    def test_rir_decodes_a_captured_readout_without_touching_a_card(self):
        rc, out = quiet(["rir"] + ["0x%08x" % w for w in rir.MEASURED_8GB]
                        + ["--dump", SAMPLE])
        self.assertEqual(rc, 0)
        self.assertIn("free (blank) slots: 0 of 16", out)
        self.assertIn("force 1 @ row18 bit8", out)
        self.assertIn("0x0d59", out)                 # what this card would need
        self.assertIn("half order is not settled", out)

    def test_rir_refuses_a_half_read_dump_unless_told_otherwise(self):
        rc, out = quiet(["rir", "7 0xffff0923"])
        self.assertEqual(rc, 2)
        self.assertIn("an unread row is not a blank one", out)
        rc, out = quiet(["rir", "7 0xffff0923", "--partial"])
        self.assertEqual(rc, 0)
        self.assertIn("counted, but only 2 of 16 slots were read", out)


class TestNothingWrites(unittest.TestCase):
    """RIR is knowledge here, not a feature. Reaching the macro means writing
    FUSEDEBUG, and the采集 promise is that the readout path writes nothing."""

    def test_ffmacro_never_writes_the_debugctrl_or_the_rwl_bit(self):
        import ffcalc.ffmacro as ffmacro
        self.assertEqual(ffmacro.DEBUGCTRL_RWL, 0x2)     # documented, never set
        with open(ffmacro.__file__, encoding="utf-8") as f:
            source = f.read()
        for write in ("wr(FUSEDEBUG", "wr(FUSE_DEBUGCTRL", "w32(FUSEDEBUG",
                      "wr32(FUSEDEBUG", "DEBUGCTRL_RWL)"):
            self.assertNotIn(write, source)


if __name__ == "__main__":
    unittest.main()
