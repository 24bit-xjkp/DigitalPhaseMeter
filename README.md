# DigitalPhaseMeter

基于 **Chisel** 的数字相位计：测量两路同频信号的相位差。测量通路运行在高速时钟域（**当前 450MHz**，由 PL 内 MMCM 从 50MHz 倍频得到），控制、闸门计数与对外接口运行在低速时钟域（50MHz）。

仓库目前处于 **信号输入实现阶段**：测量与控制 RTL、PS↔PL 互联（AXI4-Lite 控制/状态与结果寄存器 +
测量完成中断）、Vivado IP 打包与 block design 均已完成，并分别用 Verilator（测量通路）与 Vivado xsim
（AXI4 接口与顶层 IP）验证。测量输入有两条来源，由 PL 内的选择寄存器切换：**外部引脚**（`sig1` → W18、
`sig2` → W19）与**片内信号源**（PL 内 MMCM 的两路输出，**频率与相位差都由 MMCM 的动态重配置在线设定**，
两路输出直接作为被测信号）。**PS 侧软件与 PC 侧上位机已完成**：PS 侧是 freestanding 裸机 C 串口服务端（读写 AXI 寄存器与
MMCM 的 DRP 口），PC 侧是 Python 上位机（CLI + PySide6 GUI，相位差 0~360° 与频率显示、单次/连续、
片内源频率与相位、输入源切换、多频点多相位扫描）。协议见 [`docs/ps-protocol.md`](docs/ps-protocol.md)，
验证结果见 [`docs/verify-report.md`](docs/verify-report.md)（第 10 节是 2026-10-16 的复验：
**相位在线设定已上板验证**，`onboard_accept.py` 44/44、扫描 32/32，端到端 28/28；
22.5° 这类需要 `PHASE_MUX` 细调的点也成立。**以上是 DSP=400MHz 轮次的历史数据**，
当前设计已改为 450MHz、冻结验证频点也已更换，见下文「DSP 域时钟」与
[`docs/verify-plan.md`](docs/verify-plan.md)，本轮尚未重新上板验证）。

## 测量原理

高速时钟域内对两路信号分别做边沿检测，得到三个观测量：

| 输出       | 含义                                                    | 理论值                                                     |
| ---------- | ------------------------------------------------------- | ---------------------------------------------------------- |
| `gateCnt`  | 测量窗口内的低速时钟周期数                              | 由 `in.bits.targetGate` 指定，实际值略大（含停止握手开销） |
| `edgeCnt`  | 窗口内参考信号 `sig1` 的上升沿数                        | `gateCnt × T_clock / T_sig1`                               |
| `phaseCnt` | 每个参考周期内「参考沿 → 被测沿」的高速时钟周期数累加值 | `phaseOffset / T_dspClock × edgeCnt`                       |

`phaseCnt` 的分辨率就是一个高速时钟周期，因此高速时钟频率越高、闸门时间越长，测量越精确。
`phaseCnt / edgeCnt` 量的是从 `sig1` 上升到**下一个** `sig2` 上升沿的**无符号**间隔，因此
`0°~360°` 的相位差都与读数一一对应（`>180°` 同样可测，不需要"小于半个周期"；只有按有符号
偏移解释时才需要 `|δ| < 180°`，且 `0°` 会因"等下一个沿"读成一个整周期）。各观测量的容限与
精度下界见 [`.agents/skills/dpm-measurement-model`](.agents/skills/dpm-measurement-model/SKILL.md)。

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
│       ├── EdgeDetector ×4  3 级流水的边沿检测（sig1、sig2 与两类握手脉冲）
│       └── DspCounter   ×2  use_dsp 计数器，分别累计 edgeCnt / phaseCnt
└── （片内无存储：数据通道就是寄存器本身，见下文「PS↔PL 互联」）

片内信号源的 MMCM（`dpm_mmcm_src`，裸 `MMCME2_ADV` 薄封装）不在 IP 内，而是 BD 里的独立单元：
它把两路输出送给 IP，PS 通过 AXI4-Lite 写 MMCM 的寄存器镜像（`dpm_src_drp` 控制器搬进 DRP 口，
见下文「片内信号源」）。
```

测量接口约定：`Controller.in.valid` 发起一次测量，`out.valid` 一次脉冲返回结果；`targetGate` 在测量期间保持不变，连续测量只需重复发起。测量窗口会等到被测信号的下一个上升沿才关闭，因此实际 `gateCnt` 略大于目标值。

## 信号输入：外部引脚与片内信号源

测量核心之前有一个输入选择器（`SignalInputMux`，主时钟域，纯组合），两条候选通路的时基与约束如下：

| 通路       | 来源                                                     | 约束/时基                                                                     |
| ---------- | -------------------------------------------------------- | ----------------------------------------------------------------------------- |
| 外部输入   | `sig1` → 引脚 **W18**、`sig2` → 引脚 **W19**（LVCMOS33） | 异步输入，`set_false_path`；在测量域经 `EdgeDetector` 的 ASYNC_REG 采样链同步 |
| 片内信号源 | PL 内 MMCM（`dpm_mmcm_src`）的两路输出                   | 频率/相位差由 PS 写 MMCM 寄存器镜像（`dpm_src_drp`）设定；同样按异步输入采样  |

两条通路共用同一条采样链：选择器本身在主时钟域，它的**输出**才进测量域，对测量域是一条异步被测信号
（与外部引脚同一待遇）。使能与选择位来自同一域的 `SIG_CTRL`，因此不需要任何跨时钟域配置通路。

### 片内信号源：MMCM 的两路输出

```text
        ┌───────────────────────────────┐   clkOut1（f_sig，相位 φ0）──► dpm_top/srcSig1 ─┐
        │ dpm_mmcm_src（裸 MMCME2_ADV） │   clkOut2（f_sig，相位 φ1）──► dpm_top/srcSig2 ─┤
        │  参考时钟 = FCLK_CLK0 (50MHz) │                                                  ├─► SignalInputMux
        │  DRP ◄── dpm_src_drp（AXI 口）┼── PS：写 MMCM 寄存器镜像 → 写 LOAD → 等 locked   │   ↕ 外部引脚
        └───────────────────────────────┘                         │                        │
                                                                  └► locked ──► SIG_STATUS │
                                                                                            ▼
                                                       PhaseMeter 的异步采样链（与外部输入共用）
```

1. **MMCM 产生两路同频、相位差可调的时钟**，两路输出直接作为被测信号（初始 25MHz、相位差 90°）：
   - **频率** = VCO / CLKOUT 分频，VCO = 50MHz × CLKFBOUT 倍频 ÷ DIVCLK（VCO 必须落在 600～1200MHz），
     因此输出频率下限约 4.7MHz（VCO 600MHz ÷ 128）；实际可用上限受相位计采样率限制，取 40MHz 量级；
   - **相位差** = 两个输出各自的相位寄存器之差，步长为 VCO 周期的 1/8（VCO 1000MHz 时 125ps，
     折算到输出是 `45 / D` 度，D = 40 时 1.125°）；
   - 两者都由 PS 算出 **MMCM 的 16 位寄存器值**，写进地址段 `0x43C1_0000` 的 `dpm_src_drp`
     （`0x08..0x24` 八个寄存器镜像 + `0x28` LOAD），控制器再按固定顺序搬进 MMCM 的 DRP 口；
     位域与编码规则见 `build/analysis/golden/mmcm_encode.py`（已对 clk_wiz 的 Write DRP 生成值
     逐点校验）。**改完写 LOAD 让 MMCM 重新锁定，期间两路输出会停止**（应在测量空闲时改）。
   - **相位范围随分频比缩小**：MMCM 每个输出的相位寄存器覆盖 64 个 VCO 周期，换成输出相位是
     `64 / D` 个输出周期（D = CLKOUTx 分频）。当前工作点 25MHz（VCO 1000MHz、D = 40）→ 1.6 个
     输出周期，0～360° 全覆盖；需要更低频率时优先让 PS 把 VCO 调到下限（600MHz），
     而不是把 D 拉大（D 越大相位可表示范围越小）。
   - **不走 clk_wiz 的语义寄存器文件**：原来的 `clk_wiz_src`（`USE_DYN_RECONFIG + PHASE_DUTY_CONFIG`）
     会把「毫度/百分比/MHz → MMCM 计数器位域」的定点换算综合成硬件（实测 13,287 个 LUT site + 38 个
     DSP48E1）；方案 A 把换算搬到 PS 软件，PL 侧只留 DRP 搬运状态机（114 LUT / 182 FF / 0 DSP），
     整设计 Slice LUTs 14,048（79.8%）→ 948（5.4%）、DSP 40 → 2
     （以上资源数据是 400MHz 轮次的实现结果）。
     **相位字生效与否未上板验证**：xsim 的 unisim MMCM 不支持 DRP 写，控制器只用 DRP 从端模型验证过
     （旧的 `PHASE_DUTY_CONFIG` 补丁脚本随之作废，已删除）。
2. **两路输出与外部输入共用同一条异步采样链**：切换不改变测量通路的延迟，相位差只被量化到
   1 个测量时钟周期（即相位计本身的分辨率，450MHz 下 2.222ns）。使能无效（`SIG_CTRL.enable = 0`）
   时片内源两路输出被门成恒低。

### 切换与配置（PS 视角）

1. （可选）写 MMCM 寄存器镜像（`0x08..0x24`）设定片内源的频率与相位差，写 `0x28` LOAD 后等 `locked`；
2. 写 `SIG_CTRL = (1<<0) | (1<<1)`：使能片内源并把它接到测量核心（`1<<0` = enable，`1<<1` = srcSelect）；
3. 读 `SIG_STATUS` 确认 `srcLocked = 1`、`sourceReady = 1`。使能门控与二选一都在主时钟域、
   与写 `SIG_CTRL` 的寄存器堆同域，因此**写这一笔的同时切换就已经生效**：AXI 写返回时配置必然已经生效；
4. 按常规流程启动测量。切回外部输入写 `SIG_CTRL = 0` 即可。

注意：

- **配置与切换都应在 `STATUS.busy = 0` 时进行**：切换输入源会让正在进行的测量作废；MMCM 重配置期间
  两路输出停止，正在等待边沿的测量会一直挂着。
- **选择片内源时必须让源真的产生边沿**：`SIG_CTRL.enable = 1` 且 MMCM 已锁定，否则两路输出恒低，
  测量会一直停在「等待参考沿」的状态（切回外部输入或重新配置即可恢复）。
  `SIG_STATUS.sourceReady` 给出这个前提是否满足（选择外部输入时恒为 1，因为外部是否有信号无法在
  PL 内判断——用一次测量的 `edgeCnt` 即可确认）。

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

**DSP 域时钟 450MHz**：Zynq-7000 的 PS `FCLK` 上限 250MHz（在 BD 里请求更高会被静默降到 250MHz），
因此测量核心的高速时钟改由 **PL 内的 MMCM** 从 `FCLK_CLK0`(50MHz) 倍频得到。「MMCM 锁定前不来时钟」
由 **clk_wiz 自带的 Safe Clock Startup** 保证：它把 MMCM 输出经内部的 `BUFGCE` 放行，CE 由 8 bit
移位寄存器连续采到 8 拍 `locked` 后才拉高（无毛刺，不需要外部门控模块）。实测在
`xc7z010clg400-1`（−1 速度等级）上 450MHz 时序收敛：**WNS = +0.291ns、WHS = +0.032ns、
TNS = THS = 0、0 个失败端点**（DSP 域内 WNS = +0.291ns / WHS = +0.163ns；官方实现策略
`Performance_Retiming`，2026-10-18 一轮实现），详见
[`vivado/README.md`](vivado/README.md#复位极性修复后的一轮复跑与两种实现策略对比)。
同一设计的早期版本在 **400MHz 轮次**实测过 WNS +0.358ns / WHS +0.062ns 一档（历史数据，
余量随 RTL 与实现策略的修订变化）。
相位分辨率因此从 200MHz 下的 5ns 提升到 2.222ns（450MHz）。

- **所有数据都走 AXI4-Lite 寄存器（GPIO 式）**：一次测量的结果只有 4 个字，PL 侧直接把最新结果锁存在只读寄存器里，PS 收到中断后连续读 4 个寄存器即可拿到一条完整结果。因此 IP 内部没有任何片内存储，对外也只有一个 AXI 从口——数据量小的时候，寄存器比 BRAM + 突发口简单得多。
- **中断**：结果锁存完成（或上一次结果被覆盖而丢失）后拉起 `interrupt`（高电平），经 `IRQ_F2P[0]` 通知 PS；软件写 `IRQ_STATUS` 清中断。
- **地址窗口**：`s_axi_lite` 占 4KB（0x43C0_0000），实际只用到前 0x34 字节；片内源的 MMCM 动态重配置口占另一段 4KB（0x43C1_0000，寄存器映射见 PG065）。

### 寄存器映射（`s_axi_lite`，基地址 0x43C0_0000）

| 偏移 | 名称           | 访问 | 说明                                                                                                                                                                                       |
| ---- | -------------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 0x00 | `CTRL`         | 混合 | [0] `start`：写 1 启动一次测量（自清脉冲，`busy` 期间忽略）；[1] `irqEnable`：中断使能；[2] `clear`：写 1 复位结果寄存器与全部标志（`busy` 期间挂起，空闲后生效）。读回时 bit0/bit2 恒为 0 |
| 0x04 | `STATUS`       | RO   | [0] `busy`：测量中；[1] `resultValid`：有未被读走的结果；[2] `lost`：有结果被覆盖丢失；[3] `irqPending`                                                                                    |
| 0x08 | `GATE_TARGET`  | RW   | [27:0] 目标闸门计数（低速时钟周期数），按字节选通写入                                                                                                                                      |
| 0x0C | `RESULT_GATE`  | RO   | 最新结果的 `gateCnt`，实际闸门计数                                                                                                                                                         |
| 0x10 | `RESULT_EDGE`  | RO   | 最新结果的 `edgeCnt`，窗口内参考信号上升沿数                                                                                                                                               |
| 0x14 | `RESULT_PHASE` | RO   | 最新结果的 `phaseCnt`，相位计数                                                                                                                                                            |
| 0x18 | `RESULT_SEQ`   | RO   | 最新结果的记录序号                                                                                                                                                                         |
| 0x1C | `LOST_COUNT`   | RO   | 结果未被读走即被覆盖的次数                                                                                                                                                                 |
| 0x20 | `IRQ_STATUS`   | W1C  | [0] 中断挂起、[1] `resultValid`（写 1 表示结果已读走）、[2] 丢失计数：均写 1 清除                                                                                                          |
| 0x24 | `VERSION`      | RO   | `0x4450_4D02`（"DPM2"）                                                                                                                                                                    |
| 0x28 | `CAPS`         | RO   | [7:0] 每条结果字数 = 4；[15:8] 结果深度 = 1；[16] 数据通道为寄存器 = 1；[17] 带片内信号源与输入选择寄存器 = 1                                                                              |
| 0x2C | `SIG_CTRL`     | RW   | [0] `enable`：片内源使能（0 时两路输出恒低）；[1] `srcSelect`：0 = 外部引脚输入、1 = 片内信号源。整体按 bit0 字节写入                                                                      |
| 0x30 | `SIG_STATUS`   | RO   | [0] `enable`；[1] `srcSelect`；[2] `srcLocked`：片内源 MMCM 已锁定；[3] `sourceReady`：当前选择在配置上可用（选外部恒为 1，选片内源要求使能且 MMCM 已锁定）                                |

`CTRL` 是「普通读写位 + 写 1 触发位」的混合：`irqEnable` 每次写 `CTRL` 都按 bit1 整体更新，因此启动测量时要写 `CTRL = (1<<1) | (1<<0)`，以保持中断使能。

### 数据交换流程（PS 视角）

0. （用片内源时）先用 MMCM 的 DRP 寄存器配置频率与相位差，再写 `SIG_CTRL` 选中它，见上文「信号输入」；
1. 写 `GATE_TARGET` 设定闸门时间；
2. 写 `CTRL = (1<<1) | (1<<0)`：保持中断使能并启动一次测量；
3. 等待 `IRQ_F2P[0]` 中断（也可以轮询 `STATUS.busy` 变 0）；
4. 依次读 `RESULT_GATE` / `RESULT_EDGE` / `RESULT_PHASE` / `RESULT_SEQ`，得到一条完整结果；
5. 写 `IRQ_STATUS = (1<<0) | (1<<1)`：清中断挂起位，同时告诉 PL「结果已读走」——否则下一次测量的结果会被记为丢失。

结果寄存器只保留最新一条：软件还没读走就再次测量，旧结果会被覆盖、`LOST_COUNT` 加一（`RESULT_SEQ` 的跳变同样能发现漏读）。需要重新开始计数或丢弃旧结果时写 `CTRL` 的 `clear` 位。

### 与 Controller 交互的时序要点

- 写 `CTRL.start` 后产生**一拍** `Controller.in.valid`；同一拍把当前 `GATE_TARGET` 锁存进 `activeTargetGate`，测量期间 `in.bits.targetGate` 恒取锁存值，因此软件在测量中改写 `GATE_TARGET` 不会破坏本次测量；`busy` 期间的启动请求被忽略。
- 结果在 `Controller.out.valid` 的那一拍**一次性**锁存进 4 个结果寄存器，同一拍撤销 `busy` 并置起 `resultValid` 与中断，因此 PS 收到中断时 4 个结果寄存器一定属于同一次测量。
- 中断是电平：`irqPending && irqEnable`。不使能中断时测量照常完成、挂起位照常置起，使能后中断立刻出现在引脚上。
- 同一周期里「软件清标志」与「新结果到达」竞争时按新结果优先：结果寄存器与 `resultValid` 保持新结果，中断不会被误清。
- AXI 从口在复位期间拉低所有 `ready`，不接受任何传输（否则会出现「已握手却等不到响应」的主机挂起）。

## 目录结构

```text
src/main/scala/DigitalPhaseMeter/   Chisel 源码，每个模块一个文件，附 Emit<Name> 生成主类
test/                               Verilator C++ 测试（测量通路），每个 <Name>.cpp 自动注册一个测试目标
sim/                                Vivado xsim 的 SystemVerilog 测试激励与运行脚本（AXI4 接口/顶层 IP）
vivado/                             Vivado IP 打包、block design、综合实现脚本与顶层约束
build/chisel/<Name>/                Chisel 生成的 SystemVerilog 与 filelist.f（构建产物，不入库）
ps/                                 PS 侧裸机 C 服务端（串口协议、AXI/DRP 驱动、startup/linker）
host/                               PC 侧 Python 上位机（协议、CLI、PySide6 GUI、仿真后端）
docs/                               协议、验证计划、验证报告、上板 bring-up
spec/                               金标准向量、独立仿真服务端、规格自检（三方实现交叉校验）
.agents/skills/                     项目级 skill：RTL 生成、测试、测量模型、AXI/Vivado、交付前检查
```

## 环境

| 依赖                                     | 用途                                            |
| ---------------------------------------- | ----------------------------------------------- |
| JDK + sbtn                               | 由 Chisel 生成 SystemVerilog                    |
| xmake ≥ 3.1.1                            | 构建 Verilator 测试目标、运行测试               |
| Verilator ≥ 5.050                        | 编译生成的 SystemVerilog（测量通路单元测试）    |
| clang（支持 C++20 模块，本项目用 C++26） | 编译测试代码，**不支持 gcc**                    |
| Vivado（本项目在 2025.2 上验证）         | 打包 IP、创建 block design、xsim 仿真、综合实现 |

## 使用

### 生成 SystemVerilog（产物在 `build/chisel/<Name>/`）

```bash
sbtn -batch "runMain DigitalPhaseMeter.EmitController"                    # 测量通路，供 Verilator 测试
sbtn -batch "runMain DigitalPhaseMeter.EmitDigitalPhaseMeterTop"          # 顶层 IP，供 xsim 与 Vivado
```

每个模块只生成**一份**产物：复位同步由纯 RTL 的 `ResetSynchronizer` 实现（异步置位、与各域时钟同步释放），Verilator、xsim 与综合共用同一份 RTL，没有 `--type=sim|syn` 的差别。改完 RTL 要先跑生成入口再跑测试——`xmake test` 与 `sim/run_xsim.sh` 都不会替你生成。

### Verilator 单元测试（测量通路）

```bash
xmake test -v                             # 全部测试
xmake run Controller                      # 单个测试目标
```

覆盖 `Controller` 的复位行为（DSP 域复位由模块内部的 `ResetSynchronizer` 从主复位同步得到，测试只驱动主复位）、闸门计数、相位计数、测量窗口跟随 `targetGate` 以及连续测量的一致性；`EdgeDetector` 有独立的时序测试；`ResetSynchronizer` 有独立的复位时序测试（异步置位、恰好 `resetDepth` 拍同步释放）；`MmcmDrpController` 覆盖 AXI/DRP 寄存器语义。

### xsim 仿真（AXI4 接口与顶层 IP）

```bash
sim/run_xsim.sh                # 先用 sbtn 生成 RTL，再编译并运行
sim/run_xsim.sh --no-gen       # 跳过 sbtn 生成
sim/run_xsim.sh --gui          # 运行结束后打开波形
```

测试激励 `sim/tb_digital_phase_meter_top.sv` 直接实例化顶层 IP，覆盖复位行为、AXI4-Lite 寄存器读写（含写地址/写数据通道顺序颠倒与字节选通、只读寄存器写忽略）、测量启动与 `busy` 时序、中断使能门控与 `IRQ_STATUS` 清除语义、结果寄存器的读回与覆盖（`LOST_COUNT`）、`CTRL.clear` 与二次复位，并用测量模型校验 `gateCnt`/`edgeCnt`/`phaseCnt`。

### Vivado：打包 IP、创建 BD、综合实现

```bash
vivado -mode batch -source vivado/package_ip.tcl       # 打包 IP → build/vivado/ip/dpm_top
vivado -mode batch -source vivado/create_bd.tcl        # PS7 + 协议转换 + IP 的 BD → build/vivado/bd_proj
vivado -mode batch -source vivado/build_bitstream.tcl  # 综合 / 实现 / bitstream / XSA
```

流程细节、地址映射、PS 侧访问示例与已知注意事项见 [`vivado/README.md`](vivado/README.md)。

## 路线图

1. ~~**AXI4 接口** —— 为 `Controller` 增加 AXI4(-Lite) 寄存器接口，与 PS 侧通信。~~
2. ~~**Vivado 工程** —— 打包 IP、创建 PS↔PL 互联的 block design、综合与实现。~~
3. ~~**片内信号源** —— 用 PLL/MMCM 生成两路同频不同相的信号，对电路做片内测试。~~（PL 内 MMCM 的两个输出同频、相位差可调，两路输出直接作为被测信号；频率与相位差由 PS 通过 MMCM 的动态重配置口在线设定，外部/内部输入切换寄存器在测量 IP 的 AXI 寄存器区）
4. ~~**PS 侧软件** —— 基于串口的交互系统：接受 PC 指令、驱动 PL 侧测量、回传结果。~~ 已实现，
   见 [`ps/`](ps/README.md)（Zynq PS 侧 freestanding 裸机 C 服务端）与 [`host/`](host/README.md)
   （PC 侧 Python 上位机：CLI + PySide6 GUI）。支持可变闸门时间、连续/单次测量、片内/片外信号源切换、
   片内源频率与相位设定、流模式上报；协议见 [`docs/ps-protocol.md`](docs/ps-protocol.md)。
   验证结果见 [`docs/verify-report.md`](docs/verify-report.md)（测量点全部通过，
   但**相位扫描未真正生效**，见下）。
5. ~~**PL 侧：让在线相位扫描真正可用**~~ —— **已完成并上板验证（2026-10-16，400MHz 轮次历史数据）**：片内源换成
   **裸 MMCM + 极简 DRP 控制器**（方案 A）后，相位由 PS 直接编码进 MMCM 的
   `PHASE_MUX`/`DELAY_TIME`（网格 `45°/D`）。上板过程中修掉 3 个只有硬件才暴露的缺陷
   （DRP 地址窗口误判完整地址、DRP 写期间未拉高 MMCM 的 `RST`、`DWE` 常置 1），
   并改正了 PS 侧 MMCM 相位编码里接反的位域与 45° 吸附遗留。
   证据：上板 8MHz 请求 22.5° → 实测 21.602°（3 个 DSP 周期）、8MHz/90° → 86.367°
   （与金标准逐位一致）；`onboard_accept.py` 44/44、扫描 32/32；
   xsim 联调（真实 MMCM 行为模型 + DRP 控制器 + 测量通路）4/4 点通过。
   详见 [`docs/verify-report.md`](docs/verify-report.md) 第 10 节。
6. ~~**扩大相移覆盖：更多频点 + >180° 相移**~~ —— **已完成并上板验证（2026-10-16 第二轮，
   400MHz 轮次历史数据，下面的数字保留原样）**：
   相位请求的上界从写死的 `< 180°` 改为**硬件依据**——9 位相位码
   `m = round(相位 × D / 45000) <= 511`，即可请求上界 `min(23017499/D, 359999)` 毫度
   （D=20/25/40/50 覆盖整个 `0~360°`，D=120 到 191.812°、D=125 到 184.139°）；越界请求
   显式回 `ERR_PARAM`（字段截断会让相位回绕，旧 bitstream 实测 8MHz 写 270° 读到 86.4°）。
   冻结频点从 7 个扩到 **10 个**（5/6.25/8/10/12.5/16/20/25/40/50MHz，覆盖 VCO
   600/750/800/1000MHz 与 D = 20/25/40/50/80/100/120/125），相位点**逐频点**按上界裁剪
   （含 22.5° 与 180°/225°/270°/315°，必要时用相位码上界点 183.96°/191.625° 兜底），
   共 **79 个点、其中 31 个 ≥180°**。证据：xsim 联调 79/79（`HANDOFF_SUMMARY ... fails=0`）、
   上板验收扫描 79/79、C/Python/spec 三侧单测与金标准 79 条向量全部通过。
   详见 [`docs/verify-report.md`](docs/verify-report.md) 第 11 节。
   **当前 450MHz 轮次的冻结表已改为新频点**（450MHz 的整数 MHz 约数：
   5/6/9/10/15/18/25/30/45/50MHz，共 80 个测量点、32 个 ≥180°，见
   [`docs/verify-plan.md`](docs/verify-plan.md) §3），验证数据待重跑，尚未重新上板验证。
7. **PS UART 与上板** —— PS7 的调试串口现在是 `create_bd.tcl` 的流水线参数
   （`-uart uart1`，默认；`uart0` / `none` 可选），重建 BD 即带上 UART1（MIO48/49、115200），
   不再需要单独的补丁脚本；上板步骤见 [`docs/bringup.md`](docs/bringup.md)。
