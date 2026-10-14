# DigitalPhaseMeter

基于 **Chisel** 的数字相位计：测量两路同频信号的相位差。测量通路运行在高速时钟域（目标 200MHz 以上），控制、闸门计数与对外接口运行在低速时钟域（50MHz）。

仓库目前处于 **PL 侧功能实现阶段**：测量与控制 RTL 已完成，并有基于 Verilator 的单元测试；AXI4 接口、综合实现、片内测试与 PS 侧软件尚未开始，见[路线图](#路线图)。

## 测量原理

高速时钟域内对两路信号分别做边沿检测，得到三个观测量：

| 输出 | 含义 | 理论值 |
| --- | --- | --- |
| `gateCnt` | 测量窗口内的低速时钟周期数 | 由 `in.bits.targetGate` 指定，实际值略大（含停止握手开销） |
| `edgeCnt` | 窗口内参考信号 `sig1` 的上升沿数 | `gateCnt × T_clock / T_sig1` |
| `phaseCnt` | 每个参考周期内「参考沿 → 被测沿」的高速时钟周期数累加值 | `phaseOffset / T_dspClock × edgeCnt` |

`phaseCnt` 的分辨率就是一个高速时钟周期，因此高速时钟频率越高、闸门时间越长，测量越精确。被测相位偏移需要小于半个信号周期，`phaseCnt` 与相位差才保持一一对应。各观测量的容限与精度下界见 [`.agents/skills/dpm-measurement-model`](.agents/skills/dpm-measurement-model/SKILL.md)。

## 模块结构

```text
Controller          低速域 FSM：闸门计数、与 PhaseMeter 握手、锁存结果
├── SyncReset       复位同步链（4 级），综合时替换为 xpm_cdc_sync_rst
└── PhaseMeter      高速域测量核心
    ├── EdgeDetector ×4   3 级流水的边沿检测（sig1、sig2 与两类握手脉冲）
    └── DspCounter   ×2   use_dsp 计数器，分别累计 edgeCnt / phaseCnt
```

接口约定：`in.valid` 发起一次测量，`out.valid` 一次脉冲返回结果；`targetGate` 在测量期间保持不变，连续测量只需重复发起。测量窗口会等到被测信号的下一个上升沿才关闭，因此实际 `gateCnt` 略大于目标值。

## 目录结构

```text
src/main/scala/DigitalPhaseMeter/   Chisel 源码，每个模块一个文件，附 Emit<Name> 生成主类
test/                               Verilator C++ 测试，每个 <Name>.cpp 自动注册一个测试目标
script/                             xmake 包定义（doctest_module、verilator_utils 等）
build/chisel/<Name>/                Chisel 生成的 SystemVerilog 与 filelist.f（构建产物，不入库）
.agents/skills/                     项目级 skill：RTL 生成、测试、测量模型、交付前检查
```

## 环境

| 依赖 | 用途 |
| --- | --- |
| JDK + sbt | 由 Chisel 生成 SystemVerilog |
| xmake ≥ 3.1.1 | 构建 Verilator 测试目标、运行测试 |
| Verilator ≥ 5.050 | 编译生成的 SystemVerilog |
| clang（支持 C++20 模块，本项目用 C++26） | 编译测试代码，**不支持 gcc** |

## 使用

生成 SystemVerilog，产物落在 `build/chisel/<Name>/`：

```bash
sbt -batch "runMain DigitalPhaseMeter.EmitController"            # 仿真用
sbt -batch "runMain DigitalPhaseMeter.EmitController --type=syn" # 综合用，复位改用 XPM 原语
```

构建并运行测试：

```bash
xmake test -v                             # 全部测试
xmake run Controller                      # 单个测试目标
```

现有测试覆盖 `Controller` 的复位行为、闸门计数、相位计数、测量窗口跟随 `targetGate` 以及连续测量的一致性；`EdgeDetector` 有独立的时序测试。

完整的生成入口、测试写法与已知陷阱见 [`.agents/skills/`](.agents/skills)；面向 AI 代理的仓库规则见 [`AGENTS.md`](AGENTS.md)。

## 路线图

1. **AXI4 接口** —— 为 `Controller` 增加 AXI4(-Lite) 寄存器接口，与 PS 侧通信。
2. **Vivado 工程** —— 综合、布局布线，并对实现后的网表做后仿。
3. **片内信号源** —— 用 PLL/MMCM 生成两路同频不同相的信号，对电路做片内测试。
4. **PS 侧软件** —— 基于串口的交互系统：接受 PC 指令、驱动 PL 侧测量、回传结果。计划支持：
   - 可变闸门时间
   - 连续 / 单次测量
   - 片内可动态配置频率与相位的信号源
   - 片内 / 片外信号源切换
