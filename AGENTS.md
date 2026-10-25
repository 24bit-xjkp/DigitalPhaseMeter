# AGENTS.md

本文件面向在本仓库（`DigitalPhaseMeter`）中工作的 AI 代理与开发者，仅定义项目最基本信息与规则。涉及具体领域时请**先加载** `.agents/skills/` 下对应 skill 再动手：工具用法以 skill 优先，网络搜索与读源码只是备选（见下文"skill 优先"）。

## 项目概览

基于 Chisel 的数字相位计：测量两路同频信号的相位差。测量通路在高速时钟域（DSP 域，当前 450MHz，由 PL 内 MMCM 从 50MHz 倍频得到），控制、闸门计数与 AXI4-Lite 接口在低速时钟域（50MHz）。PS↔PL 的数据交换走 AXI4-Lite 寄存器（GPIO 式，一次测量只有 4 个字，不用 BRAM），并用测量完成中断通知 PS。测量输入有两条通路：外部引脚（`extSig_sig1`/`extSig_sig2`，默认约束到 W18/W19）与片内信号源（裸 MMCM 时基，频率与相位差由 PS 写 MMCM 寄存器镜像设定），由 PL 内的寄存器在两者之间切换。

**工程已完成并通过实测验收（2026-10-19）**：PL 侧测量与控制 RTL、PS↔PL 互联、Vivado IP 与 block design、PS 侧裸机软件、PC 侧上位机均已实现，Verilator / xsim / 综合实现 / 上板验收均已跑通。当前工作以维护（缺陷修复、补充测试、文档更新）为主。

**固定器件**：`xc7z010clg400-1`（ALINX AX7010 的芯片）。`vivado/` 下的脚本写死该 part，创建/打开工程时回读校验，不提供切换器件的能力；综合实现签核目标是「全流程日志 0 Warnings / 0 Critical Warnings」+ 实现 methodology 报告 0 条未 waive 的 Critical（细节见 `vivado/README.md` 与 `dpm-vivado-ip`）。

## 目录结构与任务优先级

```text
├── src/main/scala/DigitalPhaseMeter/   # Chisel RTL：每个模块一个 .scala，附 Emit<Name> 生成主类
├── test/                               # Verilator C++ 测试：每个 <Name>.cpp 自动注册一个测试目标（构建时自动生成对应 Chisel 产物）
├── sim/                                # xsim 的 SystemVerilog 激励与运行脚本（只做真实 MMCM 联调）
├── vivado/                             # IP 打包、block design、综合实现脚本与顶层约束
├── ps/                                 # PS 侧裸机 C 服务端（串口协议、AXI/DRP 驱动、startup/linker）
├── host/                               # PC 侧 Python 上位机（协议、CLI、PySide6 GUI、仿真后端）
├── docs/                               # 串口协议（ps-protocol.md）与上板指南（bringup.md）
├── spec/                               # 金标准向量、独立仿真服务端、规格自检与验证脚本
├── script/                             # xmake 包定义与规则（Lua；含 Verilator 目标构建前的自动生成规则）
├── build.mill                          # Mill 构建定义（Scala 编译、生成入口、静态检查；Mill 版本在文件头固定，JDK 用系统 JDK）
├── mill                                # Mill 启动脚本（仓库根执行 `./mill`）
├── out/                                # Mill 的构建缓存与任务产物（构建产物，不入库）
├── build/chisel/<Name>/                # 由 Chisel 生成的 SystemVerilog 与 filelist.f（构建产物，不入库）
├── build/vivado/                       # 打包出的 IP、BD 工程与综合实现产物（构建产物，不入库）
├── .agents/skills/                     # 项目级 skill（见下文"skill 优先"）
└── xmake.lua                           # 顶层构建脚本
```

**任务优先级**：以 RTL 功能实现、缺陷修复、补充测试为主；未明确指定时，优先修改 `src/`、`test/` 与 `sim/`，必要时更新 `vivado/`、`ps/`、`host/` 中对应实现、`.agents/skills/` 中的 skill 及相关文档。

## 环境与构建入口

按职责选择入口，三者不可互相替代：

| 入口                     | 职责                                                                                          |
| ------------------------ | --------------------------------------------------------------------------------------------- |
| `./mill`                 | 编译 Scala、运行 Chisel 生成 SystemVerilog 到 `build/chisel/<Name>/`，并执行 Scalafix 检查（也是 Verilator 测试目标构建时的自动生成入口） |
| `xmake`                  | 构建 Verilator 测试目标、运行测试（测量通路与顶层 IP）；构建目标时自动调用 `./mill emit <模块名>` 生成/更新 SV |
| Verilator                | 由 xmake 的目标规则调用，编译生成的 SystemVerilog                                             |
| `sim/run_handoff_tb.sh`  | 用 Vivado xsim 跑依赖 MMCM 原语的联调激励（真实 `MMCME2_ADV` 行为模型）                       |
| `uv run dpm-vivado-flow` | 增量打包 IP、创建/刷新 block design、综合实现与导出 XSA（`vivado/flow/`；Tcl 脚本是其执行层） |
| `bash ps/build.sh`       | 交叉编译 PS 侧裸机程序，并构建宿主机验证台                                                    |
| `uv run pytest`          | PC 侧 Python 测试（`host/tests`，pytest 原生写法）与 GUI 冒烟测试入口                         |

- 本项目使用 clang 工具链。gcc 在编译带 C++20 模块的测试代码时会产生编译器内部错误（ICE）——该结论沿用 `verilator_utils` 系列项目的记录：不要用 gcc 配置、构建或做"可移植性验证"。
- 各入口的具体命令、选项与已知陷阱写在对应 skill 中（见下表），本文件不重复。

## skill 优先：工具用法以 skill 为准

`.agents/skills/` 是"本项目的工具与领域怎么用"的权威出处。需要知道某个命令、选项、约定或工作流时，按顺序取用三类来源：**skill 优先，网络搜索与读源码是备选**。

- **首选 skill**：加载本领域对应的 skill 并按其说明执行。本仓库领域与对应 skill：

| 领域                                                                 | 先加载                   |
| -------------------------------------------------------------------- | ------------------------ |
| 编写/修改 Chisel RTL、生成 SystemVerilog（仿真与综合共用同一份产物） | `dpm-chisel-rtl`         |
| 编写、运行、调试 Verilator C++ 测试                                  | `dpm-verilator-testing`  |
| 测量语义、理论值、容限与已知精度下界（写验证判据时）                 | `dpm-measurement-model`  |
| 打包 Vivado IP、改 PS↔PL 互联的 BD、写/跑 xsim 的 SV 激励、综合实现  | `dpm-vivado-ip`          |
| 改上位机/PS 协议、CLI/GUI、无硬件端到端验证                          | `dpm-host-protocol`      |
| 提交前的格式化、静态检查与全量回归                                   | `dpm-pre-delivery-check` |

- **备选一：网络搜索**。skill 未覆盖，或 skill 与项目/环境的实际行为不一致时，先查官方文档与 issue；就地可用的官方帮助（如 `xmake <command> --help`）属于同一类的快速形式。
- **备选二：读源码**。网络搜索仍不明确时，再阅读实现代码：生成结果查 `build/chisel/`。
- 不要跳过 skill 直接进入备选方法。通过备选方法得到的结论，在报告中注明来源（文档链接，或源码文件与行号），并考虑把结论补进 skill。
- skill 与实际行为不一致时，**以实际行为为准**，并在报告中说明差异。

## 运行约定：走工具入口，不直接执行产物

- 运行测试使用构建系统提供的入口（本项目为 `xmake test` / `xmake run`），**不要**直接执行 `build/` 下的可执行文件：入口会先构建目标，并应用该目标或用例声明的运行参数与环境（例如 `xmake test` 会带上用例级 `runargs`，本项目为 `+verilator+rand+reset+2`），运行目录就是目标产物目录（波形与覆盖率文件也生成在那里）。绕过入口跑出来的不是项目约定的那次运行，结果不能作为验证证据。
- Verilator 测试目标在构建时会自动调用代码生成入口，所以改完 RTL 直接跑测试即可，不需要手工先跑 `./mill emit`；该自动生成只覆盖被构建的 Verilator 目标，xsim 与 Vivado 侧仍按各自入口生成（详见 `dpm-verilator-testing` 与 `dpm-chisel-rtl`）。
- 需要详细输出、传测试框架参数或挂调试器时，同样用入口表达（如 `xmake test -v`、`xmake run <target> <参数>`）。
- 只有入口无法表达需求时（例如精确复现一次裸执行）才直接调用产物，并在报告中写明原因与命令。

## 修改边界

- 不提交 `build/` 下的生成物、波形与覆盖率文件（`.gitignore` 已覆盖，注意不要用 `git add -f` 绕过）。哪些文件属于构建产物、交付前怎么确认，见 `dpm-pre-delivery-check`。
- `MCU` 是仓库根目录下的空占位文件，与当前构建无关，不要依赖它。
- 构建脚本、测试注册、代码风格与命名等具体约定随所属领域写在对应 skill 中（RTL 见 `dpm-chisel-rtl`，测试见 `dpm-verilator-testing`，交付流程见 `dpm-pre-delivery-check`），本文件不重复。

## 文档与 skill 写作：不写死单一工具，按场景类别陈述规则

编写或修改本文件与 `.agents/skills/` 下的 skill 时：

- 规则按**场景类别**陈述（构建系统入口、代码生成、测试框架、静态检查……），当前使用的具体工具与命令只作为该类别的**示例 / 当前实现**，不要把某一条命令写成规则本身。
- 目标是"换一个同类工具后规则仍然成立"：写"通过构建系统的运行入口运行目标（本项目当前为 `xmake run` / `xmake test`）"，而不是"运行目标必须用 `xmake run`"。
- 只有当规则确实只对该工具成立时（例如某条命令的实测陷阱），才把工具名写进规则，并说明这是该工具的专属约定。
- 文档与 skill 面向**当前已完成的设计**：只写当前有效的实现、用法与约束；迭代过程、历史轮次与已完成/作废的方案不留在仓库文档里。

## 基本约定

- 所有新增的文档与代码注释使用中文。
- 报告验证结果时注明**实际运行的目标与配置**，不要仅凭单一配置断言广泛的移植性或完整回归覆盖；区分"已验证"与"未验证"。
