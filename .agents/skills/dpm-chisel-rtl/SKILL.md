---
name: dpm-chisel-rtl
description: 'Use when: 在本仓库（DigitalPhaseMeter）新增、修改或重构 Chisel RTL（src/main/scala/DigitalPhaseMeter/*.scala），或需要生成、更新供 Verilator 测试与综合使用的 SystemVerilog。覆盖模块与 Emit 主类写法、生成入口的正确调用形式、sim/syn 两种产物、filelist.f 的消费方式，以及 Chisel 信号名到 SV 端口名的映射。'
argument-hint: '要新增或修改的 RTL 模块（如 Controller、PhaseMeter）'
---

# DigitalPhaseMeter 的 Chisel RTL 与代码生成

## 适用场景

- 新增或修改 `src/main/scala/DigitalPhaseMeter/` 下的模块、参数与端口。
- 重新生成 SystemVerilog，供 `test/` 下的 Verilator 测试或后续 Vivado 综合使用。
- 排查"测试看不到新端口""生成的 SV 是旧版本""输出文件位置不对"这类问题。

## 模块与生成主类

每个模块一个 `.scala` 文件，文件内包含模块定义与它的生成主类：

```scala
class Foo(val width: Int = 32) extends Module { /* ... */ }

object EmitFoo extends Main {
    emitSystemVerilog(new Foo)
}
```

- `Main`（定义在 `Utils.scala`）提供 `emitSystemVerilog`：解析 `--type` 选项、创建输出目录、调用 `ChiselStage.emitSystemVerilogFile`。
- 输出目录为 `build/chisel/<Name>/`，`<Name>` 由主类名剥离 `Emit` 前缀得到（`EmitFoo` → `Foo`）。需要与类名不同的目录名时显式传第二个参数。
- 传给 firtool 的固定选项是 `-disable-all-randomization` 与 `-default-layer-specialization=enable`；`--split-verilog` 与 `--target-dir` 由 `emitSystemVerilog` 自动追加。
- 参数合法性用 `assert` 校验（如 `assert(n >= 2 && n <= 10)`），与既有模块保持一致。
- `build/chisel/` 是构建产物，**不入库**（`.gitignore` 已覆盖 `build`）。改 RTL 只能改 `.scala` 后重新生成，不要手工编辑生成的 `.sv`。

## 生成入口

通过 sbt 的运行入口执行生成主类，参数直接跟在类名之后：

```bash
sbt -batch "runMain DigitalPhaseMeter.EmitController"             # 仿真用，默认 --type=sim
sbt -batch "runMain DigitalPhaseMeter.EmitController --type=syn"  # 综合用
```

**不要用 sbt 的 `--` 分隔符传递参数**（这是本仓库实测的陷阱）：

```bash
sbt -batch "runMain DigitalPhaseMeter.EmitController -- --type=syn"   # 错误
```

sbt 会把字面量 `--` 作为第一个参数传给程序，`emitSystemVerilog` 只摘掉 `--type=...`，于是传给 `ChiselStage` 的参数列表变成 `-- --split-verilog --target-dir build/chisel/Controller`；混入的 `--` 让 `--target-dir` 不再生效，**SV 会被写进进程当前目录（仓库根）而不是 `build/chisel/<Name>/`**，目标目录只剩一个空目录。表现为"生成成功但测试看不到模块""仓库根目录多出一堆 `.sv` 和 `filelist.f`"。正确形式是把 `--type=...` 直接写在 `runMain` 之后。若已经产生这类散落文件，删掉仓库根下的 `*.sv` / `filelist.f` 后按正确形式重新生成。

## sim 与 syn 两种产物

`--type=sim`（默认）与 `--type=syn` 的差别通过 `Config.outType` 传播到 RTL，当前唯一的用户是 `SyncReset`：

| 产物 | `SyncReset` | 用途 |
| --- | --- | --- |
| sim | `SyncResetSimulate`，纯 RTL 移位链 | Verilator 仿真与测试 |
| syn | `xpm_cdc_sync_rst`（Vivado 原语 `ExtModule`） | xsim、综合、布局布线 |

两个分支驱动的是同一个 `io`（见 `Xpm.scala`），因此接口一致、端口相同。Verilator 侧没有 XPM 模型（会报找不到 XPM 模块 / 未声明的 `glbl`），因此**不要用 `--type=syn` 覆盖 Verilator 测试用的生成结果**；AXI4 接口与顶层 IP 的仿真统一走 xsim，用 syn 产物，见 `dpm-vivado-ip`。

## 生成结果的消费方式

`build/chisel/<Name>/` 下是 `--split-verilog` 拆出的多个 `.sv` 加一份 `filelist.f`。测试目标逐行读取 `filelist.f`（见 `test/xmake.lua` 的 `on_load`），因此：

- 不要手工维护源文件列表，也不要依赖目录 glob（源文件顺序由 firtool 决定）。
- 新模块没有出现在 `filelist.f` 中，说明生成没有跑到，或产物目录不对（见上一节的 `--` 陷阱）。
- 文件顺序即 firtool 的拓扑顺序，`filelist.f` 就是权威列表。

## 端口命名：从 Chisel 到测试

生成器保留 Chisel 侧的名字，测试代码按下面的规则拼端口名：

| Chisel | 生成的 SV 端口 |
| --- | --- |
| `val clock = IO(Input(Clock()))` | `clock` |
| `val reset = IO(Input(Bool()))` | `reset` |
| `val dspClock = IO(Input(Clock()))` | `dspClock` |
| `io.sig.sig1`（`Bundle` 嵌套） | `io_sig_sig1` |
| `io.in` 为 `Flipped(ValidIO(...))` | `io_in_valid`、`io_in_bits_targetGate` |
| `io.out` 为 `ValidIO(...)` | `io_out_valid`、`io_out_bits_gateCnt` |

即 `<bundle 路径用下划线连接>_<字段名>`，字段名保持 camelCase。`prefix("fsm")` 之类的 `prefix` 只影响内部信号名的前缀，不改变端口名。多 bit 端口在测试里必须用 `vector_slice<IData>`（`bit_slice<CData>` 只用于单 bit），详见 `dpm-verilator-testing`。

顶层 IP 的端口名还要满足 Vivado 的 AXI 接口推断约定：`val s_axi_lite = IO(new Axi4LiteSlavePorts(...))` 会生成
`s_axi_lite_awaddr` / `s_axi_lite_rvalid` 这类名字，`ipx::infer_bus_interfaces` 据此自动建立 `s_axi_lite`
从口（详见 `dpm-vivado-ip`）。当前顶层只有这一个 AXI 从口：控制/状态与测量结果都在它的寄存器里
（`ControlRegs`），没有片内存储。顶层用 `RawModule` 显式声明 `clock` / `resetn` / `dspClock`，
不要依赖隐式时钟复位。

## 顶层与 AXI 接口的实测陷阱

- **Chisel 是「后写优先」**：同一个寄存器在多个 `when` 分支里赋值时，写在后面的生效。
  在 valid/ready 数据寄存器上，若把「撤销 valid」写在「置位 valid」之后，
  同拍返回的新数据会被吃掉，表现为 `rvalid` 隔拍有效、最后一拍丢失、AXI 突发无法结束。
  正确顺序是先撤销、后置位（见 `Axi4LiteSlave` 的读通道）。
- **AXI 从口在复位期间必须拉低所有 ready**：模块内可以直接读隐式复位
  （`io.axi.arready := (...) && !reset.asBool`）。否则会出现「已握手却等不到响应」——
  主机发请求、从口收下地址，但 `rvalid` 被复位压住，主机永久挂起。
- **Bundle 构造参数不要叫 `width`**：与 Chisel `Aggregate.width` 冲突（要求 `override`），
  用 `dataWidth` 之类的名字。子类复用父 Bundle 的参数时要写 `override val`。
- **`ExtModule` 的端口不走 `io`**：像 `Xpm.scala` 那样把端口直接声明成类的成员，
  例化后写 `ram.douta` 而不是 `ram.io.douta`。未连接的 XPM 输入端口没有 Verilog 默认值，
  必须显式驱动（`sleep`、`regcea/regceb`、`injectsbiterr*` 等）。
- **顶层里「同一套 bundle、方向约定相同」的两个端口不能直接 `:=` 或 `<>`**：
  前者对输出方向不成立，后者要求方向相反。`DigitalPhaseMeterTop` 用
  `DataMirror.directionOf` 按字段方向逐字段连接（`connectTopPort` / `connectComplementary`），
  避免手写几十行同名连接。
- **firtool 会删掉子模块里未被使用的端口**（例如 `Axi4LiteSlave.io.rd.valid` 因为寄存器堆
  只用地址而被优化掉），但**顶层模块的端口都会保留**，因此顶层可以放心暴露 IP-XACT 需要的
  全部端口（`awprot`、`awregion`、`awqos` 等即使当前不使用也在）。
- **「软件清标志」与「硬件置标志」同拍竞争时，用后写优先决定谁赢**：`ControlRegs` 里软件写
  `IRQ_STATUS` 清 `resultValid` 的块在前、`Controller.out.valid` 锁存新结果并置 `resultValid`/`irqPending`
  的块在后，因此同一拍按「新结果优先」处理，刚到达的结果与中断不会被软件的清除动作吃掉。顺序反过来会丢结果。
- **数据量只有几个字时不要引入片内存储**：一次测量的结果只有 4 个字，直接把结果锁存进可由 PS 读回的
  只读寄存器（GPIO 式数据通道）远比「BRAM + 独立 AXI4 突发口」简单——后者要 AXI4 全速从口、双口 RAM、
  突发/选通/`SLVERR` 处理与地址译码，前者只要几个寄存器加一次 `out.valid` 锁存。本仓库最初按 BRAM 实现
  （`Axi4BramSlave` + `DualPortRam`），后来整体删除换成寄存器通道，顶层只剩一个 AXI4-Lite 从口，
  IP 打包与 BD 也随之简化。选通道先看数据量，不要为了「通用」上存储。
  只保留最新一条时，配一个 `LOST_COUNT`（结果未被读走即被覆盖的次数）加自增 `seq`，软件就能发现漏读。
- `Fill(n, value)` 是两个参数；`MuxCase(default, Seq(cond -> value))` 里的条件必须是 `Bool`
  （写 `(mode === 1.U) -> addr` 而不是 `mode -> addr`）。
- **400MHz 域里不要放 32 位加减法**：片内信号源最初用「32 位相位累加器 + 相位字预置」实现，实测在
  `xc7z010clg400-1`（−1）上综合实现直接违约（配置字预置 → 32 位进位链 → 累加器 D 端，WNS −0.55ns），
  把预置值改成寄存器直连后违约路径换成了「跨区域取配置字 → 比较器」，WNS −0.71ns。最终改成
  **MMCM 出两路同频、相位差可调的时钟 + 测量时钟域里的小计数器数边沿分频**：
  频率与相位差交给 PL 内 MMCM 的动态重配置（寄存器写），400MHz 域里只剩 16 位计数器与比较器，
  时序一次通过。结论：**高速域只放计数器/比较器这类短逻辑，宽加减法与相位累加放到 MMCM 或低速域**。
- **跨时钟域送来的多 bit 配置在高速域里落点可能很远**：分频比从配置总线到分频器属于同一个 DSP 域，
  但两者跟随的物理通路不同，布局后这条同域路径实测 6.3ns（逻辑 2.9 + 布线 3.5）。处理方式是在
  消费模块里先打一拍（`RegNext(io.config)`），让比较器与计数器同处一块区域，并在 XDC 里把
  「配置寄存器 → 消费模块」这条同域长路径按准静态放宽（`set_max_delay -datapath_only 20`）：
  这个值只在软件写寄存器时改变，且跨时钟域握手保证它在采样窗口内稳定。
- **派生运算放到低速域，高速域只留「取寄存器 → 比较/计数」**：分频比要先变成计数终值 `N - 1`，
  第一版在高速域里算，路径变成「取配置字 → 16 位减法 → 比较器 → 计数器」，即使两者都在同一个
  模块里也违约（实测 −1.7ns，其中布线 2.3ns）。规律：**只要一个值在配置期算得出来，就不要留到
  400MHz 的每拍路径上**（与「预置值减法」那条坑同源）。
  最终结论更彻底：这个分频器整体被删掉了——把「产生低频信号」的职责交回 MMCM（只改 MMCM 的输出
  范围），高速域不再新增任何逻辑，时序一次通过。见 `dpm-vivado-ip` 的「不要在 IP 里对 MMCM 输出
  再做数字分频」。
- **寄存器堆往下游的输出必须真的被消费，否则整条通路会被 firtool 删掉**：本仓库在 `ControlRegs` 里
  用 `WireDefault(false.B)` 声明了一个「本拍写了配置」的标志，却在三个 `is(...)` 分支里只给它赋值、
  忘了用它置 `configDirty`。结果 `configPending` 恒为 0 → `io.config.valid` 恒 0 → firtool 把
  **跨时钟域总线、目的域配置寄存器、以及 `Controller` 上那一组 `io_config_*` 端口全部优化掉**，
  顶层实例化里连端口都不剩。表现很有迷惑性：寄存器读写回读完全正常（`SIG_CTRL` 读回 3），
  但下游一直用默认配置（选择位恒 0）；而只测 `Controller`、绕过寄存器堆的 Verilator 单测**全绿**。
  防御手段有两个：生成后 `grep` 一下顶层 SV 里目标模块还有没有那组端口；以及**寄存器堆到下游的
  通路必须有 xsim 顶层用例覆盖**（见 `dpm-vivado-ip`）。
- **独立生成给 Verilator 单测的模块要能过 `-Wall`**：`test/xmake.lua` 用 `-Wall` 编译
  `build/chisel/<Name>/`，警告即失败（`%Error: Exiting due to N warning(s)`）。两类常见告警都来自
  「共享数据结构 + 只关心一部分」：
  - `UNUSEDSIGNAL: Signal is not used: 'io_config_srcSelect'`：模块接收了一个自己用不到的字段
    （输入选择位只在 `SignalInputMux` 用）。做法是给只关心部分字段的模块一个**窄 payload**
    （`SignalSourceConfigPayload`），宽 payload（`SignalConfigPayload`）只留给跨时钟域整字传递。
  - `Bits of signal are not used: '_phaseShifted_T'[30:0]`：只用中间信号的最高位，其余位成了
    「未使用信号」。寄存器（`Reg`）的部分位不被使用不会告警，**`wire` 会**。做法是把「相位差值比较
    半个周期」改成两个独立累加器（`phase1` / `phase2`，初值 `-phaseWord`），而不是
    `phase - phaseWord` 后再取最高位；实在需要中间 wire 时，把它写进寄存器。

## 时钟域

`Controller` 同时使用两个时钟：低速 `clock` 域（FSM、闸门计数）与高速 `dspClock` 域（测量核心）。新增逻辑时先确定它属于哪个域：

- 高速域模块用 `withClockAndReset(dspClock, dspReset) { ... }` 例化，`dspReset` 来自 `SyncReset()`；不要在高速域直接使用低速域的 `reset`。
- 跨域的控制信号沿用现有的握手约定（`Controller` 用 `request` / `response` 的 valid-ready 加 `EdgeDetector` 做脉冲展宽），不要新增裸的跨域电平信号。
- **多 bit 配置不要逐位同步**：`CdcConfigBus`（`Cdc.scala`）用「数据先稳定 → 翻转请求 → 目的域锁存并回应答」的
  整字握手，源域收到应答才放行下一笔，因此连续快速配置既不丢中间值、目的域也不会读到中间态。
  两个易错点：目的域配置寄存器要 `RegInit(0.U.asTypeOf(gen))` 给一个确定的复位默认值（否则 Verilator
  的随机初值会让选择位随机）；源域的「应答」与「发起」两个 `when` 的先后顺序决定同拍冲突时谁赢
  （Chisel 后写优先），这里让「发起」写在后面。跨域通路由 `dpm-vivado-ip` 里那条双向 max_delay 覆盖。
- 不要为了仿真方便改变寄存器的复位方式；测试侧通过复位序列获得确定初值（见 `dpm-verilator-testing`）。

## 格式与检查

- Scala 代码用 `scalafmt` 格式化（配置见 `.scalafmt.conf`，`maxColumn = 120`、4 空格缩进）：`scalafmt` 就地格式化，`scalafmt --test` 只检查。提交前保证 `scalafmt --test` 通过。
- 注释遵循仓库根目录 `AGENTS.md` 的语言约定，风格为 Doxygen（`/** ... */`，参数用 `@param`）；公共模块类的文档注释要说明它在哪个时钟域、各参数含义。
- 新增模块后同步补齐：生成入口（`Emit<Name>`）、必要时 `test/<Name>.cpp`，并把新踩到的坑写回本 skill。
