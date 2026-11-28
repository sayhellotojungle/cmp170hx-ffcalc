import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ffcalc import dump as snapshot, ffrecord
from ffcalc.optionmap import BY_KEY, FF_ROW_MAX, FF_ROW_MIN, set_bits

EFFECTIVE = {
    "fbio_disable": 0x000000,
    "fbpa_disable": 0xC0330C,
    "gpc_disable": 0x00,
    "tpc_gpc0_disable": 0xC0,
    "tpc_gpc1_disable": 0xC0,
    "tpc_gpc2_disable": 0xE0,
    "tpc_gpc3_disable": 0xE0,
    "tpc_gpc4_disable": 0xE0,
    "tpc_gpc5_disable": 0xF0,
    "tpc_gpc6_disable": 0xF0,
    "tpc_gpc7_disable": 0xF8,
    "pes_disable": 0x000000,
    "fbp_disable": 0x852,
    "rop_l2_disable": 0x000C40,
    "nvlink_disable": 0x7,
    "sys_pipe_disable": 0x2,
    # Media engines fused off, exactly as every CMP 170HX read out so far looks
    # before the media unlock (which the default plan performs; --no-mig leaves
    # it): all 5 NVDEC disable bits set, and both NVJPG/OFA bits set.
    # They land in chain1 blk8[15:11], blk9[2] and blk23[3]; block merging is
    # what makes a plan write those blocks once, not three times.
    "nvdec_disable": 0x1F,
    "nvjpg_ofa_disable_bit146": 1,
    "nvjpg_ofa_disable_bit371": 1,
}

# Read-only state words, as the probe reads them. STATUS_OPT_* is computed by
# hardware as OPT_DISABLE | OPT_DEFECTIVE, so every pair below being equal is
# the proof that clearing the floorsweep switches on real silicon -- which is
# what these fields require before an irreversible row is spent on them.
# The rest is what an unlocked card under the driver-side unlocker reads back:
# the fused speed selects stay at the 1/32-rate setting and the feature-override
# island is what runs the blocks at full rate, so a stock card shows the same
# runtime words whether or not its FF pool has been burnt.
INFO = {
    "secured_burn_feature_enable": 0x1,
    "en_sw_override": 0x0,          # CTRL_OPT_* overrides are inert here
    "dsov_status": 0x1,             # same word burntool reads as DSOV_STATUS
    "nvdec_disable": 0x1F,
    "status_nvdec": 0x1F,
    "nvjpg_disable": 0x1,
    "status_nvjpg": 0x1,
    "ofa_disable": 0x1,
    "status_ofa": 0x1,
    "status_display": 0x1,          # no display outputs on GA100: left fused
    "status_disp_head": 0xf,
    "status_fb_config": 0x0,
    "status_half_fbpa": 0x0,
    "status_half_ltc": 0x0,
    "status_pcie_lane": 0x0,

    # Fuse macro: healthy, outside the burn window (driver attached), factory
    # IFF window rows 488-507 -- the word that fixes where the FF pool ends.
    "fuse_debugctrl": 0x0,          # bit 1 = RWL: RIR macro not selected
    "iff_record": 0x01FB01E8,
    "crc_status": 0x55,             # healthy chain CRC
    "efp_status": 0x1,
    "dis_program": 0x0,
    "wdata_wr_secure": 0x1,         # 1 = locked (outside the window); 0 while armed
    "plm_fuse_wdata": 0xFFFFFFFF,
    "priv_mask": 0xFFFFFFFF,

    # Feature-override island: what the unlocker writes each boot, and the one
    # word that says whether compute is running at full rate right now.
    "feat_ovr_ecc_plm": 0xFFFFFFFF,
    "feat_ovr_plm": 0xFFFFFFFF,
    "feat_ovr_quadro": 0x00000181,
    "feat_ovr_ecc": 0x00999999,
    "feat_ovr_ecc_1": 0x003FFEBF,
    "feat_ovr_ecc_2": 0xF,
    "feat_ovr_sm_spd_0": 0x88888888,
    "feat_ovr_sm_spd_1": 0x00000008,
    "feat_ovr_row_remap": 0x0,
    "feat_readout_1": 0x0,          # 0 = no throttle in effect; 0x016db6ed = throttled

    # Fused speed selects: 0x5 is the 1/32-rate slot, and this is the half the
    # FF pool does not carry -- an unlocked card still reads them throttled.
    "ss_dp": 0x1,
    "ss_ffma": 0x5,
    "ss_fmla16": 0x5,
    "ss_fmla32": 0x5,
    "ss_imla0": 0x5,
    "ss_imla1": 0x5,
    "ss_imla2": 0x5,
    "ss_imla3": 0x5,
    "ss_imla4": 0x5,

    # ECC and geometry, both runtime state: fused ECC off, DRAM sideband on for
    # this boot, framebuffer mapped to the unlocked size.
    "opt_ecc_en": 0x0,
    "ecc_ctrl": 0x41,               # MASTER_EN | SIDEBAND_EN
    "fbpa_cfg1": 0x02779000,
    "cstatus_ramamount": 0x1000,    # the unlocked 80 GiB mapping
    "mmu_lmr": 0x0000028B,

    # Board facts the fuses do not restrict: no lane disabled, none defective,
    # GSP firmware boot complete. HBM at the busy coefficient (27 MHz x 64 =
    # 1728 MHz); the same two cards read 0x00011401 (540 MHz) with nothing on
    # them, and the host unlocker prints 0x14001 as its busy_coeff, so this word
    # is HBM power management and says nothing about the fuse state.
    "gfw_boot": 0x3FF,
    "opt_pcie_lane_disable": 0x0,
    "opt_pcie_lane_defective": 0x0,
    "hbm_pll_plm": 0xFFFFFFDF,
    "hbm_pll_coeff": 0x00014001,
}

DEFECTIVE = {
    "fbio_defective": 0x000000,
    "fbpa_defective": 0xC03000,
    "gpc_defective": 0x00,
    "tpc_gpc0_defective": 0x80,
    "tpc_gpc1_defective": 0x40,
    "tpc_gpc2_defective": 0x20,
    "tpc_gpc3_defective": 0x10,
    "tpc_gpc4_defective": 0x08,
    "tpc_gpc5_defective": 0x04,
    "tpc_gpc6_defective": 0x02,
    "tpc_gpc7_defective": 0x01,
    "pes_defective": 0x000000,
    "fbp_defective": 0x840,
    "rop_l2_defective": 0x840,
    "nvlink_defective": 0x0,
    "sys_pipe_defective": 0x0,
}

PCIE_EFFECTIVE = {
    "disable_gen3_speed_bit1": 1,
    "disable_gen3_speed_bit9": 0,
    "pcie_magic_gen4_dis": 1,
    "pcie_boot_gen23_dis": 1,
    "pcie_boot_gen3_dis": 1,
}


def build_rows(values):
    """FF records keyed by physical row, values as ints; the snapshot codec
    decides how they are written out."""
    blocks = {}
    for key, value in values.items():
        f = BY_KEY[key]
        set_bits(blocks.setdefault(f.chain, {}), f.bit, f.width, value)
    rows = {}
    row = FF_ROW_MIN
    for chain in sorted(blocks):
        for blk in sorted(blocks[chain]):
            rows[row] = ffrecord.encode(chain, blk, blocks[chain][blk])
            row += 1
    return rows


def main():
    rows = build_rows(dict(EFFECTIVE, **PCIE_EFFECTIVE))
    # A real readout lists every FF row; unburnt rows read back as 0.
    for r in range(FF_ROW_MIN, FF_ROW_MAX + 1):
        rows.setdefault(r, 0)
    dump = {
        "sku": "8gb",
        "comment": "synthetic dump modeled on kate-land blog card "
                   "(FBP_DISABLE=0x852, FBP_DEFECTIVE=0x840), with the media "
                   "engines fused off as on a stock CMP 170HX; FF region "
                   "fully listed, unburnt rows = 0",
        "ff_rows": snapshot.serialize_rows(rows),
        "option_words": dict(EFFECTIVE, **DEFECTIVE),
        "info": INFO,
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "blog_card_example.json")
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2, sort_keys=True)
    print(out)
    for r in sorted(rows):
        if rows[r]:
            print("  row %d = 0x%08x" % (r, rows[r]))


if __name__ == "__main__":
    main()
