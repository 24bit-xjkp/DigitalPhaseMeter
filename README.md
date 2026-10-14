# DigitalPhaseMeter

基于 **Chisel** 的数字相位计：测量两路同频信号的相位差。测量通路运行在高速时钟域（**当前 400MHz**，由 PL 内 MMCM 从 50MHz 倍频得到），控制、闸门计数与对外接口运行在低速时钟域（50MHz）。

仓库目前处于 **AXI4 互联实现阶段**：测量与控制 RTL 已完成，PS↔PL 的互联（AXI4-Lite 控制/状态与结果寄存器 + 测量完成中断）、Vivado IP 打包与 block design 均已实现，并分别用 Verilator（测量通路）与 Vivado xsim（AXI4 接口与顶层 IP）验证；PS 侧软件与片内信号源尚未开始，见[路线图](#路线图)。

## 测量原理

高速时钟域内对两路信号分别做边沿检测，得到三个观测量：

| 输出       | 含义                                                    | 理论值                                                     |
| ---------- | ------------------------------------------------------- | ---------------------------------------------------------- |
| `gateCnt`  | 测量窗口内的低速时钟周期数                              | 由 `in.bits.targetGate` 指定，实际值略大（含停止握手开销） |
| `edgeCnt`  | 窗口内参考信号 `sig1` 的上升沿数                        | `gateCnt × T_clock / T_sig1`                               |
| `phaseCnt` | 每个参考周期内「参考沿 → 被测沿」的高速时钟周期数累加值 | `phaseOffset / T_dspClock × edgeCnt`                       |

`phaseCnt` 的分辨率就是一个高速时钟周期，因此高速时钟频率越高、闸门时间越长，测量越精确。被测相位偏移需要小于半个信号周期，`phaseCnt` 与相位差才保持一一对应。各观测量的容限与精度下界见 [`.agents/skills/dpm-measurement-model`](.agents/skills/dpm-measurement-model/SKILL.md)。

## 模块结构

```text
DigitalPhaseMeterTop（顶层 RawModule：显式主时钟 / 异步复位 / 高速时钟）
├── ResetSynchronizer        主复位同步：resetn 异步置位、与主时钟同步释放
│                            主时钟域模块都在 withClockAndReset(clock, mainReset) 里例化
├── Axi4LiteSlave            AXI4-Lite 从口 → 内部读写命令总线（控制/状态/结果寄存器）
├── ControlRegs              控制/状态寄存器堆 + 结果锁存 + 与 Controller 的握手时序 + 中断
├── Controller               低速域 FSM：闸门计数、与 PhaseMeter 握手、锁存结果
│   ├── SyncReset            高速域复位同步链（仿真为移位链，综合为 xpm_cdc_sync_rst）
│   └── PhaseMeter           高速域测量核心
│       ├── EdgeDetector ×4  3 级流水的边沿检测（sig1、sig2 与两类握手脉冲）
│       └── DspCounter   ×2  use_dsp 计数器，分别累计 edgeCnt / phaseCnt
└── （片内无存储：数据通道就是寄存器本身，见下文「PS↔PL 互联」）
```

测量接口约定：`Controller.in.valid` 发起一次测量，`out.valid` 一次脉冲返回结果；`targetGate` 在测量期间保持不变，连续测量只需重复发起。测量窗口会等到被测信号的下一个上升沿才关闭，因此实际 `gateCnt` 略大于目标值。

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
                        │       400MHz DSP 域时钟 ────────┘
                        │
                    interrupt
```

**DSP 域时钟 400MHz**：Zynq-7000 的 PS `FCLK` 上限 250MHz（在 BD 里请求更高会被静默降到 250MHz），
因此测量核心的高速时钟改由 **PL 内的 MMCM** 从 `FCLK_CLK0`(50MHz) 倍频得到。「MMCM 锁定前不来时钟」
由 **clk_wiz 自带的 Safe Clock Startup** 保证：它把 MMCM 输出经内部的 `BUFGCE` 放行，CE 由 8 bit
移位寄存器连续采到 8 拍 `locked` 后才拉高（无毛刺，不需要外部门控模块）。实测在
`xc7z010clg400-1`（−1 速度等级）上 400MHz 时序收敛：**WNS = +0.358ns、WHS = +0.062ns，0 个失败端点**
（DSP 域内 WNS = +0.358ns / WHS = +0.157ns；6 次独立实现全部为正，最差 +0.191ns），详见
[`vivado/README.md`](vivado/README.md#dsp-域时钟的安全启动clk_wiz-safe-clock-startup)。
相位分辨率因此从 200MHz 下的 5ns 提升到 2.5ns。

- **所有数据都走 AXI4-Lite 寄存器（GPIO 式）**：一次测量的结果只有 4 个字，PL 侧直接把最新结果锁存在只读寄存器里，PS 收到中断后连续读 4 个寄存器即可拿到一条完整结果。因此 IP 内部没有任何片内存储，对外也只有一个 AXI 从口——数据量小的时候，寄存器比 BRAM + 突发口简单得多。
- **中断**：结果锁存完成（或上一次结果被覆盖而丢失）后拉起 `interrupt`（高电平），经 `IRQ_F2P[0]` 通知 PS；软件写 `IRQ_STATUS` 清中断。
- **地址窗口**：`s_axi_lite` 占 4KB（0x43C0_0000），实际只用到前 0x2C 字节。

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
| 0x28 | `CAPS`         | RO   | [7:0] 每条结果字数 = 4；[15:8] 结果深度 = 1；[16] 数据通道为寄存器 = 1                                                                                                                     |

`CTRL` 是「普通读写位 + 写 1 触发位」的混合：`irqEnable` 每次写 `CTRL` 都按 bit1 整体更新，因此启动测量时要写 `CTRL = (1<<1) | (1<<0)`，以保持中断使能。

### 数据交换流程（PS 视角）

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
.agents/skills/                     项目级 skill：RTL 生成、测试、测量模型、AXI/Vivado、交付前检查
```

## 环境

| 依赖                                     | 用途                                            |
| ---------------------------------------- | ----------------------------------------------- |
| JDK + sbt                                | 由 Chisel 生成 SystemVerilog                    |
| xmake ≥ 3.1.1                            | 构建 Verilator 测试目标、运行测试               |
| Verilator ≥ 5.050                        | 编译生成的 SystemVerilog（测量通路单元测试）    |
| clang（支持 C++20 模块，本项目用 C++26） | 编译测试代码，**不支持 gcc**                    |
| Vivado（本项目在 2025.2 上验证）         | 打包 IP、创建 block design、xsim 仿真、综合实现 |

## 使用

### 生成 SystemVerilog（产物在 `build/chisel/<Name>/`）

```bash
sbt -batch "runMain DigitalPhaseMeter.EmitController"                           # 仿真用
sbt -batch "runMain DigitalPhaseMeter.EmitController --type=syn"                # 综合用，复位改用 XPM 原语
sbt -batch "runMain DigitalPhaseMeter.EmitDigitalPhaseMeterTop --type=syn"   # 顶层 IP，供 xsim 与 Vivado
```

`--type=syn` 与 `--type=sim` 的差别：`SyncReset` 从移位链换成 `xpm_cdc_sync_rst`（顶层已无 BRAM，产物里不再有 `xpm_memory_tdpram`）。

### Verilator 单元测试（测量通路）

```bash
xmake test -v                             # 全部测试
xmake run Controller                      # 单个测试目标
```

覆盖 `Controller` 的复位行为、闸门计数、相位计数、测量窗口跟随 `targetGate` 以及连续测量的一致性；`EdgeDetector` 有独立的时序测试。

### xsim 仿真（AXI4 接口与顶层 IP）

```bash
sim/run_xsim.sh                # 先用 sbt 生成综合产物 RTL，再编译并运行
sim/run_xsim.sh --no-gen       # 跳过 sbt 生成
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
3. **片内信号源** —— 用 PLL/MMCM 生成两路同频不同相的信号，对电路做片内测试。
4. **PS 侧软件** —— 基于串口的交互系统：接受 PC 指令、驱动 PL 侧测量、回传结果。计划支持：
   - 可变闸门时间
   - 连续 / 单次测量
   - 片内可动态配置频率与相位的信号源
   - 片内 / 片外信号源切换
