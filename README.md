# DigitalPhaseMeter

基于 **Chisel** 的数字相位计：测量两路同频信号的相位差。测量通路运行在高速时钟域（DSP 域，**450MHz**，由 PL 内 MMCM 从 50MHz 倍频得到），控制、闸门计数与 AXI4-Lite 接口运行在低速时钟域（50MHz）。

**工程状态：已完成并通过实测验收（2026-10-19）。** 测量与控制 RTL、PS↔PL 互联（AXI4-Lite 控制/状态与结果寄存器 + 测量完成中断）、Vivado IP 打包与 block design、PS 侧裸机软件与 PC 侧上位机均已实现；Verilator 单元测试与顶层用例、Vivado xsim 联调仿真、综合实现签核与上板验收均已跑通。本文件只描述当前设计与用法，历史验证记录不随仓库保留。

## 测量原理

高速时钟域内对两路信号分别做边沿检测，得到三个观测量：

| 输出       | 含义                                                    | 理论值                                                     |
| ---------- | ------------------------------------------------------- | ---------------------------------------------------------- |
| `gateCnt`  | 测量窗口内的低速时钟周期数                              | 由 `in.bits.targetGate` 指定，实际值略大（含停止握手开销） |
| `edgeCnt`  | 窗口内参考信号 `extSig_sig1` 的上升沿数                 | `gateCnt × T_clock / T_sig1`                               |
| `phaseCnt` | 每个参考周期内「参考沿 → 被测沿」的高速时钟周期数累加值 | `phaseOffset / T_dspClock × edgeCnt`                       |

`phaseCnt` 的分辨率就是一个高速时钟周期（450MHz 下 2.222ns），因此高速时钟频率越高、闸门时间越长，测量越精确。`phaseCnt / edgeCnt` 量的是从 `extSig_sig1` 上升到**下一个** `extSig_sig2` 上升沿的**无符号**间隔，因此 `0°~360°` 的相位差都与读数一一对应（`>180°` 同样可测；只有按有符号偏移解释时才需要 `|δ| < 180°`；`0°` 时两沿重合，读数按 1 个 DSP 周期的分辨率量化，判据按取模 360° 比较）。

各观测量的容限与精度下界见 [`.agents/skills/dpm-measurement-model`](.agents/skills/dpm-measurement-model/SKILL.md)。

## 模块结构

```text
DigitalPhaseMeterTop（顶层 RawModule：显式主时钟 / 异步复位 / 高速时钟）
├── ResetSynchronizer        每个时钟域各一个：低有效 resetn 异步置位、与该域时钟同步释放
│                            mainReset 给主时钟域（模块都在 withClockAndReset(clock, mainReset) 里例化），
│                            DSP 域那一个在 Controller 内部（从 mainReset 再同步到 dspClock）
├── Axi4LiteSlave            AXI4-Lite 从口 → 内部读写命令总线（控制/状态/结果寄存器）
├── ControlRegs              控制/状态寄存器堆（含片内源使能与输入选择）+ 结果锁存 + 中断
├── SignalInputMux           主时钟域输入选择：片内源使能门控 + 外部输入二选一（纯组合）
├── Controller               低速域 FSM：闸门计数、与 PhaseMeter 握手、锁存结果
│   ├── ResetSynchronizer    DSP 域复位：从主复位同步得到（异步置位、与 dspClock 同步释放）
│   └── PhaseMeter           高速域测量核心
│       ├── EdgeDetector ×4  3 级流水的边沿检测（RisingA/BothA；sig1、sig2 与两类握手脉冲）
│       └── DspCounter ×2    DSP48E1 累加器计数器，分别累计 edgeCnt / phaseCnt
└── （片内无存储：数据通道就是寄存器本身，见下文「PS↔PL 互联」）
```

片内信号源的 MMCM（`dpm_mmcm_src`，裸 `MMCME2_ADV` 薄封装）不在 IP 内，而是 BD 里的独立单元；PS 通过 AXI4-Lite 写 MMCM 的寄存器镜像，由单独的 `dpm_src_drp` IP 搬进 DRP 口（即「方案 A」）。

测量接口约定：`Controller.in.valid` 发起一次测量，`out.valid` 一次脉冲返回结果；`targetGate` 在启动的那一拍被 `Controller` 锁存，测量期间改写不影响本次测量（连续测量只需重复发起）。测量窗口会等到被测信号的下一个上升沿才关闭，因此实际 `gateCnt` 略大于目标值。

## 信号输入：外部引脚与片内信号源

测量核心之前有一个输入选择器（`SignalInputMux`，主时钟域，纯组合），两条候选通路：

| 通路       | 来源                                                                   | 约束/时基                                                                     |
| ---------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| 外部输入   | `extSig_sig1` → 引脚 **W18**、`extSig_sig2` → 引脚 **W19**（LVCMOS33） | 异步输入，`set_false_path`；在测量域经 `EdgeDetector` 的 ASYNC_REG 采样链同步 |
| 片内信号源 | PL 内 MMCM（`dpm_mmcm_src`）的两路输出                                 | 频率/相位差由 PS 写 MMCM 寄存器镜像（`dpm_src_drp`）设定；同样按异步输入采样  |

两条通路共用同一条采样链：选择器本身在主时钟域，它的**输出**才进测量域，对测量域是一条异步被测信号（与外部引脚同一待遇）。使能与选择位来自同一域的 `SIG_CTRL`，因此不需要任何跨时钟域配置通路。

### 片内信号源：MMCM 的两路输出

```text
        ┌───────────────────────────────┐   clkOut1（f_sig，相位 φ0）──► dpm_top/intSig_sig1 ─┐
        │ dpm_mmcm_src（裸 MMCME2_ADV） │   clkOut2（f_sig，相位 φ1）──► dpm_top/intSig_sig2 ─┤
        │  参考时钟 = FCLK_CLK0 (50MHz) │                                                  ├─► SignalInputMux
        │  DRP ◄── dpm_src_drp（AXI 口）┼── PS：写 MMCM 寄存器镜像 → 写 LOAD → 等 locked   │   ↕ 外部引脚
        └───────────────────────────────┘                         │                        │
                                                                  └► locked ──► SIG_STATUS │
                                                                                            ▼
                                                       PhaseMeter 的异步采样链（与外部输入共用）
```

- **频率** = VCO / D，VCO = 50MHz × MULT / DIVCLK（VCO 必须落在 600～1200MHz），D 为 `CLKOUTx` 的整数分频（1..126），因此输出频率下限约 4.7MHz（600MHz ÷ 126）；实际可用上限受相位计采样率限制，验证覆盖到 50MHz。
- **相位差** = 两个输出各自的相位寄存器之差，步长为 1/8 VCO 周期（VCO 1000MHz 时 125ps，折算到输出是 `45/D` 度）。
- 两者都由 PS 算出 **MMCM 的 16 位寄存器值**，写进地址段 `0x43C1_0000` 的 `dpm_src_drp`（`0x08..0x24` 八个寄存器镜像 + `0x28` LOAD），控制器再按固定顺序搬进 MMCM 的 DRP 口。位域与编码规则（XAPP888）的权威参考实现是 `host/dpm_host/sig.py` 与 `ps/include/dpm_regs.h`（两者逐位一致，由 `host/tests/test_sig.py` / `ps/tests/test_sig.c` 对拍）。**改完写 LOAD 让 MMCM 重新锁定，期间两路输出会停止**（应在测量空闲时改）。
- **9 位相位码限制**：`m = round(相位 × D / 45000) ≤ 511`，因此可请求的相位上界为 `min(23017499/D, 359999)` 毫度；越界请求由固件显式回 `ERR_PARAM`（字段截断会让相位回绕）。`D ≤ 126`（HIGH_TIME + LOW_TIME 各 6 位）。
- **重配置必须先拉高 MMCM 的 `RST`**：`dpm_src_drp` 在收到 LOAD 后自动完成 `RST=1 → 8 笔 DRP 写 → RST=0 → 重新锁定`，软件只需轮询 `locked`。

### 切换与配置（PS 视角）

1. （可选）写 MMCM 寄存器镜像（`0x08..0x24`）设定片内源的频率与相位差，写 `0x28` LOAD 后等 `locked`；
2. 写 `SIG_CTRL = (1<<0) | (1<<1)`：使能片内源并把它接到测量核心（`1<<0` = enable，`1<<1` = srcSelect）；
3. 读 `SIG_STATUS` 确认 `srcLocked = 1`、`sourceReady = 1`。使能门控与二选一都在主时钟域、与写 `SIG_CTRL` 的寄存器堆同域，因此**写这一笔的同时切换就已经生效**；
4. 按常规流程启动测量。切回外部输入写 `SIG_CTRL = 0` 即可。

注意：

- **配置与切换都应在 `STATUS.busy = 0` 时进行**：切换输入源会让正在进行的测量作废；MMCM 重配置期间两路输出停止，正在等待边沿的测量会一直挂着。
- **选择片内源时必须让源真的产生边沿**：`SIG_CTRL.enable = 1` 且 MMCM 已锁定，否则两路输出恒低，测量会一直停在「等待参考沿」的状态（切回外部输入或重新配置即可恢复）。`SIG_STATUS.sourceReady` 给出这个前提是否满足（选择外部输入时恒为 1，因为外部是否有信号无法在 PL 内判断——用一次测量的 `edgeCnt` 即可确认）。

## PS↔PL 互联与寄存器接口

```text
        ┌──────────────┐  M_AXI_GP0 (AXI3)   ┌───────────────────────┐
        │  Zynq-7000   ├────────────────────►│ axi_protocol_converter│
        │  PS7         │  50MHz              │   AXI3 → AXI4-Lite    │
        │  FCLK_CLK0   ├──┬─────────────────►└───────────┬───────────┘
        │  IRQ_F2P[0]  │  │  ┌────────────────────────┐    │ s_axi_lite
        └──────────────┘  └─►│ clk_wiz (MMCM)         ├──┐ ▼（4KB 寄存器区）
                        ▲    │ + Safe Clock Startup   │   │  dpm_top（本仓库打包的 IP）
                        │    └────────────────────────┘   │
                        │       450MHz DSP 域时钟 ────────┘
                        │
                    interrupt
```

**DSP 域时钟 450MHz**：Zynq-7000 的 PS `FCLK` 上限 250MHz（在 BD 里请求更高会被静默降到 250MHz），因此测量核心的高速时钟由 **PL 内的 `clk_wiz`** 从 `FCLK_CLK0`(50MHz) 倍频得到（VCO 1012.5MHz）。「MMCM 锁定前不来时钟」由 **clk_wiz 自带的 Safe Clock Startup** 保证。当前签核结果（xc7z010clg400-1，Vivado 2025.2，2026-10-26 用 HEAD 的 RTL 增量重跑）：**WNS = +0.271ns、WHS = +0.060ns、WPWS = +0.067ns，TNS = THS = 0、0 个失败端点**；methodology 0 新问题（13 条已 waive）、DRC 0、综合实现日志 0 warning。整体 WNS 的限制者仍是 clk_wiz 内部 Safe Clock Startup 路径；测量域自身最差 setup 路径余量在 +0.5ns 量级（2026-10-26 实测 +0.553ns）。`impl_stability.tcl` 的 6/6 稳定性结论对应 2026-10-19 网表，RTL 改动后需重跑后再引用。详细策略、告警清理与签核方式见 [`vivado/README.md`](vivado/README.md) 与 [`.agents/skills/dpm-vivado-ip`](.agents/skills/dpm-vivado-ip/SKILL.md)。

- **所有数据都走 AXI4-Lite 寄存器（GPIO 式）**：一次测量的结果只有 4 个字，PL 侧直接把最新结果锁存在只读寄存器里，PS 收到中断后连续读 4 个寄存器即可拿到一条完整结果。IP 内部没有任何片内存储，对外也只有一个 AXI 从口。
- **中断**：结果锁存完成（或上一次结果被覆盖而丢失）后拉起 `interrupt`（高电平），经 `IRQ_F2P[0]` 通知 PS；软件写 `IRQ_STATUS` 清中断。
- **地址窗口**：`s_axi_lite` 占 4KB（0x43C0_0000），实际只用到前 0x34 字节；片内源的 MMCM 动态重配置口占另一段 4KB（0x43C1_0000）。

### 寄存器映射（`s_axi_lite`，基地址 0x43C0_0000）

| 偏移 | 名称           | 访问 | 说明                                                                                                                                                                                                           |
| ---- | -------------- | ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0x00 | `CTRL`         | 混合 | [0] `start`：写 1 启动一次测量（自清脉冲，`busy` 期间忽略）；[1] `irqEnable`：中断使能；[2] `clear`：写 1 当拍复位结果寄存器与全部标志（不清 `busy`，在途测量继续完成并照常锁存结果）。读回时 bit0/bit2 恒为 0 |
| 0x04 | `STATUS`       | RO   | [0] `busy`：测量中；[1] `resultValid`：有未被读走的结果；[2] `lost`：有结果被覆盖丢失；[3] `irqPending`                                                                                                        |
| 0x08 | `GATE_TARGET`  | RW   | [27:0] 目标闸门计数（低速时钟周期数），按字节选通写入                                                                                                                                                          |
| 0x0C | `RESULT_GATE`  | RO   | 最新结果的 `gateCnt`，实际闸门计数                                                                                                                                                                             |
| 0x10 | `RESULT_EDGE`  | RO   | 最新结果的 `edgeCnt`，窗口内参考信号上升沿数                                                                                                                                                                   |
| 0x14 | `RESULT_PHASE` | RO   | 最新结果的 `phaseCnt`，相位计数                                                                                                                                                                                |
| 0x18 | `RESULT_SEQ`   | RO   | 最新结果的记录序号                                                                                                                                                                                             |
| 0x1C | `LOST_COUNT`   | RO   | 结果未被读走即被覆盖的次数                                                                                                                                                                                     |
| 0x20 | `IRQ_STATUS`   | W1C  | [0] 中断挂起、[1] `resultValid`（写 1 表示结果已读走）、[2] 丢失计数：均写 1 清除                                                                                                                              |
| 0x24 | `VERSION`      | RO   | `0x4450_4D02`（"DPM2"）                                                                                                                                                                                        |
| 0x28 | `CAPS`         | RO   | [7:0] 每条结果字数 = 4；[15:8] 结果深度 = 1；[16] 数据通道为寄存器 = 1；[17] 带片内信号源与输入选择寄存器 = 1                                                                                                  |
| 0x2C | `SIG_CTRL`     | RW   | [0] `enable`：片内源使能（0 时两路输出恒低）；[1] `srcSelect`：0 = 外部引脚输入、1 = 片内信号源。整体按 bit0 字节写入                                                                                          |
| 0x30 | `SIG_STATUS`   | RO   | [0] `enable`；[1] `srcSelect`；[2] `srcLocked`：片内源 MMCM 已锁定；[3] `sourceReady`：当前选择在配置上可用（选外部恒为 1，选片内源要求使能且 MMCM 已锁定）                                                    |

`CTRL` 是「普通读写位 + 写 1 触发位」的混合：`irqEnable` 每次写 `CTRL` 都按 bit1 整体更新，因此启动测量时要写 `CTRL = (1<<1) | (1<<0)`。

### 数据交换流程（PS 视角）

0. （用片内源时）先用 MMCM 的 DRP 寄存器配置频率与相位差，再写 `SIG_CTRL` 选中它；
1. 写 `GATE_TARGET` 设定闸门时间；
2. 写 `CTRL = (1<<1) | (1<<0)`：保持中断使能并启动一次测量；
3. 等待 `IRQ_F2P[0]` 中断（也可以轮询 `STATUS.busy` 变 0）；
4. 依次读 `RESULT_GATE` / `RESULT_EDGE` / `RESULT_PHASE` / `RESULT_SEQ`，得到一条完整结果；
5. 写 `IRQ_STATUS = (1<<0) | (1<<1)`：清中断挂起位，同时告诉 PL「结果已读走」——否则下一次测量的结果会被记为丢失。

结果寄存器只保留最新一条：软件还没读走就再次测量，旧结果会被覆盖、`LOST_COUNT` 加一。需要重新开始计数或丢弃旧结果时写 `CTRL` 的 `clear` 位。与 `Controller` 交互的时序要点：启动时锁存 `GATE_TARGET`（测量期间改写不影响本次测量）；结果在 `out.valid` 那一拍一次性锁存并置中断；「软件清标志」与「新结果到达」同拍时按新结果优先。

## 目录结构

```text
src/main/scala/DigitalPhaseMeter/   Chisel 源码，每个模块一个文件，附 Emit<Name> 生成主类
test/                               Verilator C++ 测试（测量通路与顶层 IP），每个 <Name>.cpp 自动注册一个测试目标（构建时自动生成对应 Chisel 产物）
sim/                                Vivado xsim 的 SystemVerilog 激励与运行脚本（只做真实 MMCM 联调）
vivado/                             Vivado IP 打包、block design、综合实现脚本与顶层约束
build/chisel/<Name>/                Chisel 生成的 SystemVerilog 与 filelist.f（构建产物，不入库）
ps/                                 PS 侧裸机 C 服务端（串口协议、AXI/DRP 驱动、startup/linker）
host/                               PC 侧 Python 上位机（协议、CLI、PySide6 GUI、仿真后端）
docs/                               串口协议与上板指南
spec/                               金标准向量、独立仿真服务端、规格自检与验证脚本
.agents/skills/                     项目级 skill：RTL 生成、测试、测量模型、AXI/Vivado、上位机协议、交付前检查
```

## 环境

| 依赖                                     | 用途                                            |
| ---------------------------------------- | ----------------------------------------------- |
| JDK + Mill                               | 由 Chisel 生成 SystemVerilog（用仓库根 `./mill`；使用系统 PATH 上的 JDK，Mill 版本在 `build.mill` 头部固定）；Verilator 测试目标构建时也会自动调用它 |
| xmake ≥ 3.1.1                            | 构建 Verilator 测试目标、运行测试；构建目标时自动调用 `./mill emit <模块名>` 生成/更新 SV |
| Verilator ≥ 5.050                        | 编译生成的 SystemVerilog（测量通路与顶层测试）  |
| clang（支持 C++20 模块，本项目用 C++26） | 编译测试代码，**不支持 gcc**                    |
| Vivado（本项目在 2025.2 上验证）         | 打包 IP、创建 block design、xsim 仿真、综合实现 |
| uv + Python ≥ 3.12                       | PC 侧上位机与 spec/ 验证脚本                    |

Verilator 测试目标在编译 SV 之前会自动调用下文「生成 SystemVerilog」里的生成入口（`script/rule.lua` 的 `mill.verilator.shared` 规则，逐目标执行 `./mill emit <模块名>`），所以跑 `xmake test` / `xmake run` 前不需要手工生成；JDK 与 Mill 因此也是跑这些测试的前提。

## 使用

### 生成 SystemVerilog（产物在 `build/chisel/<Name>/`）

```bash
./mill emit                                  # 全部 Emit<Name>（测量通路、顶层 IP、DRP 控制器等）
./mill emit Controller DigitalPhaseMeterTop  # 只生成指定模块（多个模块名写在一条命令里）
./mill emit --force Controller               # 强制重新生成（--force 必须写在模块名之前）
./mill emit --check                          # 只查询全部 Emit<Name> 是否需要重新生成（不生成、不写缓存）
./mill emit --check Controller               # 只查询指定模块（--check 与 --force 互斥）
./mill runMain DigitalPhaseMeter.EmitController  # 无条件运行单个主类
```

`emit` 按内容缓存：入口点（`Emit<Name>` 主类）及其传递依赖的字节码依赖闭包没有变化时直接跳过，变化时才执行一次（判定看编译产物、不看源码文本，不改变字节码的编辑不会触发重跑）；缓存按主类隔离，例如改 `PhaseMeter` 只会让依赖它的入口点重跑。缓存命中后还会检查约定产物 `build/chisel/<Name>/filelist.f` 是否存在，产物被删掉时会重新生成。缓存文件在 `build/chisel/.emit-cache/`，`./mill clean` 不会删 `build/chisel/`；需要无条件重跑时用上面的 `runMain`（单个主类）或 `--force`（`emit`）。构建定义与缓存实现见仓库根 `build.mill`。

`emit --check` 只查询、不生成：模块名写法与 `emit` 相同（省略时检查全部 `Emit<Name>` 入口点，多个模块名写在一条命令里），判定依据与 `emit` 的缓存完全一致——同一个缓存键（入口主类及其传递依赖的字节码闭包叠加依赖 classpath 标识等）加上产物 `build/chisel/<Name>/filelist.f` 的存在性检查。它逐模块输出是否需要重新生成及原因（产物缺失、无缓存记录、入口点或其传递依赖的字节码变化），但**不执行生成、不写 `build/chisel/.emit-cache/` 缓存、不改动 `build/chisel/` 下的产物**。全部模块已是最新时退出码为 0；只要有模块需要重新生成，就逐条输出原因并打印汇总，以非零退出码失败（检查语义，与 `./mill fix --check` 相同），适合在 CI 或提交前确认生成产物是否最新。`--check` 与 `--force` 互斥，同时使用会报错；两者都必须写在模块名之前。

每个模块只生成**一份**产物：复位同步由纯 RTL 的 `ResetSynchronizer` 实现，Verilator、xsim 与综合共用同一份 RTL，没有 `--type=sim|syn` 的差别。各消费入口对生成的处理不同：Verilator 测试目标的构建规则会在编译前自动执行 `./mill emit <模块名>`（需要时加 `--force`），所以改完 RTL 直接跑 `xmake test` / `xmake run` 即可；`sim/run_handoff_tb.sh` 默认也自带生成入口；Vivado 流程不会替你生成，改完 RTL 仍需先跑 `./mill emit DigitalPhaseMeterTop MmcmDrpController`（见 [`vivado/README.md`](vivado/README.md)）。生成入口的调用形式与陷阱见 [`.agents/skills/dpm-chisel-rtl`](.agents/skills/dpm-chisel-rtl/SKILL.md)。

模块名可以用 `override def desiredName` 与类名不同（例如 `EdgeDetector` 按边沿类型/同步方式生成 `EdgeDetectorRising` / `EdgeDetectorBoth`）：`build/chisel/<Name>/` 下的文件名与 `filelist.f` 中的模块名以生成结果为准，`xmake`、xsim 与 Vivado 都按 `filelist.f` 取文件，不按目录名推导。

### 代码检查

```bash
./mill compile                 # 编译器 lint（-Werror），0 warning 门禁
./mill fix --check             # Scalafix 规则检查
./mill fix                     # 自动修复可修的规则（导入排序、删除未使用导入）
scalafmt --test                # 格式检查（scalafmt 就地修复）
```

Scalafix 通过 Mill 的 scalafix 插件执行，规则与配置仍在 `.scalafix.conf`，只作用于 `src/main/scala`。Mill 构建定义 `build.mill` 由 Mill 自己编译（Scala 3），不在 Scalafix 规则范围内，但同样要按 scalafmt 风格格式化；格式化用 scalafmt CLI，不是 Mill 任务。规则内容、有意关闭的检查项与验证报告要求见 [`.agents/skills/dpm-pre-delivery-check`](.agents/skills/dpm-pre-delivery-check/SKILL.md)。

### Verilator 测试（测量通路与顶层 IP）

```bash
xmake test -v                             # 全部测试
xmake run Controller                      # 单个测试目标（定位问题用）
```

构建测试目标前会自动调用生成入口：每个 `<Name>Verilator` 目标先执行 `./mill emit <Name>`（只生成该目标对应的模块；`test/xmake.lua` 用 `mill.verilator.shared` 规则接入，规则定义在 `script/rule.lua`），再把 `build/chisel/<Name>/filelist.f` 列出的 SV 交给 Verilator。因此改完 RTL 不需要先手工跑 `./mill emit`；要强制重新生成时用 `./mill emit --force <Name>`。

覆盖 `Controller`（复位、闸门/相位计数、窗口跟随、连续测量一致性）、`EdgeDetector`、`ResetSynchronizer`、`DspCounter`、`MmcmDrpController`，以及顶层 IP 的 `DigitalPhaseMeterTop`：用 AXI4-Lite BFM 按真实基地址 `0x43C0_0000 + 偏移` 驱动，覆盖复位/寄存器默认值、写数据先到与字节选通、测量启动与中断时序、W1C 与只读寄存器写忽略、结果锁存与 `LOST_COUNT`、`CTRL.clear`、目标闸门锁存，以及片内源/外部输入切换。**顶层 AXI 寄存器通路由它守门**：动 `Axi*.scala`、`ControlRegs.scala` 或 `DigitalPhaseMeterTop.scala` 必须跑 `xmake test -v`，并把新寄存器纳入 [`test/DigitalPhaseMeterTop.cpp`](test/DigitalPhaseMeterTop.cpp)。测试目标的注册方式、时间精度与常见陷阱见 [`.agents/skills/dpm-verilator-testing`](.agents/skills/dpm-verilator-testing/SKILL.md)。

### xsim 仿真（MMCM 联调）

```bash
sim/run_handoff_tb.sh          # 片内源 ↔ 测量通路联调（真实 MMCM 行为模型 + 80 个测量点）
sim/run_handoff_tb.sh --no-gen # 跳过生成入口（RTL 已是最新时）
```

`sim/` 只保留依赖厂商 MMCM 原语（unisims_ver 的 `MMCME2_ADV`）的联调激励，判定依据是日志里出现 `HANDOFF_SUMMARY ... fails=0`（xsim 的退出码不足以判定失败）。激励覆盖内容与实测陷阱见 [`sim/README.md`](sim/README.md)。

### Vivado：打包 IP、创建 BD、综合实现

```bash
uv sync                                                 # 把 dpm-vivado-flow 装进根 .venv（一次性）
uv run dpm-vivado-flow                                  # 官方增量流程：只重跑变化的阶段/run
uv run dpm-vivado-flow --status                         # 查看 stamp/指纹/DCP/报告状态
uv run dpm-vivado-flow --force                          # 需要时全量重建
vivado -mode batch -source vivado/impl_stability.tcl -tclargs 6  # 同策略 6 次独立实现，验证余量稳定性
```

底层的 IP 打包 / BD 构造脚本仍可单独执行（`vivado/package_ip.tcl`、`vivado/package_src_ip.tcl`、
`vivado/create_bd.tcl`），但推荐用 `dpm-vivado-flow`，它会自动处理 IP → BD → 综合/实现的依赖顺序
并复用已完成的结果。流程细节、增量复用矩阵、地址映射与告警清理见
[`vivado/README.md`](vivado/README.md) 与 [`.agents/skills/dpm-vivado-ip`](.agents/skills/dpm-vivado-ip/SKILL.md)。

### PS 侧固件与上位机

```bash
bash ps/build.sh                          # 交叉编译裸机程序，并构建宿主机验证台
uv run pytest -q                          # PC 侧上位机与协议测试
uv run python -m dpm_host --help          # CLI
uv run python -m dpm_host gui             # PySide6 GUI
```

上板步骤（BD 串口参数、重建 bitstream、BOOT.bin、JTAG 加载、验收脚本）见 [`docs/bringup.md`](docs/bringup.md)；串口协议契约见 [`docs/ps-protocol.md`](docs/ps-protocol.md)；PS 侧与上位机侧细节分别见 [`ps/README.md`](ps/README.md)、[`host/README.md`](host/README.md)。

## 验证状态

工程已完成并通过实测验收（2026-10-19）。可复跑的验证入口：

| 层次         | 入口                                                           | 说明                                                               |
| ------------ | -------------------------------------------------------------- | ------------------------------------------------------------------ |
| RTL 单元     | `xmake test -v`                                                | Verilator：测量通路与复位/DRP 语义                                 |
| 顶层 AXI     | `xmake test -v`                                                | Verilator：顶层 IP 的寄存器读写、中断、结果锁存、输入源切换        |
| MMCM 联调    | `sim/run_handoff_tb.sh`                                        | xsim：真实 MMCM 行为模型，80 点相位/频率比对                       |
| 协议与换算   | `uv run pytest -q`、`uv run python spec/selftest_spec.py`      | 上位机单测、C/Python/金标准三方一致性自检                          |
| 离线验收演练 | `bash ps/tools/onboard_accept_sim.sh`                          | C 验证台经 pty 跑完整验收流程（80 点扫描）                         |
| 综合实现签核 | `uv run dpm-vivado-flow` + `build/vivado` 下的时序报告         | 450MHz：WNS +0.271ns / WHS +0.060ns / 0 失败端点（稳定性需按 RTL 版本复跑） |
| 上板验收     | `uv run python ps/tools/onboard_accept.py --port /dev/ttyUSB0` | JTAG 冷启动 + 串口链路 + 协议 + 80 点扫描                          |

> 上表的**综合实现签核**已于 2026-10-26 用当前 HEAD 的 RTL 重跑（单次实现，数据见
> [`vivado/README.md`](vivado/README.md)）；**上板验收**仍是 2026-10-19 的记录。
> `impl_stability.tcl 6` 的 6/6 稳定性结论对应 2026-10-19 网表，RTL 改动后需重跑后再引用。

报告验证结果时请注明实际运行的目标与配置、区分「已验证」与「未验证」，详见 [`.agents/skills/dpm-pre-delivery-check`](.agents/skills/dpm-pre-delivery-check/SKILL.md)。
