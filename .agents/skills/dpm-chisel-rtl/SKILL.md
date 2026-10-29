---
name: dpm-chisel-rtl
description: 'Use when: 在本仓库（DigitalPhaseMeter）新增、修改或重构 Chisel RTL（src/main/scala/DigitalPhaseMeter/*.scala），或需要生成、更新供 Verilator 测试与综合使用的 SystemVerilog。覆盖模块与 Emit 主类写法、生成入口的正确调用形式（Verilator 测试目标构建时会自动调用）、产物目录（每个模块一份，仿真与综合共用）、filelist.f 的消费方式、时钟域与复位分配约定，以及 Chisel 信号名到 SV 端口名的映射。'
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

- `Main`（定义在 `Utils.scala`）提供 `emitSystemVerilog`：创建输出目录、调用 `ChiselStage.emitSystemVerilogFile`；程序收到的参数原样转交给 `ChiselStage`（不再有 `--type` 之类被 `Main` 摘掉的选项，见下文）。
- 输出目录为 `build/chisel/<Name>/`（**每个模块只有一份产物**），`<Name>` 由主类名剥离 `Emit` 前缀得到（`EmitFoo` → `Foo`）。需要与类名不同的目录名时显式传第二个参数。
- 传给 firtool 的固定选项是 `-disable-all-randomization`、`-default-layer-specialization=enable`、
  `--lowering-options=emittedLineLength=120` 与 `--preserve-aggregate=1d-vec`；`--split-verilog` 与
  `--target-dir` 由 `emitSystemVerilog` 自动追加。`emittedLineLength` 只改输出格式（单行代码上限
  放宽到 120 字符）；`--preserve-aggregate=1d-vec` 让一维 `Vec`（如 `MmcmDrpController` 的
  `daddrTable`/`diTable`）保留为具名 packed array，不改变端口与逻辑（对未改动模块，生成的 SV 除
  位置注释外与关闭该选项时逐字节相同）。位置注释保持 firtool 默认的 `// <源文件> <行列>` 形式
  （没有开 `locationInfoStyle=wrapInAtSquareBracket`），路径按编译期系统属性 `chisel.project.root`
  相对化。该项由 `build.mill` 设置：工程根同时写进该系统属性和编译选项（`-Xmacro-settings:...`），
  因此工程根变化、或沿用引入该设置之前编译出的旧 class 产物时，Zinc 会全量重编译并恢复相对路径；
  生成物里不应出现检出路径。生成后可 `grep -rF "$(pwd)" build/chisel` 复核（应无命中）。位置注释
  只影响生成物的可读性与可复现性，不影响逻辑。要改善生成物的可读性，优先从命名与层次入手。
- 参数合法性用 `require` 校验（elaboration 期，如 `require(n >= 2)`、`require(dataWidth % 8 == 0)`）；
  不要用 `assert`（那是硬件断言：常量条件会被优化掉，检查时机也不同）。现有模块统一用 `require`，
  例如 `DspCounter` 的位宽上限、`ResetSynchronizer` 的级数下限。
- `build/chisel/` 是构建产物，**不入库**（`.gitignore` 已覆盖 `build`）。改 RTL 只能改 `.scala` 后重新生成，不要手工编辑生成的 `.sv`。
- 跨模块共享的 FSM 枚举声明在模块外的顶层 `object`（如 `PhaseMeterStateEnum` 定义在 `PhaseMeter.scala`，被 `PhaseMeter` 与 `Controller` 共用）。改动枚举值/顺序会同时影响两个状态机，新增状态时确认两边的 `switch` 都覆盖到。

## 生成入口

通过构建系统的代码生成入口执行生成主类（本项目当前是 Mill，命令在仓库根执行，用 `./mill` 启动脚本）：

```bash
./mill emit                                  # 生成全部 Emit<Name>（按内容缓存，未变化的跳过）
./mill emit Controller DigitalPhaseMeterTop  # 只生成指定模块
./mill emit --force Controller               # 强制重新生成
./mill emit --check                          # 只查询全部 Emit<Name> 是否需要重新生成（不生成、不写缓存）
./mill emit --check Controller               # 只查询指定模块（--check 与 --force 互斥）
./mill runMain DigitalPhaseMeter.EmitController   # 无条件运行单个主类
```

Mill 专属的注意点：

- **一条命令可传多个模块名**：`emit` 的位置参数就是模块名（可写 `Controller`、`EmitController` 或主类全名），
  需要生成多个模块时写在同一条命令里即可，不要拆成多条重复命令。
- **`--force` / `--check` 必须写在模块名之前**：模块名之后的选项不会被当作选项解析，必须写成
  `./mill emit --force Controller` / `./mill emit --check Controller`。
- **`--check` 只查询、不生成，且与 `--force` 互斥**：`./mill emit --check [模块名...]` 用与 `emit`
  相同的缓存判定（见下文「按内容缓存」）逐模块报告是否需要重新生成及原因（产物缺失 / 无缓存记录 /
  入口点或其传递依赖的字节码变化），不执行生成、不写 `build/chisel/.emit-cache/` 缓存、不改
  `build/chisel/` 下的产物；模块名写法与 `emit` 一致，省略时检查全部 `Emit<Name>` 入口点。
  全部最新时退出码为 0；有模块需要重新生成时逐条输出原因并打印汇总，以非零退出码失败
  （检查语义，类似 `./mill fix --check`）。`--check` 与 `--force` 同时使用会报错。
- **`runMain` 的主类名是位置参数**：`./mill runMain DigitalPhaseMeter.EmitController`，其余参数原样传给主类。
  JVM 参数由构建定义（`build.mill` 的 `forkArgs` / `forkEnv`）配置，不在命令行用 `--` 分隔。
- **生成入口会先编译**：`emit` / `runMain` / `emit --check` 都依赖编译产物（`--check` 的缓存键
  基于字节码闭包，同样先编译）；`-Werror` 下编译失败时 `emit` 不会生成，`--check` 也无法给出检查结果。
- **Verilator 测试目标会自动调用生成入口**：`xmake` 构建 `<Name>Verilator` 目标时先执行
  `./mill emit <Name>`（`script/rule.lua` 的 `mill.verilator.shared` 规则；需要时带 `--force`），
  因此跑 `xmake test` / `xmake run` 前不需要手工生成。手工跑生成入口用于：单独生成某个模块、
  为 xsim / Vivado 准备产物，或强制重生成。

### 按内容缓存（`emit`）

`emit` 只在入口点或其传递依赖变化时执行生成：

- **依赖判定取字节码闭包**：从入口主类的 `.class` 出发解析常量池里的类引用，沿项目 class 目录展开，对可达的
  `.class` 文件求 SHA-256，再叠加依赖 classpath 标识。判定看编译产物而不是源码文本：不改变字节码的编辑（例如在文件末尾追加注释）不会触发重跑。
- **缓存按主类隔离**：改 `PhaseMeter` 只会让 `EmitPhaseMeter` / `EmitController` / `EmitDigitalPhaseMeterTop`
  重跑，与 `EmitSignalInputMux` 等入口点无关。
- **持久且不在 clean 范围**：缓存落在 `build/chisel/.emit-cache/`，跨会话有效；`./mill clean` 删的是 Mill 的
  构建缓存/产物 `out/`，不会清掉 `build/chisel/`。
- **带产物检查**：`Emit<Name>` 入口点还会检查约定产物 `build/chisel/<Name>/filelist.f`，缺失时重新生成
  （避免删掉产物后误跳过）。
- **`--check` 复用同一判定且无副作用**：`./mill emit --check [模块名...]` 不生成，只按同一缓存键
  （入口点字节码闭包 + 依赖 classpath 标识等）与 `filelist.f` 产物检查判断每个模块是否需要重新生成，
  并逐模块输出状态与原因；它不写 `build/chisel/.emit-cache/` 缓存、不改 `build/chisel/` 下的产物。
  全部最新时退出码为 0，有模块需要重新生成时以非零退出码失败（见上一节）。
- 需要无条件重跑时，用 `./mill runMain DigitalPhaseMeter.Emit<Name>`（单个主类）或
  `./mill emit --force <模块名>`。
- 实现见仓库根 `build.mill` 的 `emit` 任务；改动摘要或缓存键的构造方式时递增 `EmitCacheFormat`，
  避免旧缓存条目误命中。

排查「产物是不是最新 / 这次改 RTL 会不会触发重生成」或需要 CI 门禁时，先跑 `./mill emit --check`
（或带模块名只查本次改动涉及的入口点）：全部最新时退出码 0，需要重新生成时非零退出并给出原因；
只想确认、不想改动 `build/chisel/` 时用它，确实需要刷新产物再跑 `./mill emit`。

## 产物目录：每个模块一份

`emitSystemVerilog` 对每个模块只生成**一份**产物，仿真与综合消费同一份 RTL：
复位同步由纯 RTL 的 `ResetSynchronizer`（`ResetSynchronizer.scala`）实现，产物里没有 XPM 原语，
Verilator 与 xsim / Vivado 都能直接编译。

| 消费方 | 读的目录 | 入口 |
| --- | --- | --- |
| Verilator 单元测试 | `build/chisel/<Name>/` | `script/rule.lua` 的 `mill.verilator.shared` 规则（构建前先跑 `./mill emit <Name>`，再逐行读 `filelist.f`） |
| xsim 联调（真实 MMCM） | `build/chisel/DigitalPhaseMeterTop/`、`build/chisel/MmcmDrpController/` | `sim/run_handoff_tb.sh` |
| IP 打包 / 综合实现 | `build/chisel/<Name>/` | `vivado/package_ip.tcl`、`vivado/package_src_ip.tcl` |

**新增厂商原语前要先确认 Verilator 与 xsim 两侧都拿得到它的模型**，否则「一份产物」的前提就不成立
（历史上曾按 `--type=sim|syn` 生成两套产物，共用目录时先综合再跑 Verilator 会读到综合产物并报
`Cannot find file containing module: 'xpm_cdc_sync_rst'`；现在复位同步改成纯 RTL 后已删除双产物机制）。

## 生成结果的消费方式

`build/chisel/<Name>/` 下是 `--split-verilog` 拆出的多个 `.sv` 加一份 `filelist.f`。Verilator 测试目标由 `mill.verilator.shared` 规则逐行读取 `filelist.f`（见 `script/rule.lua`），因此：

- 不要手工维护源文件列表，也不要依赖目录 glob（源文件顺序由 firtool 决定）。
- 新模块没有出现在 `filelist.f` 中，说明生成没有跑到，或产物目录不对（见上一节的生成入口）。
- 文件顺序即 firtool 的拓扑顺序，`filelist.f` 就是权威列表。
- **生成目录名、filelist 中的文件名与模块名不一定相同**：模块可用 `override def desiredName` 改名，
  例如 `EdgeDetector` 按配置生成 `EdgeDetectorRising`/`EdgeDetectorBoth`——`filelist.f` 里列的是
  后者，而目录仍叫 `build/chisel/EdgeDetector/`。消费方一律按 `filelist.f` 取文件与模块，不要按目录名
  推导；新增消费方时同样只认 `filelist.f`。**参数不同会改变端口宽度的共享子模块必须把参数编进
  `desiredName`**：`Axi4LiteSlave` 现在按 `Axi4LiteSlaveA<addrUsedWidth>D<dataUsedWidth>` 命名
  （如顶层 `Axi4LiteSlaveA6D32`、DRP 控制器 `Axi4LiteSlaveA12D16`，注意文件名里没有下划线）。否则两个 IP 的生成文件
  被编进同一个仿真库时（`sim/run_handoff_tb.sh` 会把两个 `filelist.f` 一起交给 xvlog）同名模块
  互相覆盖，xelab 报 `[VRFC 10-3091] actual bit length ... differs from formal ...`、仿真行为全错。
- **模块层次与命名**：`MmcmDrpController` 直接 `extends TopModule`（`Utils.scala` 里的公共顶层：
  显式 `clock`/`resetn`，内部 `ResetSynchronizer(clock, resetn)` 同时作为 `ImplicitClock`/
  `ImplicitReset`），不再有外层 `RawModule` + 内层 `Impl`/`InlineInstance`/`FlatIO` 包装；端口仍是
  `clock`/`resetn`/`s_axi_lite_*`/`drp_*`/`mmcm_reset`，内部名没有 `impl_` 前缀（如 `state`、
  `clkFbOut`、`loadPending`；子模块连线 `_axiLiteSlave__io_*`，复位同步器实例
  `reset_resetSynchronizer`）。`DigitalPhaseMeterTop` 同样继承 `TopModule`，主时钟域复位就是
  `TopModule.reset`。写断言或看波形时按这些名字找。
- `Axi4LiteSlave.apply` / `PhaseMeter.apply` 里的 `.suggestName("")` 只是去掉「实例名 + 模块名」的
  重复前缀（生成实例 `axiLiteSlave_`/`phaseMeter_`，内部线名 `_axiLiteSlave__io_*`/`_phaseMeter__io_*`），
  不影响连接与端口名。
- 目录里可能残留上一次生成、这次已被删除（或改名）的模块 `.sv`：消费方都按 `filelist.f` 取文件，
  残留文件不参与编译，但排查"读到了旧模块"时先清掉整个 `build/chisel/` 再重新生成最省事。

## 端口命名：从 Chisel 到测试

生成器保留 Chisel 侧的名字，测试代码按下面的规则拼端口名：

| Chisel | 生成的 SV 端口 |
| --- | --- |
| `val clock = IO(Input(Clock()))` | `clock` |
| `val reset = IO(Input(Bool()))` | `reset` |
| `val dspClock = IO(Input(Clock()))` | `dspClock` |
| `val dstReset = IO(Input(Bool()))`（模块显式声明的复位端口，不在 `io` 里） | `dstReset` |
| `io.sig.extSig_sig1`（`Bundle` 嵌套） | `io_sig_sig1` |
| `io.in` 为 `Flipped(ValidIO(...))` | `io_in_valid`、`io_in_bits_targetGate` |
| `io.out` 为 `ValidIO(...)` | `io_out_valid`、`io_out_bits_gateCnt` |

即 `<bundle 路径用下划线连接>_<字段名>`，字段名保持 camelCase。`prefix("fsm")` 之类的 `prefix` 只影响内部信号名的前缀，不改变端口名。多 bit 端口在测试里必须用 `vector_slice<IData>`（`bit_slice<CData>` 只用于单 bit），详见 `dpm-verilator-testing`。

顶层 IP 的端口名还要满足 Vivado 的 AXI 接口推断约定：`val s_axi_lite = IO(new Axi4LiteSlavePorts(...))` 会生成
`s_axi_lite_awaddr` / `s_axi_lite_rvalid` 这类名字，`ipx::infer_bus_interfaces` 据此自动建立 `s_axi_lite`
从口（详见 `dpm-vivado-ip`）。当前顶层只有这一个 AXI 从口：控制/状态与测量结果都在它的寄存器里
（`ControlRegs`），没有片内存储。顶层统一继承 `Utils.scala` 的 `TopModule`（`RawModule` +
`ImplicitClock`/`ImplicitReset`，提供 `clock` / `resetn` 与内部复位同步器），其余端口（`dspClock`、
被测信号、`s_axi_lite`、`interrupt`）仍显式声明；子模块依赖该隐式时钟复位，不要各自再派生一套。

## 顶层与 AXI 接口的实测陷阱

- **Chisel 是「后写优先」**：同一个寄存器在多个 `when` 分支里赋值时，写在后面的生效。
  在 valid/ready 数据寄存器上，若把「撤销 valid」写在「置位 valid」之后，
  同拍返回的新数据会被吃掉，表现为 `rvalid` 隔拍有效、最后一拍丢失、AXI 突发无法结束。
  正确顺序是先撤销、后置位（见 `Axi4LiteSlave` 的读通道）。
- **AXI 从口在复位期间必须拉低所有 ready**：模块内可以直接读隐式复位
  （`io.axi.arready := (...) && !reset.asBool`）。否则会出现「已握手却等不到响应」——
  主机发请求、从口收下地址，但 `rvalid` 被复位压住，主机永久挂起。
- **AXI4-Lite 端口固定 32 位，命令总线按下游实际使用位宽截断**：`Axi4LiteSlave` 的
  `addrWidth` / `dataWidth` 不可配置（固定 32 位地址 / 32 位数据），只保留 `addrUsedWidth` /
  `dataUsedWidth`（默认 32，即全宽）描述下游真正消费的位宽。写命令与读命令总线本身就按这些低位声明
  （`AxiLiteWritePayload` / `AxiLiteReadPayload` 的地址、数据、字节选通都是截断宽度），AXI 高位在从口处
  直接丢弃、不再零扩展回 32 位；读数据 `rdData` 仍保持 32 位（AXI `rdata` 全宽）。不这样做，综合器会把
  未读的寄存器高位 trim 掉并报 `[Synth 8-3936]`。新增/修改 AXI 从口时按下游实际译码位宽传参：本仓库
  `dpm_top` 只译码 `addr[5:2]`（`addrUsedWidth=6` / `dataUsedWidth=32`），`dpm_src_drp` 用
  `addr[11:0]`、写数据只用低 16 位（`addrUsedWidth=12` / `dataUsedWidth=16`）；下游模块的 payload
  宽度必须与从口参数一致（顶层 `ControlRegs` 按 6 位地址实例化，`MmcmDrpController` 的地址窗口译码只看
  `addr[11:0]`）。行为等价性：`MmcmDrpController` 的 Verilator 用例覆盖「真实基地址 + 页内偏移」；
  `dpm_top` 由顶层 Verilator 用例 `test/DigitalPhaseMeterTop.cpp` 按真实基地址 `0x43C0_0000 + 偏移` 驱动覆盖。
- **Chisel 7 有现成的位宽/方向表意写法，不必手写 helper**：内部命令总线用 `EnqIO`（等价
  `DecoupledIO`，从口产出命令）与 `DeqIO`（等价 `Flipped(DecoupledIO)`，寄存器堆消费命令）；
  位宽剪裁用 `take(n)`（取低 n 位）、零扩展用 `pad(w)`（`w` 不大于原位宽时返回原值，否则高位补 0）。
  它们生成的 SV 与手写切片/拼接完全一致（`Axi4LiteSlave` 与 `ControlRegs` 已按此写法重构）。
- **Bundle 构造参数不要叫 `width`**：与 Chisel `Aggregate.width` 冲突（要求 `override`），
  用 `dataWidth` 之类的名字。子类复用父 Bundle 的参数时要写 `override val`。
- **显式复位端口也是普通端口，测试不驱动就等于该域不复位**：模块自己声明的复位端口（不在 `io` 里，
  例如 `RawModule` 里显式写的 `resetn`）会生成同名端口。Verilator 里未驱动的输入保持 0，于是该时钟域的
  寄存器**永远不进入复位**、只拿到 `--x-assign unique` 的随机初值——测试可能时好时坏。给模块加复位
  端口时，`test/<Name>.cpp` 的 `port_t` 与每个用例的复位序列都要同步补上（见 `dpm-verilator-testing`）。
- **`ResetSynchronizer` 的输入是低有效复位**（顶层直接接 PS 的 `resetn`）。在模块内部用它从**高有效**的
  隐式 `reset` 生成本域复位时必须取反：`ResetSynchronizer(dspClock, !reset.asBool)`。写成
  `reset.asBool` 极性正好反了——RTL 能生成、能综合、时序也正常，但该域的寄存器会在**系统复位撤销后**
  被永久按在复位里。实测表现：Verilator 的 `Controller` 用例全部超时（测量永远不返回），波形里
  `dspReset` 在主复位拉低的那一刻被异步置位后一直为 1。这类错误只能靠行为测试抓到。
- **`ExtModule` 的端口不走 `io`**（本仓库暂无 `ExtModule` 实例，将来引入厂商原语时会用到）：
  把端口直接声明成类的成员，例化后写 `ram.douta` 而不是 `ram.io.douta`。
  未连接的厂商原语输入端口没有 Verilog 默认值，必须显式驱动。
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
  `CTRL.clear` 反过来是**clear 优先**：它用 `WireDefault` 脉冲当拍生效（不挂起，无需 pending 状态），
  块写在结果锁存块之后，同拍到达的结果被丢弃、`busy` 仍按测量完成撤销；clear 只清结果区与标志，不动
  `busy` / `GATE_TARGET` / `SIG_CTRL`。
- **数据量只有几个字时不要引入片内存储**：一次测量的结果只有 4 个字，直接把结果锁存进可由 PS 读回的
  只读寄存器（GPIO 式数据通道）远比「BRAM + 独立 AXI4 突发口」简单——后者要 AXI4 全速从口、双口 RAM、
  突发/选通/`SLVERR` 处理与地址译码，前者只要几个寄存器加一次 `out.valid` 锁存。选通道先看数据量，
  不要为了「通用」上存储；只保留最新一条时配 `LOST_COUNT`（结果未被读走即被覆盖的次数）+ 自增
  `seq`，软件就能发现漏读。
- `Fill(n, value)` 是两个参数；`MuxCase(default, Seq(cond -> value))` 里的条件必须是 `Bool`
  （写 `(mode === 1.U) -> addr` 而不是 `mode -> addr`）。
- **高速域（当前 450MHz）只放「边沿检测 + 计数器/比较器 + 二级 FSM」**：频率/相位全部交给 MMCM，
  高速域不要放配置运算或分频逻辑。450MHz 下测量域自身最差 setup 路径余量在 **+0.5~0.6ns 量级**
  （2026-10-26 当前 RTL 实测 **+0.553ns**：`dspReset` 释放链 → 边沿检测链复位脚；纯数据/控制脚
  路径余量 ≥ +0.65ns，同类路径随布局布线浮动，具体以最新签核报告为准）；**整体 WNS 受 clk_wiz
  Safe Clock Startup 内部路径限制（+0.271ns）**，不要拿整体 WNS 判断测量逻辑还剩多少优化空间。
- **450MHz 的 32 位计数器用 DSP48E1，不要用 fabric 进位链**：fabric 32 位计数器（8 个 CARRY4 串联）
  与「16 位低段 + 16 位高段」的分段计数器在该器件上都收敛不到正余量，固定开销在
  「FF → LUT → CARRY4 的 S 输入」与计数器 Q 的远端布线。DSP48E1 在 -1 速度等级的 FMAX 是
  464.25MHz（DS187 Table 66），450MHz 用掉约 97%，但 Vivado 会检查 DSP 内部路径与
  CARRYIN/CEP/RSTP 控制脚；本设计实现后计数器/控制路径余量在 +0.5ns 量级（具体值以
  `impl_stability.tcl` 的 `stability_a<N>_timing.rpt` 为准，随实现轮次略有变化）。
- **计数器的控制写法决定 DSP 映射**：`cnt := Mux(io.clear, 0.U, cnt + io.enable)` 会让综合器把使能
  推断成 DSP 的 CARRYIN（前面多一级 LUT）；写成
  `when(io.clear) { cnt := 0.U }.elsewhen(io.enable) { cnt := cnt + 1.U }` 才会映射成 DSP 的
  RSTP（同步复位）+ CEP（时钟使能），使能/清零由**寄存器直接**驱动控制脚。用
  `addAttribute(cnt, """use_dsp = "yes"""")` 显式要求 DSP，避免综合器换成 fabric 实现。
- **边沿检测器的组合触发要打一拍再进 FSM/使能译码**：3 级同步链最后一级 → 组合触发脉冲 → FSM 两级
  LUT 是一条 0.188~0.212ns 余量的路径。给**四条**边沿检测输出（sig1 / sig2 / request / responseReady）
  统一加一级 `RegNext` 后：检测器输出只驱动本地触发器，FSM 输入来自寄存器，本设计里相关路径
  余量 > +0.7ns。因为四路整体晚 1 个高速周期，`pulseSig1Valid` / `pulseSig2Valid` / `phaseCntEnable`
  之间的相对时序与相位计数窗口长度不变，握手只是多 1 拍；计数器停止后冻结、慢域握手完成后才锁存，
  读数不变（顶层用例与加流水前逐条 diff 为空、Verilator Controller 用例全过）。
- **派生运算放到低速域，高速域只留「取寄存器 → 比较/计数」**：只要一个值在配置期算得出来，
  就不要留到高速域的每拍路径上（「取配置 → 减法 → 比较器 → 计数器」这种链在高速域必违约）。
  需要更低频率时把职责交回 MMCM 或换外部手段，不要在测量域里做数字分频。
- **寄存器堆往下游的输出必须真的被消费，否则整条通路会被 firtool 删掉**：本仓库曾在 `ControlRegs` 里
  用 `WireDefault(false.B)` 声明了一个「本拍写了配置」的标志，却在三个 `is(...)` 分支里只给它赋值、
  忘了用它置「配置待送出」标志。结果下游 `io.config.valid` 恒 0 → firtool 把**整条下游通路与端口
  全部优化掉**，顶层实例化里连端口都不剩。表现很有迷惑性：寄存器读写回读完全正常（`SIG_CTRL` 读回 3），
  但下游一直用默认配置（选择位恒 0）；而只测下游模块、绕过寄存器堆的 Verilator 单测**全绿**。
  防御手段有两个：生成后 `grep` 一下顶层 SV 里目标模块还有没有那组端口；以及**寄存器堆到下游的
  通路必须由顶层 Verilator 用例覆盖**（`test/DigitalPhaseMeterTop.cpp`，见 `dpm-verilator-testing`）。
- **Verilator 单测当前按默认告警集编译**：`test/xmake.lua` 只传
  `--trace-fst --x-assign unique --x-initial unique --coverage -Wno-SYMRSVDWORD`，没有 `-Wall`，
  也没有逐目标豁免表。下面两类告警因此不挡构建，但设计上仍要知道它们（临时加 `-Wall` 排查时会碰到）：
  - `UNUSEDSIGNAL`：主要是**接口固有未使用位**（`MmcmDrpController` 的 AXI4-Lite `awprot` /
    `wstrb` 高字节、截断后的地址高位等，本设计不用保护级与 >16 位数据，`SignalInputMux` 已内联、
    不再有未消费的域端口）。临时开 `-Wall` 时按需关掉这一条并写清原因，不要用 `-Wno-fatal`
    之类的整体开关。
  - `Bits of signal are not used: '_phaseShifted_T'[30:0]`：只用中间信号的最高位，其余位成了
    「未使用信号」。寄存器（`Reg`）的部分位不被使用不会告警，**`wire` 会**。做法是把「相位差值比较
    半个周期」改成两个独立累加器（`phase1` / `phase2`，初值 `-phaseWord`），而不是
    `phase - phaseWord` 后再取最高位；实在需要中间 wire 时，把它写进寄存器。

## 时钟域与复位分配

`Controller` 同时使用两个时钟：低速 `clock` 域（FSM、闸门计数）与高速 `dspClock` 域（测量核心）。新增逻辑时先确定它属于哪个域：

- 高速域模块用 `withClockAndReset(dspClock, dspReset) { ... }` 例化；`dspReset` 由 `Controller` 内部的 `ResetSynchronizer` 从主复位同步得到，不要在高速域直接使用低速域的 `reset`。
- **每个时钟域一个复位同步器，复位释放必须与消费它的时钟同域**：顶层例化主时钟域那个（`TopModule.reset` = `ResetSynchronizer(clock, resetn)`，与 `clock` 同步，接 PS 的低有效 `resetn`）；**DSP 域那个在 `Controller` 内部**（与 `dspClock` 同步，从高有效的 `TopModule.reset` 取反接入，见上面的极性陷阱），其输出给 `PhaseMeter`。原因是两个时钟都源自 MMCM、相位关系不固定，而
  `withClockAndReset(clk, boolReset)` 生成的复位在消费端是**同步复位**（`always @(posedge clk) if (reset)`）：
  拿主时钟域的复位去释放高速域寄存器，会让释放沿落在 `dspClock` 的建立/保持窗口里。**曾经踩过**——
  把 `dspReset` 也接到 `clock` 域的同步器上（照抄上一行），生成 RTL 表面完全正常，实际是跨域释放。
  DSP 域复位**由谁产生**还会影响 `dspClock` 域复位分配网络的布局与余量；当前 450MHz 设计把链的
  异步端接在 `TopModule.reset` 上。2026-10-26 当前 RTL 实测测量域最差 setup 路径是
  `dspReset` 释放链 → 边沿检测链复位脚（+0.553ns；同类路径随布局布线在 0.5~0.6ns 量级浮动），
  整体 WNS 由 clk_wiz Safe Clock Startup 内部路径决定（+0.271ns）。
- `ResetSynchronizer` 是 `RawModule`，自己显式声明 `clock` / `resetn` / `reset`；复位链直接复用
  `MyShiftRegister(false.B, n, true.B)`（`ShiftRegister.scala`），并且例化在
  `withClockAndReset(clock, (!resetn).asAsyncReset)` 里，因此链上寄存器是**异步复位**的：置位立即生效，
  释放与传入的时钟对齐（n 级）。消费端把它当普通 `Bool` 同步复位用即可，不需要 `AsyncReset` 类型。
- **同步链的寄存器要带 `ASYNC_REG`**：`MyShiftRegisters`/`MyShiftRegister`（`ShiftRegister.scala`）
  现在**不生成子模块**：它返回一个打包的 `UInt` 寄存器（`RegInit(Fill(n, init))`），按
  `pipeline := Cat(pipeline.take(n - 1), in)` 移位，`addAttribute` 加在这一个向量寄存器上——生成 SV
  是**一条** `(* ASYNC_REG = "TRUE" *) reg [n-1:0] ...`（父版本是逐位 `pipeline_i` 各一条）。
  `asBools` 返回**从最低位到最高位**：bit0 是最新采样、最高位最旧；`EdgeDetector` 用
  `pipeline(n-1)`/`pipeline(n-2)` 取最旧的两级。复位同步链、`EdgeDetector` 采样链与 `CdcLevel` 都
  复用它，不要在别处另写裸移位链。
  该属性只在**综合/实现期**起作用（工具据此把链上触发器放近、按异步输入处理），Verilator 与 xsim
  完全看不见——所以"属性还在不在"只能靠生成后 grep 生成的 SV，或综合后查
  `get_property ASYNC_REG [get_cells -hier -filter {ASYNC_REG == 1}]`。当前向量写法已在
  **2026-10-26 的 routed DCP 上逐位复核**：67 个同步链触发器（4 条边沿检测链、3 条复位链、
  2 处 `locked` 同步链、厂商 clk_wiz/MMCM 启动链）全部 `ASYNC_REG=1`，向量写法没有丢属性；
  后续 RTL 改动交付前按同样方式再复核一次。行为本身由 `test/ResetSynchronizer.cpp` 覆盖
  （异步置位 + 恰好 n 拍释放）。
- 跨域的控制信号沿用现有的握手约定（`Controller` 用 `request` / `response` 的 valid-ready 加 `EdgeDetector` 做脉冲展宽），不要新增裸的跨域电平信号。
- **软件写的配置位不要跨域**：`SIG_CTRL` 的使能/选择位与「输入门控 + 二选一」都放在主时钟域
  （和写它的寄存器堆同域，见 `SignalInputMux`），因此写寄存器与切换同拍生效，不需要任何跨域通路。
  **判断标准**：当一个跨域方案是为了消掉「目的域可能读到中间态」时，先问中间态本身有没有危害；
  没有危害就不要建通路，把两侧逻辑放进同一个域更简单（软件本来就只在 `STATUS.busy = 0` 时切换，
  中间态最多让一次测量作废，不会给出貌似合理的错值）。
- 真正需要跨域的信号分两类，各有现成写法：**事件/脉冲**用 `CdcToggleHandShake`
  （「电平翻转 + 同步链」，`EdgeDetector(both)` 收）；**准静态电平**用 `CdcLevel`
  （3 级 `ASYNC_REG` 移位链，例如 MMCM 的 `locked`）。两者都不要另写裸电平跨域。
- 跨域模块的目的域复位**由调用方给**（如 DSP 域复位由 `Controller` 产生、传给它例化的高速域模块），
  不要在模块内部用 `Module.reset` 重新派生一份——复位释放必须与消费它的时钟同域。
- 不要为了仿真方便改变寄存器的复位方式；测试侧通过复位序列获得确定初值（见 `dpm-verilator-testing`）。

## 格式与检查

- Scala 代码用 `scalafmt` 格式化（配置见 `.scalafmt.conf`，`maxColumn = 120`、4 空格缩进）：`scalafmt` 就地格式化，`scalafmt --test` 只检查。提交前保证 `scalafmt --test` 通过。
- 静态分析工具链：Mill 的 Scalafix 插件（`com.goyeau::mill-scalafix`，规则与配置在 `.scalafix.conf`）+ 编译器 lint（`build.mill` 的 `scalacOptions`）。规则一句话：Scalafix 禁 `var` / `throw` / `null` / `while` / `return` 等语法（改用 Chisel 的 `Reg` / `Wire` / `require` 写法）并整理导入，编译器侧统一打开 `-Xlint` / `-Wunused` / `-Wdead-code`，且 `-Werror` 让告警变成构建失败。检查命令：`./mill compile`（编译器 lint）、`./mill fix --check`（Scalafix 检查）、自动修复 `./mill fix`、格式检查 `scalafmt --test`；标准都是 0 warning。
- 改完 RTL 先保证上述检查通过：`-Werror` 下任何告警都会让 `compile` 失败，带着告警去生成没有意义。跑 `xmake test` 时构建目标会自动调用生成入口；单独生成、或为 xsim / Vivado 准备产物时再手工跑 `./mill emit`。新增/重命名模块、调整导入时尤其注意未使用 import、非穷尽 match 与 nullary-unit 这几类告警（检查项与有意关闭的规则见 `dpm-pre-delivery-check` 第 2 节）。
- 注释遵循仓库根目录 `AGENTS.md` 的语言约定，风格为 Doxygen（`/** ... */`，参数用 `@param`）；公共模块类的文档注释要说明它在哪个时钟域、各参数含义。
- 新增模块后同步补齐：生成入口（`Emit<Name>`）、必要时 `test/<Name>.cpp`，并把新踩到的坑写回本 skill。
