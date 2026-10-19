---
name: dpm-pre-delivery-check
description: 'Use when: 在 DigitalPhaseMeter 中完成一处修改、准备提交或向用户报告结果之前，需要跑格式化、静态检查与回归测试。覆盖 C++（clang-format / clang-tidy）、Scala（scalafmt）与全量测试的检查命令、编译数据库的重新生成、交付范围的确认，以及验证结果的报告要求。'
argument-hint: '本次要交付的改动'
---

# 交付前检查

## 适用场景

- 改完 RTL 或测试，准备提交、交付或向用户汇报。
- 需要判断"跑哪些检查才算数"。

各步骤按顺序执行，任何一步不通过都不要进入下一步。

本仓库的 `.clang-format` / `.clang-tidy` / `.clangd` 与 `verilator_utils` 仓库中的同名配置完全一致，C++ 层面的检查约定可以对照 `~/verilator_utils/.agents/skills/verilator-utils-pre-delivery-check/SKILL.md`（该目录不被自动加载，需按路径直接读取）。

## 1. 格式化

| 语言 | 命令 |
| --- | --- |
| C++ | `clang-format -i <改动的 .cpp>`（配置 `.clang-format`，`ColumnLimit = 130`、4 空格缩进） |
| Scala | `scalafmt`（就地格式化）；`scalafmt --test` 只检查、不改文件 |

`scalafmt --test` 会检查仓库内的全部 Scala 源文件（含 `build.sbtn`），是提交前的最低要求。

## 2. 静态检查

C++ 代码用 clang-tidy，配置在 `.clang-tidy`（`clang-analyzer-*`、`modernize-*`、`google-*`、`readability-*`、`cert-*`、`concurrency-*`、`bugprone-*`、`cppcoreguidelines-*` 等，含项目自定的若干关闭项）：

```bash
clang-tidy -p .vscode --config-file=.clang-tidy test/Controller.cpp
```

- `-p .vscode` 指向编译数据库 `.vscode/compile_commands.json`；它由 xmake 生成，需要重新生成时：

  ```bash
  xmake project -k compile_commands --lsp=clangd .vscode
  ```

- **标准是 0 warning**。新增告警要么改代码，要么在确实属于工具误报时按下一条处理。

### 误报的处理约定

确认是误报时用行内 `NOLINT(<check>)` 精确抑制，并写清原因，不要整段关闭检查。当前的既有例子：`REQUIRE` 失败会终止用例，其后对 `std::optional` 的访问是安全的，但 clang-tidy 仍会报 `bugprone-unchecked-optional-access`：

```cpp
REQUIRE(after_reset.has_value());
// REQUIRE失败会终止当前用例，因此此处访问是安全的
const auto& result{after_reset.value()};  // NOLINT(bugprone-unchecked-optional-access)
```

当前代码里已按下述方式处理过这类告警，改动时不要重新引入：先判断后执行的循环用普通 `while` 而不是 `do-while`（`cppcoreguidelines-avoid-do-while`）；浮点转整用 `::std::lround`（`bugprone-incorrect-roundings`）。

## 3. 回归测试

按改动范围选择回归入口，改了 AXI4 接口或顶层就要两条都跑：

```bash
xmake test -v          # Verilator：测量通路与复位同步（Controller / EdgeDetector / ResetSynchronizer / MmcmDrpController）
sim/run_xsim.sh        # xsim：AXI4 接口与顶层 IP（SystemVerilog 激励，详见 dpm-vivado-ip）
```

- 改了 `Axi*.scala`、`ControlRegs.scala`、`DigitalPhaseMeterTop.scala` 时，
  **必须**跑 `sim/run_xsim.sh`（脚本默认会先跑生成入口）：这些通路只有 xsim 覆盖得到。
- 只改了测量通路（`Controller` / `PhaseMeter` / `EdgeDetector` / `DspCounter`）时，
  重新生成产物后跑 `xmake test -v` 即可；但顶层的端口或行为变了也要顺带跑 xsim。
- 改了 `vivado/` 下的脚本、约束或 `vivado/rtl/dpm_mmcm_src.v` 时，至少重新执行对应脚本并确认
  没有 ERROR / CRITICAL WARNING（`validate_bd_design` 的告警不会让脚本退出码非零，必须看日志）；
  改了 BD 源文件还要重跑 `create_bd.tcl`（`build_bitstream.tcl` 的复用判断不看 RTL 内容）。
  交付目标是「全流程日志 0 Warnings / 0 Critical Warnings / 0 Errors」+ 实现 methodology 报告
  `Checks found: 0`（waiver 条目见 `build_bitstream.tcl` 的 `METHODOLOGY_*` 输出）。

- **每个模块只有一份产物**：`build/chisel/<Name>/`（见 `dpm-chisel-rtl`）。Verilator、xsim 与
  Vivado 消费的是同一份 RTL（复位同步是纯 RTL 的 `ResetSynchronizer`，没有 XPM 原语），因此各
  流水线可以按任意顺序交替跑。**新增厂商原语前先确认 Verilator 与 xsim 两侧都有它的仿真模型**，
  否则「一份产物」的前提就不成立。注意 `xmake test` **不会**替你生成产物：改了 RTL 仍要先跑生成入口。

- 必须是**全量**：只跑单个目标或用例（`xmake run <Name> ...`）不能作为回归证据，它只用于定位问题。
- 运行参数是用例级配置（`test/xmake.lua` 中 `add_tests` 的 `runargs`，本项目为 `+verilator+rand+reset+2` 与 `-fc`），只有 `xmake test` 会应用：`xmake run` 走的是 target 级 `runargs`，本项目没有设置。因此"寄存器初值随机"这类只在带 plusargs 时出现的行为，在 `xmake run` 下可能被掩盖——两个入口结果不一致时以 `xmake test` 为准。
- 改动过 RTL 时，先重新生成 SystemVerilog 再跑测试（见 `dpm-chisel-rtl`）；只改了测试代码则不需要重新生成。
- `xmake test -v` 的通过判据是末尾的 `100% tests passed`；xsim 的通过判据是日志里出现
  `==== 测试通过`（xsim 的退出码不足以判定，`$fatal` 也可能返回 0）。
- 报告里写明实际跑的目标、用例数与断言数（doctest 结尾会打印 `test cases / assertions / Status`）。

### Python（`host/` 上位机、`spec/` 校验脚本）

环境由 **uv** 管理（`uv.lock` + `.venv`），不要手工 `pip install`；测试是 **pytest** 原生写法：

```bash
uv run pytest -q                       # 全量（testpaths = host/tests，仓库根或 host/ 下都能跑）
uv run black --check host/ spec/ ps/   # 全部 Python 脚本（行宽 140 来自 pyproject 的 [tool.black]）
uv run isort --check-only host/ spec/ ps/  # 导入顺序（profile/行宽来自 pyproject 的 [tool.isort]）
uv run python spec/selftest_spec.py    # 文档 ↔ 头文件 ↔ 金标准的一致性自检
uv run python host/tools/gui_smoke.py  # 改了 GUI 时跑：连接/测量/扫描 + 截图（QT_QPA_PLATFORM=offscreen 可无头）
```

- 格式化的范围是**整个 Python 脚本树**（`host/`、`spec/`、`ps/tests`、`ps/tools`），不是只格式化改动
  的文件：`pyproject.toml` 里已配好 `[tool.black] line-length = 140` 与 `[tool.isort] profile = "black"`，
  两条命令不必带额外参数。这棵树应始终保持干净；提交前尽量把纯格式化改动与功能改动分开。
- **C 源码不做整体格式化**：`ps/**/*.c|h` 的风格与仓库根 `.clang-format` 并不完全一致，整体重排会
  淹没功能改动；只要求新增/改动处与周围风格一致，且交叉编译在 `-Wall -Wextra -Werror` 下零警告。

改了 C 服务端（`ps/`）还要重建验证台并跑端到端：

```bash
bash ps/build.sh && cmake --build ps/build/cmake -j8
ctest --test-dir ps/build/cmake --output-on-failure
bash ps/tools/onboard_accept_sim.sh                # 离线验收演练（C 验证台，80 点扫描）
```

改了固件命令处理的时间/超时口径时，另外确认：`DPM_WAIT_TIMEOUT_MS` 是**毫秒**界
（`50ms + 4 × 闸门时间（秒）`，协议约定见 `docs/ps-protocol.md` §3.0），`DPM_WAIT_POLL_LIMIT`
是**慢时钟周期**界，两者不要混用；`ps/tests/test_proto.c::test_wait_budget` 与
`ps/tests/test_server_selftest.py::test_long_gate_bounded_wait` 钉住这两条。

## 4. 交付范围

```bash
git diff --check      # 行尾空白等
git status --short    # 确认没有把不该提交的东西带进来
```

- 逐项确认 `git status --short` 里的文件都是本次改动**有意**产生的；哪些目录属于构建产物、不要提交，见仓库根目录 `AGENTS.md` 的"修改边界"。
- 特别注意仓库根目录有没有散落生成的 `.sv` / `filelist.f`：用 sbtn 生成 SystemVerilog 时若参数形式写错，产物会落到仓库根而不是 `build/chisel/<Name>/`（见 `dpm-chisel-rtl`）。

## 5. 报告

按仓库根目录 `AGENTS.md` 的"基本约定"报告（注明实际运行的目标与配置、区分已验证与未验证），另外：

- 报告里给出本次实际跑的目标、用例数与断言数（doctest 结尾会打印 `test cases / assertions / Status`）。
- 发现 skill 与项目/环境实际行为不一致时，在报告里指出，并把结论补回对应 skill。
