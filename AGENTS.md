# AGENTS.md

本文件面向在本仓库（`DigitalPhaseMeter`）中工作的 AI 代理与开发者，仅定义项目最基本信息与规则。涉及具体领域时请**先加载** `.agents/skills/` 下对应 skill 再动手：工具用法以 skill 优先，网络搜索与读源码只是备选（见下文"skill 优先"）。

## 项目概览

基于 Chisel 的数字相位计：测量两路同频信号的相位差。测量通路在高速时钟域（DSP 域，当前 400MHz，由 PL 内 MMCM 从 50MHz 倍频得到），控制、闸门计数与 AXI4-Lite 接口在低速时钟域（50MHz）。PS↔PL 的数据交换走 AXI4-Lite 寄存器（GPIO 式，一次测量只有 4 个字，不用 BRAM），并用测量完成中断通知 PS。PL 侧测量与控制 RTL、PS↔PL 互联、Vivado IP 与 block design 已完成；其余阶段的规划见 `README.md` 的路线图。

## 目录结构与任务优先级

```text
├── src/main/scala/DigitalPhaseMeter/   # Chisel RTL：每个模块一个 .scala，附 Emit<Name> 生成主类
├── test/                               # Verilator C++ 测试：每个 <Name>.cpp 自动注册一个测试目标
├── sim/                                # xsim 的 SystemVerilog 测试激励与运行脚本（AXI4 接口/顶层 IP）
├── vivado/                             # IP 打包、block design、综合实现脚本与顶层约束
├── script/                             # xmake 包定义（Lua）
├── build/chisel/<Name>/                # 由 Chisel 生成的 SystemVerilog 与 filelist.f（构建产物，不入库）
├── build/vivado/                       # 打包出的 IP、BD 工程与综合实现产物（构建产物，不入库）
├── .agents/skills/                     # 项目级 skill（见下文"skill 优先"）
└── xmake.lua                           # 顶层构建脚本
```

**任务优先级**：以 RTL 功能实现、缺陷修复、补充测试为主；未明确指定时，优先修改 `src/`、`test/` 与 `sim/`，必要时更新 `vivado/` 脚本与 `.agents/skills/` 中对应的 skill。

## 环境与构建入口

按职责选择入口，三者不可互相替代：

| 入口 | 职责 |
| --- | --- |
| `sbt` | 运行 Chisel，生成 SystemVerilog 到 `build/chisel/<Name>/` |
| `xmake` | 构建 Verilator 测试目标、运行测试（测量通路） |
| Verilator | 由 xmake 的目标规则调用，编译生成的 SystemVerilog |
| `sim/run_xsim.sh` | 用 Vivado xsim 跑 `sim/` 下的 SystemVerilog 激励（AXI4 接口与顶层 IP） |
| `vivado/` 下的 tcl | 打包 IP、创建 block design、综合实现与导出 XSA |

- 本项目使用 clang 工具链。gcc 在编译带 C++20 模块的测试代码时会产生编译器内部错误（ICE）——该结论沿用 `verilator_utils` 系列项目的记录，未在本仓库单独复现：不要用 gcc 配置、构建或做"可移植性验证"。
- 各入口的具体命令、选项与已知陷阱写在对应 skill 中（见下表），本文件不重复。

## skill 优先：工具用法以 skill 为准

`.agents/skills/` 是"本项目的工具与领域怎么用"的权威出处。需要知道某个命令、选项、约定或工作流时，按顺序取用三类来源：**skill 优先，网络搜索与读源码是备选**。

- **首选 skill**：加载本领域对应的 skill 并按其说明执行。本仓库领域与对应 skill：

| 领域 | 先加载 |
| --- | --- |
| 编写/修改 Chisel RTL、生成 SystemVerilog（`sim` / `syn` 两种产物） | `dpm-chisel-rtl` |
| 编写、运行、调试 Verilator C++ 测试 | `dpm-verilator-testing` |
| 测量语义、理论值、容限与已知精度下界（写验证判据时） | `dpm-measurement-model` |
| 打包 Vivado IP、改 PS↔PL 互联的 BD、写/跑 xsim 的 SV 激励、综合实现 | `dpm-vivado-ip` |
| 提交前的格式化、静态检查与全量回归 | `dpm-pre-delivery-check` |

- **备选一：网络搜索**。skill 未覆盖，或 skill 与项目/环境的实际行为不一致时，先查官方文档与 issue；就地可用的官方帮助（如 `xmake <command> --help`）属于同一类的快速形式。
- **备选二：读源码**。网络搜索仍不明确时，再阅读实现代码：生成结果查 `build/chisel/`。
- 不要跳过 skill 直接进入备选方法。通过备选方法得到的结论，在报告中注明来源（文档链接，或源码文件与行号），并考虑把结论补进 skill。
- skill 与实际行为不一致时，**以实际行为为准**，并在报告中说明差异。

## 运行约定：走工具入口，不直接执行产物

- 运行测试使用构建系统提供的入口（本项目为 `xmake test` / `xmake run`），**不要**直接执行 `build/` 下的可执行文件：入口会先构建目标，并应用该目标或用例声明的运行参数与环境（例如 `xmake test` 会带上用例级 `runargs`，本项目为 `+verilator+rand+reset+2`），运行目录就是目标产物目录（波形与覆盖率文件也生成在那里）。绕过入口跑出来的不是项目约定的那次运行，结果不能作为验证证据。
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

## 基本约定

- 所有新增的文档与代码注释使用中文。
- 报告验证结果时注明**实际运行的目标与配置**，不要仅凭单一配置断言广泛的移植性或完整回归覆盖；区分"已验证"与"未验证"。
