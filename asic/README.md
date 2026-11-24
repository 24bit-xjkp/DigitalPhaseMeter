# ASIC 实现（Yosys + OpenROAD，sky130 HD）

本目录把 Chisel 显式生成的 ASIC 变体 `DigitalPhaseMeterTopA` 从 SystemVerilog 做到
sky130 HD 的 **routed DEF / ODB**，并可选用 KLayout 合并出 GDS。它是与 FPGA（Vivado）
流程并列的一条独立实现路径，不改动 `src/` 的 RTL。当前交付基线为 **dspClock 300MHz、
clock 50MHz**，综合固定使用 `ss_100C_1v60`（时序感知），OpenROAD 用 ss/tt/ff 三个
corner 做 MCMM 时序检查。

## 快速开始

`asic/flow` 是仓库根 uv workspace 的成员，入口装进同一个根 `.venv`（不单独建虚拟环境）：

```bash
# 仓库根，一次性
uv sync

uv run dpm-asic-flow                 # ss 综合 + floorplan + place + CTS + MCMM route + GDS
uv run dpm-asic-flow synth           # 只跑到 ASIC 综合网表（会先执行 ./mill emit DigitalPhaseMeterTopA）
uv run dpm-asic-flow floorplan       # 从综合网表开始，floorplan + 四边 IO + tapcell + PDN
uv run dpm-asic-flow place
uv run dpm-asic-flow cts
uv run dpm-asic-flow route-opt       # 只做布线前电气规则修复（04_route_opt.odb）
uv run dpm-asic-flow route           # 布线前修复 + 布线，输出 final.def / final.odb
uv run dpm-asic-flow gds             # final.def + PDK 单元 GDS -> dpm_asic.gds

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
`--place-density`（默认 0.60）、`--sdc`（替换 OpenROAD 用的默认 SDC）。ABC 的 `-D`
按域写在 `synth.ys` 的 `--clock` 参数里，`--sdc` 只影响 OpenROAD；改频率必须同时改
SDC、`synth.ys` 的 `--clock` 与 `partition.py` 的调用参数。这些参数会转成
`DPM_UTILIZATION` / `DPM_PLACE_DENSITY` / `DPM_SDC`，直接在环境里设置也仍然有效。

依赖：Yosys（带 `read_slang` 前端与 `clockgate` pass，本项目在 0.71 上验证）、OpenROAD
（本项目在 26Q4 上验证）、Python 3.12+（流程入口，由 uv 管理）、Mill/JDK（生成 SV）。
KLayout 的 Python 包（`klayout.db`）只用于可选的 GDS/LYP 导出。

## 测试与格式

```bash
uv run pytest asic/flow/tests -v              # 路径、PDK 派生逻辑与参数解析（不启动 Yosys/OpenROAD）
uv run black --check asic/flow
uv run isort --check-only asic/flow
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
│   ├── synth.ys               # Yosys：read_slang + ss clockgate + 分域 ABC 时序映射
│   ├── partition.py           # 按 clock 域拆分组合逻辑，生成分域 abc 脚本
│   ├── abc.constr             # ABC 时序约束（输入驱动 / 输出负载）
│   └── dpm_asic.sdc           # 时钟、IO delay、异步时钟组与 false path
└── openroad/
    ├── config.tcl             # 公共路径、ss/tt/ff MCMM scene、四边 IO 摆放 proc
    ├── tracks.tcl             # sky130 HD 轨道
    ├── rc.tcl                 # 层 RC 估计
    ├── pdn.tcl                # 标准单元 PDN
    ├── 01_floorplan.tcl       # 利用率 60%、四边 IO、tapcell、PDN
    ├── 02_place.tcl           # 全局布局、修复、详细布局
    ├── 03_cts.tcl             # 时钟树、CTS 后修复
    ├── 04_route_opt.tcl       # GRT 寄生下的电气规则修复，保存 04_route_opt.odb
    ├── 05_route.tcl           # 全新全局布线、天线修复、详细布线、填充、MCMM 报告
    ├── 06_gds.py              # KLayout DEF + GDS 合并（含 LEF→GDS 层映射与 .lyp 生成）
    └── check_pins.py          # 检查 IO 是否真的分布在四边
```

运行产物在 `build/asic/`（已被 `.gitignore` 忽略），ASIC SV 输入在
`build/chisel/DigitalPhaseMeterTopA/`。主要文件：

| 文件 | 内容 |
| ---- | ---- |
| `build/chisel/DigitalPhaseMeterTopA/filelist.f` | `./mill emit DigitalPhaseMeterTopA` 的 ASIC filelist（本流程的输入） |
| `build/asic/synth/filelist.f` | 去掉 `verification/` 并改成绝对路径后的综合 filelist |
| `build/asic/synth/dpm_asic_synth.v` | 映射到 sky130 标准单元的综合网表 |
| `build/asic/synth/yosys.log` | Yosys 完整日志（clockgate 选择、单元统计） |
| `build/asic/openroad/01_floorplan.def` | floorplan 与四边 IO 的 DEF |
| `build/asic/openroad/03_cts.odb` | CTS 后数据库 |
| `build/asic/openroad/04_route_opt.odb` | 做过 GRT 寄生电气修复的布线前数据库 |
| `build/asic/openroad/final.def` / `final.odb` | 布线后的最终数据库 |
| `build/asic/openroad/dpm_asic.gds` | 可选 GDS（需要 `klayout` 包；已按 sky130 层号映射） |
| `build/asic/openroad/dpm_asic.lyp` | 与 GDS 同时生成的 KLayout 图层配置（层名/可见性，颜色用 KLayout 默认调色板） |

## Yosys：read_slang、ss 综合与自动时钟门控

`asic/synth/synth.ys` 的关键命令：

```text
read_slang --std 1800-2023 --single-unit -f build/asic/synth/filelist.f
synth -top DigitalPhaseMeterTop -flatten -noabc
opt_dff
clockgate -liberty build/asic/sky130_fd_sc_hd__ss_100C_1v60_synth.lib -min_net_size 1
dfflibmap -liberty build/asic/sky130_fd_sc_hd__ss_100C_1v60_synth.lib
write_json build/asic/synth/pre_abc.json
read_liberty -lib build/asic/sky130_fd_sc_hd__ss_100C_1v60_synth.lib  # 只为 submod 提供端口方向
script build/asic/synth/partition.ys   # 由 partition.py 生成，按域跑 abc
```

* **前端**固定用 `read_slang`，直接读 `build/asic/synth/filelist.f`。这份文件由
  `dpm-asic-flow synth` 从 `build/chisel/DigitalPhaseMeterTopA/filelist.f` 过滤得到：去掉
  `verification/` 下的断言，路径改成绝对路径。ASIC 产物是显式 emit 的独立目录，
  **没有平台宏、没有平台分支文件、也没有带 `A` 后缀的模块文件**；顶层就是
  `DigitalPhaseMeterTop`。
* **顶层**固定为 `DigitalPhaseMeterTop`；`synth -flatten` 得到扁平网表，便于
  OpenROAD 处理。
* **ss 时序感知综合**：`clockgate`、`dfflibmap`、`read_liberty` 与分域 ABC 全部使用
  `sky130_fd_sc_hd__ss_100C_1v60_synth.lib`（由 `dpm-prepare-pdk` 去掉 `lpflow_*`
  后生成）。ss 是 300MHz setup 的最慢 corner，综合与实现的最紧约束保持一致。
* **自动时钟门控**：先 `opt_dff` 把 mux 反馈提取成带 CE 的 FF，再由
  `clockgate -liberty` 按 liberty 里的 `clock_gating_integrated_cell` 自动选 ICG。
  当前选中的是 `sky130_fd_sc_hd__dlclkp_1`（latch_posedge），综合后得到 13 个 ICG。
* **ASIC 计数器流水线**：`Config` 中 ASIC 流水线级数为 8，32 位的 `edgeCnt` /
  `phaseCnt` 按 `ceil(cntWidth / depth)` 拆成 **8 段、每段 4 位**。段间进位寄存，
  每段只在本段内做 4 位加法；输出时把低段逐级延迟到与最高段对齐，因此读数整体比
  实际计数晚 `depth - 1 = 7` 拍。输出对齐延迟链**不随 `clear` 复位**：`clear` 后
  各段旧值要 7 拍才完全排空，排空期间输出可能是旧值与已清零高段的混合，不能读取；
  ASIC 变体 `PhaseMeterA` 的停止响应同样按该深度延后 7 拍，且 `clear` 只在 idle
  期间有效，保证读数时链已排空。`DspCounter` 内部按 `cfg.platform` 选择实现：FPGA
  用单个 DSP48E1 累加器，ASIC 用这条 8×4 分段流水线；没有额外的“启用流水线”配置
  开关。`DspCounterA` / `PhaseMeterA` 两个 Verilator 目标覆盖该 ASIC 实现（详见
  `test/`）。
* **ABC 分域时序映射**：Yosys 的 `abc` 只有一个全局 `-D`；整片设计都用
  dspClock 的 3333ps 时，50MHz 的 `clock` 域也会被按 3.333ns 映射。`partition.py`
  在 `dfflibmap` 之后读取 `write_json` 网表，沿时钟网络给每个触发器/ICG 确定
  时钟域，再按“组合逻辑的扇出能到达哪个域的寄存器”分组：同时到达多个域时取
  周期最短的域，只驱动顶层输出的组合逻辑取最慢的域。随后用 `submod -hidden`
  把每组移进子模块，分别执行 `abc -D 20000`（`clock`，50MHz）与
  `abc -D 3333`（`dspClock`，300MHz），最后 `flatten` 回单层网表。跨域连接
  本来就是触发器到触发器（`CdcToggleHandShake`），拆分不会切断组合逻辑路径。
  修改 SDC 或 `synth.ys` 的 `--clock` 周期时，两处要同步。
* **`read_liberty -lib`**：只为 `submod` 提供库单元的端口方向（否则会把所有
  引脚当成 inout）。黑盒模块不会写进最终网表。
* **FSM pass**：`Utils.scala` 的 firtoolOpts 带 `disallowPackedArrays`，
  `switch(state)` 因此下降成 `casez`（RTLIL 里是 `$mux`/`$pmux`，不是 `$bmux`）；
  `Controller` / `PhaseMeter` 又给 state 寄存器加了
  `addAttribute(state, "fsm_encoding = \"auto\"")`。这两点对 FPGA 与 ASIC 两份
  显式产物都生效，标准 `synth` 的 coarse 阶段就会提取并重编码两个状态机
  （当前为 one-hot），不需要自定义 coarse 序列或 `bmuxmap`。Yosys 会对
  “用户指定的 fsm_encoding 可能让电路变大”给出提示，但当前 300MHz 下
  one-hot 重编码后的 WNS 与面积都满足目标（见“结果”）。
* **综合用 liberty**：`dpm-prepare-pdk` 生成去掉 `lpflow_*` 单元的副本。lpflow 是
  低功耗隔离单元，ABC 会把它们当普通逻辑使用，但其 SLEEP 控制/输出驱动不适合普通
  信号路径，容易在布线后留下 max transition 残余违例。
* **常量**：最后用 `hilomap -singleton` 接到 `sky130_fd_sc_hd__conb_1` 的 HI/LO，
  避免 OpenROAD 里出现不可布线的 `one_`/`zero_` 常量网；布局阶段再由
  `repair_tie_fanout` 拆分扇出。

脚本末尾有三条自检：`select -assert-any t:sky130_fd_sc_hd__dlclkp_1` 确认门控确实
发生，`select -assert-none t:$dff ...` 确认没有残留内部 FF 单元，
`select -assert-none t:$_*` 确认分域 ABC 没有漏掉组合单元。

## OpenROAD：floorplan、四边 IO 与利用率

`01_floorplan.tcl` 用：

```tcl
initialize_floorplan -utilization 60 -aspect_ratio 1.0 -core_space 2.0 -site unithd
```

注意 OpenROAD 的 `-utilization` 单位是**百分比**，所以 0.6 要写成 `60`。本设计
配置得到：

* die：`199.145 x 199.145 um`（方芯片）
* core：`(2.300, 2.720) - (196.880, 195.840) um`，core 面积 `37577.290 um^2`
* floorplan 时的标准单元面积：`22849.414 um^2`（2004 个实例），core 有效利用率
  `60.8%`（`initialize_floorplan` 的目标是 60%，边界按 site/row 对齐后略有偏差）

IO 摆放不是 `place_pins` 的默认行为（默认会把 161 个 pin 压到一两条边上）。
`config.tcl` 里的 `place_io_pins` 按端口名顺序把端口平均分成四组，分别用
`set_io_pin_constraint -region top:*|right:*|bottom:*|left:*` 约束，再调用：

```tcl
place_pins -hor_layers met3 -ver_layers met2 -corner_avoidance 1.0
```

结果是 161 个 pin 分布为 **top 41 / right 41 / bottom 41 / left 38**。
`check_pins.py` 在 floorplan 和 route 后各检查一次，只要有一条边没有 pin 或 pin
落在 die 内部就失败。

floorplan 之后插入 tapcell（511 个）、建立 met1/met4/met5 的 PDN；布局阶段用
`global_placement -density 0.60`、`repair_design`、`repair_tie_fanout` 和详细布局；
CTS 用 `clkbuf_4/8/16`，两棵时钟树（`clock` 176 个 sink、`dspClock` 342 个 sink），
TritonCTS 把 6 个门控单元拆成 22 份，并把 26/29 个门控单元的输出缓冲重整为
`clkbuf_16`；最终版图里有 29 个 ICG（3 个 `dlclkp_1` + 26 个 `dlclkp_4`）。
布线前先在 CTS 数据库上做一次全局布线并用 GRT 寄生修 max slew/cap，结果保存为
`04_route_opt.odb`（`04_route_opt.tcl`）；`05_route.tcl` 在**新的 OpenROAD 进程**
里从该数据库重新全局布线，避免同一进程里增量布线漏掉新插入单元的 guide，然后做
天线修复循环、详细布线和填充。

## 查看 GDS 与图层含义

最终版图在 `build/asic/openroad/dpm_asic.gds`，顶层单元是 `DigitalPhaseMeterTop`，
85 个 GDS cell 定义、1nm 数据库单位，实际几何范围约 `198.680 x 198.560 um`（die 为
`199.145um` 见方）。用 KLayout 打开并加载与 GDS 同时生成的图层配置：

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

* `clock`：50MHz（20ns），对应 PS 的 `FCLK_CLK0`；`synth.ys` 里对应
  `--clock clock=20000`；
* `dspClock`：300MHz（3.333ns），测量通路的 ASIC 频率；`synth.ys` 里对应
  `--clock dspClock=3333`，`partition.py` 用同一个数值。
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

2026-11-24 用 `uv run dpm-asic-flow`（`all`）在本机（Yosys 0.71 + OpenROAD 26Q4 + sky130 HD）
跑通，单次实现的关键结果：

* 综合：2004 个标准单元（Yosys 统计的 2006 个 cell 中另有 2 个 `$scopeinfo`），
  其中 152 个 `dfrtp_1`、11 个 `dfstp_1`、601 个 `dfxtp_1`、13 个
  `sky130_fd_sc_hd__dlclkp_1`（自动时钟门控）；无 lpflow 单元。
* floorplan：die `199.145um` 见方，标准单元 2004 个、面积 `22849.414um^2`，core
  有效利用率 `60.8%`，161 个 IO 四边分布 `41/41/41/38`，tapcell 511 个，PDN 正常生成。
* 布线：详细布线 DRC 报告为空（0 条），天线检查最终为 0 条；最终 `final.def` 共
  6008 个 component（含 2004 个标准单元、511 个 tapcell、填充单元与天线二极管），
  电源连接完整。
* 电气规则：最终 `report_check_types -max_slew -max_capacitance -max_fanout -violators`
  没有列出违例（`-violators` 无输出），`report_floating_nets` 也没有列出浮空网。
* MCMM 时序（route 阶段，`set_propagated_clock` 已生效；单位 ns）：

  | scene | setup WNS | hold WNS | setup TNS | hold TNS |
  | --- | --- | --- | --- | --- |
  | ss | **+0.011** | **+0.301** | 0 | 0 |
  | tt | +1.501 | +0.098 | 0 | 0 |
  | ff | +2.053 | +0.027 | 0 | 0 |

  主场景 ss 的 setup 关键路径在 dspClock 域，余量只有 **+0.011ns**，属于 300MHz
  临界；ff 的 hold 余量为 +0.027ns。两次独立 `all` 运行的最终 DEF 与上述 MCMM/面积/
  功耗数字一致。
* 面积：`report_design_area` 为 **25537um^2**（68% 利用率，已含 CTS 缓冲、tapcell
  与二极管等）。
* 功耗（默认活动率，MCMM 逐场景）：ss 约 **6.52mW**、tt 约 **8.33mW**、
  ff 约 **10.3mW**。

具体数字以 `build/asic/openroad/05_route.log` 的最终报告为准；不同工具版本/线程数
可能带来小幅差异。

日志里还有几类可解释的告警：`IFP-0028`（core 左下角按 site/row 对齐）、`GPL-0302`
（目标密度低于最小可行密度，placer 会自动调整）、`RSZ-0104`（AXI 高位地址与
`awprot`/`arprot` 悬空——RTL 只按 `addr[5:2]` 译码）、`STA-2402`（没有激励时功耗
活动率传播不收敛）。

## 已知限制

* 综合固定用 ss、OpenROAD 已覆盖 ss/tt/ff 三个 corner 的 MCMM 时序检查；没有更多
  工艺角、电压/温度点或模式组合。
* 没有 DRC/LVS 签核：本机没有 magic/netgen，`06_gds.py` 只负责把 DEF 与 PDK 单元
  GDS 合并成版图视图，不代替 DRC/LVS。
* 没有 IR drop / EM / 功耗签核；PDN 只做基本供电网格。
* route 阶段已用 propagated clock 做 MCMM 报告；ss setup 余量只有 +0.011ns，
  ff hold 余量 +0.027ns，上板/签核前仍需按实际时钟抖动与 OCV 复核。
* 延迟链不复位意味着 `clear` 后 7 拍内计数器输出不是有效读数；当前 `PhaseMeter`
  的空闲/停止响应时序不会在这个窗口读数，但如果后续加入短于 7 拍的测量或直接观测
  `DspCounter`，需要额外处理。
* 综合是扁平网表，不保留模块层次；ASIC 流程不参与 Vivado 的 IP 打包。

## 与 FPGA 流程的关系

平台由生成主类显式选择，不注入任何宏：

* `./mill emit DigitalPhaseMeterTop` 生成 FPGA 实现到 `build/chisel/DigitalPhaseMeterTop/`；
* `./mill emit DigitalPhaseMeterTopA` 生成 ASIC 实现到
  `build/chisel/DigitalPhaseMeterTopA/`。

Vivado/Verilator/xsim 消费前者，本目录消费后者；两份产物各自是独立的顶层模块，
没有平台分支文件，也没有模块名后缀。`disallowPackedArrays` 与 state 上的
`fsm_encoding` 是全局 firtool 选项 / RTL 属性，对两份产物都生效；两条流程各自按
自己读到的那份 RTL 综合，不需要定义平台宏。
