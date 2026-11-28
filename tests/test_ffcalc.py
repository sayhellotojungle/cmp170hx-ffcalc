import contextlib
import functools
import glob
import io
import json
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

from ffcalc import (capture, cli, ffmacro, ffrecord, ffrows, hardrows, ingest,
                    rir)
from ffcalc import dump as snapshot
from ffcalc.analyze import analyze
from ffcalc.dump import load_dump
from ffcalc.generate import plan
from ffcalc.optionmap import (BY_KEY, ENGINE_KEYS, ENGINE_PAIR_KEYS, FIELDS)
from ffcalc.simulate import simulate


def quiet(argv):
    """Run one CLI command, returning (rc, stdout) -- failure messages are
    part of the contract, so tests assert on them."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(argv))
    return rc, buf.getvalue()


def write_rows_file(dump, path):
    with open(path, "w") as f:
        f.write("# physical FF row readout (CMD_READ), unburnt = 0\n")
        for r, v in sorted(dump["ff_rows"].items()):
            r = int(r, 0) if isinstance(r, str) else r
            f.write("%d %s\n" % (r, v))
    return path

SAMPLE = os.path.join(os.path.dirname(__file__), "..",
                      "samples", "blog_card_example.json")


class TestCodec(unittest.TestCase):
    def test_blog_example(self):
        d = ffrecord.decode(0x0fe805c1)
        self.assertEqual(d, {"chain": 1, "block": 23, "data": 0x0fe8})
        self.assertEqual(ffrecord.encode(1, 23, 0x0fe8), 0x0fe805c1)

    def test_replay_later_wins(self):
        rows = {222: ffrecord.encode(1, 5, 0xffff),
                223: ffrecord.encode(1, 5, 0x0f0f)}
        blocks = ffrecord.replay(rows)
        self.assertEqual(blocks[1][5], 0x0f0f)

    def test_iff_rejected(self):
        with self.assertRaises(ValueError):
            ffrecord.decode(0x12340021)


class TestDump(unittest.TestCase):
    def setUp(self):
        self.dump = load_dump(SAMPLE)

    def test_potential(self):
        rep = analyze(self.dump)
        self.assertEqual(rep["targets"]["fbp_disable"], 0x840)
        self.assertEqual(rep["usable_fbp"], 10)
        self.assertEqual(rep["fb_gib"], 80)
        self.assertEqual(rep["sm_count"], 112)
        self.assertTrue(rep["nvlink_ok"])
        self.assertTrue(rep["pcie_gen4_ok"])

    def test_defective_not_subset_warns(self):
        rep = analyze(self.dump, {"fbp_disable": 0x0})
        self.assertTrue(any("does not contain defective" in w
                            for w in rep["warnings"]))

    def test_gen_sim_roundtrip(self):
        obj = plan(self.dump)
        res = simulate(self.dump, obj)
        self.assertEqual(res["errors"], [])
        changed = {r["key"] for r in res["table"] if r["changed"]}
        self.assertIn("fbp_disable", changed)
        self.assertIn("tpc_gpc0_disable", changed)
        self.assertNotIn("pes_disable", changed)

    def test_block_merge_preserves_neighbors(self):
        obj = plan(self.dump)
        blk19 = [r for r in obj["burn"] if r["block"] == 19]
        self.assertEqual(len(blk19), 1)
        self.assertEqual(int(blk19[0]["new"], 0), 0x4200)

    def test_noop_fields_produce_no_rows(self):
        obj = plan(self.dump, only=["fbio_disable"])
        self.assertEqual(obj["burn"], [])

    def test_skip_and_only(self):
        obj = plan(self.dump, only=["fbp_disable"])
        self.assertTrue(all("fbp_disable" in r["fields"]
                            for r in obj["burn"]))
        obj2 = plan(self.dump, skip=["fbp_disable"])
        self.assertFalse(any("fbp_disable" in r["fields"]
                             for r in obj2["burn"]))

    def test_default_plan_burns_the_media_bits_too(self):
        """The media-engine floorsweep bits are part of the default plan now: the
        clear is measured on our own card, and taking it in this round is two rows
        and one write-window trip cheaper than a later add-on round. The records
        below are that plan -- the historical 12 plus chain1 blk8 (NVDEC), blk9
        (bit146) and blk23's third field (bit371)."""
        obj = plan(self.dump)
        self.assertEqual(
            [(r["row"], r["chain"], r["block"], r["old"], r["new"],
              r["record"]) for r in obj["burn"]],
            [(241, 1, 6, "0x6000", "0x0000", "0x00000181"),
             (242, 1, 7, "0x0198", "0x0180", "0x018001c1"),
             (243, 1, 8, "0xf806", "0x0006", "0x00060201"),
             (244, 1, 9, "0x0004", "0x0000", "0x00000241"),
             (245, 1, 12, "0x0606", "0x0204", "0x02040301"),
             (246, 1, 13, "0x0707", "0x4081", "0x40810341"),
             (247, 1, 14, "0x8787", "0x1020", "0x10200381"),
             (248, 1, 15, "0x07c7", "0x0008", "0x000803c1"),
             (249, 1, 19, "0x4290", "0x4200", "0x420004c1"),
             (250, 1, 20, "0x0620", "0x0420", "0x04200501"),
             (251, 1, 23, "0x002f", "0x0000", "0x000005c1"),
             (252, 2, 39, "0x0002", "0x0000", "0x000009c2"),
             (253, 2, 47, "0x0008", "0x0000", "0x00000bc2"),
             (254, 2, 49, "0x00c0", "0x0000", "0x00000c42")])
        for key in ("pcie_devida", "pcie_devidb", "magic_d"):
            self.assertNotIn(key, obj["targets"])
        for key in ENGINE_KEYS:
            self.assertEqual(obj["targets"][key], "0x0")

    def test_no_mig_is_the_plan_before_the_engine_bits_were_default(self):
        """The opt-out has to reproduce the historical plan exactly: the same 12
        records this tool emitted before the media bits moved into the default,
        and no engine field anywhere in it. What moved is only the sample's FF
        *data*: it now also carries the factory media-engine bits, including one
        more burnt record (chain1 blk9 = `0x00040241`, the stock bit146), which
        shifts every planned row by one."""
        obj = plan(self.dump, mig=False)
        self.assertEqual(
            [(r["row"], r["chain"], r["block"], r["old"], r["new"],
              r["record"]) for r in obj["burn"]],
            [(241, 1, 6, "0x6000", "0x0000", "0x00000181"),
             (242, 1, 7, "0x0198", "0x0180", "0x018001c1"),
             (243, 1, 12, "0x0606", "0x0204", "0x02040301"),
             (244, 1, 13, "0x0707", "0x4081", "0x40810341"),
             (245, 1, 14, "0x8787", "0x1020", "0x10200381"),
             (246, 1, 15, "0x07c7", "0x0008", "0x000803c1"),
             (247, 1, 19, "0x4290", "0x4200", "0x420004c1"),
             (248, 1, 20, "0x0620", "0x0420", "0x04200501"),
             (249, 1, 23, "0x002f", "0x0008", "0x000805c1"),
             (250, 2, 39, "0x0002", "0x0000", "0x000009c2"),
             (251, 2, 47, "0x0008", "0x0000", "0x00000bc2"),
             (252, 2, 49, "0x00c0", "0x0000", "0x00000c42")])
        for key in ("pcie_devida", "pcie_devidb", "magic_d"):
            self.assertNotIn(key, obj["targets"])
        for key in ENGINE_KEYS:
            self.assertNotIn(key, obj["targets"])
            self.assertFalse([r for r in obj["burn"] if key in r["fields"]])
        # blk8 (NVDEC bits) and blk9 (bit146) are left exactly as fused
        self.assertFalse([r for r in obj["burn"] if r["block"] in (8, 9)])
        self.assertFalse([w for w in obj["warnings"]
                          if "FUSED device id" in w or "MAGIC_BITS_D" in w
                          or "NVDEC" in w or "SYS_PIPE" in w])

    def test_rows_after_last_used(self):
        obj = plan(self.dump)
        rows = [r["row"] for r in obj["burn"]]
        self.assertEqual(rows, sorted(rows))
        self.assertTrue(all(r >= 238 for r in rows))

    def test_overrides_flow_into_targets(self):
        """A target above the defective floor is planning material; one *below*
        it is a different failure mode and `plan` says so (see BeyondHardFloor)."""
        obj = plan(self.dump, overrides={"gpc_disable": 0x0})
        self.assertEqual(obj["targets"]["gpc_disable"], "0x0")
        with self.assertRaises(hardrows.BeyondHardFloor):
            plan(self.dump, overrides={"fbp_disable": 0x0})

    def test_a_target_below_the_hard_floor_costs_nothing_and_says_so(self):
        """Clearing the disable bit of a dead FBP moves the shadow registers and
        not the memory, so the potential must stay at the hard ceiling -- the
        claim `--allow-hard-floor` is allowed to make must be the true one."""
        obj = plan(self.dump, overrides={"fbp_disable": 0x0},
                   allow_hard_floor=True)
        self.assertTrue(any("does not contain defective" in w
                            for w in obj["warnings"]))
        self.assertEqual(obj["analysis"]["fb_gib"], 80)   # not 96: 2 FBP are dead
        self.assertEqual(obj["analysis"]["hard"]["beyond"], ["fbp_disable"])
        rep = analyze(self.dump, {"fbp_disable": 0x0})
        self.assertEqual(rep["hard"]["ceiling_gib"], 80)
        self.assertTrue(any("hard ceiling" in w for w in rep["warnings"]))

    def test_plan_refused_without_ff_rows(self):
        for rows in ({}, {222: 0x141, 223: 0x181}):
            dump = dict(self.dump, ff_rows=rows)
            with self.assertRaises(ffrows.FFRowsNotRead):
                plan(dump)

    def test_sim_refused_without_ff_rows(self):
        obj = plan(self.dump)
        res = simulate(dict(self.dump, ff_rows={}), obj)
        self.assertTrue(any("ff rows readout" in e for e in res["errors"]))

    def test_a100_rewrites_fused_devid(self):
        """--a100 burns the device id itself, the way the community conversion
        reports it: chain2 blk37/38 to 0x416a (0x20b5 << 1), and it says what
        that costs."""
        obj = plan(self.dump, a100=True)
        by_block = dict((r["block"], r) for r in obj["burn"]
                        if r["block"] in (37, 38))
        self.assertEqual(sorted(by_block), [37, 38])
        self.assertEqual(int(by_block[37]["new"], 0), 0x416a)
        self.assertEqual(by_block[37]["record"], "0x416a0942")
        self.assertEqual(by_block[37]["fields"], ["pcie_devida"])
        self.assertEqual(int(by_block[38]["new"], 0), 0x416a)
        self.assertEqual(by_block[38]["record"], "0x416a0982")
        self.assertEqual(by_block[38]["fields"], ["pcie_devidb"])
        self.assertEqual(obj["targets"]["pcie_devida"], "0x20b5")
        self.assertEqual(obj["targets"]["pcie_devidb"], "0x20b5")
        warn = " ".join(obj["warnings"])
        for must in ("FUSED device id", "vBIOS", "CH341A", "MMIO"):
            self.assertIn(must, warn)
        # full capability + Gen4 do not need the conversion, so say so
        self.assertIn("do NOT need this", warn)
        res = simulate(self.dump, obj)
        self.assertEqual(res["errors"], [])

    def test_a100_noop_when_devid_already_a100(self):
        rows = dict(self.dump["ff_rows"])
        free = sorted(int(r, 0) if isinstance(r, str) else r
                      for r, v in rows.items()
                      if (v if isinstance(v, int) else int(v, 0)) == 0)
        rows[free[0]] = ffrecord.encode(2, 37, 0x416a)
        rows[free[1]] = ffrecord.encode(2, 38, 0x416a)
        obj = plan(dict(self.dump, ff_rows=rows), a100=True)
        self.assertFalse([r for r in obj["burn"] if r["block"] in (37, 38)])
        self.assertFalse(any("FUSED device id" in w for w in obj["warnings"]))

    def test_devid_untouched_without_a100(self):
        obj = plan(self.dump)
        for key in ("pcie_devida", "pcie_devidb"):
            self.assertNotIn(key, obj["targets"])
        self.assertFalse([r for r in obj["burn"] if r["block"] in (37, 38)])

    def test_a100_old_is_the_factory_record_when_the_readout_covers_it(self):
        """A card fused as CMP carries its own chain-2 record for blocks 37/38,
        and a full readout sees it: then `old` is the id in use, `new` is the
        A100 one, and there is nothing to cross-check."""
        rows = dict(self.dump["ff_rows"])
        free = sorted(int(r, 0) if isinstance(r, str) else r
                      for r, v in rows.items()
                      if (v if isinstance(v, int) else int(v, 0)) == 0)
        rows[free[0]] = ffrecord.encode(2, 37, 0x4184)   # 0x20c2 << 1
        rows[free[1]] = ffrecord.encode(2, 38, 0x4184)
        words = dict(self.dump["option_words"], pcie_devida=0x20C2,
                     pcie_devidb=0x20C2)
        dump = dict(self.dump, ff_rows=rows, option_words=words)
        obj = plan(dump, a100=True)
        olds = dict((r["block"], r["old"]) for r in obj["burn"]
                    if r["block"] in (37, 38))
        self.assertEqual(olds, {37: "0x4184", 38: "0x4184"})
        self.assertFalse([w for w in obj["warnings"]
                          if "fuse register" in w])
        self.assertEqual(simulate(dump, obj)["errors"], [])

    def test_half_an_id_warns(self):
        """One half of the id is not an id: naming it must be deliberate."""
        obj = plan(self.dump, overrides={"pcie_devida": 0x20b5})
        self.assertTrue(any("only pcie_devida is rewritten" in w
                            for w in obj["warnings"]))

    def test_a100_also_declares_full_pcie_capability(self):
        """--a100 takes the whole MAGIC_BITS_D declaration with it: a card that
        pays the vBIOS price to be an A100 should not keep declaring CMP-era
        capability next to an A100 id. The value is a guess, so the plan says
        so and points at the escape hatch."""
        obj = plan(self.dump, a100=True)
        by_block = dict((r["block"], r) for r in obj["burn"]
                        if r["chain"] == 2 and r["block"] in (45, 46, 47))
        self.assertEqual(sorted(by_block), [46, 47])
        self.assertEqual(obj["targets"]["magic_d"], "0x200000")
        # bit21 of the word lives in block46, bit25 in block47
        self.assertEqual(by_block[46]["record"], "0x80000b82")
        self.assertEqual(by_block[46]["fields"], ["magic_d"])
        self.assertEqual(by_block[47]["new"], "0x0000")
        self.assertEqual(sorted(by_block[47]["fields"]),
                         ["magic_d", "pcie_magic_gen4_dis"])
        warn = " ".join(obj["warnings"])
        self.assertIn("capability declaration", warn)
        self.assertIn("EXPERIMENTAL", warn)
        self.assertIn("--skip magic_d", warn)
        self.assertEqual(simulate(self.dump, obj)["errors"], [])
        # and the escape hatch has to work: the conversion report itself
        # leaves this word alone, so --skip magic_d has to leave no trace of it
        obj = plan(self.dump, a100=True, skip=["magic_d"])
        self.assertNotIn("magic_d", obj["targets"])
        self.assertFalse([r for r in obj["burn"] if "magic_d" in r["fields"]])
        self.assertFalse([w for w in obj["warnings"]
                          if "capability declaration" in w])
        self.assertTrue([r for r in obj["burn"] if r["block"] in (37, 38)])
        self.assertEqual(simulate(self.dump, obj)["errors"], [])

    def test_magic_d_whole_word_is_opt_in_and_still_experimental(self):
        """MAGIC_BITS_D is a capability declaration, so it is never a default
        target: --a100 or an explicit --set has to ask for it, and the A100
        value for it remains a guess that keeps warning."""
        obj = plan(self.dump, overrides={"magic_d": 0x00200000})
        b46 = [r for r in obj["burn"] if r["block"] == 46]
        self.assertEqual(len(b46), 1)
        self.assertEqual(int(b46[0]["new"], 0), 0x8000)
        self.assertIn("magic_d", b46[0]["fields"])
        self.assertEqual(obj["targets"]["magic_d"], "0x200000")
        self.assertTrue(any("EXPERIMENTAL" in w for w in obj["warnings"]))
        self.assertTrue(any("capability declaration" in w
                            for w in obj["warnings"]))
        res = simulate(self.dump, obj)
        self.assertEqual(res["errors"], [])

    def test_magic_not_touched_without_a100(self):
        obj = plan(self.dump)
        self.assertNotIn("magic_d", obj["targets"])
        self.assertFalse([r for r in obj["burn"] if r["block"] == 46])


class TestRuntimeState(unittest.TestCase):
    """The runtime half of the picture. Rate, ECC and geometry are registers the
    driver-side unlocker rewrites every boot, not fuse outcomes, so `report`
    keeps them in their own section and never lets a burnt fuse claim them."""

    def setUp(self):
        self.rt = analyze(load_dump(SAMPLE))["runtime"]

    def test_compute_rate_is_runtime_not_fused(self):
        self.assertTrue(self.rt["compute"]["full_rate_now"])
        self.assertEqual(self.rt["compute"]["override"], [0x88888888, 0x8])
        # 0x5 is the 1/32-rate select; ss_dp holds something else, so it is not
        # listed as throttled. The FF pool does not carry any of these words.
        self.assertEqual(self.rt["compute"]["fused_throttled"],
                         ["ss_ffma", "ss_fmla16", "ss_fmla32", "ss_imla0",
                          "ss_imla1", "ss_imla2", "ss_imla3", "ss_imla4"])

    def test_ecc_separates_the_fuse_from_this_boot(self):
        self.assertEqual(self.rt["ecc"]["fused"], 0)      # OPT_ECC_EN: not fused on
        self.assertTrue(self.rt["ecc"]["dram_master"])    # ECC_CTRL bit 0
        self.assertTrue(self.rt["ecc"]["dram_sideband"])  # ECC_CTRL bit 6: full size

    def test_geometry_board_and_macro(self):
        self.assertEqual(self.rt["geometry"]["mmu_lmr"], 0x28B)
        self.assertEqual(self.rt["geometry"]["cstatus_ramamount"], 0x1000)
        self.assertEqual(self.rt["macro"]["iff_window"], (488, 507))
        # the macro words a burn decision rests on, read as a snapshot
        self.assertEqual(self.rt["macro"]["crc"], 0x55)
        self.assertEqual(self.rt["macro"]["efp"], 1)
        self.assertEqual(self.rt["macro"]["wdata_wr_secure"], 1)
        self.assertEqual(self.rt["macro"]["priv_mask"], 0xFFFFFFFF)
        self.assertEqual(self.rt["board"]["hbm_mhz"], 1728)
        self.assertIn("busy coefficient", self.rt["board"]["hbm_state"])
        self.assertTrue(self.rt["board"]["gfw_clean"])

    def test_hbm_coeff_is_power_management_not_a_fuse_state(self):
        """The same card reads 0x14001 under traffic and 0x11401 idle; only the
        MHz figure is worth printing, and it has to follow the word."""
        dump = load_dump(SAMPLE)
        dump["info"]["hbm_pll_coeff"] = 0x00011401
        board = analyze(dump)["runtime"]["board"]
        self.assertEqual(board["hbm_mhz"], 540)
        self.assertIn("downclocked", board["hbm_state"])

    def test_report_prints_the_runtime_section(self):
        rc, out = quiet(["report", SAMPLE])
        self.assertEqual(rc, 0)
        for needle in ("[Runtime state", "no throttle bit set",
                       "IFF window rows 488-507", "1728 MHz",
                       "speed selects reading 0x"):
            self.assertIn(needle, out)

    def test_a_snapshot_without_the_words_says_so(self):
        dump = load_dump(SAMPLE)
        for key in ("feat_readout_1", "feat_ovr_sm_spd_0", "feat_ovr_sm_spd_1",
                    "ss_dp", "ss_ffma", "ss_fmla16", "ss_fmla32", "ss_imla0",
                    "ss_imla1", "ss_imla2", "ss_imla3", "ss_imla4",
                    "ecc_ctrl", "opt_ecc_en", "iff_record", "hbm_pll_coeff",
                    "crc_status", "efp_status", "wdata_wr_secure",
                    "priv_mask", "plm_fuse_wdata", "dsov_status",
                    "fuse_debugctrl", "dis_program"):
            dump["info"].pop(key)
        rt = analyze(dump)["runtime"]
        self.assertNotIn("compute", rt)
        self.assertNotIn("ecc", rt)
        self.assertIsNone(rt["macro"]["iff_window"])
        self.assertIsNone(rt["macro"]["crc"])
        self.assertIsNone(rt["board"]["hbm_mhz"])
        # A missing word must not be printed as 0: an all-zero CRC line reads
        # like a dead chain, and DSOV/PRIV_MASK at 0 read like a burn window.
        snap = tempfile.mktemp(suffix=".json")
        with open(snap, "w") as f:
            json.dump(dump, f)
        self.addCleanup(os.remove, snap)
        rc, out = quiet(["report", snap])
        self.assertEqual(rc, 0)
        self.assertIn("CRC=not read", out)
        self.assertIn("PRIV_MASK=not read", out)
        self.assertNotIn("CRC=0x00 ", out)


class TestMediaUnlock(unittest.TestCase):
    """`--mig`: the Chain-1 floorsweep bits on NVDEC/NVJPG/OFA.

    The sample card is fused the way a stock CMP 170HX is (all 5 NVDEC bits
    set, both NVJPG/OFA bits set), so the record values asserted below are the
    ones a real readout replays -- and the pair bit nobody has attributed yet
    always has to travel with its partner.
    """

    def setUp(self):
        self.dump = load_dump(SAMPLE)

    def _by_block(self, obj):
        return dict((r["block"], r) for r in obj["burn"])

    def test_mig_plan_clears_the_three_engine_bits(self):
        obj = plan(self.dump, mig=True)
        by_block = self._by_block(obj)
        # NVDEC = blk8[15:11]: 0xf806 -> 0x0006, with the block's fbpa bits kept
        self.assertEqual(by_block[8]["record"], "0x00060201")
        self.assertEqual(by_block[8]["fields"], ["nvdec_disable"])
        # bit146 sits alone in blk9, so it is the row that always has to be new
        self.assertEqual(by_block[9]["record"], "0x00000241")
        self.assertEqual(by_block[9]["fields"], ["nvjpg_ofa_disable_bit146"])
        # bit371 rides in blk23 with nvlink and sys_pipe: one record, 3 fields
        self.assertEqual(by_block[23]["record"], "0x000005c1")
        self.assertEqual(set(by_block[23]["fields"]),
                         set(("nvjpg_ofa_disable_bit371", "nvlink_disable",
                              "sys_pipe_disable")))
        for key in ENGINE_KEYS:
            self.assertEqual(obj["targets"][key], "0x0")
        res = simulate(self.dump, obj)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["pending"], [])

    def test_mig_plan_says_what_the_bits_are_and_are_not(self):
        warn = " ".join(plan(self.dump, mig=True)["warnings"])
        # what the bits are, and why only an FF record moves them
        for must in ("floorsweep", "CTRL_OPT_", "FUSE_EN_SW_OVERRIDE",
                     "STATUS_OPT_*"):
            self.assertIn(must, warn)
        # what comes out of burning them, including what stays impossible
        for must in ("5x NVDEC", "no AV1", "NVJPG0", "OFA0", "8g/4g/2g/1g"):
            self.assertIn(must, warn)
        # the inert route is named as *this* route only: the 0x8238xx island is
        # live, and the warning may not blur the two
        self.assertIn("feature-override island", warn)
        # what this does not prove, and the MIG-mode reboot trap
        for must in ("does NOT prove", "bit146/bit371", "ECC", "REBOOT"):
            self.assertIn(must, warn)

    def test_mig_row_cost_is_measured_against_this_plan(self):
        """Not "one row" in the abstract: blocks this plan already rewrites are
        free, and the warning names the ones that were not."""
        warn = " ".join(plan(self.dump, mig=True)["warnings"])
        # On the sample card gpc/fbpa need no chain1 blk8 rewrite, so NVDEC and
        # bit146 each need a row while bit371 rides along in blk23.
        self.assertIn("2 extra FF row(s) (chain1 blk8, chain1 blk9)", warn)
        self.assertIn("the rest rode along in chain1 blk23", warn)

    def test_a100_and_mig_compose_in_one_round(self):
        """`--a100 --mig` is the whole unlock as one plan. The two features
        touch different chains, so neither moves the other's records, and
        --a100 does not make the engines cost an extra row (the baseline that
        count is measured against already contains the identity rows). Chain 1
        is emitted before chain 2, which is also the order worth executing in:
        a burn interrupted between the two leaves a card that still boots as a
        CMP 170HX with its engines unlocked, not one waiting on a vBIOS flash.
        """
        obj = plan(self.dump, a100=True, mig=True)
        both, only_mig = self._by_block(obj), self._by_block(
            plan(self.dump, mig=True))
        only_a100 = self._by_block(plan(self.dump, a100=True))
        for blk in (8, 9, 23):          # engine blocks: --mig's values exactly
            self.assertEqual(both[blk]["record"], only_mig[blk]["record"])
            self.assertEqual(sorted(both[blk]["fields"]),
                             sorted(only_mig[blk]["fields"]))
        for blk in (37, 38, 46, 47):    # identity/capability: --a100's exactly
            self.assertEqual(both[blk]["record"], only_a100[blk]["record"])
            self.assertEqual(sorted(both[blk]["fields"]),
                             sorted(only_a100[blk]["fields"]))
        rows = [(r["row"], r["chain"], set(r["fields"])) for r in obj["burn"]]
        self.assertEqual([r[0] for r in rows],
                         sorted(r[0] for r in rows))   # burntool follows order
        engine = [r[0] for r in rows if set(ENGINE_KEYS) & r[2]]
        ident = [r[0] for r in rows if {"pcie_devida", "pcie_devidb"} & r[2]]
        self.assertLess(max(engine), min(ident))       # chain 1 sorts first
        cost = [w for w in obj["warnings"] if "extra FF row" in w]
        self.assertEqual(cost, [w for w in plan(self.dump, mig=True)["warnings"]
                               if "extra FF row" in w])
        self.assertEqual(simulate(self.dump, obj)["errors"], [])
        # and planning a card never writes back into the caller's overrides
        mine = {}
        plan(self.dump, overrides=mine, a100=True, mig=True)
        self.assertEqual(mine, {})

    def test_row_cost_on_a_card_whose_unlock_is_already_burnt(self):
        """Both of our cards are in this state (measured 2026-11-25): the
        capability records are burnt, so nothing else rewrites blk8/blk23 any
        more and every engine bit has to carry a row of its own. Pretend the
        sample card's capability records have been burnt -- the media bits left
        out, which is the state this warning exists for -- and ask again."""
        rows = dict(self.dump["ff_rows"])
        for rec in plan(self.dump, mig=False)["burn"]:
            # load_dump normalises rows to {int: int}, so the fake burn has to
            # write the same shape it would read back.
            rows[rec["row"]] = int(rec["record"], 16)
        burnt = dict(self.dump, ff_rows=rows)
        obj = plan(burnt)
        self.assertEqual([r["record"] for r in obj["burn"]],
                         ["0x00060201", "0x00000241", "0x000005c1"])
        warn = " ".join(obj["warnings"])
        self.assertIn("3 extra FF row(s) (chain1 blk8, chain1 blk9, "
                      "chain1 blk23)", warn)
        self.assertIn("each engine bit carries its own row", warn)
        self.assertEqual(simulate(burnt, obj)["errors"], [])

    def test_mig_analysis_reports_the_engines(self):
        m = plan(self.dump, mig=True)["analysis"]["media"]
        self.assertEqual(m["mig"]["after"], 8)
        by_label = dict((e["label"], e) for e in m["engines"])
        self.assertEqual((by_label["NVDEC"]["now"], by_label["NVDEC"]["after"]),
                         (0, 5))
        for label in ("NVJPG", "OFA"):
            self.assertEqual((by_label[label]["now"], by_label[label]["after"]),
                             (0, 1))
            self.assertEqual(by_label[label]["defective"], 0)
        # the plan file carries the evidence with it, so an operator reading
        # only the plan can see the override route was dead
        self.assertTrue(m["sw_override"]["read"] and m["sw_override"]["dead"])
        self.assertTrue(m["display"]["read"])

    def test_mig_without_the_pair_readout_refuses_the_pair(self):
        """No OPT_*/STATUS_OPT_* for an engine means no evidence the unit is
        there, so no irreversible row is spent on it -- and the fields whose
        evidence *was* read still plan normally."""
        info = dict((k, v) for k, v in self.dump["info"].items()
                    if k not in ("nvjpg_disable", "status_nvjpg",
                                 "ofa_disable", "status_ofa"))
        obj = plan(dict(self.dump, info=info), mig=True)
        for key in ENGINE_PAIR_KEYS:
            self.assertNotIn(key, obj["targets"])
        self.assertIn("nvdec_disable", obj["targets"])      # its pair was read
        by_block = self._by_block(obj)
        self.assertEqual(by_block[8]["fields"], ["nvdec_disable"])
        self.assertNotIn(9, by_block)
        # blk23 is still rewritten for nvlink/sys_pipe, and with bit371 not
        # planned that bit stays fused instead of being cleared as collateral
        self.assertEqual(int(by_block[23]["new"], 0), 0x0008)
        warn = " ".join(obj["warnings"])
        self.assertIn("never read", warn)
        self.assertIn("re-capture", warn)

    def test_mig_warns_when_the_status_words_show_a_defect(self):
        """STATUS_OPT has a bit OPT_DISABLE does not = that unit is physically
        gone, so the unlock gets that engine one unit shorter."""
        info = dict(self.dump["info"], nvdec_disable=0x0f, status_nvdec=0x1f)
        rep = analyze(dict(self.dump, info=info), {"nvdec_disable": 0})
        self.assertTrue(any("physically missing" in w for w in rep["warnings"]),
                        rep["warnings"])
        nvdec = [e for e in rep["media"]["engines"]
                 if e["label"] == "NVDEC"][0]
        self.assertEqual(nvdec["defective"], 0x10)
        self.assertEqual(nvdec["after"], 4)     # 5 minus the one that is gone

    def test_mig_report_prints_the_engine_section(self):
        """`report` shows the engines, what the readout proves about them, and
        what the default plan makes them -- which is unlocked, until `--no-mig`
        says otherwise, in which case the section has to say so out loud."""
        rc, out = quiet(["report", SAMPLE])
        self.assertEqual(rc, 0)
        self.assertIn("[MIG / media engines]", out)
        self.assertIn("NVDEC  0/5 live now -> 5/5 after this plan", out)
        self.assertIn("floorsweep override (CTRL_OPT_*): INERT", out)
        self.assertIn("DISPLAY/DISP_HEAD", out)
        rc, out = quiet(["report", SAMPLE, "--no-mig"])
        self.assertEqual(rc, 0)
        self.assertIn("NVDEC  0/5 live now -> unchanged", out)
        self.assertIn("stays fused: --no-mig", out)
        self.assertIn("`--no-mig` to leave them fused", out)

    def test_a_planned_field_at_its_factory_value_clears_nothing(self):
        """`--set nvdec_disable=0x1f` puts the field in the plan without clearing
        it, and being in the plan is not the same as being cleared: the section
        may promise decoders only where the target is actually 0. The engines the
        default does clear are unaffected by one field's non-zero target."""
        rep = analyze(self.dump, {"nvdec_disable": 0x1f}, mig_keys=ENGINE_KEYS)
        by_label = dict((e["label"], e) for e in rep["media"]["engines"])
        self.assertTrue(by_label["NVDEC"]["planned"])
        self.assertFalse(by_label["NVDEC"]["cleared"])
        self.assertEqual(by_label["NVDEC"]["after"], 0)
        self.assertTrue(by_label["OFA"]["cleared"])
        self.assertEqual(by_label["OFA"]["after"], 1)
        rc, out = quiet(["report", SAMPLE, "--set", "nvdec_disable=0x1f"])
        self.assertEqual(rc, 0)
        self.assertIn("NVDEC  0/5 live now -> unchanged", out)
        self.assertIn("a --set value not 0", out)
        self.assertIn("NVJPG  0/1 live now -> 1/1 after this plan", out)

    def test_gen_mig_cli(self):
        """`--mig` on by default, `--no-mig` taking the three engine records back
        out of the plan, and `--mig` spelled out still accepted."""
        tmp = tempfile.mkdtemp(prefix="ffcalc-mig-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "plan.json")
        for flags in ([], ["--mig"]):
            rc, out = quiet(["gen", SAMPLE, "-o", path] + flags)
            self.assertEqual(rc, 0)
            with open(path) as f:
                obj = json.load(f)
            self.assertIn("0x00000241", [r["record"] for r in obj["burn"]])
            self.assertIn("row cost of the media-engine bits", out)
        rc, out = quiet(["gen", SAMPLE, "-o", path, "--no-mig"])
        self.assertEqual(rc, 0)
        with open(path) as f:
            obj = json.load(f)
        self.assertNotIn("0x00000241", [r["record"] for r in obj["burn"]])
        for key in ENGINE_KEYS:
            self.assertNotIn(key, obj["targets"])
        self.assertNotIn("row cost of the media-engine bits", out)

    def test_engine_proof_words_exist_in_the_readout_map(self):
        """A proof key the probe does not read could never be satisfied, which
        would make its field silently unplannable forever."""
        info_map = ingest.load_map()["info"]
        for field in FIELDS:
            for key in field.proof_keys:
                self.assertIn(key, info_map,
                              "%s: proof key %s is not read by the probe"
                              % (field.key, key))


class TestIdentitySurface(unittest.TestCase):
    """The identity rewrite is the one burn that changes what the card *is*, so
    every surface that ends in a burn says so: `report` shows the id the chip
    carries and what a plan would make it carry, and the plan/burn path repeats
    the cost (vBIOS flash, CH341A as the only way back)."""

    def setUp(self):
        self.dump = load_dump(SAMPLE)
        self.tmp = tempfile.mkdtemp(prefix="ffcalc-ident-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.words = dict(self.dump["option_words"])
        self.words["pcie_devida"] = 0x20C2
        self.words["pcie_devidb"] = 0x20C2
        self.card = os.path.join(self.tmp, "card.json")
        with open(self.card, "w") as f:
            json.dump({"sku": "8gb", "option_words": self.words,
                       "ff_rows": dict(("%d" % r, v) for r, v
                                       in self.dump["ff_rows"].items())}, f)

    def test_report_shows_the_fused_id_and_no_change(self):
        rc, out = quiet(["report", self.card])
        self.assertEqual(rc, 0)
        self.assertIn("[Fused device id]", out)
        self.assertIn("devida=0x20c2  devidb=0x20c2", out)
        self.assertIn("no id change planned", out)

    def test_report_of_a_conversion_names_the_price(self):
        rc, out = quiet(["report", self.card,
                         "--set", "pcie_devida=0x20b5",
                         "--set", "pcie_devidb=0x20b5",
                         "--set", "magic_d=0x200000"])
        self.assertEqual(rc, 0)
        self.assertIn("target: devida=0x20b5", out)
        # the capability word gets its own line: it is the part of the plan
        # whose value nobody has measured
        self.assertIn("MAGIC_BITS_D -> 0x00200000", out)
        self.assertIn("CH341A", out)

    def test_gen_a100_prints_the_conversion_warning(self):
        rc, out = quiet(["gen", self.card, "-o",
                         os.path.join(self.tmp, "plan.json"), "--a100"])
        self.assertEqual(rc, 0)
        self.assertIn("0x416a0942", out)
        self.assertIn("WARN: pcie_devida/pcie_devidb", out)
        self.assertIn("vBIOS", out)
        # this readout does not cover the record that carries the id, so the
        # plan's 0x0000 cannot be the id in use and has to say so
        self.assertIn("while the fuse register reads 0x20c2", out)


class TestFfRowsParsing(unittest.TestCase):
    def test_formats(self):
        rows = ffrows.parse_rows(
            "# comment\n"
            "222 0x0fe805c1\n"
            "0xde 0xfe805c1\n"
            "FFROW 223 141\n"
            "[ 1569.145419] FFROW 224 0x19801c1   # trailing comment\n"
            "%d 0\n" % ffrows.FF_ROW_MAX)
        self.assertEqual(rows[222], 0x0fe805c1)
        self.assertEqual(rows[223], 141)
        self.assertEqual(rows[224], 0x19801c1)
        self.assertEqual(rows[ffrows.FF_ROW_MAX], 0)

    def test_bad_lines(self):
        with self.assertRaises(ValueError):
            ffrows.parse_rows("222\n")
        with self.assertRaises(ValueError):
            ffrows.parse_rows("512 0\n")
        with self.assertRaises(ValueError):
            ffrows.parse_rows("222 0x100000000\n")
        with self.assertRaises(ValueError):
            ffrows.parse_rows("222 1\n222 2\n")
        ffrows.parse_rows("222 1\n222 0x1\n")

    def test_coverage_states(self):
        self.assertEqual(ffrows.coverage({"ff_rows": {}})[0], "missing")
        self.assertEqual(ffrows.coverage({"ff_rows": {"0": 5}})[0], "missing")
        self.assertEqual(ffrows.coverage({"ff_rows": {"222": 1}})[0],
                         "partial")
        full = dict((r, 0) for r in range(ffrows.FF_ROW_MIN, ffrows.FF_ROW_MAX + 1))
        self.assertEqual(ffrows.coverage({"ff_rows": full})[0], "complete")


class TestMergeffCli(unittest.TestCase):
    def setUp(self):
        self.dump = load_dump(SAMPLE)
        self.tmp = tempfile.mkdtemp(prefix="ffcalc-merge-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.card = os.path.join(self.tmp, "card.json")
        with open(self.card, "w") as f:
            json.dump({"sku": "8gb", "ff_rows": {}}, f)
        self.rows = write_rows_file(self.dump,
                                    os.path.join(self.tmp, "rows.txt"))

    def test_gen_refused_then_available(self):
        out = os.path.join(self.tmp, "plan.json")
        rc = cli.main(["gen", self.card, "-o", out])
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(out))
        self.assertEqual(cli.main(["mergeff", self.card, self.rows]), 0)
        rc = cli.main(["gen", self.card, "-o", out])
        self.assertEqual(rc, 0)
        with open(out) as f:
            obj = json.load(f)
        self.assertTrue(all(r["row"] >= 240 for r in obj["burn"]))

    def test_mergeff_partial_still_refused(self):
        partial = os.path.join(self.tmp, "partial.txt")
        with open(partial, "w") as f:
            for r, v in sorted(self.dump["ff_rows"].items()):
                if r != ffrows.FF_ROW_MAX:
                    f.write("%s %s\n" % (r, v))
        rc = cli.main(["mergeff", self.card, partial])
        self.assertEqual(rc, 1)
        rc = cli.main(["gen", self.card, "-o",
                       os.path.join(self.tmp, "plan.json")])
        self.assertEqual(rc, 2)

    def test_mergeff_over_capture_style_dump(self):
        """mergeff over a capture snapshot: the rows it carries over are the
        `"0x%08x"` strings `capture.py` wrote, and they survive the round trip.

        `capture.py` writes row values as `"0x%08x"` strings, `ingest` writes
        ints, and both are legal snapshots. An external rows file normally
        covers only the FF region (222-487), so mergeff has to carry every row
        outside it through unchanged. Carrying them through *unexamined* is
        what crashed mergeff with `"0x%08x" % "0x00000000"` (real card,
        2026-11-24); merely writing them back without checking is what would
        push an invalid row into the snapshot to fail during `gen`, far from
        the cause.
        """
        card = os.path.join(self.tmp, "capture-style.json")
        outside = {0: 0x53557C3D, 1: 0x53557C3D, 221: 0x00000000}
        with open(card, "w") as f:
            json.dump({"sku": "8gb", "option_words": {},
                       "ff_rows": dict(("%d" % r, "0x%08x" % v)
                                       for r, v in outside.items())}, f)
        self.assertEqual(cli.main(["gen", card, "-o",
                                   os.path.join(self.tmp, "p.json")]), 2)
        self.assertEqual(cli.main(["mergeff", card, self.rows]), 0)
        self.assertEqual(cli.main(["gen", card, "-o",
                                   os.path.join(self.tmp, "plan.json")]), 0)
        # The invariant is not how the file looks but that loading it back
        # gives the merged rows plus the rows that were already there, as-is.
        rows = load_dump(card)["ff_rows"]
        ff = dict((r, v) for r, v in self.dump["ff_rows"].items()
                  if ffrows.FF_ROW_MIN <= r <= ffrows.FF_ROW_MAX)
        self.assertEqual(dict((r, rows[r]) for r in ff), ff)
        self.assertEqual(dict((r, rows[r]) for r in outside), outside)
        # Idempotent: the file mergeff wrote is a snapshot mergeff can read
        # back, and merging the same readout again changes nothing.
        before = open(card).read()
        self.assertEqual(cli.main(["mergeff", card, self.rows]), 0)
        self.assertEqual(open(card).read(), before)

    def test_sim_with_ffrows_option(self):
        self.assertEqual(cli.main(["mergeff", self.card, self.rows]), 0)
        plan_path = os.path.join(self.tmp, "plan.json")
        cli.main(["gen", self.card, "-o", plan_path])
        empty = os.path.join(self.tmp, "empty.json")
        with open(empty, "w") as f:
            json.dump({"sku": "8gb", "ff_rows": {}}, f)
        rc = cli.main(["sim", empty, plan_path])
        self.assertEqual(rc, 1)
        rc = cli.main(["sim", empty, plan_path, "--ffrows", self.rows])
        self.assertEqual(rc, 0)


class TestUuidMismatch(unittest.TestCase):
    def setUp(self):
        self.dump = load_dump(SAMPLE)

    def test_sim_rejects_foreign_plan(self):
        obj = plan(self.dump)
        obj["uuid"] = "GPU-00000000-0000-0000-0000-000000000000"
        self.dump["uuid"] = "GPU-11111111-1111-1111-1111-111111111111"
        res = simulate(self.dump, obj)
        self.assertTrue(any("uuid" in e for e in res["errors"]))

    def test_sim_accepts_matching_ids(self):
        self.dump["uuid"] = "GPU-11111111-1111-1111-1111-111111111111"
        obj = plan(self.dump)
        self.assertEqual(obj["uuid"], self.dump["uuid"])
        res = simulate(self.dump, obj)
        self.assertEqual([e for e in res["errors"] if "uuid" in e], [])


class FakeFuseBar(object):
    IDLE = 4 << 16

    def __init__(self, pmc=0x10000001, idle=True, re_idle=True,
                 debug=0, latch_ok=True):
        self.pmc = pmc
        self.idle = idle
        self.re_idle = re_idle
        self.debug = debug
        self.latch_ok = latch_ok
        self.addr = 77
        self.data = 0
        self.cmds = 0

    def rd(self, off):
        if off == ffmacro.PMC_BOOT_0:
            return self.pmc
        if off == ffmacro.FUSECTRL:
            return FakeFuseBar.IDLE if self.idle else 0xe0050000
        if off == ffmacro.FUSEADDR:
            return self.addr
        if off == ffmacro.FUSEDATA:
            return self.data
        if off == ffmacro.FUSEDEBUG:
            return self.debug
        return 0

    def wr(self, off, val):
        if off == ffmacro.FUSEADDR:
            self.addr = val if self.latch_ok else val + 1
        elif off == ffmacro.FUSECTRL and val == ffmacro.CMD_READ:
            self.cmds += 1
            self.idle = self.re_idle
            self.data = self.addr * 3 % 0x100000000


class TestFfMacro(unittest.TestCase):
    def test_full_read_restores_address(self):
        bar = FakeFuseBar()
        rows = ffmacro.capture_rows(bar.rd, bar.wr)
        self.assertEqual(len(rows), 512)
        self.assertEqual(rows[100], 300)
        self.assertEqual(bar.addr, 77)
        self.assertEqual(bar.cmds, 512)

    def test_partial_rows_survive_error(self):
        bar = FakeFuseBar(re_idle=False)
        clock_times = iter([0.0] * 4 + [0.5] * 100)
        out = {}
        with self.assertRaises(ffmacro.FuseControllerError):
            ffmacro.capture_rows(bar.rd, bar.wr, sleep=lambda s: None,
                                 clock=lambda: next(clock_times),
                                 rows=out)
        self.assertEqual(out, {})
        self.assertEqual(bar.addr, 77)  # FUSEADDR restored on failure

    def test_busy_controller_refuses_to_start(self):
        bar = FakeFuseBar(idle=False)
        with self.assertRaises(ffmacro.FuseControllerError) as cm:
            ffmacro.capture_rows(bar.rd, bar.wr)
        self.assertIn("busy", str(cm.exception))
        self.assertEqual(bar.cmds, 0)

    def test_debug_register_and_sentinel_and_latch(self):
        with self.assertRaises(ffmacro.FuseControllerError):
            ffmacro.capture_rows(FakeFuseBar(debug=1).rd, FakeFuseBar().wr)
        with self.assertRaises(ffmacro.FuseControllerError):
            ffmacro.capture_rows(FakeFuseBar(pmc=0).rd,
                                 FakeFuseBar().wr)
        bar = FakeFuseBar(latch_ok=False)
        with self.assertRaises(ffmacro.FuseControllerError) as cm:
            ffmacro.capture_rows(bar.rd, bar.wr)
        self.assertIn("latch", str(cm.exception))

    def test_generated_probe_c_carries_protocol(self):
        src = ffmacro.probe_c_source()
        self.assertNotIn("@", src)
        for needle in ("ffprobe_read_rows", "ffprobe_idle",
                       "writel(1, h + 8519680)",  # CMD_READ -> FUSECTRL
                       "h + 8519684",             # FUSEADDR
                       "h + 8519688"):            # FUSEDATA
            self.assertIn(needle, src)


class FakeMmap(object):
    """BAR0 word array with mmap's seek/read/write, enough to run one
    CMD_READ: a command write to FUSECTRL completes at once and FUSEDATA
    answers with f(FUSEADDR)."""

    def __init__(self, words=(), size=capture.BAR0_LEN):
        self.words = dict(words)
        self.pos = 0
        self.size = size

    def __len__(self):
        return self.size      # a real mmap reports the mapped length; rd()/wr()
                              # bounds-check against it

    def seek(self, off):
        self.pos = off

    def read(self, n):
        assert n == 4
        return struct.pack("<I", self.words.get(self.pos, 0))

    def write(self, buf):
        assert len(buf) == 4
        val = struct.unpack("<I", buf)[0]
        if self.pos == ffmacro.FUSECTRL and val == ffmacro.CMD_READ:
            self.words[ffmacro.FUSEDATA] = \
                self.words.get(ffmacro.FUSEADDR, 0) * 3 % 0x100000000
            val = ffmacro.IDLE_STATE << 16      # command done: back to IDLE
        self.words[self.pos] = val


class TestCaptureAccessors(unittest.TestCase):
    """capture.rd/wr are the accessors used against a live card, so they have
    to take both address styles: hex strings from the JSON map, and the plain
    ints ffmacro passes for FUSECTRL/FUSEADDR/FUSEDATA."""

    def test_offsets_accept_ints_and_strings(self):
        mm = FakeMmap({ffmacro.FUSECTRL: 4 << 16})
        self.assertEqual(capture.rd(mm, ffmacro.FUSECTRL), 4 << 16)
        self.assertEqual(capture.rd(mm, "0x00820000"), 4 << 16)
        capture.wr(mm, ffmacro.FUSEADDR, 9)
        self.assertEqual(mm.words[ffmacro.FUSEADDR], 9)
        capture.wr(mm, "0x0082000c", 0x100000000 + 5)   # masked to 32 bits
        self.assertEqual(mm.words[ffmacro.FUSEWDATA], 5)

    def test_capture_rows_over_real_accessors(self):
        # regression: ffmacro calls rd(PMC_BOOT_0) with an int, which used to
        # raise TypeError: int() can't convert non-string with explicit base
        mm = FakeMmap({ffmacro.PMC_BOOT_0: 0x10000001,
                       ffmacro.FUSECTRL: 4 << 16,
                       ffmacro.FUSEADDR: 77})
        rows = ffmacro.capture_rows(functools.partial(capture.rd, mm),
                                    functools.partial(capture.wr, mm),
                                    first=0, last=3, sleep=lambda s: None)
        self.assertEqual(rows, dict((r, r * 3) for r in range(4)))
        self.assertEqual(mm.words[ffmacro.FUSEADDR], 77)  # address restored


ALLLOG = os.path.join(os.path.dirname(__file__), "..",
                      "samples", "ffprobe_all.txt")
UUIDS = {
    0xfa000000: ("0000:05:00.0",
                 "GPU-aaaaaaaa-1111-2222-3333-000000000005"),
    0xf9000000: ("0000:06:00.0",
                 "GPU-aaaaaaaa-1111-2222-3333-000000000006"),
    0xfb000000: ("0000:09:00.0",
                 "GPU-aaaaaaaa-1111-2222-3333-000000000009"),
}


class TestIngest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(ALLLOG) as f:
            text = f.read()
        cls.bybar = {}
        for line in text.splitlines():
            m = ingest.LINE.search(line)
            if m:
                bar = int(m.group(2), 16) & 0xff000000
                cls.bybar.setdefault(bar, []).append(line)

    def _collect_dir(self, uuids=None):
        d = tempfile.mkdtemp(prefix="ffcalc-collect-")
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "bar0map.tsv"), "w") as f:
            for bar, (bdf, uuid) in sorted((uuids or UUIDS).items()):
                f.write("%s\t0x%08x\t%s\n" % (bdf, bar, uuid))
                with open(os.path.join(d, "probe_%s.txt"
                                        % ingest.sanitize(bdf)), "w") as lf:
                    lf.write("\n".join(self.bybar[bar]))
        return d

    def test_grouping(self):
        self.assertEqual(sorted(self.bybar), sorted(UUIDS))
        for lines in self.bybar.values():
            # A real capture, taken before the OPT_/STATUS_OPT_* media-engine
            # words were added to the map, so it is 7 words shorter than what
            # ffprobe.ko prints today -- and ingest still builds a usable dump
            # from it (the fields those words prove are skipped, not guessed).
            self.assertEqual(len(lines), 61)

    def test_dumps_distinct_uuids_and_fingerprints(self):
        m = ingest.load_map()
        dumps = list(ingest.read_collected(self._collect_dir(), m))
        self.assertEqual(len(dumps), 3)
        ids = {d["uuid"] for d in dumps}
        fps = {d["fingerprint"] for d in dumps}
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(fps), 3)
        for d in dumps:
            want = 0x2d if d["bdf"] == "0000:06:00.0" else 0x840
            self.assertEqual(d["option_words"]["fbp_defective"], want)
            self.assertEqual(ingest.card_id(d), d["uuid"][4:])

    def test_card_id_falls_back_to_fingerprint(self):
        d = {"uuid": None, "fingerprint": "deadbeef00000000"}
        self.assertEqual(ingest.card_id(d), "fp-deadbeef00000000")

    def _ffrow_lines(self):
        return ["[ 1569.9] FFPROBE ffrow.%d 08200008 %08x"
                % (r, 0x0fe805c1 if r == 222 else 0) for r in range(512)]

    def test_cmd_read_log_fills_ff_rows(self):
        m = ingest.load_map()
        entries = ingest.parse_log("\n".join(self._ffrow_lines()))
        dump = ingest.build_dump(m, entries)
        self.assertEqual(ffrows.coverage(dump)[0], "complete")
        self.assertEqual(dump["ff_rows"][222], 0x0fe805c1)
        self.assertFalse(dump["read_only"])

    def test_scan_with_cmd_read_log_yields_plans(self):
        out = tempfile.mkdtemp(prefix="ffcalc-out-")
        self.addCleanup(shutil.rmtree, out, True)
        d = self._collect_dir()
        for _bdf, _uuid in UUIDS.values():
            path = os.path.join(
                d, "probe_%s.txt" % ingest.sanitize(_bdf))
            with open(path, "a") as f:
                f.write("\n" + "\n".join(self._ffrow_lines()))
        self.assertEqual(
            cli.main(["scan", "--dir", d,
                      "--outdir", os.path.join(out, "cards"),
                      "--plans", os.path.join(out, "plans")]), 0)
        for _bdf, uuid in UUIDS.values():
            with open(os.path.join(out, "plans",
                                   uuid[4:] + ".burn.json")) as f:
                obj = json.load(f)
            self.assertEqual(obj["uuid"], uuid)
            # row 222 is occupied by the captured record
            self.assertTrue(all(r["row"] >= 223 for r in obj["burn"]))

    def _scan(self, out):
        return cli.main(["scan", "--dir", self._collect_dir(),
                         "--outdir", os.path.join(out, "cards"),
                         "--plans", os.path.join(out, "plans")])

    def test_scan_a100_plans_carry_the_identity_and_capability_rows(self):
        """The one-shot path has to plan what `gen --a100` plans, and let the
        capability word be declined from there too (ffscan.sh --no-magic-d)."""
        out = tempfile.mkdtemp(prefix="ffcalc-out-")
        self.addCleanup(shutil.rmtree, out, True)
        d = self._collect_dir()
        for _bdf, _uuid in UUIDS.values():
            path = os.path.join(d, "probe_%s.txt" % ingest.sanitize(_bdf))
            with open(path, "a") as f:
                f.write("\n" + "\n".join(self._ffrow_lines()))
        first = list(UUIDS.values())[0][1][4:]

        def scan(tag, extra):
            plans = os.path.join(out, "plans-" + tag)
            rc = cli.main(["scan", "--dir", d, "--outdir",
                           os.path.join(out, "cards"), "--plans", plans]
                          + extra)
            self.assertEqual(rc, 0)
            with open(os.path.join(plans, first + ".burn.json")) as f:
                obj = json.load(f)
            return obj, dict((r["block"], r) for r in obj["burn"])

        obj, by_block = scan("a100", ["--a100"])
        self.assertEqual(by_block[37]["record"], "0x416a0942")
        self.assertEqual(by_block[38]["record"], "0x416a0982")
        self.assertEqual(int(by_block[46]["new"], 0), 0x8000)
        self.assertTrue(any("capability declaration" in w
                            for w in obj["warnings"]))
        obj, by_block = scan("no-magic", ["--a100", "--skip", "magic_d"])
        self.assertNotIn("magic_d", obj["targets"])
        self.assertEqual(by_block[37]["record"], "0x416a0942")
        self.assertFalse([r for r in obj["burn"]
                          if "magic_d" in r["fields"]])

    def _merge_rows(self, out):
        rows = write_rows_file(load_dump(SAMPLE),
                               os.path.join(out, "rows.txt"))
        for _bdf, uuid in UUIDS.values():
            rc = cli.main(["mergeff",
                           os.path.join(out, "cards", uuid[4:] + ".json"),
                           rows])
            self.assertEqual(rc, 0)
        return rows

    def test_scan_cli_refuses_plans_without_ff_rows_then_ok(self):
        out = tempfile.mkdtemp(prefix="ffcalc-out-")
        self.addCleanup(shutil.rmtree, out, True)
        # read-only probe cannot see FF rows -> dumps saved, plans refused
        self.assertEqual(self._scan(out), 1)
        plans = os.path.join(out, "plans")
        self.assertFalse(os.path.isdir(plans))
        for _bdf, uuid in UUIDS.values():
            self.assertTrue(os.path.exists(
                os.path.join(out, "cards", uuid[4:] + ".json")))
        # after merging a full FF readout, a rescan yields the plans
        self._merge_rows(out)
        self.assertEqual(self._scan(out), 0)
        for _bdf, uuid in UUIDS.values():
            with open(os.path.join(plans, uuid[4:] + ".burn.json")) as f:
                self.assertEqual(json.load(f)["uuid"], uuid)

    def test_verify_cli_ok_and_mismatch(self):
        out = tempfile.mkdtemp(prefix="ffcalc-out-")
        self.addCleanup(shutil.rmtree, out, True)
        self._scan(out)
        rows = self._merge_rows(out)
        self._scan(out)
        plan5 = os.path.join(out, "plans",
                             UUIDS[0xfa000000][1][4:] + ".burn.json")
        rc = cli.main(["verify", plan5, "--dir", self._collect_dir(),
                       "--ffrows", rows])
        self.assertEqual(rc, 0)
        # fresh read without the FF rows file -> replay cannot be trusted
        rc = cli.main(["verify", plan5, "--dir", self._collect_dir()])
        self.assertEqual(rc, 1)
        wrong = {bar: (bdf, "GPU-" + "f" * 32)
                 for bar, (bdf, _u) in UUIDS.items()}
        rc = cli.main(["verify", plan5,
                       "--dir", self._collect_dir(wrong),
                       "--ffrows", rows])
        self.assertEqual(rc, 1)


class TestSnapshotCodec(unittest.TestCase):
    """A snapshot is the only basis a burn plan is built from, so the codec
    rules hold for every writer and every reader: any shape that can be a
    real readout is believed exactly, anything that cannot is refused out loud
    before a byte is written, and no failure may damage the file."""

    INVALID_ROWS = [
        ("row outside the array", {"999": "0x00000000"}),
        ("negative row", {"-1": 0}),
        ("value is not a number", {"222": "0xGG"}),
        ("value is null", {"222": None}),
        ("value is a bool", {"222": True}),
        ("value is a float", {"222": 1.5}),
        ("value wider than 32 bits", {"222": "0x1ffffffff"}),
        ("same row listed twice with two values", {"222": 1, "0xde": 2}),
        ("row key is not a number", {"ff222": 0}),
    ]

    FORMS = [("capture", lambda v: "0x%08x" % v),
             ("probe", lambda v: v),
             ("mixed", lambda v: "0x00000000" if v == 0 else v)]

    def setUp(self):
        self.dump = load_dump(SAMPLE)
        self.tmp = tempfile.mkdtemp(prefix="ffcalc-snap-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ff_rows = dict((r, v) for r, v in self.dump["ff_rows"].items()
                            if ffrows.FF_ROW_MIN <= r <= ffrows.FF_ROW_MAX)
        self.rows = write_rows_file(self.dump,
                                    os.path.join(self.tmp, "rows.txt"))

    def _card(self, rows, name="card.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            json.dump({"sku": "8gb", "option_words": {}, "ff_rows": rows}, f)
        return path

    def _disk_rows(self, path):
        with open(path) as f:
            return json.load(f)["ff_rows"]

    def test_every_legal_shape_is_believed_exactly(self):
        expected = {0: 0x53557C3D}                  # a row outside the FF region
        expected.update(self.ff_rows)
        for name, fmt in self.FORMS:
            rows = dict(("%d" % r, fmt(v)) for r, v in sorted(expected.items()))
            card = self._card(rows, "card-%s.json" % name)
            self.assertEqual(load_dump(card)["ff_rows"], expected, name)
            rc, out = quiet(["report", card])
            self.assertEqual(rc, 0, (name, out))
            rc, out = quiet(["mergeff", card, self.rows])
            self.assertEqual(rc, 0, (name, out))
            back = load_dump(card)["ff_rows"]
            self.assertEqual(dict((r, back[r]) for r in self.ff_rows),
                             self.ff_rows, name)
            self.assertEqual(back[0], 0x53557C3D, name)

    def test_invalid_rows_are_refused_before_anything_is_written(self):
        for why, rows in self.INVALID_ROWS:
            card = self._card(dict(rows))
            before = open(card).read()
            rc, out = quiet(["mergeff", card, self.rows])
            self.assertEqual(rc, 2, (why, out))
            self.assertIn("FAIL:", out, why)
            self.assertEqual(open(card).read(), before,
                             "%s: a refused merge must leave the snapshot "
                             "exactly as it was" % why)
            for cmd in (["report", card],
                        ["gen", card, "-o", os.path.join(self.tmp, "p.json")]):
                rc, out = quiet(cmd)
                self.assertEqual(rc, 2, (why, cmd[0], out))
                self.assertIn("FAIL:", out, "%s: %s" % (why, cmd[0]))
            self.assertEqual(glob.glob(os.path.join(self.tmp, "*.tmp")), [])

    def test_missing_file_is_a_message_not_a_traceback(self):
        rc, out = quiet(["report", os.path.join(self.tmp, "nope.json")])
        self.assertEqual(rc, 2)
        self.assertIn("FAIL:", out)
        self.assertIn("cannot be read", out)

    def test_plan_file_that_is_not_a_plan_is_refused(self):
        """A plan is a burn order: a truncated or foreign file must be
        refused with a message (rc 2), not a traceback -- `sim` and `verify`
        are what stand between a plan file and burntool."""
        card = self._card(dict(("%d" % r, "0x%08x" % v)
                               for r, v in sorted(self.ff_rows.items())))
        plan_path = os.path.join(self.tmp, "plan.json")
        self.assertEqual(quiet(["gen", card, "-o", plan_path])[0], 0)
        good = open(plan_path).read()
        cases = {"truncated": good[:len(good) // 2],
                 "not a plan": '{"burn": "no"}'}
        for why, body in cases.items():
            with open(plan_path, "w") as f:
                f.write(body)
            for cmd in (["sim", card, plan_path],
                        ["verify", plan_path, "--dump", card]):
                rc, out = quiet(cmd)
                self.assertEqual(rc, 2, (why, cmd[0], out))
                self.assertIn("FAIL:", out, (why, cmd[0]))
        with open(plan_path, "w") as f:
            f.write(good)

    def test_bad_rows_file_names_the_line_that_broke(self):
        path = os.path.join(self.tmp, "rows-bad.txt")
        with open(path, "w") as f:
            f.write("222 0x0fe805c1\n999 0\n")
        rc, out = quiet(["mergeff", self._card({}), path])
        self.assertEqual(rc, 2)
        self.assertIn("line 2", out)

    def test_truncated_snapshot_says_so_instead_of_raising(self):
        # What an interrupted, non-atomic write by an older ffcalc leaves
        # behind -- the fix is re-capturing, so say that, not "JSONDecodeError".
        path = os.path.join(self.tmp, "truncated.json")
        with open(path, "w") as f:
            f.write('{"sku": "8gb", "ff_rows": {"222": "0x0f')
        for cmd in (["report", path], ["mergeff", path, self.rows]):
            rc, out = quiet(cmd)
            self.assertEqual(rc, 2, (cmd[0], out))
            self.assertIn("not valid JSON", out)

    def test_mergeff_shows_which_rows_it_overwrote(self):
        rows = dict(("%d" % r, "0x%08x" % v)
                    for r, v in sorted(self.ff_rows.items()))
        rows["222"] = "0xdeadbeef"          # snapshot disagrees with the readout
        card = self._card(rows)
        rc, out = quiet(["mergeff", card, self.rows])
        self.assertEqual(rc, 0)
        self.assertIn("note:", out)
        self.assertIn("222 0xdeadbeef->0x%08x" % self.ff_rows[222], out)
        # the live readout wins: that is the whole point of merging one in
        self.assertEqual(load_dump(card)["ff_rows"][222], self.ff_rows[222])

    def test_write_failure_keeps_the_snapshot_that_was_there(self):
        card = self._card(dict(("%d" % r, "0x%08x" % v)
                               for r, v in sorted(self.ff_rows.items())))
        good = open(card).read()
        for where, exc in (("os.replace", OSError(28, "No space left on device")),
                           ("json.dumps", TypeError("not serializable"))):
            with mock.patch("ffcalc.dump." + where, side_effect=exc):
                rc, out = quiet(["mergeff", card, self.rows])
            self.assertEqual(rc, 2, (where, out))
            self.assertIn("FAIL:", out, where)
            self.assertEqual(open(card).read(), good,
                             "%s: a failed write must leave the old snapshot"
                             % where)
            self.assertFalse(os.path.exists(card + ".tmp"),
                             "%s: the temp file must not be left behind" % where)

    def test_coverage_refuses_a_key_that_is_not_a_row(self):
        # coverage() is the gate that decides "burn or not", so a key that is
        # not a physical row must not quietly read as "some rows are known":
        # that is how a corrupt snapshot could look "partial" instead of bad.
        for rows in ({"999": "0x00000000", "222": 0},
                     {"ff222": 0},
                     {"-3": 0}):
            self.assertRaises(snapshot.BadSnapshot, ffrows.coverage,
                              {"ff_rows": rows})

    def test_both_writer_channels_emit_one_shape(self):
        """Whatever a dump looked like in memory, both writers put the same
        canonical rows on disk: probe saves through ingest.save_dump, capture
        through to_json_form + atomic_write_json, and neither passes an int
        value through for the next reader to trip over."""
        mem = {"sku": "8gb", "ff_rows": dict(self.ff_rows),
               "option_words": {"fbp_disable": 0x852}}
        via_probe = ingest.save_dump(os.path.join(self.tmp, "cards"), mem)
        via_capture = os.path.join(self.tmp, "capture.json")
        snapshot.atomic_write_json(via_capture,
                                   snapshot.to_json_form(mem, via_capture))
        self.assertEqual(self._disk_rows(via_probe),
                         self._disk_rows(via_capture))
        for path in (via_probe, via_capture):
            self.assertTrue(all(isinstance(v, str) and len(v) == 10
                                for v in self._disk_rows(path).values()), path)
            self.assertEqual(load_dump(path)["ff_rows"],
                             dict((r, v) for r, v in
                                  load_dump(via_probe)["ff_rows"].items()))


if __name__ == "__main__":
    unittest.main()
