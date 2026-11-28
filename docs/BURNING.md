# FF 熔丝烧写操作规程

熔丝烧写**物理不可逆**，错误目标值最坏导致整卡报废。本文件是 `python -m ffcalc.burntool` 的执行依据：寄存器与命令值、写窗口判据、断言顺序、锁死恢复、熔丝与运行时状态的边界。所有数值均为本仓库在自己卡上读出的实测值；未实测的项目在标题或正文中明确标注，不作为依据。

> 三道闸缺一不可：`ffcalc verify`（UUID / fingerprint / 回放）、burntool 的离线核验（行占用、old 值、重编码、FF 范围）、烧写现场逐行回读对快照。任何一道不满足即停止，不允许绕过。

## 1. 熔丝阵列地址空间

| 行区间 | 性质 |
|---|---|
| 0–221 | 出厂 OTP（硬行）。只读，**禁止写入**；`*_DEFECTIVE` 掩码在这一区，FF 记录寻不到它（见 README「硬行与 RIR」与 `ffcalc/hardrows.py`） |
| 222–487 | FF 池（266 行），烧写计划唯一允许使用的区间 |
| 488–507 | IFF 窗口（易失，冷启动失效） |
| 508–511 | 未使用 |
| 宏：RIR | 物理独立的 8 行 × 每行 2 条 16-bit 记录；读它要把 `FUSEDEBUG(0x820060)` 的 bit1 置 1（该位是写操作）。本仓库不读不写，只解码外部读数（`ffcalc/rir.py`） |

- 上界来源：卡自己上报 `IFF_RECORD(0x820050) = 0x01fb01e8` → start `0x1e8`=488、last `0x1fb`=507。FF 池到 `IFF_ROW_MIN - 1` = 487 为止，不是估值。
- `ffmacro.program_row()` 对行号做硬断言，区间外的行一律拒绝；这层保护独立于计划生成器。
- 禁止写入清单：行 0 与行 1。外部报告称对这两行写全 1 会使融合控制器永久锁死；该后果不去验证，规则按禁止执行，代码层同样拒绝。
- 熔丝只能 0→1。同一链内后写的记录覆盖先写的记录，因此新记录必须追加在该卡最后一条已烧行之后。
- 硬行的"每行都有等值副本"不完全成立：实测三块 die 都有同样的 21 行两份不等值（18、48–80 偶数、84、86、94），而 `CRC_STATUS` 仍为 `0x55`，说明这些字不跨副本比对。`hardrows.factory_words()` 取偶数行副本（本仓库 8 GB 卡的 row18/row19 仅差 bit8，正是该卡出厂 RIR 记录 `0x0923` 补的那个位）；`hardrows.copy_mismatches()` 把它作为快照事实报出，`defmap` 打印。求解出的字段都不落在这 21 行内。

## 2. FF 记录格式

```
bits[4:0]  = chain        bits[5] = 0 (FF)
bits[15:6] = 16-bit 块号   bits[31:16] = 块的新值
```

例：`0x0fe805c1` = chain1 / block23 / data `0x0fe8`。记录写的是**整个 16-bit 块**，所以一条记录的行代价取决于本轮是否本来就要重写该块。

## 3. 寄存器（实测值）

| 寄存器 | BAR0 偏移 | 健康态读数 / 说明 |
|---|---|---|
| `FUSECTRL` | `0x820000` | 命令/状态。状态 = bits[20:16]，IDLE=4（`0xE0040000`）；命令 = 低 5 位直写 |
| `FUSEADDR` | `0x820004` | 行号锁存；读写行之前写入，结束后恢复原值 |
| `FUSEDATA` | `0x820008` | **只读**数据口；向它写记录会静默丢失 |
| `FUSEWDATA` | `0x82000c` | 记录写入口，PROGRAM 之前写这里 |
| `IFF_RECORD` | `0x820050` | `0x01fb01e8`：IFF 窗口 488–507 |
| `EN_SW_OVERRIDE` | `0x820040` | 0（出厂）；`CTRL_OPT_*` floorsweep 覆盖路的总开关 |
| `FUSEDEBUG` | `0x820060` | 必须为 0。bit1 = RWL，文献记为 RIR 宏选择位（为 1 时读到的行是 RIR 的 8 行而非主阵列；该后果本仓库未复现，判据按整字必须为 0 执行）。置这一位是**写**操作，故 words 通道不读 RIR |
| `FUSE_DEBUGCTRL_PLM` | `0x820054` | 外部报告称 `0xff`；本 bench 读 `0x33`。`FUSEDEBUG` 的 PLM 影子（只影响写权限，不改 bit1 的"必须为 0"判据），故仅作信息，不参与任何判据 |
| `CRC_STATUS` | `0x820070` | `0x55` = 链 CRC 健康；烧写后仍为 `0x55`，CRC 由控制器自动重算 |
| `EFP_STATUS` | `0x820078` | 1 |
| `DIS_PROGRAM` | `0x82007c` | 0；为 1 表示编程被禁止 |
| `DSOV_STATUS` | `0x820084` | 写窗口内 = 0；驱动在场时 = 1（同一字在 floorsweep 文献里叫 `FUSE_DIS_SW_OVR`） |
| `IFF_SW_FUSING` | `0x820088` | 写 1 使一条 IFF 记录在本次启动生效 |
| `PLM_FUSE_WDATA` | `0x8200f4` | `0xffffffff` = `FUSEWDATA` 通路已放开；未放开时写入被丢弃 |
| `PRIV_MASK` | `0x8200fc` | `0xffffffff` = 权限放开（含 SBFE PLM）；不满即为近失状态，不构成可用窗口 |
| `SBFE_SHADOW` | `0x821108` | 授权影子位，`DSOV=0` 时可写，写 0 = 授权烧写；冷启动恢复为 1 |
| `WDATA_WR_SECURE` | `0x82048c` | 写 0 → `PLM_FUSE_WDATA` 自动读 `0xffffffff`。直接写 PLM 无效 |
| `MAGIC_BITS_D` | `0x820520` | `0x14680000`（CMP 170HX）：PCIe **能力宣告**字，不代表实际速率 |

| 命令（`FUSECTRL` 低 5 位） | 值 | 行为 |
|---|---|---|
| `CMD_READ` | 1 | 读 `FUSEDATA`；未烧行读 `0x00000000` |
| `CMD_PROGRAM` | 2 | 烧一条记录；瞬时状态 `0xE00C0000`（state=3），随后回 IDLE |
| `CMD_SENSE` | 3 | 重新解析 OTP→影子，保留 DSOV；冷启动或 PEX_RST 之后第一步执行 |

`PEX_RST`（冷启动或平台复位脉冲）使卡重新解析 FF 记录，效果等同完整冷启动，是烧写后免断电的复核手段。

## 4. 写窗口判据

未授权状态（任何卡、任何时刻都可安全检查，对应 `ffmacro.window_problems()`）：

```
FUSECTRL idle ∧ ¬poisoned ∧ FUSEDEBUG = 0 ∧ CRC_STATUS = 0x55 ∧ EFP_STATUS = 1
∧ DIS_PROGRAM = 0 ∧ DSOV = 0 ∧ PRIV_MASK = 0xffffffff
```

授权状态（允许 `CMD_PROGRAM`，对应 `ffmacro.armed_problems()`，在上面基础上追加）：

```
∧ SBFE_SHADOW = 0 ∧ WDATA_WR_SECURE = 0 ∧ PLM_FUSE_WDATA = 0xffffffff
```

`DSOV = 0` 单独不构成写窗口：清掉 DSOV 而 `PRIV_MASK` 未放开的近失状态下写入会被丢弃，必须连同权限位一起判定。烧写只允许在卡**从未被 PCIe 枚举**时进行（VFI/glitch 上电 + PEX_RST 脉冲，驱动未绑定）；已被枚举过的卡必须整机断电（不是 warm reboot）回到该状态。

## 5. 烧写流程

`burntool burn` 强制以下顺序，任一步失败即中止且不写卡：

1. **回显计划自带的 `warnings`**。计划含身份改写时额外打印 `NOTICE`：冷启动后卡自认 A100，后续欠一次 vBIOS 刷写，平台 POST 失败只能离线刷 SPI。
2. **离线核验**（不碰卡）：`simulate(plan, dump)` 必须 0 错误——记录重编码、行占用、`old` 与快照回放一致、目标行空白、行号在 FF 池内。输入文件本身必须可解析；计划缺 `burn` 或快照任一行非法（行号越界、值超 32 位、同一行两个值、JSON 不完整）即 `REFUSED`，且文件未被修改。
3. **`YES` 确认**，位于警告与离线核验之后。
4. **窗口判据**（§4 未授权状态）复检，控制器不得处于锁死。
5. **`CMD_SENSE`**，完成后 `DSOV` 仍必须为 0。
6. **现场逐行对快照**：计划涉及的每一行 `CMD_READ` 回读必须与 `dump["ff_rows"]` 一致。
7. **`arm`**：写 `SBFE_SHADOW=0`、`WDATA_WR_SECURE=0`，断言 §4 授权状态全部成立。
8. **逐条 `program_row`**：断言已授权 → 目标行读 0 → 锁行校验 → 写 `FUSEWDATA` → `CMD_PROGRAM` → 等 IDLE（≤10 s）→ 回读必须等于记录值。目标行已恰好等于该记录值（上次中断已烧成）时原样跳过，因此中断后重跑同一计划是安全的。
9. **收尾**：再读一次状态（`CRC_STATUS` 应仍 `0x55`），冷启动，重跑 `scripts/ffscan.sh`：defective 指纹不变（SBFE 位翻为 1 属预期），剩余计划应显著缩短或为空。每轮烧写后旧计划作废。

首次执行建议用 `--rows <单行>` 单条试烧（例如 `fbp_disable` 那条），确认 PROGRAM 行为与 CRC 自补之后再上全量。

## 6. 预演：`burntool swiff`

把计划的记录写进 IFF 窗口而不是烧进 FF 池，冷启动即全部消失；不发 `CMD_PROGRAM`，不消耗任何熔丝单元。窗口要求与 `burn` 相同（§4），窗口按 `IFF_RECORD` 读取，每条记录占一行，工厂卡为 488–507 共 20 行，超出该行数的计划必须分批。

**本仓库尚未在实卡上执行过这条路径。** 序列中的每个寄存器都已从两张卡读出，序列本身有针对假控制器的单元测试，但卡收到 IFF 记录后的行为（SM 数、显存、链路是否随记录变化）属于外部报告的描述，不是本仓库的实测结论。首次执行即为验证：应用后立即读 deviceQuery / 显存容量 / 链路宽度，然后冷启动撤销。

## 7. 控制器锁死与恢复

任何命令失败后 `FUSECTRL` 停在 `0xBADFxxxx`（state=31），控制器不再应答任何 BAR0 读。处置：

1. 看到 `0xBADF` 前缀立即停止一切读操作；
2. 整机断电冷启动；
3. 重新进入窗口后先发 `CMD_SENSE`，确认 `FUSEDEBUG=0`、`CRC_STATUS=0x55`，再评估是否继续；
4. 锁死未清除前不得重试 `CMD_PROGRAM`。

`burntool` 在每一步检测锁死并以 rc=3 中止，消息中给出上述指引。断点续烧安全：已达目标值的行会被跳过。

## 8. 熔丝决定的量与运行时决定的量

FF 记录决定资源集合，运行时寄存器决定本启动的实际状态。两者不可互相推断：熔丝全开的卡仍可能以节流、无 ECC 的状态起来。

| 由 FF 记录决定（持久） | 由运行时寄存器决定（每次启动由主机侧解锁程序重写，断电/FLR 回到出厂值） |
|---|---|
| FBP/FBPA/FBIO/GPC/TPC/PES/ROP-L2 可用集合、NVLink、SYS_PIPE、媒体引擎 floorsweep、PCIe Gen3/Gen4 位、熔丝 device id | 计算块速率档、ECC 开关、显存几何映射、行重映射 |

实测读数（两张 80 GiB 潜力的卡，驱动在场、空闲）：

| 项 | 寄存器与读数 | 含义 |
|---|---|---|
| 计算速率是否满速 | `FEAT_READOUT_1(0x823818) = 0x00000000` | feature-override 岛的读出字：解锁态读 0（无节流位）。本仓库仅在解锁态采集过该字，未取到节流态样本 |
| 速率覆盖来源 | `feat_ovr_sm_spd_0(0x82381c)=0x88888888`、`_1(0x823820)=0x00000008` | 易失，每启动由主机侧解锁程序写入 |
| 融合速率档 | `ss_ffma(0x82059c)`、`ss_fmla16`/`ss_fmla32`/`ss_imla0..4` 均读 `0x5`；`ss_dp(0x820224)=0x1` | 社区地址表把 `0x5` 标为 1/32 档。实测该字为节流值而计算块并不节流，说明有效速率由上面的易失覆盖决定，FF 池不承载这些字 |
| ECC | `OPT_ECC_EN(0x820228)=0x0`、`ECC_CTRL(0x9a0470)=0x41`、SRAM 覆盖 `0x00999999/0x003ffebf/0x0000000f` | 熔丝未开 ECC；`0x41` 置 bit0（MASTER_EN）与 bit6（SIDEBAND_EN）。`nvidia-smi -q -d ECC` 报 `ECC Mode: Enabled` 且容量仍为 81920 MiB，即侧带 ECC 不占主存。以上为本启动的运行时状态，与熔丝无关 |
| 显存几何 | `FBPA_CFG1(0x9a0204)=0x02779000`、`MMU_LMR(0x100ce0)=0x0000028B`、`CSTATUS_RAMAMOUNT(0x9a020c)=0x1000` | 与 80 GiB 映射一致的读数（`0x28B` 为该映射的 `LOCAL_MEMORY_RANGE` 值）。驱动内 FLR（例如 `nvidia-smi -mig 1`）会把它打回出厂 8192 MiB |
| GSP 固件启动 | `GFW_BOOT(0x118234)=0x3FF` | 全部步骤完成 |
| PCIe 通道 | `OPT_PCIE_LANE_DISABLE(0x820394)=0`、`OPT_PCIE_LANE_DEFECTIVE(0x8205e4)=0` | 没有通道被熔丝或硅片限制；实际训练宽度由插槽与 BIOS 决定（`lspci LnkSta`） |
| HBM 时钟 | `HBM_PLL_COEFF(0x98bc98)`：有负载时 `0x00014001`（27 MHz × 64 = 1728 MHz）、空闲时 `0x00011401`（× 20 = 540 MHz） | 该字随负载变化，是 HBM 调频，不反映熔丝状态；主机侧驱动打印的 `busy_coeff=0x14001` 与此一致 |
| PLM | `feat_ovr_plm(0x823804)=0xffffffff`、`hbm_pll_plm(0x9a3c7c)=0xffffffdf` | 解锁程序打开的权限位 |
| 行重映射 | `feat_ovr_row_remap(0x823824)=0`；`nvidia-smi -q -d ECC` 报 `Remapped Rows: N/A` | 该两卡不呈现行重映射计数（无重映射存储可查） |

`ffcalc report` 的 `[Runtime state (BAR0, this boot)]` 段输出以上内容；快照中缺这些字时该项显示 `not read`，不会用 0 代替。

## 9. 计划包含媒体引擎位（默认在内，`--no-mig` 退出）

chain1 的三个 floorsweep 位段——`blk8[15:11]`（5 × NVDEC）、`blk9[2]`、`blk23[3]`（NVJPG0 + OFA0）——在默认计划里；`--no-mig`（或 `MIG=0`）把它们拿掉，`--mig` 仍可显式写出。烧写动作与其他记录完全相同，§1–§7 规则一条不漏。放在默认里是因为同一轮做比后补便宜：同一批位在同轮里搭车重写块，实测 1 行，事后补烧 3 行，且少付一次开窗口成本（见 §12 与 README「计划范围」）。

- **位段归属**：`bit146` 与 `bit371` 与 `{OPT_NVJPG_DISABLE(0x820370), OPT_OFA_DISABLE(0x8202d8)}` 的对应关系尚未分离，因此成对清除。
- **零缺陷自证**：这三个引擎没有 `*_DEFECTIVE` 寄存器，硬件计算 `STATUS_OPT_* = OPT_DISABLE | OPT_DEFECTIVE`，故 `STATUS_OPT == OPT_DISABLE` 即零缺陷证据。实测：`nvdec_disable/status_nvdec = 0x1f/0x1f`、`nvjpg_disable/status_nvjpg = 0x1/0x1`、`ofa_disable/status_ofa = 0x1/0x1`，SYS_PIPE `0x8203a4/0x820e30` 均为 0 且 `sys_pipe_defective = 0`。计划只有在快照读到这四对字时才会为相应位出行，缺字即跳过并警告；`--set` 不能绕过。
- **行代价**：本轮本来就要重写 `blk8`/`blk23` 时（新卡走默认计划）新增 1 行（承载 `blk9[2]`）；能力记录已烧完的卡三个位段各占一行，共 3 行。`gen` 打印该卡本轮的真实数字，`--no-mig` 时这份代价为零。
- **烧后校验**：四个 `STATUS_OPT_*` 应读 0。任一仍非 0 表示该单元物理缺陷，不是烧写失败（FF 层回读等于新值即可），不得补烧。
- `0x820c04` / `0x820c08`（DISPLAY / DISP_HEAD，chain1 bit 78..82）保持原样：GA100 无显示输出，清除它们没有收益。
- **MIG 模式**：`nvidia-smi -mig 1/-mig 0` 触发驱动内 FLR，会把易失的 `FBPA_CFG1` / `LOCAL_MEMORY_RANGE` 打回出厂 8192 MiB，已在世的 slice 缩为 `1g.1gb`，因此切换 MIG 模式后必须重启，让主机侧解锁程序先恢复满容量几何。清 `sys_pipe_disable` 之后 `mig -lgip` 才出全套 profile 是外部报告的功能侧结论，本仓库未做 MIG 功能验证；本仓库确认的是该位段位置与 `SYS_PIPE` 的 `STATUS == OPT == 0`、`sys_pipe_defective = 0`。

## 10. 计划包含身份改写（`--a100`）

`gen --a100` 追加 chain2 blk37（DEVIDA）、blk38（DEVIDB）= `0x416a`（`0x20B5 << 1`）即记录 `0x416A0942` / `0x416A0982`，并把 `MAGIC_BITS_D` 整字写成 `0x00200000`（按卡现值涉及 blk45/46/47 中真正变化的块）。

- `MAGIC_BITS_D` 是能力宣告字而非速率寄存器，且不是启动门槛；清 bit25 即宣告 Gen4（默认计划已清，本仓库卡整字未动时 Gen4 训练通过）。整字改写是本仓库的判断，`0x00200000` 无实测来源，可用 `--a100 --skip magic_d` 退出，或 `--set magic_d=HEX` 钉入自己读到的真值。
- 启动门槛比较的是**熔丝 device id**与 VBIOS device id，因此改完熔丝必须刷对应 A100 80GB PCIe vBIOS，ROM 无需修改。先刷 ROM 而熔丝仍为 `0x20C2` 会被拒。
- **DEVIDA 与 DEVIDB 必须同一轮烧完。** 打开一次烧写窗口的成本（台架时序、上电时机）远高于多写一条记录，把两半分到两轮会把同一个成本付两次；而只烧一半会留下两份 id 不一致、门槛读哪一份无文档可依的状态，那种状态下卡起不来。这是本仓库刻意把 `--a100` 默认设成同轮写两条的原因，`--skip` 中其一会触发计划警告。
- 平台必须给出 128 GB 的 64-bit prefetchable BAR1：`Above 4G Decoding` 开、`CSM` 关、`Re-Size BAR` 开、`MMIO High Size ≥ 128G`。判据是 MMIO 窗口大小。
- 平台 POST 失败时带内无退路（驱动不在、nvflash 不可用），只能 CH341A + SOP8 夹离线刷 SPI。
- `old` 列是 FF 层对该块的回放值：出厂在 FF 链中写了 `0x4184`（`0x20C2<<1`），正常显示 `0x4184 -> 0x416a`；若本次读出的行未覆盖承载 id 的记录，`old` 显示 `0x0000` 且计划额外警告——记录写的是整块新值，不受影响，但必须用 `ffcalc report` 的 `[Fused device id]`（读 `0x8204d8` / `0x82056c`）确认当前身份。
- 媒体引擎位默认就在同一份计划里，于是它与身份位同轮执行，计划按链排序，chain1 的引擎位排在 chain2 的身份位之前，`burntool` 按计划顺序执行，因此中途断链留下的状态是"仍是 CMP 170HX、媒体引擎已放开"，而不是"熔丝已是 A100、ROM 未刷"。身份转换不会释放媒体引擎：`--no-mig --a100` 时那三位仍是出厂关闭，日后要放开就付 §9 说的 3 行那一档。

## 11. 采集探针的操作规则

`scripts/ffscan.sh` 从 `ffcalc/data/ga100-fuses.json` 生成 `ffprobe.ko`：`ioremap(bar0, PROBE_MAPLEN)` → `readl` 全表（106 词，不含 RIR 宏：切读 RIR 要写 `FUSEDEBUG` bit1，words 通道就不再是零写入）→ 可选 `CMD_READ` 512 行 → `pr_info` → 自卸载（`return -EAGAIN`）。规则：

- **只在卡空闲时加载。** `ffscan.sh` 检测到计算进程时拒绝 `insmod`（rc=3）；`FFROWS=1` 时主机侧解锁程序的 keeper 进程也视为占用。`FORCE=1` 是显式承担后果的覆盖开关，不是常规参数。
- **映射长度由寄存器表自动推导**（当前 `0x9a5000`，覆盖到 `0x9a3c7c` 的 FB/HBM 侧寄存器）。表项超出 `ioremap` 范围时 `readl` 不产生干净错误：本仓库在该状态下遇到过 `insmod` 被内核终止、整机需要重启。两层防护：生成器在表项超出 BAR0 时拒绝生成；模块内做运行时边界检查，越界表项打印 `SKIPPED` 而不读取。
- words 通道对设备零写入；`capture.py` 默认以 `O_RDONLY` + `ACCESS_READ` 映射 BAR0，只有 `--read-ff-rows`（CMD_READ）与 `burntool burn`/`swiff` 请求可写映射。
- 免驱态刚烧完不要依赖 `CMD_SENSE` 刷新 FF 层：或冷启动，或 `revert` 后 `echo bus > .../reset_method; echo 1 > reset` 触发 bus reset 并重新映射 BAR0，再读新行。读回 `0x00000000` 不等于烧坏。

## 12. 已确认基线（本仓库两张卡）

- 一次完整烧写闭环：17 条记录，行 304–320，`fbp 0x852→0x840`、`fbpa/fbio/rop_l2 0xc0330c→0xc03000`、`gpc→0x20`、`tpc4→0x40`、`tpc7→0x20`、`pes→0x38000`、`nvlink/sys_pipe→0`、Gen4 位清零（block39 的两个 `DISABLE_GEN3_SPEED` 位 + `MAGIC_BITS_D` bit25）。冷启动后 `PMC_BOOT_0 = 0x170000a1`、`CRC_STATUS = 0x55`、FF 区除新 17 行外零改动、defective 全部不变。
- 两卡当前状态：512 行快照齐全，`plans/*.burn.json` 为 0 记录（潜力已全部落地）；`GPU-38bbb4b3…` 124 SM / `fc497d01b8255867`，`GPU-85b20be7…` 108 SM / `3ab5093e8f9f78b5`（该卡 `gpc_disable = gpc_defective = 0x20`，即一个 GPC 由硅片缺陷限制）。
- `tpc_gpc3/6_disable` 的 **status 层读 0x1、fused 层读 0x0**，两张卡一致。两层不一致说明该位当前不是由熔丝决定；fused 层已为 0，向这两个位补烧记录不会改变 status 读数。
- 硬行侧基线（三张卡）：`*_DEFECTIVE` 的位置用 `defmap` 从快照重算，`fbp_defective` = 行26 bit16..27 唯一解，`pes_defective` = 行34 bit20..行36 bit11 唯一解；三块 die 的"两份不等值"行号完全相同（21 行：18、48–80 偶数、84、86、94），`CRC_STATUS` 同时仍为 `0x55`。`fbpa`/`fbio`/`rop_l2_defective` 是每个 FBP 两位的编码，折算后与 `fbp_defective` 逐位一致（三张卡都是），所以四寄存器一份判断可当快照自检用。
- 显存容量的两条实测边界：`GPU-b6b9f653…`（8 GB SKU，`fbp_defective = 0x2d`）64 GiB 是硅片限制，FF 记录无从改动；把该行 disable 位清掉后 `STATUS_OPT_FBP` 跟着目标变成 0x1f，但 `PTOP_GFW_BOOT` 读 `0x1`、`gsp_riscv_status` 为 0（GSP-RM 停在 booter 取指），冷启动仍是 64 GiB。**影子读数通过不是容量类计划的成功判据。**
- 同卡 RIR 由外部工具读出为 rows 0–6 全 `0xffffffff`、row7 `0xffff0923`：16 个槽位全部耗尽，`0x0923` 解出行18 bit8 强制 1，与该卡 row18/row19 仅差 bit8 互为印证。一个硬行位需一条记录 ⇒ 该卡的天花板在本卡上没有可写入口。本仓库不读也不写该宏（切换读多路器要写 `FUSEDEBUG` bit1）。
- 复核（卡上有负载时）：`FORCE=1 FFROWS=0` 在负载卡上能拿到新的寄存器读数（探针打印完读数后以 `-EAGAIN` 退出、不常驻），但拿不到行读数——快照里的 512 行因此是上次采集的。两卡的 option word 与上次逐字相同（熔丝读数幂等再确认）。`FUSE_DEBUGCTRL` 读 `0x0`：读多路器指向主阵列，快照里的行就是主阵列的行。`FUSE_DEBUGCTRL_PLM` 两块 die 都读 `0x33`（外部 dump 记 `0xff`）：它是写权限侧的影子，不参与任何判据。

## 13. 速查

| 症状 | 原因 | 处置 |
|---|---|---|
| 写了记录但行仍为 0 | 写进了只读口 `FUSEDATA` | 写 `FUSEWDATA (0x82000c)` |
| `PLM_FUSE` 写不进 | 由 `WDATA_WR_SECURE` 门控 | 写 `WDATA_WR_SECURE=0`，PLM 自动放开 |
| 冷启动后 `SBFE_SHADOW` 仍为 1 | 冷启动恢复默认值 | `DSOV=0` 时写 0 即授权，属正常 |
| `DSOV=0` 但写入不生效 | `PRIV_MASK` / `PLM_FUSE_WDATA` 未放开（近失状态） | 不是可用窗口，重新冷启动 |
| `FUSECTRL` 读 `0xBADFxxxx` | 命令失败，控制器锁死 | 停止读 → 断电冷启动 → `CMD_SENSE` 评估（§7） |
| `FUSEDEBUG` 非 0 | 外部报告称 bit1（RWL）选择 RIR 宏，读到的是 RIR 的 8 行而非主阵列（该后果未复现） | §4 判据要求 `FUSEDEBUG=0`，不满足即不得烧写 |
| 影子读数与计划一致，但显存没变、GSP-RM 起不来（`PTOP_GFW_BOOT=0x1`、`gsp_riscv_status=0`） | 目标值清掉了 `*_DEFECTIVE` 置位的块：`STATUS_OPT_*` 跟的是 `OPT_DISABLE`，硬行位 FF 记录够不到 | 不是烧写失败也不是可救状态：`defmap` / `report` 的 `[Hard rows]` 段核对天花板；`gen` 默认拒绝该目标（`--allow-hard-floor` 才放行）。实测发生在 8 GB SKU 卡（`fbp_disable == fbp_defective == 0x2d`）|
| `report` 报 `CROSS-CHECK FAILS` | 快照的 `fbpa`/`fbio`/`rop_l2_defective` 折算后与 `fbp_defective` 不等（三块实测 die 都等值，且都是每个 FBP 两位的编码） | 容量按 `fbp_defective` 读；查该快照是否混了两张卡或某寄存器读数被误映射 |
| 默认计划里没有 NVJPG/OFA 位 | 快照缺 `OPT_*`/`STATUS_OPT_*` 对，或给了 `--no-mig` | 用当前采集表重跑 `ffscan.sh`；`--set` 不能绕过缺字，`--mig` 只解决 `--no-mig` |
| 烧完某个 `STATUS_OPT_*` 仍非 0 | 该单元物理缺陷 | 非烧写失败，不得补烧 |
| 开/关 MIG 后显存显示 8192 MiB、slice 缩成 `1g.1gb` | 驱动内 FLR 恢复出厂几何 | 重启，让解锁程序先恢复几何 |
| 烧完 devid 后卡不 POST | 熔丝为 A100、SPI 内为 CMP ROM | 刷 A100 80GB PCIe vBIOS；已挂死则 CH341A 离线刷 SPI |
| 刷了 A100 ROM 仍卡 VGA | 平台未给出 128G prefetchable BAR1 | `Above 4G` / `CSM=off` / `Re-Size BAR` / `MMIO High Size ≥ 128G` |
| 计划行号与上一轮不同 | 每轮的可用空白行位置随已烧行前移 | 每轮烧完重新 `scan`，以新计划为准 |
| `insmod` 长时间无输出后被终止 | 控制器忙或映射越界 | 检查卡是否空闲；确认 `ffprobe.c` 由当前表重新生成（`PROBE_MAPLEN` 覆盖全表） |
