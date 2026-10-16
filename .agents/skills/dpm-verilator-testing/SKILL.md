---
name: dpm-verilator-testing
description: 'Use when: 为 DigitalPhaseMeter 的 Chisel 模块编写、修改、运行或调试 Verilator C++ 测试（test/<Name>.cpp），或需要解读测试目标的注册方式、运行目录与覆盖率/波形产物。覆盖测试目标的自动注册、运行入口、时间精度陷阱、未初始化寄存器、set_base_name 与多 bit 端口等本项目实测的坑。'
argument-hint: '要测试的模块或用例（如 Controller 的相位测量精度）'
---

# DigitalPhaseMeter 的 Verilator 单元测试

## 适用场景

- 在 `test/` 下新增或修改 C++ 测试。
- 运行测试、传 doctest 参数、看波形与覆盖率产物。
- 排查"频率/周期与设定不符""读到随机值""覆盖率文件互相覆盖""端口宽度不匹配"之类的问题。

框架本身的用法（协程调度、`dut_context`、信号包装、断言风格）以 `~/verilator_utils/.agents/skills/verilator-utils-unit-testing/SKILL.md` 为准。这些 skill **不在本仓库的 skill 根目录下，不会被自动加载**，需要按路径直接读取。本 skill 只记录本仓库特有的接线方式与实测陷阱。

## 测试目标的注册方式

`test/xmake.lua` 会遍历 `test/*.cpp`，为每个文件自动注册两个目标：

| 目标 | 作用 |
| --- | --- |
| `<Name>Verilator` | 用 `verilator.shared` 规则编译 `build/chisel/<Name>/filelist.f` 列出的 SystemVerilog |
| `<Name>` | 编译 `<Name>.cpp`，链接 `verilator_utils`（组件 `main`，提供 doctest main）与 `doctest_module`（组件 `core`），依赖 `<Name>Verilator` |

要点：

- **新增测试通常只需新增 `test/<Name>.cpp`**，不要为每个测试手工加目标；`<Name>` 必须与 `build/chisel/<Name>/` 的目录名一致（即 Chisel 模块名）。
- 测试用例通过 `add_tests("rtl", { runargs = { "+verilator+rand+reset+2", "-fc" } })` 声明；`-fc` 让 doctest 强制彩色输出。
- SV 编译选项含 `-Wall --trace-fst --x-assign unique --x-initial unique --coverage`：波形为 FST，**寄存器在复位前的取值是随机的**（见下文"未初始化寄存器"）。
- 源文件来自 `filelist.f`，所以必须先跑生成入口（见 `dpm-chisel-rtl`），否则构建失败。

## 运行入口

用构建系统的运行入口运行测试目标（为什么必须走入口见仓库根目录 `AGENTS.md` 的"运行约定"）：

```bash
xmake test -v                                                     # 全部测试目标与用例
xmake run Controller "-ts=Controller"                             # 一个测试目标
xmake run Controller "-ts=Controller" "-tc=*gate count matches*"  # 一个用例（doctest 通配）
```

`-ts=` 指定 TEST_SUITE，`-tc=` 指定 TEST_CASE（支持通配）。产物与运行目录是 `build/<plat>/<arch>/<mode>/<Name>/`：可执行文件、FST 波形与覆盖率 `.dat` 都写在这里；本机当前 `mode = releasedbg`（配置在 `.xmake/`，不入库）。

## 实测陷阱

### 1. 时间精度必须能整除时钟半周期（本项目用 `ps_10`）

```cpp
dut_context_t ctx{{.coverage = true, .time_precision = verilator_time_unit::ps_10}};
```

框架的 `register_wait` 把等待时长换算成时间精度单位时做的是**整数除法**（`~/verilator_utils/src/scheduler.cppm` 的 `register_wait`：`time_to_wait.rep / time_precision_fs_`）。于是任何不能整除等待时长的精度都会**静默截断**。以本项目的高速时钟为例：`.time_precision = ns` 时，`generate_clock(port.dsp_clock, 5_ns)` 内部按 2.5ns 半周期等待，2.5ns 被截断为 2ns，实际时钟是 **4ns / 250MHz**，而不是期望的 5ns / 200MHz。

危险之处在于它不报错，只会让所有"理论值 vs 实测值"的对比悄悄偏掉（曾表现为相位计数需要一个 1.25 的补偿系数才能对上）。

选择原则是"精度能整除所有等待时长"：`ps`、`ps_10` 都能精确表示 2.5ns，本项目沿用 `test/EdgeDetector.cpp` 的 `.time_precision = ps_10`（10ps）。换算到飞秒要乘 `10000zu`，且 `time_in_time_precision()` 是协程 awaiter，必须 `co_await`：

```cpp
const auto now{static_cast<std::uint64_t>(co_await time_in_time_precision()) * 10000zu};  // 10ps 单位 → fs
```

换用别的时间精度时这个乘数要跟着改。涉及周期的整数运算统一在飞秒域做，避免二次截断。

### 2. 不要依赖寄存器上电初值

DUT 以 `--x-assign unique --x-initial unique` 编译，测试声明的 `runargs` 含 `+verilator+rand+reset+2`，两者叠加的结果是**仿真 0 时刻寄存器是随机值**：直接读 `out.bits.*` 会拿到 230841764、2950827459 这类数字。

- 任何用例都先走 `co_await generate_reset(port.reset, port.clock)` 再开始激励。
- 只检查"复位期间无输出"的用例区分度有限（`RegInit(false.B)` 本来就保证 0）；稳妥的做法是复位释放后**真的跑一次测量**，确认复位没有把功能一起关掉。
- 反向也要注意：只在 `xmake run` 下通过的用例不足以说明问题，用 `xmake test` 复核（那是项目约定的运行参数）。

### 3. 多个 TEST_CASE / GENERATE 迭代要设置 base name

一个 `.cpp` 里出现多个 `TEST_CASE`，或一个用例里用 doctest 的 `GENERATE` 扫参数时，波形与覆盖率文件按 base name 命名，同名会互相覆盖。在 `ctx.loop_until_finish()` **之前**调用：

```cpp
ctx.set_base_name(std::format("Controller.gate.{}_{}", sig1_period_ns, sig2_period_ns));
```

把 `GENERATE` 的参数写进名字，产出的文件带后缀、互不覆盖（如 `Controller.gate.100_200.fst`）。第一个不带 base name 的版本可能已在产物目录留下无后缀的旧文件，它们不参与后续运行，也不会被自动清理。

### 4. 端口宽度与类型

- 单 bit 端口用 `bit_slice<CData>`；多 bit 端口按 **Verilator 的实际类型**选 `vector_slice<T>`：
  ≤8 位是 `CData`、≤16 位是 `SData`、≤32 位是 `IData`。**16 位端口必须写 `vector_slice<SData>`**，
  写成 `vector_slice<IData>` 会编译失败（`no matching constructor ... SData to IData&`）。
- 端口构造建议集中在一个 `port_t` 结构体里，顺便在这里指定宽度与显示进制（如 `{dut.io_out_bits_gateCnt, 28zu, dec_unsigned}`），避免宽度散落在断言里。
- 宽度要和 Chisel 侧一致（`Controller` 的 `gateCnt` 28 位、`edgeCnt` / `phaseCnt` 32 位）。

### 5. 窄脉冲激励要跨过一个上升沿再撤销

在下降沿驱动、上升沿采样之后，`co_await wait_stimulate(port.clock)` 会一直停在**下降沿**。若写成
「置 1 → 等一个下降沿 → 清零」，脉冲只存在半个时钟周期，正好落在两个上升沿之间，**永远采不到**：
本仓库给 `SignalSource` 送 `reload` 脉冲时就这么错过过一次，表现为「改了相位字但输出没变」。
正确写法是让脉冲跨过一个上升沿：

```cpp
port.io_reload = 1;
co_await wait_stimulate(port.clock);   // 对齐到下降沿
co_await wait_verify(port.clock);      // 上升沿采到 1
port.io_reload = 0;                    // 上升沿之后撤销，恰好一拍
```

### 6. 被改动模块的下游端口不会因为「没驱动」而报错

DUT 新增输入端口后，`port_t` 里没写的端口在 Verilator 里保持 0（不是 X），因此旧测试会静默地
继续跑「默认配置」那条分支。给 DUT 加端口时，`port_t` 与各用例里的初始化要显式补上新端口；
真正验证新通路的用例必须显式驱动它们。

**复位端口尤其要注意**：模块用成员端口（不在 `io` 里）显式声明复位时（`RawModule` 的 `resetn`，
或跨域模块由调用方给的目的域复位），漏驱动不会报错，只会让该寄存器**从不复位**、拿到
`--x-assign unique` 的随机初值，表现为用例时好时坏。做法是在 `port_t` 里加
`bit_slice<CData> dst_reset{dut.dstReset, boolean}`，并在每个用例里与主复位一起驱动：

```cpp
ctx.add_task(generate_reset(port.reset, port.clock));
ctx.add_task(generate_reset(port.dst_reset, port.dst_clock));   // 目的域自己的复位
```

手写复位序列的用例（直接 `port.reset = 1/0`）要同样成对设置目的域复位。

**域内同步产生的复位不要额外驱动**：`Controller` 的 DSP 域复位由模块内部的 `ResetSynchronizer` 从主复位
同步得到，`port_t` 里只有 `reset`（主复位），测试不必（也无法）单独驱动它——但**主复位撤销后 DSP 域还要
等 `resetDepth`（默认 4）拍 `dspClock` 才退出复位**，测试的等待时间要覆盖这段延迟。
这类"模块自己产生复位"的结构一旦极性接反（见 `dpm-chisel-rtl` 的极性陷阱），表现是所有用例超时；
反过来，只要测试全绿就说明该域的复位确实被撤销了——这是行为测试相对静态检查的价值所在。

**逐目标的 Verilator 检查豁免**写在 `test/xmake.lua` 的 `extra_verilator_options` 里，并必须写清为什么
（当前两条：`MmcmDrpController` 的 `UNUSEDSIGNAL` 是 AXI4-Lite 接口固有未使用位；`Controller` 的
`SYNCASYNCNET` 是"同一根 `reset` 既做同步复位又做同步链异步端"这一复位同步器固有结构）。豁免只针对
单个检查，其余 `-Wall` 检查全部保留，不要用 `-Wno-fatal` 之类的整体开关。

### 7. 波形与仿真结束

- 激励最后用 `wait_stimulate` 多等几个周期再 `eval_finish()`，避免波形被截断。
- 每个用例结束时 `MESSAGE(ctx.stats())` 打印仿真时间与速度，便于判断闸门时间是否合理。

### 8. AXI 激励必须按**真实地址**驱动（基地址 + 偏移）

AXI 互连**不剥离基地址**，从口看到的是完整 32 位地址。测 AXI 从口时如果按「裸偏移」
（`word_index * 4`）驱动，就测不出「窗口判断误用高位地址」这类缺陷——本项目踩过：
`MmcmDrpController` 早期按 `addr[31:6] == 0` 判窗口，Verilator 用例按裸偏移能过，
上板时真实地址 `0x43C1_0008` 被判为窗口外，所有 DRP 读写被丢弃、`SET_SIG` 恒回 `ERR_BUSY`。
现在 `test/MmcmDrpController.cpp` 用 `kIpBase + word*4` 驱动，并有一条专门的地址译码用例。

### 9. AXI BFM 的握手采样顺序

从口在握手那一拍的下一个周期就会拉低 `awready`/`wready`（地址/数据被锁存），因此
**不能**写成「等 AW 握手 → 等 W 握手」：第二个 `while (!(wvalid && wready))` 会永远等不到。
要么把两个通道同拍举起、在 `@(posedge clk)` **之前**判断握手，要么按
`sim/tb_digital_phase_meter_top.sv` 的顺序（AW 握手 → 撤销 → W 握手 → 撤销 → 等 B）。
本项目 `sim/tb_src_handoff.sv` 早期版本因为这条把整条联调卡死在第一笔写。

## 验证取向：把被测模块当仪表，而不是复刻它的时序

本项目的测试约定是**黑盒精度验证**：用 `verilator_utils::approx` 把测量结果与理论值对比，检查偏差是否落在容限内，而不逐周期建模内部状态机。

- 容限写成"物理量 + 余量"的形式，并在注释里说明余量的来源（握手多等一个信号周期、窗口边界少算/多算一个沿、每个参考周期的量化下界……）。
- 用 doctest 的 `GENERATE` 扫描频率与相位等参数，覆盖多个工作点。
- 闸门时间取足够长（本项目 2000 个低速周期，约 40µs，覆盖数十到上千个信号周期），让量化误差在长时间窗内相互抵消。
- 被测信号必须是**自由运行的周期信号**：控制器在等待停止时需要被测信号的下一个上升沿，一次性脉冲会丢失并挂住握手。
- 具体的观测量定义、理论公式与容限推导见 `dpm-measurement-model`，写断言前先读它。

## 交付前

格式化与静态检查的完整流程见 `dpm-pre-delivery-check`：C++ 侧用 `clang-format` 与 `clang-tidy`（`checks` 与 `NOLINT` 约定按仓库配置），并用 `xmake test -v` 做全量回归。
