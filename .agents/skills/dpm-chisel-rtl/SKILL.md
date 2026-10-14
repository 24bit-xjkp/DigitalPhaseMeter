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

| 产物 | `SyncReset` 的实现 | 用途 |
| --- | --- | --- |
| sim | `SyncResetSimulate`，纯 RTL 移位链 | Verilator 仿真与测试 |
| syn | `xpm_cdc_sync_rst`（Vivado 原语 `ExtModule`） | 综合、布局布线 |

两个分支驱动的是同一个 `io`（见 `Xpm.scala`），因此接口一致、端口相同——这一点是源码层面的结论，仓库里没有 syn 产物可对照。测试目标只消费 sim 版本；综合版本里是 XPM 原语实例，Verilator 侧没有对应模型，因此**不要用 `--type=syn` 覆盖测试用的生成结果**。

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

## 时钟域

`Controller` 同时使用两个时钟：低速 `clock` 域（FSM、闸门计数）与高速 `dspClock` 域（测量核心）。新增逻辑时先确定它属于哪个域：

- 高速域模块用 `withClockAndReset(dspClock, dspReset) { ... }` 例化，`dspReset` 来自 `SyncReset()`；不要在高速域直接使用低速域的 `reset`。
- 跨域的控制信号沿用现有的握手约定（`Controller` 用 `request` / `response` 的 valid-ready 加 `EdgeDetector` 做脉冲展宽），不要新增裸的跨域电平信号。
- 不要为了仿真方便改变寄存器的复位方式；测试侧通过复位序列获得确定初值（见 `dpm-verilator-testing`）。

## 格式与检查

- Scala 代码用 `scalafmt` 格式化（配置见 `.scalafmt.conf`，`maxColumn = 120`、4 空格缩进）：`scalafmt` 就地格式化，`scalafmt --test` 只检查。提交前保证 `scalafmt --test` 通过。
- 注释遵循仓库根目录 `AGENTS.md` 的语言约定，风格为 Doxygen（`/** ... */`，参数用 `@param`）；公共模块类的文档注释要说明它在哪个时钟域、各参数含义。
- 新增模块后同步补齐：生成入口（`Emit<Name>`）、必要时 `test/<Name>.cpp`，并把新踩到的坑写回本 skill。
