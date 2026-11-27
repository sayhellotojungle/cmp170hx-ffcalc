#!/usr/bin/env python3
"""Generate the ffprobe.ko kernel module from the vendored fuse register map.
Build on the target, insmod once per GPU (bar0=<phys>), values land in
dmesg, module auto-removes itself. Option-word reads are pure MMIO; by
default the module then captures all 512 physical fuse rows via fuse-macro
CMD_READ (writes to FUSEADDR/FUSECTRL only; procedure from
kate-land/ga100vfi, shared with ffcalc/ffmacro.py). Pass ffrows=0 at
insmod time to skip the CMD_READ pass on a card that is not idle.

    python3 gen_probe_c.py > ffprobe.c
    make -C /lib/modules/$(uname -r)/build M=$PWD modules
    sudo insmod ffprobe.ko bar0=0xfa000000
    sudo dmesg | grep FFPROBE
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAP = os.path.join(HERE, "..", "ffcalc", "data", "ga100-fuses.json")
sys.path.insert(0, os.path.join(HERE, ".."))

from ffcalc import ffmacro  # noqa: E402

FUSE_REGION_END = 0x00830000   # the fuse macro block CMD_READ touches
BAR0_SIZE = 0x01000000         # GA100 BAR0, as ffcalc.capture maps it

TPL = """\
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/io.h>
#include <linux/delay.h>
#include <linux/jiffies.h>
#include <linux/types.h>

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("ffcalc GA100 fuse probe (words read-only + CMD_READ rows)");

/* Generated: covers every offset in the table below, page aligned. The probe
 * reads only inside this window (see the bounds check in ffprobe_init). */
#define PROBE_MAPLEN @MAPLEN@

static u64 bar0;
module_param(bar0, ullong, 0444);
MODULE_PARM_DESC(bar0, "physical BAR0 base address of the GPU");

static bool ffrows = true;
module_param(ffrows, bool, 0444);
MODULE_PARM_DESC(ffrows, "capture 512 raw rows via CMD_READ (default on)");

struct probe_entry { u32 off; const char *name; };
static const struct probe_entry entries[] = {
@BODY@
};
@FFROWS@
static int __init ffprobe_init(void)
{
	void __iomem *h;
	size_t i;

	if (!bar0)
		return -EINVAL;
	h = ioremap(bar0, PROBE_MAPLEN);
	if (!h)
		return -ENOMEM;
	for (i = 0; i < ARRAY_SIZE(entries); i++) {
		/* Never readl() outside the mapping: on a card that is serving
		 * traffic that is an OOPS, not a clean failure. A table entry past
		 * PROBE_MAPLEN is a generator bug; skipping it costs one word,
		 * honouring it can cost the host.
		 */
		if (entries[i].off + 4 > PROBE_MAPLEN) {
			pr_info("FFPROBE %s %08llx SKIPPED %08x\\n", entries[i].name,
				(u64)bar0 + entries[i].off, PROBE_MAPLEN);
			continue;
		}
		pr_info("FFPROBE %s %08llx %08x\\n", entries[i].name,
			(u64)bar0 + entries[i].off,
			readl(h + entries[i].off));
	}
	if (ffrows)
		pr_info("FFPROBE ffrows_done 00000000 %08x\\n",
			ffprobe_read_rows(h) ? 1 : 0);
	iounmap(h);
	return -EAGAIN;
}

static void __exit ffprobe_exit(void) { }

module_init(ffprobe_init);
module_exit(ffprobe_exit);
"""


def main():
    with io.open(MAP, encoding="utf-8") as f:
        m = json.load(f)
    regs = []
    for key, spec in sorted(m["bit_fields"].items()):
        regs.append((int(spec["reg"], 0), "bitfield." + key))
    for key, addr in sorted(m["defective"].items()):
        regs.append((int(addr, 0), "defective." + key))
    for key, addr in sorted(m["info"].items()):
        regs.append((int(addr, 0), "info." + key))
    entries = []
    for key, loc in sorted(m["fields"].items()):
        for kind in ("status", "fused"):
            if loc.get(kind):
                entries.append((int(loc[kind], 0), "%s.%s" % (key, kind)))
    entries += regs
    body = ['\t{ 0x%08x, "%s" },' % (off, name)
            for off, name in sorted(entries)]
    # The window has to cover every word the module touches: the framebuffer,
    # MMU and HBM PLL words sit at 0x9a_xxxx / 0x98_xxxx, well past the fuse
    # block, and a readl past the end of the mapping is a kernel OOPS on a card
    # that is serving traffic -- not a clean failure. It used to be hardcoded to
    # 0x00830000, which was exactly wide enough for the fuse block and nothing
    # else. The floor keeps the CMD_READ path's macro registers inside.
    last = max([off for off, _ in entries] + [FUSE_REGION_END])
    maplen = min(BAR0_SIZE, (last + 0x1000 + 0xFFF) & ~0xFFF)
    if last >= BAR0_SIZE:
        raise SystemExit("register table offset 0x%x is outside BAR0 (0x%x)"
                         % (last, BAR0_SIZE))
    src = TPL.replace("@BODY@", "\n".join(body))
    src = src.replace("@MAPLEN@", "0x%08x" % maplen)
    src = src.replace("@FFROWS@", ffmacro.probe_c_source())
    print(src)


if __name__ == "__main__":
    main()
