from dataclasses import dataclass

FF_ROW_MIN = 222
# Where the IFF window starts is hardware state, not folklore: a factory
# CMP 170HX reads IFF_RECORD (0x00820050) as 0x01fb01e8, i.e. start
# 0x1e8 = 488, last 0x1fb = 507 (read on two cards). The FF pool ends where
# that window begins, so row 487 is writable and 488 is not.
IFF_ROW_MIN = 488
IFF_ROW_MAX = 507
FF_ROW_MAX = IFF_ROW_MIN - 1
# Physical fuse array: every row a snapshot or a rows file may name.
PHYS_ROW_MIN = 0
PHYS_ROW_MAX = 511

TOTAL_GPC = 8
TPC_PER_GPC = 8
SM_PER_TPC = 2
TOTAL_FBP = 12
BYTES_PER_FBP = {"8gb": 8, "10gb": 4}
# As-built counts on GA100 (NV_PTOP_SCAL_NUM_NVDECS / _NVJPGS and the 8 GR
# entries in NV_PTOP_DEVICE_INFO2), i.e. what a cleared disable word actually
# frees. The CMP 170HX is the same die as the A100 here: every one of these
# blocks is present and only the floorsweep fuse says otherwise.
TOTAL_SYS_PIPE = 8
TOTAL_NVDEC = 5
TOTAL_NVJPG = 1
TOTAL_OFA = 1


@dataclass(frozen=True)
class OptionField:
    key: str
    name: str
    chain: int
    bit: int
    width: int
    category: str
    defect_key: str = ""
    note: str = ""
    burnable: bool = True
    opt_in: bool = False
    # Readout words (`dump["info"]`) that carry this field's OPT_* and
    # STATUS_OPT_* values. Only category "engine" uses them: those engines have
    # no *_DEFECTIVE register, so STATUS_OPT_* -- which hardware computes as
    # OPT_DISABLE | OPT_DEFECTIVE -- is the only way to see whether a block is
    # there before spending an irreversible FF row on it. A field whose proof
    # words were not read is skipped, not guessed at.
    proof_keys: tuple = ()


def _tpc_fields():
    out = []
    for i in range(8):
        out.append(OptionField(
            "tpc_gpc%d_disable" % i,
            "NV_FUSE_OPT_TPC_GPC%d_DISABLE" % i,
            1, 187 + 8 * i, 8, "floorsweep", "tpc_gpc%d_defective" % i))
    return out


FIELDS = [
    OptionField("fbio_disable", "NV_FUSE_OPT_FBIO_DISABLE",
                1, 83, 24, "floorsweep", "fbio_defective"),
    OptionField("fbpa_disable", "NV_FUSE_OPT_FBPA_DISABLE",
                1, 107, 24, "floorsweep", "fbpa_defective"),
    OptionField("gpc_disable", "NV_FUSE_OPT_GPC_DISABLE",
                1, 131, 8, "floorsweep", "gpc_defective"),
] + _tpc_fields() + [
    OptionField("pes_disable", "NV_FUSE_OPT_PES_DISABLE",
                1, 251, 24, "floorsweep", "pes_defective"),
    OptionField("fbp_disable", "NV_FUSE_OPT_FBP_DISABLE",
                1, 307, 12, "floorsweep", "fbp_defective"),
    OptionField("rop_l2_disable", "NV_FUSE_OPT_ROP_L2_DISABLE",
                1, 319, 24, "floorsweep", "rop_l2_defective"),
    OptionField("nvlink_disable", "NV_FUSE_OPT_NVLINK_DISABLE",
                1, 368, 3, "nvlink", "nvlink_defective",
                "NVLink additionally needs the U11 bridge mod + ROM check bypass"),
    OptionField("sys_pipe_disable", "NV_FUSE_OPT_SYS_PIPE_DISABLE",
                1, 372, 8, "mig", "sys_pipe_defective",
                "MIG gates here; burnt 0 on our card, and a community card "
                "reports 8-way MIG after the clear (8 SYS_PIPEs -> "
                "8g/4g/2g/1g, more than a retail A100's 7g). ECC is a separate "
                "question this proves nothing about"),
    # ---- media-engine floorsweep bits (Chain 1 / TOP_FS) -----------------
    # These engines are physically present on the die and are held off by these
    # Chain-1 disable bits alone: not by a 10de:20c2 driver check, not by a
    # VBIOS lock. Our own readouts prove the silicon half of that -- on both
    # cards we captured, OPT_DISABLE == STATUS_OPT_* for all three engines, and
    # since hardware computes STATUS_OPT = OPT | DEFECTIVE, that equality says
    # every unit is there with no defects (the community card adds the PTOP
    # side: NV_PTOP_DEVICE_INFO2 slots go from all-zero to 5 NVDEC + NVJPG0 +
    # OFA0 once the bits are clear). The volatile route for these floorsweep
    # bits is inert on datacenter GA100: FUSE_EN_SW_OVERRIDE (0x820040) = 0, and
    # writes to the CTRL_OPT_*/OPT_* words never moved STATUS_OPT_*, even inside
    # the burn window -- which is why an FF record is the only lever for them.
    # (Scoped to this route: the 0x8238xx feature-override island is a separate
    # mechanism and is written every boot; see analyze.runtime_state.) They stay
    # `opt_in` so nothing reaches them by accident (`--set nvdec_disable=0` alone
    # is not enough), but the default plan does ask for them: the clear is
    # measured on our own card, and `--no-mig` is what leaves them fused.
    OptionField("nvdec_disable", "NV_FUSE_OPT_NVDEC_DISABLE",
                1, 139, 5, "engine", "",
                "5x hardware video decode (H264/HEVC/VP9/MPEG2/JPEG; no AV1, "
                "GA100's NVDEC predates the GA10x AV1 block); shares chain1 "
                "blk8 with fbpa_disable[23:21] and gpc_disable",
                opt_in=True, proof_keys=("nvdec_disable", "status_nvdec")),
    # bit146 and bit371 are a pair. The community bit-budget diff over three
    # dies leaves exactly these two as the only unclaimed 1-bits in chain 1 once
    # display and NVDEC are accounted for, and burning both took
    # STATUS_OPT_NVJPG (0x820c0c) and STATUS_OPT_OFA (0x820e34) to 0 together --
    # so {bit146, bit371} maps bijectively onto {NVJPG, OFA}, but which bit is
    # which was never separated (that takes a bus reset between the two rows on
    # the next card). We clear both together, exactly like the two
    # DISABLE_GEN3_SPEED bits. Our own captures read both OPT words as 0x1 with
    # STATUS equal, so both units exist on our dies and the pair costs the same
    # one row either way. bit371 sits in blk23 next to nvlink_disable[2:0] and
    # sys_pipe_disable[11:4], so one record writes all three fields.
    OptionField("nvjpg_ofa_disable_bit146",
                "NV_FUSE_OPT_NVJPG_DISABLE / NV_FUSE_OPT_OFA_DISABLE",
                1, 146, 1, "engine", "",
                "NVJPG/OFA pair, attribution not separated: chain1 blk9[2]; "
                "the other half is nvjpg_ofa_disable_bit371",
                opt_in=True,
                proof_keys=("nvjpg_disable", "status_nvjpg", "ofa_disable",
                            "status_ofa")),
    OptionField("nvjpg_ofa_disable_bit371",
                "NV_FUSE_OPT_NVJPG_DISABLE / NV_FUSE_OPT_OFA_DISABLE",
                1, 371, 1, "engine", "",
                "NVJPG/OFA pair, attribution not separated: chain1 blk23[3], "
                "in the same block as nvlink_disable and sys_pipe_disable",
                opt_in=True,
                proof_keys=("nvjpg_disable", "status_nvjpg", "ofa_disable",
                            "status_ofa")),
    # Device id as it is fused into the chip: lspci, the driver and the
    # vBIOS-vs-fuse boot gate compare *this*, not the ROM's own id. Chain-2
    # layout follows the community A100 conversion report (block37 = devida<<1,
    # block38 = devidb<<1, each id's 16th bit in bit0 of the next block, 0 for
    # both the CMP and the A100 id), so burning 0x20c2 -> 0x20b5 rewrites
    # blocks 37/38 to 0x416a -- records 0x416A0942 and 0x416A0982. The encoding
    # checks out against our own FF snapshots (which replay 0x4184 for these
    # blocks); the conversion itself we have not yet walked on our own card.
    # opt_in: this is an identity change that only makes sense together with an
    # A100 vBIOS flash, so it never happens unless --a100 / --set asks for it.
    OptionField("pcie_devida", "NV_FUSE_OPT_PCIE_DEVIDA",
                2, 593, 16, "identity", "",
                "fused device id; --a100 rewrites it to 0x20b5 (A100)",
                opt_in=True),
    OptionField("pcie_devidb", "NV_FUSE_OPT_PCIE_DEVIDB",
                2, 609, 16, "identity", "",
                "fused device id (mirror); --a100 rewrites it to 0x20b5",
                opt_in=True),
    OptionField("disable_gen3_speed_bit1", "NV_FUSE_OPT_DISABLE_GEN3_SPEED",
                2, 39 * 16 + 1, 1, "pcie", "",
                "which of bits 1/9 is unresolved upstream; burn both by default"),
    OptionField("disable_gen3_speed_bit9", "NV_FUSE_OPT_DISABLE_GEN3_SPEED",
                2, 39 * 16 + 9, 1, "pcie", "",
                "which of bits 1/9 is unresolved upstream; burn both by default"),
    OptionField("pcie_magic_gen4_dis", "NV_FUSE_OPT_PCIE_MAGIC_BITS_D[25]",
                2, 47 * 16 + 3, 1, "pcie", "", "Gen4 speed-disable flag"),
    OptionField("pcie_boot_gen23_dis", "NV_FUSE_OPT_PCIE_BOOT_GEN23_DISABLE",
                2, 49 * 16 + 6, 1, "pcie", ""),
    OptionField("pcie_boot_gen3_dis", "NV_FUSE_OPT_PCIE_BOOT_GEN3_DISABLE",
                2, 49 * 16 + 7, 1, "pcie", ""),
    # Whole MAGIC_BITS_D register (BAR0 0x820520). Chain-2 mapping confirmed
    # on three CMP 170HX cards: MAGIC = (block47<<22)|(block46<<6), matching
    # the upstream bit25=block47.3 anchor. opt_in: never a default target;
    # --a100 asks for it (and --skip magic_d refuses) because a card that takes
    # an A100 id should not keep declaring CMP-era capability. This word is a
    # PCIe *capability declaration*: it states what the link may train, it is
    # not a measured speed, which is why clearing bit25 alone (the default plan
    # does) is enough to run Gen4 and why the whole-word value is a guess.
    OptionField("magic_d", "NV_FUSE_OPT_PCIE_MAGIC_BITS_D",
                2, 730, 32, "identity", "",
                "whole MAGIC_BITS_D word; --a100 rewrites it, --set pins it",
                opt_in=True),
]

# The two halves of the fused device id, and what the community A100
# conversion burns them to. Everything that compares ids (lspci, the driver,
# the GSP/DevInit vBIOS boot gate) reads this, so an A100 vBIOS is refused on
# a card still fused as 0x20c2 and accepted, unmodified, on one fused 0x20b5.
DEVID_KEYS = ("pcie_devida", "pcie_devidb")
CMP_DEVID = 0x20C2
A100_DEVID = 0x20B5

# The three Chain-1 media-engine bits the media unlock clears, i.e. what is in the
# default plan and what `--no-mig` leaves alone (see the long comments on the
# fields above), and the two that only mean something as a pair.
ENGINE_KEYS = ("nvdec_disable", "nvjpg_ofa_disable_bit146",
               "nvjpg_ofa_disable_bit371")
ENGINE_PAIR_KEYS = ("nvjpg_ofa_disable_bit146", "nvjpg_ofa_disable_bit371")

# How to describe each engine in the report: (label, OPT word, STATUS_OPT word,
# as-built unit count, mask of the disable bits inside those words, the FF
# field(s) that gate it). Both word names are keys into ``dump["info"]``. NVJPG
# and OFA name the same two gating fields because those two bits have not been
# told apart -- they are always cleared together.
ENGINES = (
    ("NVDEC", "nvdec_disable", "status_nvdec", TOTAL_NVDEC, 0x1f,
     ("nvdec_disable",)),
    ("NVJPG", "nvjpg_disable", "status_nvjpg", TOTAL_NVJPG, 0x1,
     ENGINE_PAIR_KEYS),
    ("OFA", "ofa_disable", "status_ofa", TOTAL_OFA, 0x1, ENGINE_PAIR_KEYS),
)

# Community guess for the whole MAGIC_BITS_D word a real A100 is said to
# carry: UNTESTED, and the community conversion report does not touch this word
# at all (the boot gate compares the fused device id). Kept for
# `--set magic_d=...` experiments, where generate.py still warns about it.
A100_MAGIC_D = 0x00200000

BY_KEY = {f.key: f for f in FIELDS}


def block_mask(field, blk):
    """Which bits of one 16-bit block belong to `field`.

    A field can straddle a block boundary (both device-id fields do), and two
    fields can share a block (the whole-word MAGIC_BITS_D and its bit25), so
    "would this record change anything this field owns?" is answered per block,
    not per field -- that is what makes a record's `fields` list say why that
    record has to be burnt.
    """
    mask = 0
    for i in range(field.width):
        b = field.bit + i
        if b // 16 == blk:
            mask |= 1 << (b % 16)
    return mask


def get_bits(blocks_by_chain, chain, bit, width):
    value = 0
    chain_blocks = blocks_by_chain.get(chain, {})
    for i in range(width):
        b = bit + i
        blk = chain_blocks.get(b // 16, 0)
        value |= ((blk >> (b % 16)) & 1) << i
    return value


def set_bits(chain_blocks, bit, width, value):
    for i in range(width):
        b = bit + i
        blk = chain_blocks.get(b // 16, 0)
        blk = (blk & ~(1 << (b % 16))) | (((value >> i) & 1) << (b % 16))
        chain_blocks[b // 16] = blk


def decode_field(blocks_by_chain, field):
    return get_bits(blocks_by_chain, field.chain, field.bit, field.width)


def popcount(v):
    return bin(v).count("1")
