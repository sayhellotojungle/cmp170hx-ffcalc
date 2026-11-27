# ffcalc — GA100 CMP 170HX 熔丝解锁预计算工具

[kate-land GA100 VFI 研究](https://blog.kate.land/posts/using-voltage-glitching-to-unlock-the-a100/)的离线辅助实现：只读采集、潜力分析、烧写计划生成、回放核验，以及可选的断言式烧写执行。纯 Python 标准库，采集端附加一个自卸载的内核探测模块；与 cmpunlocker 无代码依赖。

> **熔丝烧写物理不可逆，错误目标值最坏导致整卡报废。** 计划、核验与烧写的每一道断言都必须真实成立；数据须交叉核对（`verify` / `sim` / 外部读行工具 / 另一张已知好卡），任一不符即停止。烧写执行的风险由操作者承担。

## 依赖

- Python ≥ 3.6（分析端可完全离线）
- 采集端（Linux）：`gcc` / `make` / 与运行内核匹配的 `linux-headers`、`sudo`、已加载 nvidia 驱动的机器（`nvidia-smi` 用于关联 UUID，缺失时退化为按 fingerprint 认卡）

## 子命令

| 命令 | 作用 |
|---|---|
| `scan --dir collected [--outdir cards --plans plans] [--mig] [--a100]` | 全表潜力分析 + 按 UUID 保存 dump 与计划；FF 行未知时不出计划（rc=1） |
| `report dump.json` | 单卡分析：潜力、媒体引擎/MIG、运行时状态、熔丝身份 |
| `gen dump.json -o plan.json [--ffrows ffrows.txt] [--mig] [--a100] [--only …] [--skip …] [--set k=v]` | 生成 FF 烧写记录（自动 16-bit 块合并）；FF 记录未读时拒绝出计划（rc=2，不写文件） |
| `sim dump.json plan.json [--ffrows ffrows.txt]` | 逐字段回放核对 |
| `verify <plan> --dir collected [--ffrows ffrows.txt] \| --dump dump.json` | UUID / fingerprint / 回放 核验 |
| `mergeff dump.json ffrows.txt` | 合并外部读出的 FF 行并报告覆盖是否完整 |
| `burntool status --bdf …` | 写窗口判据（只读） |
| `burntool burn plan.json --dump dump.json --bdf … [--rows 315] [--force] [--yes]` | 断言式烧写（`docs/BURNING.md` §5） |
| `burntool swiff plan.json [--dump …] [--rows …]` | 把计划记录作为易失 IFF 熔丝应用于本次启动，冷启动即撤销（`docs/BURNING.md` §6，本仓库未在实卡执行过） |

常用参数：`--set fbp_disable=0x840` 钉目标值；`--only` / `--skip` 过滤字段，首条试烧建议 `--only fbp_disable`。

## 计划范围

默认范围（不加参数）已放出该卡的全部算力并拿到 Gen4：

| 范围 | 追加字段 | 结果 | 代价 |
|---|---|---|---|
| 默认 | `fbp/fbio/fbpa/gpc/tpc/pes/rop_l2/nvlink/sys_pipe` + PCIe Gen3/4 | 80 GiB + 全 SM + NVLink + Gen4，卡仍为 `CMP 170HX` | 不碰 vBIOS；媒体引擎保持出厂关闭 |
| `--mig` | 三个媒体引擎 floorsweep 位段 | 5 × NVDEC + NVJPG + OFA；MIG 从单一 `1g` 变为 `8g/4g/2g/1g` | 不可逆；与默认同轮通常只多 1 行，事后补烧 3 行 |
| `--a100` | 熔丝 device id + `MAGIC_BITS_D` 整字 | 卡自称 A100 | 一次侵入式 vBIOS 刷写；平台需 128 GB MMIO 窗口；POST 失败只能离线刷 SPI |

`--mig` 与 `--a100` 可同时给出，先后无关：引擎位在 chain1，身份与能力字在 chain2，互不重叠。

## 使用流程

### 1. 采集与分析

```sh
git clone <repo> && cd ffcalc
sudo bash scripts/ffscan.sh              # 默认范围
sudo bash scripts/ffscan.sh --mig        # 追加媒体引擎
sudo bash scripts/ffscan.sh --a100       # 追加身份改写（--no-magic-d 只改 devid）
```

脚本构建 `ffprobe.ko`，对每张 CMP 170HX（device `0x20c2`；已改写身份的为 `0x20b5`，两种都识别）读取 105 个寄存器，并按 `CMD_READ` 读出全部 512 物理熔丝行（只写 `FUSEADDR`/`FUSECTRL`；控制器非 IDLE 则中止并如实报 partial），随后关联 UUID 并出表：

```
card UUID                                BDF           FB now->max    SM  FP         plan
GPU-38bbb4b3-…-0cce33417b95              0000:05:00.0  80->80 GiB    124 fc497d01…  plans/38bbb4b3-….burn.json (0 records)
```

| 列 | 含义 |
|---|---|
| `FB now->max` | 当前显存 → 硬件潜力。两者相等且等于出厂值表示该卡已被物理缺陷封顶，烧写无收益 |
| `SM` | 按 defective 底线可放出的 SM 数 |
| `FP` | 全部 `*_DEFECTIVE` 字与 SBFE 位的 sha256 前 16 位。物理缺陷不随烧写/重启变化；同批次卡的烧前指纹可能与另一张卡的烧后指纹相同，因此认卡与对计划以 UUID 为准 |
| `plan` | 该卡计划；`SKIPPED (FF rows not read)` 表示 FF 行未读全，工具拒绝出计划（见「FF 行读取不可用时的处置」） |

### 2. 计划文件

`plans/<uuid>.burn.json` 是烧写方唯一需要读的文件，每条记录对应一行物理熔丝：

```json
"burn": [ { "row": 297, "record": "0x80070141", "chain": 1, "block": 5,
            "old": "0x9867", "new": "0x8007", "fields": ["fbio_disable"] } ]
```

| 字段 | 含义 |
|---|---|
| `row` | 目标行号，必在 FF 池 222–487 内；计划自动排在该卡最后一条已烧行之后 |
| `record` | 烧入的 32-bit 值，编码见「FF 记录格式」 |
| `chain` / `block` | 记录的可读拆解 |
| `old` → `new` | 该 16-bit 块由真实 FF 快照回放出的当前值 → 烧后有效值；相邻字段的块合并为一条记录 |
| `fields` | 该记录在该块内真正改动了哪些 option word 的位，即烧它的理由 |

顶层另有 `uuid` / `fingerprint`（计划身份）、`targets`（各 option word 目标值）、`analysis`（FB/SM 潜力，含 `--mig` 时的 `analysis.mig` 与 `analysis.engines`）、`warnings`。

`cards/<uuid>.json` 是完整快照，计划由它推出：

| 字段 | 含义 |
|---|---|
| `uuid` / `fingerprint` / `bdf` | 卡的身份（槽位号会变，UUID 不变） |
| `sku` | `8gb` / `10gb`，决定每个 FBP 的折算容量 |
| `ff_rows` | 全部 512 行快照（0–221 出厂 OTP、222–487 FF、488–507 IFF）；未烧行值为 0。计划的存在性与正确性完全依赖该字段覆盖完整。值支持 `"0x0fe805c1"` 与 `267006913` 两种写法，读取统一经 `ffcalc/dump.py` 归一化；新写入统一为 `0x%08x` |
| `option_words` | 当前有效值（`status` 优先于 `fused`，含 `*_defective` 底线）；`xxx__fused` 表示二者不一致 |
| `info` | 只读状态字：熔丝宏（`crc_status`、`efp_status`、`dis_program`、`dsov_status`、`fuse_debugctrl`、`priv_mask`、`plm_fuse_wdata`、`wdata_wr_secure`、`iff_record`、`en_sw_override`）、feature-override 岛（`feat_ovr_*`、`feat_readout_1`）、融合速率档（`ss_*`）、ECC（`opt_ecc_en`、`ecc_ctrl`）、显存几何（`fbpa_cfg1`、`mmu_lmr`、`cstatus_ramamount`）、板级（`gfw_boot`、`opt_pcie_lane_*`、`hbm_pll_*`）、媒体引擎 `OPT_*`/`STATUS_OPT_*` 对 |
| `read_only` | 本次采集是否零写入（执行过 `CMD_READ` 则为 false） |

`collected/` 为原始留档：`bar0map.tsv`（BDF、BAR0 物理基址、UUID）、`probe_<bdf>.txt`（dmesg 摘录，`FFPROBE <名字> <地址> <值>`；`ffrow.<n>` 即第 n 行，`ffrow_error` 表示读行中止）、`kprobe/`（模块源码与产物）。这些与 `cards/`、`plans/` 都是运行产物，不提交。

### 3. 烧写前核验

```sh
python -m ffcalc verify plans/<uuid>.burn.json --dir collected
```

现场重新采集后拒绝四种情况：无此 UUID 的卡；UUID 对而 fingerprint 不对；FF 行读不全；回放不通过（目标行已被占、`old` 不符、烧后达不到目标值）。输出 `OK` 且 rc=0 才可烧。

### 4. 烧写

```sh
python3 -m ffcalc.burntool status --bdf 0000:05:00.0
python3 -m ffcalc.burntool burn   plans/<uuid>.burn.json --dump cards/<uuid>.json \
    --bdf 0000:05:00.0 --rows 315      # 先试烧一行
python3 -m ffcalc.burntool burn   plans/<uuid>.burn.json --dump cards/<uuid>.json \
    --bdf 0000:05:00.0                  # 再上全量
```

卡必须处于烧写窗口（`docs/BURNING.md` §4）。执行顺序与断言见 §5；控制器锁死的处置见 §7。可用 `burntool swiff` 先在 IFF 窗口里预演同一份计划，冷启动撤销，不消耗熔丝（§6）。

### 5. 冷启动闭环

断电重启（FF 只在开机沿重新解析）→ 重跑 `sudo bash scripts/ffscan.sh`：defective 指纹必须不变（SBFE 位翻为 1 属预期），`FB now->max` 与 plan 反映烧后状态，已烧字段消失、剩余记录行号后移。每轮烧写后旧计划作废。

## 熔丝与运行时状态

FF 记录决定资源集合（可用 FBP/GPC/TPC/PES/ROP-L2、NVLink、SYS_PIPE、媒体引擎、PCIe 代际、熔丝 device id）。计算速率档、ECC 开关、显存几何映射不由 FF 池承载，而是每次启动由主机侧解锁程序写入的易失寄存器，断电或驱动内 FLR 后回到出厂值。两层互不蕴含：本仓库的卡 `OPT_ECC_EN` 熔丝为 0，而 `ECC_CTRL` 在本启动报使能；速率选择字仍是出厂值，而计算块不节流。

`report` 的 `[Runtime state (BAR0, this boot)]` 段输出后者，实测读数与逐条解释见 `docs/BURNING.md` §8。快照缺字时该项显示 `not read`，不以 0 填充。

## 媒体引擎与 MIG（`--mig`）

媒体引擎与 MIG 分片由 chain1（`TOP_FS`）的 floorsweep 位段控制，不是驱动按 `10de:20c2` 做的判断，也不是 VBIOS 锁。`--mig` 清除其中三个（`sys_pipe_disable` 默认计划本就清除）：

| 字段 | 位置 | 出厂值 | 清除后 |
|---|---|---|---|
| `nvdec_disable` | chain1 `blk8[15:11]`（bit 139..143） | `0x1f` | 5 个硬件视频解码器（H264/HEVC/VP9/MPEG2/JPEG）。不含 AV1：GA100 的 NVDEC 无 AV1 块 |
| `nvjpg_ofa_disable_bit146` | chain1 `blk9[2]` | `1` | NVJPG0 与 OFA0 之一（见下） |
| `nvjpg_ofa_disable_bit371` | chain1 `blk23[3]` | `1` | 同上；该位夹在 `nvlink_disable[2:0]` 与 `sys_pipe_disable[11:4]` 之间，三字段合成一条记录 `0x000005c1` |
| `sys_pipe_disable`（默认已含） | chain1 `blk23[11:4]` | `0xfe` | 8 条 SYS_PIPE，即 `8g/4g/2g/1g(+me)` 全套 MIG profile |

- **只能靠烧熔丝**：`FUSE_EN_SW_OVERRIDE(0x820040)=0` 时 `CTRL_OPT_*`（`0x82080c` NVJPG / `0x820824` NVDEC / `0x820a30` SYS_PIPE）的易失写推不动 `STATUS_OPT_*`，在写窗口内直接写 `0x820378`、`0x8203a4`、`0x820824`、`0x82080c` 同样无效。这条覆盖路只对 floorsweep 位失效；feature-override 岛（`0x8238xx`，速率/ECC/几何）是另一套机制，每启动被主机侧解锁程序写入。
- **零缺陷证明**：这三个引擎无 `*_DEFECTIVE` 寄存器，硬件计算 `STATUS_OPT_* = OPT_DISABLE | OPT_DEFECTIVE`，故 `STATUS_OPT == OPT_DISABLE` 即无缺陷。实测两卡：`nvdec/status_nvdec = 0x1f/0x1f`、`nvjpg = 0x1/0x1`、`ofa = 0x1/0x1`、SYS_PIPE `0x8203a4/0x820e30 = 0` 且 `sys_pipe_defective = 0`。计划仅当快照读到相应 `OPT`+`STATUS` 对时才出行，缺字跳过并警告，`--set` 不能绕过。
- **行代价**：记录写整块，故取决于本轮是否本来重写该块。与默认计划同轮为 1 行（仅 `blk9` 新增）；能力记录已烧完的卡为 3 行（`blk8`/`blk9`/`blk23` 各一行）。`gen --mig` 打印该卡本轮实测数字。
- `bit146` 与 `bit371` 与 NVJPG/OFA 的归属未分离（外部实测证明两位与两个 `OPT_*` 寄存器双射，但未分离谁对应哪个引擎），因此成对清除；`--only` 只清其一时计划会额外警告。
- 边界：不证明 ECC（与这些位无关，无证据支持把两者混为一谈）；不放 AV1；不影响显存容量与 SM 数；`status_display(0x820c04)=0x1` / `status_disp_head(0x820c08)=0xf` 对应 chain1 bit 78..82，GA100 无显示输出，保持熔丝状态不清除。
- 切换 MIG 模式（`nvidia-smi -mig 1/-mig 0`）触发驱动内 FLR，把易失几何打回出厂 8192 MiB，必须重启由解锁程序恢复。
- 功能侧结果（8 路 MIG 并发、5 路 NVDEC 并发会话、`nvjpegDecode(HARDWARE)`、NvOF API 5.0、`NV_PTOP_DEVICE_INFO2` 槽位填充）出自外部实卡报告，本仓库未做功能侧验证；本仓库自行确认的是位段位置、FF 回放一致性与四对 `STATUS_OPT`/`OPT_*` 读数。

## A100 身份转换（`--a100`）

只需全部能力与 Gen4 时使用默认范围即可；`--a100` 不是解锁的一部分，是换身份。它在默认计划之外追加：chain2 blk37（DEVIDA）`0x416A0942`、blk38（DEVIDB）`0x416A0982`（熔丝编码 `device_id << 1`，`0x20C2→0x20B5`），以及 `MAGIC_BITS_D` 整字 `0x00200000`。

- **启动门槛比较熔丝 device id 与 VBIOS device id**，因此改完熔丝必须刷对应 A100 80GB PCIe vBIOS（ROM 不需修改）；熔丝未改而直刷 A100 ROM 会被拒。
- **DEVIDA 与 DEVIDB 同一轮烧完。** 打开一次烧写窗口的成本远高于多写一条记录，分两轮会把该成本付两次；只烧一半会留下两份 id 不一致、且门槛读哪一份无文档可依。`--skip` 其中一条会触发计划警告。
- `MAGIC_BITS_D(0x820520)=0x14680000`（CMP 出厂值）是 PCIe 能力宣告字，不代表实际速率，也不是启动门槛；清 bit25 即宣告 Gen4（默认计划已清，实测本仓库卡在整字未变时 Gen4 训练通过）。整字改写是本仓库的判断而非外部报告的步骤，`0x00200000` 无实测来源，故该计划会单独警告：不认可则 `--a100 --skip magic_d`，有真 A100 读数则 `--set magic_d=HEX`。
- 冷启动后芯片自认 `10de:20b5`，lspci、驱动、GSP/DevInit 一律按 A100 识别。平台必须能分配 128 GB 的 64-bit prefetchable BAR1（`Above 4G Decoding` 开、`CSM` 关、`Re-Size BAR` 开、`MMIO High Size ≥ 128G`），判据是 MMIO 窗口大小。
- 平台 POST 失败时带内无退路，只能 CH341A + SOP8 夹离线刷 SPI。
- 顺序：平台 BIOS 设置 → `nvflash --save` 备份本卡 CMP ROM → `ffscan.sh --a100 [--mig]` 出计划 → `verify` → 同一窗口内烧完 → 冷启动 → 刷 A100 vBIOS → 冷启动 → 重跑 `ffscan.sh` 确认 `[Fused device id]` 为 `0x20b5`。
- 状态：改熔丝 devid → 刷 A100 ROM → 识别为 A100 80GB PCIe 这条路线出自外部实测报告；本仓库验证的是 FF 编码与记录格式自洽（`gen --a100` 生成的正是那两条记录），未在自己的卡上走过该路线。平台侧结果一律按未验证对待。

## FF 行读取不可用时的处置

`ff_rows` 缺失或覆盖不全即视为未读，工具一律拒绝出计划。此时：

- 卡不空闲：`FFROWS=0 sudo bash scripts/ffscan.sh` 先只采 words，卡空闲后单跑 `python3 -m ffcalc.capture --read-ff-rows -o cards/<uuid>.json`；
- 外部读出 512 行后存为行文件（每行 `<行号> <值>`，十进制或 `0x`，未烧行必须写 0，允许 `#` 注释与 dmesg 前缀），再 `python -m ffcalc mergeff cards/<uuid>.json ffrows.txt` 合并并重跑 `scan`（或 `gen --ffrows ffrows.txt`）。

`mergeff` 合并前校验快照已有每一行（行号 0–511、值为 32 位、同一行不得有两个值），任一不合格即 `FAIL:` 退出且不写入任何字节。保存为原子操作：同目录临时文件 → 回读校验 → 重命名。外部行文件与快照同行不同值时以现场读取值为准并打印被覆盖的行。

## 退出码与消息

| 消息 / 命令 | rc | 含义与下一步 |
|---|---|---|
| `FAIL: <快照路径>: …` | 2 | 快照不可信（行号越界、值超 32 位、JSON 不完整、同行双值）。文件未被修改；修正后重跑或重新采集 |
| `REFUSED: ff_rows is empty / covers only …`（gen） | 2 | FF 快照不全，未写出计划；走「FF 行读取不可用时的处置」 |
| `SKIPPED (FF rows not read)`（scan 表格） | 1 | 同上，dump 已保存，补行后重跑 |
| `STILL partial: N of 266 FF rows unknown`（mergeff） | 1 | 行文件未列全（通常是未烧行未写 0） |
| verify `FAIL: …` | 1 | 换卡重采 / 排查 defective 是否被烧 / 先补 FF 行 |
| verify `OK: plan … belongs to card …` | 0 | 可烧 |
| burntool `REFUSED: …` | 1 | 计划、快照或窗口条件不满足，卡未被写入 |
| burntool `ABORT: …` | 3 | 控制器锁死 / 超时 / 未授权：停止一切 BAR0 读 → 整机断电 → 重新进窗口先 `CMD_SENSE`（`docs/BURNING.md` §7）。断点续烧安全 |
| burntool `plan warning:` / `NOTICE: … fused device id …` | 0 | 提示非错误：该计划改写熔丝 devid，后续欠一次 A100 vBIOS |
| `WARN: nvjpg_ofa_disable_bit146/bit371: OPT_*/STATUS_OPT_* not in this snapshot` | 0 | 快照缺该对字，本轮不出行；重新采集后 `gen --mig` |
| `WARN: row cost of --mig on this card: N extra FF row(s)` | 0 | 该卡本轮的真实行代价 |
| `refusing to load the fuse probe` | 3 | 卡上有计算进程（`FFROWS=1` 时含解锁程序 keeper）；先停负载，`FORCE=1` 自行承担风险 |
| `insmod: … Resource temporarily unavailable` | — | 正常：探测模块打印完毕 `return -EAGAIN` 自卸载 |

## FF 记录格式

```
bits[4:0] = chain   bits[15:6] = 16-bit 块号   bits[31:16] = 块新值   (bit5 = 0 表示 FF)
```

例：`0x0fe805c1` = chain1 / block23 / data `0x0fe8`。链内后写覆盖先写，故新记录追加在最后一条已烧行之后。FF 池 222–487 共 266 行，上界由卡上报的 `IFF_RECORD(0x820050)=0x01fb01e8`（IFF 窗口 488–507）确定。

## 采集原理

目标内核拒绝 GPU BAR0 的 sysfs `resource0` mmap（BAR1/3 正常），故走 `samples/gen_probe_c.py` 从 `ffcalc/data/ga100-fuses.json` 生成的内核模块：`ioremap(bar0, PROBE_MAPLEN)` → `readl` 全表 → 可选 `CMD_READ` 读行 → `pr_info` → 自卸载。`PROBE_MAPLEN` 由表内最大偏移自动推导（当前 `0x9a5000`，覆盖到 `0x9a3c7c` 的 FB/HBM 侧寄存器），模块内对越界表项跳过读取——超出映射的 MMIO 读在带驱动的卡上是内核 OOPS。words 通道对设备零写入；`capture.py` 默认 `O_RDONLY` + `ACCESS_READ` 映射，只有 `CMD_READ` 与烧写路径请求可写映射。`ffscan.sh` 在检测到计算进程时拒绝 `insmod`。原始日志格式：

```
FFPROBE <字段名> <寄存器物理地址> <32bit值>      # 熔丝行行名形如 ffrow.<行号>
```

`ffcalc/ingest.py` 按 BAR0 前缀分组、还原 option words、计算 fingerprint。

## 已确认事实

- 采集过的三张卡：两张 80 GiB 潜力（10 FBP，124 SM / 108 SM），一张 64 GiB 封顶（`defective 0x2d`）；defective 底线全部读到，option word 映射与研究数据一致。
- 烧写闭环：一张卡 17 条记录（行 304–320），`CMD_PROGRAM=2` 确认（瞬时 `0xE00C0000`）、空白行读 0、CRC 自动重算（烧后仍 `0x55`）、冷启动 `BOOT0=0x170000a1`、潜力全部落地。细节与断言清单见 `docs/BURNING.md`。
- 两卡当前状态：512 行快照齐全、计划 0 记录、`IFF_RECORD=0x01fb01e8`、`CRC=0x55`、`EFP=1`、`DIS_PROGRAM=0`、`PRIV_MASK=PLM_FUSE_WDATA=0xffffffff`、`GFW_BOOT=0x3FF`。
- `DISABLE_GEN3_SPEED` 在 block39 的位归属（bit1 或 bit9）未定，默认两位全清；实测两位全清且 `MAGIC_BITS_D` 仅动 bit25 时 Gen4 训练通过。
- 10 GB SKU 每 FBP 折算容量按 4 GiB 估算。
- FF 行读出已内置：`ffprobe.ko` 默认执行 `CMD_READ` pass（协议与 ga100vfi 一致，Python 与 C 同源生成于 `ffcalc/ffmacro.py`），控制器忙则中止并保留已读行，工具按"读取不完整"拒绝出计划。

## 仓库结构

```
ffcalc/optionmap.py    option word ↔ FF chain/bit 映射、FF/IFF 行区间
ffcalc/ffrecord.py     FF 32-bit 记录编解码 + 链回放
ffcalc/ffrows.py       FF 行快照完整性判定 + 外部行文件解析/合并
ffcalc/ffmacro.py      融合宏协议（CMD_READ 读行、sense/arm/program_row、IFF 预演、写窗口判据；Python 与内核 C 同源）
ffcalc/burntool.py     断言式烧写 CLI（status / burn / swiff）
ffcalc/analyze.py      潜力分析与运行时状态
ffcalc/generate.py     计划生成（16-bit 块合并、行分配）
ffcalc/simulate.py     计划回放核对
ffcalc/ingest.py       探测日志 → UUID 化 dump + fingerprint
ffcalc/capture.py      sysfs BAR0 采集（内核允许映射的主机；--read-ff-rows 读行）
ffcalc/data/ga100-fuses.json   寄存器地址表
samples/gen_probe_c.py         从地址表生成 ffprobe.c
samples/build_sample.py        构造测试样本快照
scripts/ffscan.sh              一键采集与分析
docs/BURNING.md                烧写操作规程、寄存器实测值、运行时状态表
tests/                         unittest，无外部依赖
```

## 测试环境

Debian 12，内核 6.18.x-trim，nvidia-open 610.x（含 cmpunlocker 的 p2p 与 rebar 修复补丁）。该内核对 NVIDIA BAR0 的 sysfs `resource0` mmap 返回 EINVAL，故默认走内核模块；其他内核可先用 `ffcalc/capture.py`。

## 数据来源与致谢

寄存器地址与 option word 位映射来自 [kate-land / ga100vfi](https://blog.kate.land/posts/using-voltage-glitching-to-unlock-the-a100/) 的社区研究，非 NVIDIA 官方文档；`CMD_READ` 读行流程（`FUSECTRL`/`FUSEADDR`/`FUSEDATA` 偏移、IDLE 判据、超时与地址恢复）照其 `gpu0-registers/gpu0-capture` 实现，见 `ffcalc/ffmacro.py`。媒体引擎与 MIG 一节另采信一份 CMP 170HX 的 floorsweep 报告（stock `nvidia-615srv-open` + `zagitta/gpu-unlock`，CC0），其中功能侧结论本仓库未复现，已在各处标注。

## License

MIT（见 LICENSE）。
