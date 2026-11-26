# ASIC 实现（Yosys + OpenROAD，sky130 HD）

本目录把 Chisel 显式生成的 ASIC 变体 `DigitalPhaseMeterTopA` 从 SystemVerilog 做到
sky130 HD 的 **routed DEF / ODB**，并可选用 KLayout 合并出 GDS。它是与 FPGA（Vivado）
流程并列的一条独立实现路径，不改动 `src/` 的 RTL。

当前交付基线为 **dspClock 450MHz、clock 50MHz**，与 FPGA 侧的测量域同频。综合固定
使用 `ss_100C_1v60`（时序感知），OpenROAD 用 ss/tt/ff 三个 corner 做 MCMM 时序检查。
默认开启 450MHz 收敛所用的 PnR 调优（虚拟 CTS、GRT critical nets、`repair_setup_margin
1.0`、gate cloning、2 次布局迭代 resynth）。当前实现结果为 ss setup **+0.231ns**、
hold **+0.277ns**、面积 **20276um²**、DRC 与天线 0（详见「结果」）。

## 快速开始

`asic/flow` 是仓库根 uv workspace 的成员，入口装进同一个根 `.venv`（不单独建虚拟环境）：

```bash
# 仓库根，一次性
uv sync

uv run dpm-asic-flow                 # ss 综合 + floorplan + place + CTS + MCMM route + GDS
uv run dpm-asic-flow synth           # 只跑到 ASIC 综合网表（会先执行 ./mill emit DigitalPhaseMeterTopA）
uv run dpm-asic-flow floorplan       # 从综合网表开始，floorplan + 左右两边 IO + tapcell + PDN
uv run dpm-asic-flow place
uv run dpm-asic-flow cts
uv run dpm-asic-flow route-opt       # 全局布线 + GRT 寄生修复 + 预布线天线修复（04_route_opt.odb）
uv run dpm-asic-flow route           # 详细布线 + 填充，输出 final.def / final.odb / final.v
uv run dpm-asic-flow gds             # final.def + PDK 单元 GDS -> dpm_asic.gds

# 单独关闭某项调优（例如虚拟 CTS 与布局迭代 resynth）
uv run dpm-asic-flow all --no-virtual-cts --place-resynth-iters 0

# 换目标频率：dspClock 频率由 DPM_DSP_FREQ_MHZ 控制（单位 MHz，默认 450），
# 综合的 ABC 目标和 OpenROAD 的 SDC 都读同一个变量
DPM_DSP_FREQ_MHZ=300 uv run dpm-asic-flow all

uv run dpm-prepare-pdk                # 准备默认的全部 corner（ss/tt/ff）
uv run dpm-prepare-pdk --corner ss_100C_1v60   # 也可以只准备指定的一个或多个 corner

# 激活根 .venv 后也可以直接调用
. .venv/bin/activate
dpm-asic-flow --help

# 调试入口
uv run python -m dpm_asic_flow --help
```

根 `pyproject.toml` 通过 `[tool.uv.workspace]`、`asic` 依赖组与
`[tool.uv.sources] dpm-asic-flow = { workspace = true }` 把它装进根 `.venv`；
`asic/flow/pyproject.toml` 只注册两个 console script，运行时无第三方依赖。

`uv run dpm-asic-flow synth` 会先运行 `./mill emit DigitalPhaseMeterTopA`，再把
`build/chisel/DigitalPhaseMeterTopA/filelist.f` 改写成本流程用的
`build/asic/synth/filelist.f`；同时用 `dpm-prepare-pdk` 准备
`ss_100C_1v60` / `tt_025C_1v80` / `ff_100C_1v95` 三个 corner 的 liberty。综合只用
ss，OpenROAD 三个都读，分别建立 ss/tt/ff 三个 scene（MCMM）。

工艺库默认 `/home/luo/skywater-pdk`，可用 `--pdk` 覆盖：

```bash
uv run dpm-asic-flow --pdk /path/to/skywater-pdk
```

`--corner` 接受一个或多个 corner，默认准备流程需要的全部三个（`ss_100C_1v60` /
`tt_025C_1v80` / `ff_100C_1v95`）；`DPM_CORNER`（空格或逗号分隔）可覆盖这个默认列表。

评估/调参时用 `--utilization`（floorplan 利用率百分比，默认 60）、
`--place-density`（默认 0.60）、`--sdc`（替换 OpenROAD 用的默认 SDC）。dspClock
的目标频率由环境变量 `DPM_DSP_FREQ_MHZ` 决定（MHz，默认 450）：`synth` 用它生成
Yosys 的分模块 ABC 目标，OpenROAD 的 SDC 也读同一个变量，两边用同一套 ps 取整规则
（450MHz -> 2222ps / 2.222ns）。只跑 OpenROAD 阶段时要 export 与综合时相同的值。
`--utilization` / `--place-density` / `--sdc` 会转成 `DPM_UTILIZATION` /
`DPM_PLACE_DENSITY` / `DPM_SDC`，直接在环境里设置也仍然有效。IO 摆放的边由
`DPM_IO_SIDES` 控制（默认 `left right`；例如 `DPM_IO_SIDES="top bottom"`），
`config.tcl` 与 `check_pins.py` 都读它，两者必须一致。
PnR 调优与实验开关见下文「PnR 调优与实验开关」，它们同样转成 `DPM_*`；优先级是
显式命令行参数 > 环境变量 > 内置默认值。

依赖：Yosys（带 `read_slang` 前端与 `clockgate` pass，本项目在 0.71+128 /
git 8759daead 上验证）、OpenROAD（本项目在 26Q4-2869-g9a6ae2e63d 上验证）、
Python 3.12+（流程入口，由 uv 管理）、Mill/JDK（生成 SV）。
KLayout 的 Python 包（`klayout.db`）只用于可选的 GDS/LYP 导出。

## 测试与格式

```bash
uv run pytest asic/flow/tests -v              # 路径、PDK 派生逻辑与参数解析（不启动 Yosys/OpenROAD）
uv run black --check asic/flow asic/openroad
uv run isort --check-only asic/flow asic/openroad
```

单元测试用临时目录里的假 PDK 覆盖 `dpm-prepare-pdk` 的符号链接、`lpflow_*` 过滤、
LEF 合并与天线属性补全；真实 PDK 与工具链验证仍按上面的阶段命令手工执行。

## 目录与产物

```text
asic/
├── flow/                      # uv workspace 成员（dpm-asic-flow / dpm-prepare-pdk）
│   ├── pyproject.toml         # console script 注册（运行时无第三方依赖）
│   ├── tests/test_core.py     # 路径、PDK 派生逻辑与参数解析的纯 Python 测试
│   └── dpm_asic_flow/
│       ├── cli.py             # 按阶段编排 mill/Yosys/OpenROAD/KLayout
│       ├── prepare_pdk.py     # 各 corner liberty、合并 PDK LEF、补天线二极管属性
│       └── paths.py           # 仓库根识别、build/asic 目录与阶段列表
├── synth/
│   ├── synth.ys               # Yosys：read_slang + ss clockgate + 按模块边界分域 ABC
│   ├── abc_targets.tcl        # Yosys 内置 Tcl：读 DPM_DSP_FREQ_MHZ，算顶层/PhaseMeter 的 -D
│   ├── abc.constr             # ABC 时序约束（输入驱动 / 输出负载）
│   ├── synth_retime.ys        # 可选实验入口：ABC 跨寄存器 retime（默认不用）
│   ├── abc_targets_retime.tcl # 与 synth_retime.ys 配套的 ABC 目标
│   └── dpm_asic.sdc           # 时钟、IO delay、异步时钟组与 false path
└── openroad/
    ├── config.tcl             # 公共路径、ss/tt/ff MCMM scene、按 IO_SIDES 摆 IO 的 proc
    ├── tracks.tcl             # sky130 HD 轨道
    ├── rc.tcl                 # 层 RC 估计
    ├── pdn.tcl                # 标准单元 PDN
    ├── 01_floorplan.tcl       # 利用率 60%、左右两边 IO、tapcell、PDN
    ├── 02_place.tcl           # 全局布局、修复、详细布局、可选迭代 resynth
    ├── 03_cts.tcl             # 时钟树、CTS 后修复
    ├── 04_route_opt.tcl       # 全局布线、GRT 寄生修复、预布线天线修复，保存带 guide 的 04_route_opt.odb
    ├── 05_route.tcl           # 详细布线、布线后天线修复、填充、MCMM 报告、写 final.v（不再全局布线）
    ├── 06_gds.py              # KLayout DEF + GDS 合并（含 LEF→GDS 层映射与 .lyp 生成）
    └── check_pins.py          # 检查 IO 是否落在 IO_SIDES 指定的边
```

运行产物在 `build/asic/`（已被 `.gitignore` 忽略），ASIC SV 输入在
`build/chisel/DigitalPhaseMeterTopA/`。主要文件：

| 文件 | 内容 |
| ---- | ---- |
| `build/chisel/DigitalPhaseMeterTopA/filelist.f` | `./mill emit DigitalPhaseMeterTopA` 的 ASIC filelist（本流程的输入） |
| `build/asic/synth/filelist.f` | 去掉 `verification/`、路径相对 `filelist.f` 所在目录的综合 filelist（`read_slang -F` 读取） |
| `build/asic/synth/dpm_asic_synth.v` | 映射到 sky130 标准单元的综合网表 |
| `build/asic/synth/yosys.log` | Yosys 完整日志（clockgate 选择、单元统计） |
| `build/asic/openroad/01_floorplan.def` | floorplan 与左右两边 IO 的 DEF |
| `build/asic/openroad/03_cts.odb` | CTS 后数据库 |
| `build/asic/openroad/04_route_opt.odb` | 已完成全局布线、GRT 寄生修复与预布线天线修复的数据库（含完整 guide） |
| `build/asic/openroad/final.def` / `final.odb` | 布线后的最终数据库 |
| `build/asic/openroad/final.v` | 布线后的门级网表（保留信号名；已去掉 filler/tapcell） |
| `build/asic/openroad/05_route.log` | 最终 DRC/天线、MCMM 时序与功耗报告 |
| `build/asic/openroad/dpm_asic.gds` | 可选 GDS（需要 `klayout` 包；已按 sky130 层号映射） |
| `build/asic/openroad/dpm_asic.lyp` | 与 GDS 同时生成的 KLayout 图层配置（层名/可见性，颜色用 KLayout 默认调色板） |

## Yosys：read_slang、ss 综合、按模块边界分域 ABC 与可读网表

`asic/synth/synth.ys` 的关键命令：

```text
read_slang --std 1800-2023 --single-unit --best-effort-hierarchy -F build/asic/synth/filelist.f
keep_hierarchy PhaseMeter$*              # 只保留 PhaseMeter 的模块边界
flatten -noscopeinfo -separator _        # 其余层次压进顶层，只剩 2 个模块
synth -top DigitalPhaseMeterTop -noabc
opt_dff
clockgate -liberty build/asic/sky130_fd_sc_hd__ss_100C_1v60_synth.lib -min_net_size 2
dfflibmap -liberty build/asic/sky130_fd_sc_hd__ss_100C_1v60_synth.lib
read_liberty -lib build/asic/sky130_fd_sc_hd__ss_100C_1v60_synth.lib  # 为 abc 提供端口方向
tcl asic/synth/abc_targets.tcl           # 顶层与 PhaseMeter 各跑一次 abc
opt -fast; setundef -zero; opt_clean
setattr -mod -unset keep_hierarchy PhaseMeter$*
flatten -noscopeinfo -separator _        # 合并回单层网表
hilomap -singleton ...
rename -wire; rename -unescape           # 按被驱动信号命名单元/清理转义名
write_verilog -attr2comment -nohex -nodec -renameprefix u build/asic/synth/dpm_asic_synth.v
```

* **前端**固定用 `read_slang`，直接读 `build/asic/synth/filelist.f`。这份文件由
  `dpm-asic-flow synth` 从 `build/chisel/DigitalPhaseMeterTopA/filelist.f` 过滤得到：去掉
  `verification/` 下的断言，路径保持相对（`read_slang -F` 按 `filelist.f` 所在
  目录解析）。ASIC 产物是显式 emit 的独立目录，
  **没有平台宏、没有平台分支文件、也没有带 `A` 后缀的模块文件**；顶层就是
  `DigitalPhaseMeterTop`。
* **保留 PhaseMeter 模块边界**：默认的 `read_slang` 会把层次展平成单个模块；这里用
  `--best-effort-hierarchy` 保留层次，再用 `keep_hierarchy PhaseMeter$*` 给
  `PhaseMeter$DigitalPhaseMeterTop.controller.phaseMeter` 模块加属性。
  `flatten -noscopeinfo -separator _` 时该模块不展开，但它的子模块（DspCounter/EdgeDetector）
  照常压进 PhaseMeter，其余模块（Controller/ControlRegs/AXI）全部压进顶层，最终
  只剩顶层和 PhaseMeter 两个模块。脚本用 `select -assert-any PhaseMeter$*` 与
  `select -assert-mod-count 2 *` 断言展平结果，RTL 层次或实例名变化时会直接报错，
  而不是静默退回单模块。
* **顶层**固定为 `DigitalPhaseMeterTop`；`synth` 不加 `-flatten`，ABC 留到分模块阶段。
* **网表命名**：最终仍然展平成单模块（OpenROAD 直接消费），但两处 `flatten` 都加
  `-separator _`，把默认的 `\controlRegs.sigEnable` 变成普通标识符
  `controlRegs_sigEnable`，`\controller.phaseMeter...` 变成
  `controller_phaseMeter...`；不需要反斜杠转义，信号名也保留 RTL 的层次路径。
  展平后再执行 `rename -wire` 按被驱动的公共信号给单元命名（例如
  `axiLiteSlave_awHeld_sky130_fd_sc_hd__dfrtp_1`），`rename -unescape` 清理剩余的
  特殊字符。当前综合网表 1754 个单元里有 671 个因此得到可读名字；其余 1083 个是
  ABC/techmap 生成的私有组合节点，RTL 里本来就没有对应名字。
  `write_verilog -renameprefix u` 把私有线网和私有单元统一加 `u_` 前缀（线网
  `u_0704_`、单元 `u_1087_`），与具名信号/单元区分开；`-attr2comment` 再把
  `hdlname`/`src` 等属性写成注释，公共信号可以直接定位回
  `build/chisel/DigitalPhaseMeterTopA/*.sv` 的对应行，私有节点没有这些属性。
  也试过 ABC 的 `-dress`（按等价性转移名字）：本机 Yosys 0.71+128 / ABC 1.01 会把
  `-liberty` 映射后的 `.gate` 网络改写成 LUT/SOP 逻辑网络（Yosys 读回 `$lut`），
  最终只剩 FF 单元和 `assign` 表达式，不能作为 OpenROAD 输入，因此流程不使用它。
* **最终门级网表**：`05_route.tcl` 在写出 `final.def`/`final.odb` 后，用
  `write_verilog -remove_cells $PHYSICAL_CELLS final.v` 输出布线后的门级网表。
  filler/tapcell 只服务于制造与供电，写网表时去掉；天线二极管是真实器件，保留。
  OpenROAD 会沿用综合网表里的信号名与单元名，并为 CTS/修复插入的 buffer 与新
  拆分的线网继续分配 `u_*`/`netN`/`splitnet_*` 等名字，因此 `final.v` 和
  `dpm_asic_synth.v` 可以按原有信号名对照，新增名字则对应实现阶段新插入的单元。
* **ss 时序感知综合**：`clockgate`、`dfflibmap`、`read_liberty` 与分域 ABC 全部使用
  `sky130_fd_sc_hd__ss_100C_1v60_synth.lib`（由 `dpm-prepare-pdk` 去掉 `lpflow_*`
  后生成）。ss 是 450MHz setup 的最慢 corner，综合与实现的最紧约束保持一致。
* **自动时钟门控**：先 `opt_dff` 把 mux 反馈提取成带 CE 的 FF，再由
  `clockgate -liberty` 按 liberty 里的 `clock_gating_integrated_cell` 自动选 ICG。
  `-min_net_size 2` 跳过只有 1 个 FF 的 enable 组：`controlRegs.irqEnable` 是
  CTRL 里唯一需要存储的位，单独插 ICG 比把它折成 D mux 更费面积；现在最小
  的组是 `sigEnable`/`srcSelect` 两个 FF，共 13 个 `dlclkp_1`。
* **ASIC 计数器流水线**：`Config.asicDspCounterPipeline` 为 16，32 位的 `edgeCnt` /
  `phaseCnt` 按 `ceil(cntWidth / depth)` 拆成 **16 段、每段 2 位**。段间进位寄存，
  每段只在本段内做 2 位加法，D 路径只剩「段内加法 + 进位寄存器」。`io.value` 直接取
  各段寄存器，**没有输出对齐延迟链**：段间进位是寄存的，撤销使能后要等
  `depth - 1 = 15` 拍输出才稳定到最终计数值（连续计数、没跨段进位时逐拍就是当前值）；
  调用方必须等这么久再读，`PhaseMeterA` 的停止响应同样延后 15 拍，而实际读数又发生在
  慢时钟域的响应握手里（远晚于进位传播完）。`clear` 接在计数寄存器的异步复位端，
  clear 保持期间（idle）进位寄存器随段寄存器归零而自然清零，不需要额外排空拍数。
  段间进位是**无条件传播**的：只有 block0 的加数受 `enable` 门控，更高段直接加前一段
  的进位寄存器，这样 enable 撤销时不会丢掉最后一拍产生的进位。
  `DspCounter` 内部按 `cfg.platform` 选择实现：FPGA 用单个 DSP48E1 累加器，ASIC 用这条
  16×2 分段流水线；没有额外的“启用流水线”配置开关。`DspCounterA` / `PhaseMeterA` 两个
  Verilator 目标覆盖该 ASIC 实现（详见 `test/`）。
* **ABC 分域时序映射**：Yosys 的 `abc` 只有一个全局 `-D`；整片设计都用
  dspClock 的 2222ps 时，50MHz 的 `clock` 域也会被按 2.222ns 映射，整片都用
  20000ps 又会放松测量通路。PhaseMeter 的端口全部是寄存器边界（模块内所有 FF
  都在 `dspClock`；所有输出由 FF 驱动，输入只接 FF 的 D 和复位反相器），因此可以
  按模块分别设置 `-D`：`select DigitalPhaseMeterTop` 后用 20000ps（`clock`，
  50MHz），`select PhaseMeter$*` 后用 `DPM_DSP_FREQ_MHZ` 换算出的整数 ps
  （默认 450MHz -> 2222ps）。Yosys 原生 .ys 没有环境变量读取/替换，因此这段逻辑
  放在 `asic/synth/abc_targets.tcl`，由 `synth.ys` 用内置 `tcl` 命令调用；Tcl
  直接读环境变量，不需要 Python 生成脚本。它与 OpenROAD SDC 使用同一个周期。
  顶层里只剩
  4 个 `dspClock` 域的复位同步 FF（Controller 的 `dspReset_synchronizer`）和喂给
  PhaseMeter 的异步输入 mux，它们按 20ns 映射；这些路径在 SDC 里是 false path，
  ABC 不感知。
* **`read_liberty -lib`**：为 `abc` 提供库单元的端口方向（否则会把所有引脚当成
  inout）。黑盒模块不会写进最终网表。
* **FSM pass**：`Utils.scala` 的 firtoolOpts 带 `disallowPackedArrays`，
  `switch(state)` 因此下降成 `casez`（RTLIL 里是 `$mux`/`$pmux`，不是 `$bmux`）；
  `Controller` / `PhaseMeter` 又给 state 寄存器加了
  `addAttribute(state, "fsm_encoding = \"auto\"")`。这两点对 FPGA 与 ASIC 两份
  显式产物都生效，标准 `synth` 的 coarse 阶段就会提取并重编码两个状态机
  （当前为 one-hot），不需要自定义 coarse 序列或 `bmuxmap`。
* **综合用 liberty**：`dpm-prepare-pdk` 生成去掉 `lpflow_*` 单元的副本。lpflow 是
  低功耗隔离单元，ABC 会把它们当普通逻辑使用，但其 SLEEP 控制/输出驱动不适合普通
  信号路径，容易在布线后留下 max transition 残余违例。
* **常量**：最后用 `hilomap -singleton` 接到 `sky130_fd_sc_hd__conb_1` 的 HI/LO，
  避免 OpenROAD 里出现不可布线的 `one_`/`zero_` 常量网；布局阶段再由
  `repair_tie_fanout` 拆分扇出。

当前综合网表共 1754 个标准单元，其中 243 个 `clkbuf_1`、215 个 `dfrtp_1`、
10 个 `dfstp_1`、339 个 `dfxtp_1`、13 个 `dlclkp_1`。脚本末尾有三条自检：
`select -assert-any t:sky130_fd_sc_hd__dlclkp_1` 确认门控确实发生，
`select -assert-none t:$dff ...` 确认没有残留内部 FF 单元，
`select -assert-none t:$_*` 确认分域 ABC 没有漏掉组合单元。

## OpenROAD：floorplan、左右两边 IO 与利用率

`01_floorplan.tcl` 用：

```tcl
initialize_floorplan -utilization 60 -aspect_ratio 1.0 -core_space 2.0 -site unithd
```

注意 OpenROAD 的 `-utilization` 单位是**百分比**，所以 0.6 要写成 `60`。当前设计
配置得到：

* die：`180.485 x 180.485 um`（方芯片）
* core：`(2.300, 2.720) - (178.480, 176.800) um`，core 面积 `30669.414 um^2`
* floorplan 时的标准单元面积：`18687.923 um^2`（1754 个实例），core 有效利用率
  `60.9%`（`initialize_floorplan` 的目标是 60%，边界按 site/row 对齐后略有偏差）
* tapcell 396 个

IO 摆放不是 `place_pins` 的默认行为（默认会把 161 个 pin 压到一两条边上）。
`config.tcl` 里的 `place_io_pins` 按端口名顺序把端口平均分成 `IO_SIDES` 指定的组，
分别用 `set_io_pin_constraint -region left:*|right:*` 约束，再调用：

```tcl
place_pins -hor_layers met3 -ver_layers met2 -corner_avoidance 1.0
```

`-hor_layers` 用于左右边（水平 pin），`-ver_layers` 用于上下边（垂直 pin）。
当前 `IO_SIDES` 是 `{left right}`，161 个 pin 全部分到**左右两边（left 81 /
right 80）并落在 met3**；`-ver_layers met2` 只在把 IO 改到上下边时才会用到。
`check_pins.py` 在 floorplan 和 route 后各检查一次：`IO_SIDES` 指定的边必须有
pin，未指定的边不能有 pin，pin 也不能落在 die 内部。

需要换边时设 `DPM_IO_SIDES`（空格或逗号分隔，例如 `top bottom`），`config.tcl`
和 `check_pins.py` 读同一个环境变量，两者必须一致；`place_io_pins` 的分组数按
`llength($IO_SIDES)` 计算，不写死为四组。

布局阶段用 `global_placement -density 0.60 -timing_driven` 加虚拟 CTS，再做
`repair_design`、`repair_tie_fanout` 和详细布局；当前参数下虚拟 CTS 给 564 个
sink 设置了估计插入延迟。默认还会在初始布局后做最多 2 次 anneal resynth +
增量布局（`--place-resynth-iters 2`）；当前 450MHz 结果在第一次 resynth 时
端点 slack 已满足，工具直接 no-op。

CTS 用 `clkbuf_4/8/16`，当前布局后两棵时钟树为：`clock` 98 个 sink、
`dspClock` 142 个 sink，共 2 个 clock net；9 个 ICG 被拆成 38 份（按 sink
cluster 复制，加上未拆的 4 个共 42 个 clock gate），共插入 26 个时钟缓冲。
CTS 后 ss WNS +0.206ns、hold +0.240ns，面积 20182um²。

CTS 阶段只读 ss 一个 corner：MCMM 同时读入 ss/tt/ff 三份同名 liberty 时，
TritonCTS 的 dummy-load 候选会遍历所有 library，选到未链接到 LEF master 的
tt/ff 同名 cell 会报 `CTS-0118`。只读 ss 可以保留 dummy load 平衡（这是 03 阶段
唯一的 MCMM 差异）；04/05 阶段重新 `reload_timing` 读三个 corner，修复与最终
报告仍是 MCMM。
`04_route_opt.tcl` 对应 ORFS 的 global route 阶段：full `global_route` 后用 GRT
寄生做 `repair_design` / `repair_timing`，每次修复后用
`global_route -start_incremental` / `-end_incremental` 只重布改动过的 net、补齐
guide，再做预布线天线修复，最后写出带完整 guide 的 `04_route_opt.odb`。
`05_route.tcl` 对应 ORFS 的 detail route 阶段：从该数据库直接 `detailed_route`
（不再重新全局布线），然后做详细布线后的天线修复循环、填充和最终 MCMM 报告。
预布线天线修复必须在 04 完成：05 从数据库重载时 GRT 的运行时状态（NDR 关闭、
拥塞图）不会恢复，直接在 05 里 `repair_antennas` 会因残余拥塞报 `GRT-0232`。
05 在写完 `final.def`/`final.odb` 后用 `write_verilog -remove_cells $PHYSICAL_CELLS`
输出 `final.v`：网表沿用综合阶段的信号名，去掉只占版图位置的 filler/tapcell，
是布局布线后真实逻辑连接的可读视图。

## 查看 GDS 与图层含义

最终版图在 `build/asic/openroad/dpm_asic.gds`，顶层单元是 `DigitalPhaseMeterTop`，
1nm 数据库单位，die 为 `180.485um` 见方。用 KLayout 打开并加载与 GDS 同时生成的
图层配置：

```bash
klayout -l build/asic/openroad/dpm_asic.lyp build/asic/openroad/dpm_asic.gds
```

已经打开了 KLayout 时，用 `File -> Load Layer Properties` 选择
`build/asic/openroad/dpm_asic.lyp` 即可；右侧图层面板可以单独显示/隐藏每一层。配置文件里
默认显示 nwell、diff、tap、poly、li1、met1~met5 以及各层的接触孔/通孔，隐藏
implant、pin/label 和 cell 边界等辅助层。`.lyp` 只写层名和可见性，不指定颜色，
KLayout 会按默认调色板给每层分配颜色。

GDS 里的层号写作 `layer:datatype`。同一个 layer 号下不同 datatype 表示不同用途：
drawing（真实几何）用各自的工艺 datatype（本设计里以 20 为主），`5/16/59` 是
label/pin，`44` 多为 cut 或 implant，具体见下表（名称与含义取自 sky130 PDK 的
`docs/rules/gds_layers.csv`）。

| GDS | 名称 | 含义 |
| --- | --- | --- |
| `64:20` | nwell | N 阱 |
| `65:20` | diff | 有源区/扩散区 |
| `65:44` | tap | 与阱同型的扩散接触（N+/P+ tap） |
| `66:20` | poly | 多晶硅栅 |
| `66:44` | licon1 | 到 local interconnect 的接触孔 |
| `67:20` | li1 | 第一层局部互连 |
| `67:44` | mcon | li1 到 met1 的接触孔 |
| `68:20` | met1 | 金属 1 |
| `68:44` | via | met1 到 met2 的通孔 |
| `69:20` | met2 | 金属 2 |
| `69:44` | via2 | met2 到 met3 的通孔 |
| `70:20` | met3 | 金属 3 |
| `70:44` | via3 | met3 到 met4 的通孔 |
| `71:20` | met4 | 金属 4 |
| `71:44` | via4 | met4 到 met5 的通孔 |
| `72:20` | met5 | 金属 5（本设计的 PDN 顶层） |
| `93:44` | nsdm | N+ 源漏注入 |
| `94:20` | psdm | P+ 源漏注入 |
| `78:44` | hvtp | 高阈值 PMOS 注入 |
| `95:20` | npc | 氮化硅多晶硅切割（licon1 下方） |
| `66:15` | poly.short | 多晶硅短接标记（单元内部） |
| `81:4` | areaid.sc | 标准单元标识 |
| `81:23` | areaid.de | 二极管标识 |
| `64:5` / `67:5` / `68:5` | nwell/li1/met1 label | 版图文字标签 |
| `64:16` / `122:16` / `67:16` / `68:16` | nwell/pwell/li1/met1 pin | 端口标记（文本+数据） |
| `83:44` | text | 通用文字 |
| `236:0` | cell boundary | 单元 GDS 自带的边界/辅助层，不是工艺流程层 |

说明：

* `pwell` 在 sky130 里通常由衬底隐含，GDS 里没有独立的 pwell drawing 层；本设计只
  出现 pwell 的 label/pin（`64:59` / `122:16`）。
* `dpm_asic.lyp` 由 `06_gds.py` 用 `xml.etree.ElementTree` 生成，不是 PDK 自带文件：
  层号/名称来自 sky130 的 `gds_layers.csv`，文件里只写层名与可见性，颜色由 KLayout
  默认调色板分配；因此它和 sky130 HD 工艺绑定，换工艺或改层映射时要同步改
  `06_gds.py` 里的 `SKY130_LEF_TO_GDS` / `DISPLAY_LAYERS`。它只影响查看，不改变 GDS 数据。
* `06_gds.py` 里显式把 LEF 的 `li1/mcon/met1/via/.../met5` 映射到上表的 sky130 层号；
  如果不映射，KLayout 的 LEF/DEF 读取器会按 LEF 层顺序把 DEF 走线放到 `1:0`、`5:0`
  等回退层，和单元 GDS 的 `68:20` 等层分离，看起来就像“只有单元、没有连线”。
* 这个 GDS 是**版图视图**：用于查看布局布线结果，不替代 DRC/LVS 签核（见“已知限制”）。

## 约束与时钟

`asic/synth/dpm_asic.sdc`：

* `clock`：50MHz（20ns），对应 PS 的 `FCLK_CLK0`，固定不变；分模块 ABC 里顶层
  用 20000ps；
* `dspClock`：测量通路的 ASIC 频率，由环境变量 `DPM_DSP_FREQ_MHZ` 决定
  （默认 450MHz -> 2.222ns）。`dpm-asic-flow` 与 SDC 用同一套规则把频率取整到
  整数 ps（450MHz -> 2222ps），综合的 ABC `-D` 与 SDC 周期因此一致；
* setup uncertainty 0.3ns、hold uncertainty 0.1ns，时钟 transition 0.1ns；
* 两个时钟按异步时钟组处理（`set_clock_groups -asynchronous`）：控制器内部用
  `CdcToggleHandShake` 做跨域；
* `extSig_*` / `intSig_*` / `srcLocked` / `resetn` 是不与采样时钟固定相位的异步输入，
  设为 false path，测量核内部有自己的同步链；
* AXI4-Lite 数据端口按 `clock` 约束 2ns 输入/输出延迟，输出负载 0.05pF。

### MCMM 与三个 corner

`asic/openroad/config.tcl` 同时读入 `ss_100C_1v60` / `tt_025C_1v80` /
`ff_100C_1v95` 三套 liberty，用同一个 SDC mode 定义 `ss` / `tt` / `ff` 三个 scene，
主优化场景为 `ss`。`dpm-asic-flow synth` 在综合前会用
`dpm-prepare-pdk`（默认准备全部三个 corner）生成这些库；只跑 OpenROAD
阶段时，`config.tcl` 会检查库是否存在并给出明确错误。`05_route.tcl` 的最终报告
逐场景输出 setup/hold WNS/TNS 与功耗。`04_route_opt.tcl` 与 `05_route.tcl` 在
`read_db` 后都重新执行 `set_propagated_clock [all_clocks]`，route 阶段报告使用
propagated clock（含时钟树延迟）。

## 结果（本机实测）

当前默认流程（450MHz + PnR 调优）完整跑完综合、floorplan、place、CTS、route 与
GDS，关键结果如下（ss corner 为主场景；OpenROAD 26Q4-2869，16 线程）。

综合与实现规模：

* 综合网表：1754 个标准单元，其中 13 个 `dlclkp_1` 自动时钟门控；无 lpflow 单元。
  分模块 ABC 的顶层目标 20000ps、PhaseMeter 目标 2222ps。
* floorplan：die `180.485um` 见方，161 个 IO 左右分布 `left 81 / right 80`（全部
  在 met3），tapcell 396 个，标准单元面积 `18687.923um^2`，有效利用率 `60.9%`。
* CTS：`clock` 域 98 个 sink、`dspClock` 域 142 个 sink，2 个 clock net，9 个 ICG
  拆成 38 份（共 42 个 clock gate）；CTS 后 ss WNS +0.206ns / hold +0.240ns，
  面积 20182um^2。
* 布线：详细布线 DRC 0 条，最终天线违规 0；route 后面积 **20276um^2**（66%
  利用率）。

MCMM 时序（route 阶段，`set_propagated_clock` 已生效；单位 ns）：

| scene | setup WNS | hold WNS | setup TNS | hold TNS |
| --- | --- | --- | --- | --- |
| ss | **+0.231** | **+0.277** | 0 | 0 |
| tt | +1.092 | +0.106 | 0 | 0 |
| ff | +1.409 | +0.063 | 0 | 0 |

主场景 ss 的最差 setup 是 dspClock 域的 `controller_phaseMeter__layerCapture`
自环路径（slack +0.231ns）；`clearCnt` 到分段计数器异步复位脚的 recovery
检查为 +0.764ns。hold/removal 最差也在 ss（`dspReset` 同步链到
`phaseMeter_state_3` 的 RESET_B，+0.277ns），tt/ff 的 hold 也为正。

功耗（默认活动率，MCMM 逐场景）：

| scene | 总功耗 |
| --- | --- |
| ss | 4.81mW |
| tt | 6.12mW |
| ff | 7.66mW |

电气规则：最终 `report_check_types` 无 max slew / max cap 违例；详细布线与天线
本身都是 0 违例；`check_antennas` 最终为 0。

具体数字以 `build/asic/openroad/05_route.log` 的最终报告为准。网表命名步骤
（`flatten -separator`、`rename -wire` / `rename -unescape`）只改变最终 Verilog
的名字，不改变综合后的单元类型与数量。同一配置的多次独立运行会因多线程布局有
0.2ns 量级的波动，签核时要在这些数字之外留 margin。

日志里还有几类可解释的告警：`IFP-0028`（core 左下角按 site/row 对齐）、`GPL-0302`
（目标密度低于最小可行密度，placer 会自动调整）、`RSZ-0104`（AXI 高位地址与
`awprot`/`arprot` 悬空——RTL 只按 `addr[5:2]` 译码）、`STA-2402`（没有激励时功耗
活动率传播不收敛）、`RSZ-0062`（route 阶段仍有个别 setup 修复未完成，最终 slack 已为正）。

## PnR 调优与实验开关

默认值就是当前 450MHz 收敛所用的组合；需要回到未调优配置时，用对应的 `--no-*` 或
零值逐项关闭（例如 `--no-virtual-cts --place-resynth-iters 0`）。优先级是显式命令行
参数 > `DPM_*` 环境变量 > 内置默认值。开关项统一成对出现（argparse 的
`BooleanOptionalAction`），例如 `--virtual-cts` / `--no-virtual-cts`。

| CLI | 环境变量 | 作用 | 默认 |
| --- | --- | --- | --- |
| `--timing-driven-place` / `--no-timing-driven-place` | `DPM_TIMING_DRIVEN_PLACE` | `global_placement -timing_driven` | 关（虚拟 CTS / 迭代 resynth 会自动打开） |
| `--timing-driven-repair` / `--no-timing-driven-repair` | `DPM_TIMING_DRIVEN_REPAIR` | 布局时保留时序修复（隐含 `--timing-driven-place`） | 关 |
| `--virtual-cts` / `--no-virtual-cts` | `DPM_VIRTUAL_CTS` | 布局时构建轻量虚拟时钟树，用估计 clock latency 参与时序驱动布局 | 开 |
| `--virtual-cts-max-skew-fraction` | `DPM_VIRTUAL_CTS_MAX_SKEW` | 虚拟时钟树最大插入延迟占周期比例 | 0.05 |
| `--grt-critical-nets` | `DPM_GRT_CRITICAL_NETS` | `global_route -critical_nets_percentage` | 20 |
| `--repair-setup-margin` | `DPM_REPAIR_SETUP_MARGIN` | `repair_timing -setup_margin`（ns） | 1.0 |
| `--gate-cloning` / `--no-gate-cloning` | `DPM_GATE_CLONING` | 允许 `repair_timing` 做 gate cloning | 开 |
| `--place-resynth-iters` | `DPM_PLACE_RESYNTH_ITERS` | 布局阶段重复 resynth + 增量布局的次数 | 2 |
| `--place-resynth-mode` | `DPM_PLACE_RESYNTH_MODE` | 迭代 resynth 模式（plain/anneal/genetic） | anneal |
| `--resynth-iters` | `DPM_RESYNTH_ITERS` | 每次 resynth 搜索的 ABC 脚本迭代数 | 200 |
| `--resynth-initial-ops` | `DPM_RESYNTH_INITIAL_OPS` | 初始 ABC 脚本的操作数 | 3 |
| `--resynth-revert-after` | `DPM_RESYNTH_REVERT_AFTER` | 退火连续变差多少次后回退 | 50 |
| `--resynth-seed` | `DPM_RESYNTH_SEED` | 搜索随机种子 | 1 |
| `--resynth-temp` | `DPM_RESYNTH_TEMP` | 退火初始温度；不传则用最差端点的 required arrival time | — |
| `--resynth-population-size` | `DPM_RESYNTH_POPULATION_SIZE` | 遗传算法种群大小 | 8 |
| `--useful-skew` / `--no-useful-skew` | `DPM_USEFUL_SKEW` | `clock_tree_synthesis -useful_skew` | 关 |
| `--retime` / `--no-retime` | `DPM_RETIME` | 使用 `synth_retime.ys` 做 ABC 跨寄存器 retime | 关 |
| `--threads` | `DPM_THREADS` | OpenROAD 线程数 | 0（自动） |

单独跑某个 Tcl 阶段时也可以直接 `export DPM_*`；例如：

```bash
DPM_VIRTUAL_CTS=1 DPM_REPAIR_SETUP_MARGIN=0.5 \
    uv run dpm-asic-flow all
```

两个可选实验入口不参与交付基线：

* `--retime` 选择 `asic/synth/synth_retime.ys`：异步复位/置位 FF 与 `DspCounter`
  流水线在 ABC 前预映射成库单元，CE 交给 `abc -dff`。`DspCounter` 的流水线寄存器
  带 `abc_retime = "false"` 属性，避免 ABC 把分段流水线拆平。当前默认综合仍用
  `synth.ys`；450MHz 在 16×2 流水线 + 异步清零后已收敛，不依赖 retime。
* `--useful-skew` 在新 OpenROAD 上可用，但本设计全是标准单元、没有 macro sink，
  CTS 建的是扁平树，realization 没有可处理的 macro 层级，实际是 no-op；默认关闭。

## 已知限制

* 综合固定用 ss、OpenROAD 已覆盖 ss/tt/ff 三个 corner 的 MCMM 时序检查；没有更多
  工艺角、电压/温度点或模式组合。
* 没有 DRC/LVS 签核：本机没有 magic/netgen，`06_gds.py` 只负责把 DEF 与 PDK 单元
  GDS 合并成版图视图，不代替 DRC/LVS。
* 没有 IR drop / EM / 功耗签核；PDN 只做基本供电网格。
* 最终时序用 `estimate_parasitics -global_routing` 估计互连寄生，没有接入签核级
  RCX/SPEF；换一种寄生估计方式 WNS 会有 0.1ns 量级的差别（互连占比 <0.5%），
  正式签核应使用校准后的 RCX/SPEF。
* route 阶段已用 propagated clock 做 MCMM 报告；当前默认流程 ss setup 余量
  +0.231ns、ff hold +0.063ns，上板/签核前仍需按实际时钟抖动与 OCV 复核。
* 计数器没有输出对齐延迟链，段间进位要传播 `depth - 1` 拍（当前 16 段 → 15 拍）：
  撤销使能后这段时间内读到的完整计数值可能偏低；当前 `PhaseMeter` 的停止响应延后同样
  拍数，实际读数又发生在慢时钟域的响应握手里，不会落在进位传播窗口内。如果后续加入更短
  的测量或直接观测 `DspCounter`，需要自己等够 `depth - 1` 拍。
* 综合是扁平网表，不保留模块层次；展平时用 `_` 把层次路径编进信号名
  （`controller_phaseMeter_...`），私有组合节点是 `u_NNNN_`。OpenROAD 的
  `final.v` 同样是扁平网表，只是去掉了 filler/tapcell。ASIC 流程不参与 Vivado
  的 IP 打包。

## 与 FPGA 流程的关系

平台由生成主类显式选择，不注入任何宏：

* `./mill emit DigitalPhaseMeterTop` 生成 FPGA 实现到 `build/chisel/DigitalPhaseMeterTop/`；
* `./mill emit DigitalPhaseMeterTopA` 生成 ASIC 实现到
  `build/chisel/DigitalPhaseMeterTopA/`。

Vivado/Verilator/xsim 消费前者，本目录消费后者；两份产物各自是独立的顶层模块，
没有平台分支文件，也没有模块名后缀。`disallowPackedArrays` 与 state 上的
`fsm_encoding` 是全局 firtool 选项 / RTL 属性，对两份产物都生效；两条流程各自按
自己读到的那份 RTL 综合，不需要定义平台宏。
