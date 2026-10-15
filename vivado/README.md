# Vivado：IP 打包、PS↔PL 互联与实现

本目录把 PL 侧的 `DigitalPhaseMeterTop` 打包成 Vivado IP，并生成 PS7 + AXI 协议转换器 + IP 的
block design，用于综合、实现与后续的 PS 侧软件开发。

IP 对外只有**一个 AXI 从口**：`s_axi_lite`（AXI4-Lite，4KB 寄存器区）。一次测量的结果只有 4 个字，
结果直接锁存在 `ControlRegs` 的只读结果寄存器里（`RESULT_GATE` / `RESULT_EDGE` / `RESULT_PHASE` /
`RESULT_SEQ`），PS 收到中断后读 4 个寄存器即可——没有 BRAM、没有第二个 AXI 从口，也没有突发传输。

同一个寄存器区里还有**片内信号源使能与输入选择**的寄存器（`SIG_CTRL` / `SIG_STATUS`，
偏移 0x2C–0x30）：PL 内用选择位在**外部引脚输入**与**片内信号源**之间切换，`SIG_CTRL.enable` 控制
片内源的使能。
片内信号源本身由 BD 里的 **裸 MMCM（`vivado/rtl/dpm_mmcm_src.v`，`MMCME2_ADV` 薄封装）** 产生：
两路输出同频、相位差可调，**频率与相位差由 PS 通过 AXI4-Lite 直接把 MMCM 的 16 位寄存器值
写进 `dpm_src_drp`（极简 DRP 控制器）**，控制器再按固定顺序搬进 MMCM 的 DRP 口
（独立地址段 `0x43C1_0000`），两路输出**直接**作为被测信号送进 IP 的异步采样链。

> **为什么不再用 clk_wiz 的 `USE_DYN_RECONFIG + PHASE_DUTY_CONFIG`**：
> 那套配置会把「毫度/百分比/MHz → MMCM 计数器位域」的定点换算综合成硬件，
> 实测占 13,287 个物理 LUT site 与 38 个 DSP48E1（整设计 96% 的逻辑 LUT）。
> AMD PG065 明确写了「Enabling this option utilizes DSP resources. By default, this option
> is deselected to optimize the design for area.」。把这套纯整数换算搬到 PS 软件后，
> 整设计 Slice LUTs 从 14,048（79.8%）降到 **948（5.4%）**、DSP 从 40 降到 **2**。
> 编码规则见 `build/analysis/golden/mmcm_encode.py`（已对 clk_wiz 的 Write DRP
> 生成值逐点校验）。
寄存器定义见仓库根目录 [`README.md`](../README.md#寄存器映射s_axi_lite基地址-0x43c0_0000)。

## 前提

1. 先用 sbtn 生成综合产物 SystemVerilog（XPM 原语版本）：

   ```bash
   sbtn -batch "runMain DigitalPhaseMeter.EmitDigitalPhaseMeterTop --type=syn"
   ```

2. Vivado 在 PATH 中（本项目在 **Vivado 2025.2** 上验证）。脚本默认器件为 `xc7z010clg400-1`，
   可自行修改 `create_bd.tcl` 顶部的 `part`。

## 脚本

| 脚本                       | 作用                                                                                                                                                | 产物                                                                               |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `package_ip.tcl`           | 读入生成的 RTL，打包成 IP（VLNV `xjkp.org:dpm:dpm_top:1.0`），推断唯一的 AXI4-Lite 从口、补时钟/复位/中断接口与地址块                               | `build/vivado/ip/dpm_top/`（component.xml + src/ + xgui/）                         |
| `package_src_ip.tcl`       | 把 `MmcmDrpController` 打包成 IP（VLNV `xjkp.org:dpm:dpm_src_drp:1.0`），推断 AXI4-Lite 从口、补时钟/复位接口与 4KB 地址块                          | `build/vivado/ip/dpm_src_drp/`                                                     |
| `create_bd.tcl`            | 创建 PS7 + AXI 互联 + 两个 IP + 裸 MMCM 的 block design，连接时钟/复位/中断/信号，分配地址，配置 PS 串口（`-uart`，默认 uart1），验证并生成 wrapper | `build/vivado/bd_proj/`                                                            |
| `build_bitstream.tcl`      | 先写入 DSP 域高速时钟需要的综合/实现策略（见下文「400MHz 的策略」），再综合、实现、导出 bitstream 与 XSA；`-strategy-only` 只写策略不跑 run         | `build/vivado/dpm_system.xsa`、`build/vivado/{timing_summary,utilization,drc}.rpt` |
| `impl_multi.tcl`           | 同一网表上做多次独立布局布线（place/route 有 run-to-run 差异），取余量最好的一次并汇总 WNS/WHS                                                      | `best_timing.rpt`、`multi_a<N>_timing.rpt`、`impl_multi_results.csv`               |
| `query_runs.tcl`           | 只读查询：列出 `synth_1` / `impl_1` 的策略与全部 `STEPS.*.*` 参数（不改动工程）                                                                     | 终端输出（`RUN_QUERY_BEGIN` / `RUN_QUERY_END` 之间）                               |
| `constraints/dpm_top.xdc`  | 顶层约束：被测信号 false path、片内源两路 MMCM 输出的异步约束、两个时钟域之间握手通路的有界 max_delay、板级引脚                                     | 无（源文件）                                                                       |
| `hook_write_bitstream.tcl` | `write_bitstream` 的 pre-hook：把引脚未约束的两条 DRC 降级为警告（不绑定开发板时用）                                                                | 无（源文件）                                                                       |
| `hook_filter_messages.tcl` | 综合（`SYNTH_DESIGN`）与实现（`INIT_DESIGN`）的 pre-hook：用 `set_msg_config` 屏蔽厂商 IP 固有告警，见下文「告警清理」                              | 无（源文件）                                                                       |

> 早先的三个「补丁 / 单独一步」脚本已经并入流水线，不再单独存在：
> `apply_timing_strategy.tcl` 的语句并入 `build_bitstream.tcl`（启动 run 前写策略，`-strategy-only`
> 等价于「只改策略不重建」）；`patch_enable_ps_uart.tcl` 的语句并入 `create_bd.tcl`（`-uart` / `-baud`）；
> `patch_enable_src_phase.tcl` 针对的是已经被方案 A 取代的 `clk_wiz_src`，没有可迁移的语句，
> 直接删除（当前片内源的相位由 PS 写 MMCM 相位寄存器，见下文「Block design 结构」）。

用法（依次执行）：

```bash
vivado -mode batch -source vivado/package_ip.tcl
vivado -mode batch -source vivado/package_src_ip.tcl    # 片内信号源的 DRP 控制器
vivado -mode batch -source vivado/create_bd.tcl         # DSP 域默认 400MHz、PS 串口默认 uart1；可传参数改
vivado -mode batch -source vivado/build_bitstream.tcl   # 写策略 → 综合 → 实现 → bitstream/XSA
vivado -mode batch -source vivado/query_runs.tcl        # 随时可跑，查看当前运行选项
```

> `create_bd.tcl` 的可选参数：第一个位置参数是 DSP 域目标频率（MHz，默认 **400**），第二个位置参数
> 给出时会**改用 PS 的 `FCLK_CLK1` 直供** DSP 域（不建 MMCM，且不再用第一个参数），用于对比两条时钟路径。
> `FCLK_CLK1` 上限 250MHz，因此第二条路只能覆盖 250MHz 及以下。
> 串口用 `-uart uart0|uart1|none`（默认 `uart1`）与 `-baud <波特率>`（默认 115200）给出。
> 例：`create_bd.tcl` = 400MHz + uart1；`create_bd.tcl 300` = 300MHz；`create_bd.tcl 0 150` = PS 直供 150MHz；
> `create_bd.tcl -uart uart0` = 400MHz + MIO14/15 串口；`create_bd.tcl -uart none` = 不给 PS 配串口。

> `build_bitstream.tcl` 的可选参数：`-retiming 0|1`（默认 1）、`-dsp <个数>`（默认 2，给 0 表示不设）、
> `-strategy-only`（只写策略并打印当前取值，不启动 run）。策略与上一轮不同时会打印
> `STRATEGY_FORCE_RERUN` 并强制重跑已完成的 run，避免静默复用旧参数跑出来的网表。

> **改了 RTL 之后**：`package_ip.tcl`（顶层 IP）→ `package_src_ip.tcl`（DRP 控制器）→ `create_bd.tcl` →
> `build_bitstream.tcl` 要按顺序重跑（IP 内容变了，BD 里的 IP 也要重新生成）。

## 查看综合 / 实现选项

三种等价途径，取值来源都是 run 的 `STEPS.<步骤>.ARGS.<参数>` 属性：

- **脚本 / Tcl**：`vivado -mode batch -source vivado/query_runs.tcl`；GUI 里则在 Tcl Console
  执行 `report_property [get_runs synth_1]`（或 `get_property STEPS.PLACE_DESIGN.ARGS.DIRECTIVE [get_runs impl_1]`）。
- **每个 run 实际执行的命令**：`build/vivado/bd_proj/dpm_bd.runs/<run>/dpm_system_wrapper.tcl`
  与同目录 `runme.log` 里的 `Command:` 行（例如 `Command: synth_design -top dpm_system_wrapper -part xc7z020clg400-1`、
  `Command: place_design`）——脚本为空的步骤表示用的是该策略的默认参数。
- **GUI**：Project Manager → Project Settings… → Synthesis / Implementation（策略与常用选项），
  Window → Design Runs 里选 `synth_1` / `impl_1` 看策略与状态。

本工程的 run 参数除下表这些显式设置外保持默认：`synth_1` 用内置策略 `Vivado Synthesis Defaults`，
但步骤参数被改成 `DIRECTIVE = PerformanceOptimized`、`GLOBAL_RETIMING = on`、`MAX_DSP = 2`；
`impl_1` 用内置策略 **`Performance_Retiming`**（步骤 directive 不覆盖）；另有
`WRITE_BITSTREAM.TCL.PRE` = [`hook_write_bitstream.tcl`](hook_write_bitstream.tcl)
（把 `NSTD-1`/`UCIO-1` 两条 DRC 降级为警告）、`launch_runs -jobs 8`、
以及 [`constraints/dpm_top.xdc`](constraints/dpm_top.xdc) 顶层约束。
改这些参数的入口就是 [`build_bitstream.tcl`](build_bitstream.tcl) 里那段策略代码（`-retiming` / `-dsp` /
`-strategy-only`）；改了选项（或改了 RTL/IP）之后必须 `reset_run synth_1` / `reset_run impl_1`
再 `launch_runs` 才会生效——`build_bitstream.tcl` 对未完成的 run 会这样处理，发现策略真的变化时
（`STRATEGY_CHANGED > 0`）也会强制重跑已完成的 run。

## 400MHz 的策略（build_bitstream.tcl 内联）

`create_bd.tcl` 只负责把 DSP 域时钟建成 400MHz；要让综合/实现真的收敛，还需要写入运行参数
——由 [`build_bitstream.tcl`](build_bitstream.tcl) 在启动 run 前完成（run 的 `STEPS.*.ARGS.*`
可直接改，内置 strategy 的参数也能改，见该脚本注释；`-strategy-only` 可以只写策略不跑 run）：

| 侧   | 参数                                  | 值                         | 为什么                                                                                                                                         |
| ---- | ------------------------------------- | -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| 综合 | `SYNTH_DESIGN.ARGS.DIRECTIVE`         | `PerformanceOptimized`     | 以时序为目标做跨层次优化，比默认多花编译时间换余量                                                                                             |
| 综合 | `SYNTH_DESIGN.ARGS.GLOBAL_RETIMING`   | `on`                       | 允许跨寄存器重定时，把只影响使能/复位的组合逻辑吸收掉                                                                                          |
| 综合 | `SYNTH_DESIGN.ARGS.FLATTEN_HIERARCHY` | `full`                     | 让优化器跨 Chisel 展开出的模块层次动逻辑                                                                                                       |
| 综合 | `SYNTH_DESIGN.ARGS.MAX_DSP`           | `2`                        | 把 DSP 用量钉在已知规模（本设计只需 2 个），避免综合器为别处时序搬动计数器                                                                     |
| 实现 | `strategy`                            | **`Performance_Retiming`** | 它的 phys_opt 用 `AlternateFlowWithRetiming`（更激进的复制 + **DSP/BRAM 优化** + 开启重定时），正对本设计「关键路径终点是 DSP48 控制脚」的症状 |

**实现策略是逐档实测选出来的**（同一份综合网表，只换 `impl_1` 的 strategy，路径为 `Performance_*`）：

| 实现 strategy                                                 | WNS                         | WHS       | 备注                                                                 |
| ------------------------------------------------------------- | --------------------------- | --------- | -------------------------------------------------------------------- |
| `Performance_Retiming`                                        | **+0.270 ns**               | +0.095 ns | 采用（phys_opt = `AlternateFlowWithRetiming`）                       |
| `Performance_RefinePlacement`                                 | +0.109 ns                   | +0.053 ns | 刚过 0.1ns                                                           |
| `Performance_ExplorePostRoutePhysOpt`                         | +0.077 ns                   | —         | 早期采用；把它的步骤 directive 覆盖成 AggressiveExplore 后仍是这一档 |
| `Performance_Auto_1/2/3`                                      | +0.061 ns                   | +0.044 ns | 三者结果相同                                                         |
| `Performance_ExtraTimingOpt` / `Performance_ExploreWithRemap` | 跑到 `write_bitstream` 失败 | —         | 未采用                                                               |

**不要覆盖 `Performance_Retiming` 自带的步骤 directive**：该策略收益正来自 phys_opt 的
`AlternateFlowWithRetiming`，覆盖成 `AggressiveExplore` 会把它换掉，实测掉回 +0.077ns 一档
（跑之前可以用 `query_runs.tcl` 或 `runme.log` 里的 `Command:` 行确认实际 directive）。

**关于 `set_clock_uncertainty` 过约束**：可以用它给工具额外压力，但只能用来「逼」优化，
不能拿过约束下的报告当签核结果（那报出来的是虚余量）。实测在本设计上收益有限且不稳定：
0.15ns 过约束 → 清掉后 WNS −0.070ns；0.20ns → −0.083ns；0.25/0.30ns 反而退化
（−0.330ns，工具被过紧的目标带进更差的解）。因此最终交付**不使用**过约束。

**改 RTL 的效果**（慢域 50MHz 参考 + DSP 400MHz）：把计数器控制脚
（`clearCnt`/`pulseSig1Valid`/`pulseSig2Valid`）全部寄存器化后，关键路径从
`stateReg → LUT → DSP48 的 RSTP/CEP` 变成「边沿检测器 → LUT6 → LUT6 → stateReg」，
余量从 +0.109ns 提升到 +0.270ns。曾试过把 FSM 手工改成 one-hot（`RegInit(1.U(5.W))`）：
带 `PopCount` 兜底时 WNS −0.444ns、不带兜底时 −0.020ns，**都比 ChiselEnum 差**，已回退
（Vivado 自带的 FSM 重编码比手工 one-hot 更好）。

### 并发跑 Vivado 的坑

同一个工程（`build/vivado/bd_proj/dpm_bd.xpr`）**不能并发开两个 Vivado**：每次
`launch_runs` 都会按当前工程里的步骤参数重写 `dpm_bd.runs/<run>/dpm_bd_wrapper.tcl`，
后启动的进程会把先生效的策略/步骤参数覆盖掉。实测遇到过一次：先设好
`Performance_Retiming`，另一个仍在跑的脚本把它改回 `Performance_ExplorePostRoutePhysOpt`，
结果同一次改动报出 +0.077ns（与 +0.270ns 差 0.19ns）。**改策略/跑实现前先确认没有其它
Vivado 在跑**，跑完用 `runme.log` 里的 `Command:` 行核对实际生效的 directive。
`build_bitstream.tcl` 现在每次启动 run 前都会把官方策略写一遍，因此 `impl_multi.tcl` 之类的实验
脚本改过策略以后，再跑一次 `build_bitstream.tcl` 就会回到签核策略（并因为检测到变化而重跑）。

## DSP 域时钟的安全启动（clk_wiz Safe Clock Startup）

**做法**：`create_bd.tcl` 里给 clk_wiz 写 `CONFIG.USE_SAFE_CLOCK_STARTUP {true}`，`clk_out1` 直接接
`dpm_top_0/dspClock`，BD 里没有额外的门控单元。开启该选项后 Wizard 会把 `CLKOUT1_DRIVES` 自动
限定为 `BUFGCE`，生成的 Verilog 是：

```verilog
(* KEEP = "TRUE" *) (* ASYNC_REG = "TRUE" *) reg [7:0] seq_reg1 = 0;
BUFGCE clkout1_buf    (.O (clk_out1), .CE (seq_reg1[7]), .I (clk_out1_clk_wiz_0));
BUFH   clkout1_buf_en (.O (clk_out1_clk_wiz_0_en_clk),  .I (clk_out1_clk_wiz_0));
always @(posedge clk_out1_clk_wiz_0_en_clk or posedge reset_high)
    if (reset_high) seq_reg1 <= 8'h00;
    else            seq_reg1 <= {seq_reg1[6:0], locked_int};
```

即**未门控的 MMCM 输出经 `BUFH` 给移位寄存器打拍，连续 8 拍采到 `locked` 才拉高 `BUFGCE` 的 CE**，
放行无毛刺；移位寄存器不依赖被门控的时钟，`locked` 用的是 MMCM 原语内部信号（与 `USE_LOCKED`
无关）。IP 另外输出 `<ip>.xdc` 与 `<ip>_late.xdc`，其中的
`set_false_path -to [get_cells -hier {*seq_reg*[*]} -filter {is_sequential}]` 覆盖了这条跨时钟采样，
顶层不需要再补约束。

**为什么换掉手写门控**：原先的链路是「MMCM → `BUFG`（clk_wiz 默认驱动）→ `BUFGCE`」（
`vivado/rtl/DpmClockGate.v` 薄封装，CE 直接接 `locked`）——两级全局缓冲串联，还要靠 `create_bd.tcl`
里的 `add_files` + `create_bd_cell -type module -reference` 把一个只用一次的 HDL 模块塞进 BD；
BD 又无法直接例化原语（`-type primitive` 不支持，`-reference BUFGCE` 报 `[filemgmt 56-587]`）。
改用自带功能后**链路变成一级 `MMCM → BUFGCE`**，`locked` 端口也不必再接，薄封装模块已删除。

**代价与对比**（xc7z010clg400-1，同一份 RTL、同一策略、同一会话下重跑的 A/B，两版都跑完整的
`package_ip` → `create_bd` → `build_bitstream` 流水线）：

| 项                    | 手写 BUFGCE 门控                         | Safe Clock Startup                   |
| --------------------- | ---------------------------------------- | ------------------------------------ |
| Slice LUT             | 557                                      | 557                                  |
| Slice Register        | 946                                      | **954**（+8：8 bit 移位寄存器）      |
| BUFGCTRL              | 4                                        | **3**（−1：不再需要外部那一级 BUFG） |
| BUFH                  | 0                                        | **1**（+1：给移位寄存器供时钟）      |
| DSP / Block RAM / IOB | 2 / 0 / 2                                | 2 / 0 / 2                            |
| 综合+实现 WNS / WHS   | +0.270 / +0.095 ns                       | +0.358 / +0.062 ns                   |
| `report_methodology`  | TIMING-4 / 27 / 28 = 1 / 1 / 2           | **完全相同**                         |
| `check_timing`        | 12 项全零（仅 sig1/sig2 的 MEDIUM 提示） | **完全相同**                         |

最后两行说明这次替换没有引入新的方法学告警或未约束端点：TIMING-4/27 来自 clk_wiz 自带 XDC 在
`clk_in1` 上建的 primary clock，TIMING-28 来自顶层 XDC 按时钟名引用自动派生时钟，两者都是既有写法。

**启动行为（xsim 单独跑 clk_wiz 实测）**：MMCM 锁定前未门控的原始输出已有 **322** 个上升沿，而
门控后的 `clk_out1` 是 **0** 个；`locked` 拉高后 **23ns**（≈9 个 400MHz 周期 = 8 拍移位 + 缓冲器
延迟）出现第一个时钟沿。也就是说「锁定前 DSP 域不来时钟」这条保证与手写门控一致，代价是放行
比 `locked` 本身晚 8 拍——DSP 域复位本来就要等时钟，不影响功能。**运行中丢失锁定**时的反应也差
8 拍：手写门控的 CE 直接跟 `locked`（立即关时钟），Safe Clock Startup 要连续 8 拍采到
`locked=0` 才关；两者都是无毛刺地停时钟，DSP 域寄存器/FSM 保持不动。

**时序余量怎么看**：place/route 有 run-to-run 波动（同一网表实测 ±0.2ns），所以不能用单次结果
断言收敛。可比的是多次实现：**加入片内信号源（第二个 MMCM）后，`impl_multi.tcl` 的 6 次独立实现
WNS 全部为正，最差 +0.153ns、最好 +0.389ns，WHS 均为 +0.025ns**（见下文「本仓库的实测结果」）。
作为对照：Safe Clock Startup 那一版（没有片内信号源）6 次为 +0.191～+0.358ns，手写门控版最差
+0.236ns——三者同一量级，**加入片内信号源没有让 400MHz 时序变差到不可接受**；真正把余量吃掉的
是「在 400MHz 域里做 32 位加减 / 16 位分频比较」的那几版尝试（WNS −0.55 / −0.71 / −1.7ns），
最终把信号生成全部交回 MMCM、测量域不再新增逻辑后回到正值（见 `dpm-vivado-ip` 的同名结论）。

## Block design 结构

```text
processing_system7_0 ──M_AXI_GP0(AXI3, ID 12)──► axi_interconnect_0 ──M00_AXI(AXI4-Lite)──► dpm_top_0/s_axi_lite
        │                                               └──M01_AXI(AXI4-Lite)──► dpm_src_drp_0/s_axi_lite（DRP 控制器）
        │  FCLK_CLK0 50MHz ──► proc_sys_reset_0 ─► 互联与两个从口的主时钟/复位
        │                   ├► clk_wiz_0(MMCM + Safe Clock Startup) ─► dpm_top_0/dspClock
        │                   └► dpm_mmcm_src_0(裸 MMCM) ─► dpm_top_0/srcSig1、srcSig2、srcLocked
        │                             ▲
        │                             └── DRP ◄── dpm_src_drp_0（PS 写 MMCM 寄存器镜像）
        └◄── IRQ_F2P ◄── xlconcat_0 ◄── dpm_top_0/interrupt（IRQ_F2P[0]）

sig1 / sig2：BD 外部端口 → dpm_top_0（顶层 XDC 里约束到引脚 W18 / W19，LVCMOS33）
PS 串口：PS7 的 UART（默认 UART1 = MIO48/49，`create_bd.tcl -uart` 可换成 UART0 = MIO14/15 或 none）

dpm_top_0 内部：外部 sig1/sig2 与片内源（MMCM 的两路输出）经 SignalInputMux 二选一；
使能与选择位从 s_axi_lite 的寄存器区经 CdcConfigBus 跨到 DSP 域。
```

要点：

- **从口多于一个时用 `axi_interconnect`**：测量 IP 与片内源 MMCM 的动态重配置口都是 AXI4-Lite 从口，
  互联负责地址译码，并自动插入 `auto_pc` 完成 Zynq-7000 `M_AXI_GP0` 的 **AXI3**（ID 宽 12、
  最大突发 16）→ **AXI4-Lite** 协议转换（`SI_PROTOCOL=AXI3`、`MI_PROTOCOL=AXI4LITE`、`ID_WIDTH=12`、
  `TRANSLATION_MODE=2`，与手工例化 `axi_protocol_converter` 的配置一致）。只有一个从口时
  不必上互联，直接用 `axi_protocol_converter` 更省面积与延迟（本仓库早期的做法）。
- `ID_WIDTH=12` 与 PS 的 ID 宽一致：`ID_WIDTH=0` 时转换器从口根本没有 ID 信号（`C_IGNORE_ID=1`），
  PS 的 `AWID/ARID/BID/RID` 会被悬空。
- **DSP 域时钟由 PL 内 MMCM 倍频**，不用 PS 的 `FCLK_CLK1`：Zynq-7000 的 FCLK 由 PS 的 IO PLL
  分频得到，**上限 250MHz**（在 BD 里请求更高的值会被静默降到 250MHz），因此 300MHz 以上必须
  在 PL 内用 MMCM。参考时钟取 `FCLK_CLK0`（50MHz，与 AXI/低速控制域同源，锁相稳定），目标频率由
  `create_bd.tcl` 的第一个参数给出（默认 400MHz）。当前配置：`PRIM_IN_FREQ=50`、
  `MMCM_CLKFBOUT_MULT_F=16`、`MMCM_CLKOUT0_DIVIDE_F=2` → VCO 800MHz、输出 400MHz。
- **片内信号源是「裸 MMCM + 极简 DRP 控制器」（方案 A）**：`dpm_mmcm_src_0`（
  [`rtl/dpm_mmcm_src.v`](rtl/dpm_mmcm_src.v)，`MMCME2_ADV` 薄封装）出两路同频、相位差可调的信号，
  静态工作点 25MHz + 90°；频率与相位差由 **PS 算出 MMCM 的 16 位寄存器值**，经 AXI4-Lite 写进
  `dpm_src_drp_0`（[`../src/main/scala/DigitalPhaseMeter/MmcmDrpController.scala`](../src/main/scala/DigitalPhaseMeter/MmcmDrpController.scala)），
  控制器按固定顺序搬进 MMCM 的 DRP 口（0x08..0x24 八个寄存器镜像 + 0x28 LOAD）。
  寄存器位域与编码规则见 `build/analysis/golden/mmcm_encode.py`（已对 clk_wiz 的 Write DRP
  生成值逐点校验）。**不再用 clk_wiz 的 `USE_DYN_RECONFIG + PHASE_DUTY_CONFIG`**：那套配置把
  「毫度/百分比/MHz → MMCM 计数器位域」的定点换算综合成硬件（实测 13,287 LUT site + 38 DSP），
  改用本方案后整设计 Slice LUTs 从 14,048（79.8%）降到 948（5.4%）、DSP 从 40 降到 2。
  **重配置期间两路输出会停止**，因此要在测量空闲时改。
  初始频率选 25MHz（D = 40）：MMCM 相位寄存器的可表示范围是 64 个 VCO 周期，换成输出相位是
  `64 / D` 个输出周期，需要更低频率时优先把 VCO 调到下限而不是把 D 拉满。
- **「MMCM 锁定前不放时钟」用 clk_wiz 自带的 Safe Clock Startup**（`CONFIG.USE_SAFE_CLOCK_STARTUP=true`），
  不再手写门控模块。开启后 Wizard 自动把 `CLKOUT1_DRIVES` 限定成 `BUFGCE`，并生成
  「MMCM 输出 → `BUFGCE`（`CE = seq_reg1[7]`）→ `clk_out1`」：`seq_reg1` 是同一 MMCM 输出经 `BUFH`
  打拍的 8 bit 移位寄存器，逐拍移入 MMCM 的 `locked`，**连续 8 拍锁定**后才拉高 CE 放行时钟；
  IP 自带的 XDC 已给 `seq_reg` 加 `set_false_path`，不需要外部约束（详见下文「DSP 域时钟的
  安全启动」）。不用「与门 + `util_vector_logic`」门控：那会让时钟走 fabric 布线
  （BUFG → LUT → 各时钟脚），是 UG949 在 7 系列章节点名的反模式。
- **两个时钟域之间的握手通路用有界 `set_max_delay -datapath_only` 约束**（见
  [`constraints/dpm_top.xdc`](constraints/dpm_top.xdc)）：`clk_wiz` 为了让 MMCM 输出对齐到
  `FCLK_CLK0` 会把 CLKOUT0 相位设成 `-T_dsp`（相位补偿），工具的逐沿跨时钟检查会给这条通路
  只留约 2.4ns 的余量，比实际布线延迟还紧，400MHz 下必然报违例。真实需求是「一个慢时钟周期
  内到达」（翻转电平 + 3 级 ASYNC_REG 同步链），所以用 10ns 的有界 max_delay 表达，
  **两个方向都要写**：只写一个方向会让另一个方向变成新的最差路径（实测 +0.055 → −0.258ns）。
- **400MHz 的综合/实现策略**由 [`build_bitstream.tcl`](build_bitstream.tcl) 在启动 run 前写入工程：
  综合侧 `DIRECTIVE=PerformanceOptimized` + `GLOBAL_RETIMING=on` + `MAX_DSP=2`；
  实现侧 `Performance_Retiming` 策略，步骤 directive 保持策略预设（**不要**覆盖，见上文）。
- **place/route 有可观的 run-to-run 差异**：同一网表、同一策略实测 WNS 在 +0.006 与 −0.202ns
  之间摇摆。因此「跑一次通过」不能算签核，需要多次独立布局布线取余量最好的一次——
  `vivado/impl_multi.tcl` 就是做这件事的实验脚本（6 次全部为正、最差 +0.236ns）。
- IP 的 `resetn` 接 `proc_sys_reset_0/peripheral_aresetn`（低有效），
  顶层内部再用 `ResetSynchronizer` 生成与主时钟同步的高有效复位；
  `dspClock` 域的复位由 `Controller` 内部的 `SyncReset` 得到，不需要外部提供。
- 中断是**高电平**信号，经 `xlconcat` 接到 `IRQ_F2P[0]`；PS 侧中断服务程序写 `IRQ_STATUS` 清除。

## 地址映射

| 从口                       | 基地址        | 范围 | 用途                                                                       |
| -------------------------- | ------------- | ---- | -------------------------------------------------------------------------- |
| `dpm_top_0/s_axi_lite`     | `0x43C0_0000` | 4 KB | 控制/状态寄存器 + 只读测量结果寄存器 + 片内源使能/输入选择                 |
| `dpm_src_drp_0/s_axi_lite` | `0x43C1_0000` | 4 KB | 片内信号源 MMCM 的 DRP 控制器：MMCM 寄存器镜像（0x08..0x24）+ LOAD（0x28） |

两段地址（`SEG_dpm_top_0_Reg`、`SEG_dpm_src_drp_0_Reg`）。寄存器定义见仓库根目录
[`README.md`](../README.md#pspl-互联与寄存器接口) 与权威映射表
[`../src/main/scala/DigitalPhaseMeter/ControlRegs.scala`](../src/main/scala/DigitalPhaseMeter/ControlRegs.scala)；
在 Vitis 里对应 `XPAR_DPM_TOP_0_S_AXI_LITE_BASEADDR`。

## PS 侧（裸机）访问示例

```c
#include "xil_io.h"

#define DPM_BASE         0x43C00000u   /* XPAR_DPM_TOP_0_S_AXI_LITE_BASEADDR */

#define DPM_CTRL         0x00u        /* [0] start  [1] irqEnable  [2] clear */
#define DPM_STATUS       0x04u        /* [0] busy [1] resultValid [2] lost [3] irqPending */
#define DPM_GATE_TARGET  0x08u
#define DPM_RESULT_GATE  0x0Cu        /* 只读：最新结果的 gateCnt  */
#define DPM_RESULT_EDGE  0x10u        /* 只读：最新结果的 edgeCnt  */
#define DPM_RESULT_PHASE 0x14u        /* 只读：最新结果的 phaseCnt */
#define DPM_RESULT_SEQ   0x18u        /* 只读：最新结果的记录序号  */
#define DPM_LOST_COUNT   0x1Cu
#define DPM_IRQ_STATUS   0x20u        /* W1C：[0] irqPending [1] resultValid [2] lost */
#define DPM_VERSION      0x24u        /* 0x44504D02 */
#define DPM_CAPS         0x28u        /* b[7:0] 每条结果字数=4，b[15:8] 深度=1，b16 数据通道为寄存器，b17 有片内信号源 */
#define DPM_SIG_CTRL     0x2Cu        /* [0] enable 片内源使能  [1] srcSelect 0=外部引脚 1=片内源 */
#define DPM_SIG_STATUS   0x30u        /* [0] enable [1] srcSelect [2] srcLocked [3] sourceReady */

/* 片内信号源的 MMCM 寄存器镜像口（dpm_src_drp_0）：另一个 4KB 地址段（基地址见 xparameters.h
   的 XPAR_DPM_SRC_DRP_0_S_AXI_LITE_BASEADDR）。寄存器是 **MMCM 的原始 16 位寄存器值**，
   不是 clk_wiz 的语义寄存器文件：
     0x00 ID、0x04 STATUS（bit0 = MMCM locked）、0x08..0x24 八个 MMCM 寄存器、0x28 LOAD（W1S）
   写流程：先写全部要改的 MMCM 寄存器，再写 0x28 = 1 触发，控制器按固定顺序搬进 DRP 口，
   之后轮询 0x04 的 bit0 等重新锁定；**重配置期间两路输出停止**，只能在测量空闲时改。
   位域编码（ClkReg1/ClkReg2/DivReg、HIGH_TIME/LOW_TIME/EDGE/NO_COUNT/PHASE_MUX/DELAY_TIME）
   与寄存器地址映射见 build/analysis/golden/mmcm_encode.py 与 package_src_ip.tcl 的说明：
     VCO = 输入频率 × CLKFBOUT 倍频 ÷ DIVCLK，VCO 需落在 600～1200MHz；
     输出频率 = VCO ÷ CLKOUTx 分频；相位差 = 两个 CLKOUTx 相位寄存器之差（步长 1/8 VCO 周期）。 */
#define DPM_DRP_BASE     0x43C10000u

static inline void dpm_write(u32 off, u32 value) { Xil_Out32(DPM_BASE + off, value); }
static inline u32  dpm_read(u32 off)             { return Xil_In32(DPM_BASE + off); }

/* 启动一次测量：写 CTRL 时 bit1 是普通读写位，必须与 start 一起写，否则会关掉中断使能 */
void dpm_start(u32 gate_target)
{
    dpm_write(DPM_GATE_TARGET, gate_target);
    dpm_write(DPM_CTRL, (1u << 1) | (1u << 0));
}

/* 选中片内信号源（MMCM 的两路输出直接作为被测信号，初始 25MHz / 90°） */
void dpm_select_internal_source(void)
{
    /* 1) 需要改频率/相位差时，先写 MMCM 寄存器镜像（0x08..0x24）、写 0x28 LOAD，
          再轮询 STATUS 等重新锁定；这里假设沿用 BD 里的初始工作点（25MHz / 90°） */
    /* 2) 使能并选中片内源；AXI 写会在配置跨到测量域后才完成，因此写返回即已生效 */
    dpm_write(DPM_SIG_CTRL, (1u << 0) | (1u << 1)); /* enable | srcSelect */
    /* 3) 确认 MMCM 已锁定、源可用 */
    while ((dpm_read(DPM_SIG_STATUS) & ((1u << 2) | (1u << 3))) != ((1u << 2) | (1u << 3))) { }
}

/* 切回外部引脚输入（sig1 → W18、sig2 → W19） */
void dpm_select_external_source(void) { dpm_write(DPM_SIG_CTRL, 0u); }

/* 中断服务程序：清中断，读走 4 个结果寄存器（结果只有一份，不需要循环取多条记录） */
void dpm_irq_handler(void)
{
    dpm_write(DPM_IRQ_STATUS, 1u);                 /* 写 1 清中断挂起 */

    const u32 gate_cnt  = dpm_read(DPM_RESULT_GATE);
    const u32 edge_cnt  = dpm_read(DPM_RESULT_EDGE);
    const u32 phase_cnt = dpm_read(DPM_RESULT_PHASE);
    const u32 seq       = dpm_read(DPM_RESULT_SEQ);
    /* 相位差（弧度）≈ 2π × phase_cnt / (T_sig / T_dsp) / edge_cnt */
    (void)gate_cnt; (void)edge_cnt; (void)phase_cnt; (void)seq;

    dpm_write(DPM_IRQ_STATUS, 1u << 1);            /* 写 b1 表示结果已读走（清 resultValid） */
}
```

中断在 GIC 中的连接：`IRQ_F2P[0]` 对应 `XPAR_FABRIC_DPM_TOP_0_INTERRUPT_INTR`
（共享外设中断，请按板级 `xparameters.h` 中的实际编号注册）。

## 约束审计（XDC 完整性）

**结论：本设计的约束是完整的，没有漏掉任何 CDC 通路。** 依据（都在实际布线的 impl_1 上跑）：

1. **`check_timing` 全零**：0 个无时钟寄存器/锁存器、0 个未约束的最大延迟端点、
   0 个 constant clock、0 个多时钟、0 个无源生成时钟、0 个组合环路、0 个 latch 环路。
   唯一的非零项是「2 个输入端口没有 input delay 但被用户 false path 约束」（sig1/sig2，
   这是有意为之——见下）。
2. **时钟对穷举**：设计里只有 4 个时钟对象——`clk_fpga_0`(50MHz)、MMCM 的 `clk_in1` 与
   `clkfbout`（都等于 50MHz，约束在 MMCM 原语内部，不跨到 fabric）、DSP 域的
   `clk_out1_dpm_system_clk_wiz_0_0`(400MHz)。**实际存在时序路径的时钟对只有 3 类**：
   - `clk_fpga_0 → clk_fpga_0`（慢域内部，WNS +12.608ns）
   - `clk_fpga_0 → clk_out1_...`（慢→快：`request`、`responseReady` 的翻转信号，以及信号源配置的
     翻转握手与配置字，在 DSP 域被 3 级 ASYNC_REG 同步链采样；配置字在整笔传输期间保持稳定）
   - `clk_out1_... → clk_fpga_0`（快→慢，65 个端点：`response.valid`/`response.ready`
     以及结果载荷在慢域被同步链采样）
   两类跨域通路都由 [`constraints/dpm_top.xdc`](constraints/dpm_top.xdc) 的**双向**
   `set_max_delay -datapath_only 20ns` 覆盖，实测余量 +18.0/+18.6ns。
3. **为什么 sig1/sig2 用 `set_false_path` 而不是 max_delay**：它们是外部异步输入，
   在 IP 内部立刻进 `EdgeDetector` 的 3 级 `ASYNC_REG` 同步链（见 `dpm-chisel-rtl`），
   第一级就是同步器，不需要建立/保持关系。`check_timing` 把它列为 MEDIUM 提示是预期的
   （提示「有 false path 的输入端口没有 input delay」）；引脚与 IO 标准已在
   [`constraints/dpm_top.xdc`](constraints/dpm_top.xdc) 中给出（`sig1 → W18`、`sig2 → W19`，
   LVCMOS33），换板卡时按实际供电与引脚改这两行。片内信号源由 DSP 域时钟直接驱动，与测量核心
   同源，因此没有额外的输入约束。
4. **复位与时钟基础设施不是数据通路**：`resetn` 经 `ResetSynchronizer` 同步，DSP 域的
   `dspReset` 由 `SyncReset`（综合化为 `xpm_cdc_sync_rst`）从主复位同步得到；MMCM 的
   `locked` 只驱动 clk_wiz 内部 Safe Clock Startup 的移位寄存器与 `BUFGCE` 的 CE
   （`clk_wiz_0/locked` 端口在 BD 里未连接）。这些都不是需要 max_delay/false_path 的数据通路，
   `check_timing` 也确认没有遗漏的端点。

> 复核方式：`check_timing -verbose -file <rpt>` 看 12 项是否全零，再用
> `report_timing_summary` 的 Intra/Inter Clock Table 逐个时钟对确认余量；
> 需要逐对枚举时可以用 `get_timing_paths -from <clk> -to <clk>`。

## 告警清理（综合 / 实现日志）

目标：**综合与实现各步骤的日志为 0 Warnings / 0 Critical Warnings / 0 Errors**（本轮实测结果见下文）。
达到这个状态需要区分两类告警——能改设计的改设计，改不动的（厂商 IP 自带）显式屏蔽并写明理由。

### 一、改设计能消掉的（根因在本仓库里）

| 告警                                                                                                                                                                                       | 根因                                                                                                                                                                                                                                     | 处理                                                                                                                                                                                                                                                                                         |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `[Vivado 12-627] No clocks matched 'clkout0_raw' / 'clkout1_raw' / '*clk_wiz_src*'`（综合）<br>`[Vivado 12-4739] set_false_path/set_max_delay: No valid object(s) found`（实现，CRITICAL） | `constraints/dpm_top.xdc` 里用**具体时钟名**取 `get_clocks`。该文件被评估两次：综合启动时（BD 还是黑盒，时钟一个都不存在）与实现读网表时。综合阶段必然取不到对象；而 `*clk_wiz_src*` 是已被方案 A 取代的旧方案时钟名，实现阶段同样不存在 | 改成「通配符 + `-quiet`」写法：取对象用 `get_clocks -quiet`（无匹配时安静返回空串），约束命令也带 `-quiet`（对象为空时安静不生效）。**xdc 不支持 `foreach` / `if` 等流程控制命令**（[Designutils 20-1307]），所以只能这样等效实现「先判断再施加」。顺带删掉了失效的 `*clk_wiz_src*` 兜底约束 |
| `[Synth 8-7071] port 'locked' of module 'dpm_system_clk_wiz_0_0' is unconnected`                                                                                                           | `create_bd.tcl` 里 `clk_wiz_0` 的 `CONFIG.USE_LOCKED {true}`，但 BD 里没有消费者（Safe Clock Startup 用的是 MMCM 原语的 `locked`，不依赖这个端口）                                                                                       | 改成 `CONFIG.USE_LOCKED {false}`，并在脚本里回读校验（`CLK_WIZ_CHECK: USE_LOCKED='false' locked_pin=''`）。IP 写错值时不报错、只是静默不生效，所以必须回读                                                                                                                                   |

### 二、厂商 IP 固有、改不动的（写进 `hook_filter_messages.tcl` 显式屏蔽）

这些告警的来源是**厂商 IP 自己声明的端口或随 IP 生成的 xdc**，不在本仓库源码里；对功能与时序都没有影响，
但会在每个新工程里产生上百条噪声，掩盖真正需要注意的告警。

| 告警                                                                                           | 数量级 | 为什么必然出现                                                                                                                                                                                                                                   |
| ---------------------------------------------------------------------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `[Synth 8-7129] Port In16[0]...In115[0] ... unconnected or has no load`                        | 100    | `xlconcat` 的 128 个 `In*` 端口是 IP 内部 HDL **写死的**（不是 BD 里 `NUM_PORTS` 的结果——实测 `NUM_PORTS` 只决定 `dout` 位宽与哪些 `In*` 被连接；`NUM_PORTS=1/2/16` 都仍然声明 128 个端口）。本设计只用 `In0`（唯一中断源），其余 127 个必然空置 |
| `[Synth 8-7023] instance ... has N connections declared, but only M given`                     | 3      | 与下面的 8-7071 一一对应（BD 例化 IP 时未给全端口）                                                                                                                                                                                              |
| `[Synth 8-7071] port 'DDR_*' / 'MIO' / 'PS_CLK' ... is unconnected`                            | 16     | PS7 的 DDR/MIO 引脚只要不使能对应功能就一律不连接；未使能 UART 之外的 MIO 全部 unassigned                                                                                                                                                        |
| `[Synth 8-7071] port 'mb_reset' / 'bus_struct_reset' / 'interconnect_aresetn' ... unconnected` | 3      | `proc_sys_reset` 的三个输出在本设计里没有消费者（无 MicroBlaze、无带 SysReset 接口的 AXI4 从口），该 IP 不提供关闭选项                                                                                                                           |
| `[Synth 8-4446] all outputs are unconnected for this instance`                                 | 1      | 顶层 `dpm_system_wrapper` 的唯一实例没有任何输出端口——本设计的对外通路全在 PS 侧（AXI4-Lite 寄存器 + PS7 硬核引脚），PL 侧本就没有输出引脚。消掉它必须刻意加一个板级调试输出引脚（反而引入未约束引脚告警），收益不足                             |
| `[Opt 31-32] Removing redundant IBUF since it is not being driven by a top-level port`         | 1      | `clk_wiz_0` 的输入时钟由 fabric 上的 `FCLK_CLK0` 驱动（不是从顶层引脚进来），它内部例化的 IBUFG 因此多余                                                                                                                                         |
| `[Constraints 18-550] Could not create 'IBUF_LOW_PWR' constraint ...`                          | 1      | 同上：随 clk_wiz 生成的 xdc 试图给这条内部网络设 `IBUF_LOW_PWR`                                                                                                                                                                                  |
| `[Netlist 29-160] Cannot set property 'iostandard'/'PACKAGE_PIN'/... (CRITICAL WARNING)`       | 100    | PS7 自带 `dpm_system_processing_system7_0_0.xdc` 给**未使能因而在网表里不存在**的引脚设属性。MIO 的 IO 标准与方向由 PS 启动时的配置字决定，不依赖这些 xdc 语句                                                                                   |

其余仍在日志里的告警**不在屏蔽范围内**（原样保留，便于发现新问题）：

- `[Board 49-26] cannot add Board Part ... as part ... is either invalid or not available` × 96：
  出现在 `build_bitstream.tcl` 的 `open_project` **内部**——Vivado 扫描 XilinxBoardStore 时逐个报告
  「board 声明的 part 未安装」（安装里只有 `xc7z010clg400-1`）。**它早于本脚本的任何一条语句，
  因此 `set_msg_config` 对它无效（实测）**，只能在 `vivado -mode batch` 启动前用
  `-init_script`/`vivado_init.tcl` 之类的进程级手段拦。绑定了 board part 的工程不会看到。
  与本设计无关，不影响任何结果。
- `create_bd.tcl` 阶段的 `[IP_Flow 19-5661] Bus Interface 'clkIn' does not have any bus interfaces
  associated with it` 与 `[IP_Flow 19-11770] Clock interface 'clkIn' has no FREQ_HZ parameter`、
  以及 `[IP_Flow 19-4994] Overwriting existing constraint file ..._ooc.xdc`（重建 BD 时必然出现）：
  前两条来自 BD 里以 `create_bd_cell -type module -reference dpm_mmcm_src` 例化的**裸 HDL 模块**
  （不是打包好的 IP，因此没有 IP-XACT 时钟接口声明），是该做法的已知代价；第三条是正常覆盖提示。
- `package_ip.tcl` 阶段的 `[IP_Flow 19-5101] Packaging a component with a SystemVerilog top file is
  not fully supported`、`[IP_Flow 19-2187] The Product Guide file is missing`（×2）、
  `[IP_Flow 19-11888] IP description ... is not meaningful`、
  `[IP_Flow 19-11770] Clock interface 'clock' has no FREQ_HZ parameter`、
  `[IP_Flow 19-5661] Bus Interface 'dspClock' does not have any bus interfaces associated with it`：
  都是打包器的**提示级**告警，不影响 IP 的正确性——接口推断、地址块、`ipx::check_integrity`
  都是通过的，BD 里也能正常例化与连接。其中 `dspClock` 本来就不挂在 AXI 从口上（它是测量核心的
  独立高速时钟域），所以没有 `ASSOCIATED_BUSIF` 正是预期；`clock` 的 `FREQ_HZ` 由
  `package_ip.tcl` 显式写成 50MHz，报这句的是打包器对「另一路时钟」的通用检查。

> 复核方式：run 日志末尾打印的 `N Infos, 0 Warnings, 0 Critical Warnings and 0 Errors encountered.`
> 是最直接的判据（综合、实现每一步都应如此）；也可以用 `grep -c WARNING <run>/runme.log` 复核。
> 想确认「屏蔽了哪些」就看 `hook_filter_messages.tcl`——每条都写了理由，删掉对应行即可让它重新出现。

## 本仓库的实测结果（Vivado 2025.2，xc7z010clg400-1）

### 把三个补丁/策略脚本并进流水线之后的一轮复跑

按新流水线（`create_bd.tcl` → `build_bitstream.tcl`，不含单独的补丁/策略脚本）完整重跑的
实际结果：

| 步骤                                             | 结果                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `create_bd.tcl`（默认 uart1）                    | 通过（`BD_DONE: dpm_system`），0 ERROR / 0 CRITICAL WARNING；`PS7_UART_CHECK: EN_UART1=1 IO='MIO 48 .. 49' BAUD=115200`、MIO48/49 = `UART 1/uart1_tx` / `uart1_rx`                                                                                                                                                                                                                                              |
| `create_bd.tcl -uart uart0`                      | 通过，0 ERROR / 0 CRITICAL WARNING；`EN_UART0=1 IO='MIO 14 .. 15'`；`ps/tools/check_bd_uart.py` 在该 XCI 上报 `PS_UART: UART0`                                                                                                                                                                                                                                                                                  |
| `create_bd.tcl -uart none`                       | 通过；`PS7_UART_CHECK: -uart none`，check 脚本报 `PS_UART: none`（MIO 全 unassigned）                                                                                                                                                                                                                                                                                                                           |
| `build_bitstream.tcl`（新工程第一次）            | `STRATEGY_CHANGED: 4`（写入策略）→ `SYNTH_DONE` / `IMPL_DONE` / `XSA_DONE`。实际执行的命令：`synth_design … -directive PerformanceOptimized -global_retiming on -max_dsp 2`；`place_design -directive ExtraPostPlacementOpt`、`phys_opt_design -directive AlternateFlowWithRetiming`、`route_design -directive Explore`（`Performance_Retiming` 的预设，未被覆盖）                                              |
| 时序                                             | **WNS +0.281ns / WHS +0.032ns**（TNS = THS = 0，0 个失败端点）                                                                                                                                                                                                                                                                                                                                                  |
| `build_bitstream.tcl`（紧接着再跑）              | `XDC_REUSED` + `SYNTH_REUSED` + `IMPL_REUSED`、`STRATEGY_CHANGED: 0`（不重复写属性、不重跑综合实现）                                                                                                                                                                                                                                                                                                            |
| `build_bitstream.tcl -retiming 0 -strategy-only` | `STRATEGY_SET: …GLOBAL_RETIMING: 'on' -> 'off'`、`STRATEGY_CHANGED: 1`，不启动 run                                                                                                                                                                                                                                                                                                                              |
| 恢复默认策略后完整重跑                           | `STRATEGY_SET: 'off' -> 'on'`、`STRATEGY_CHANGED: 1`、`STRATEGY_FORCE_RERUN` → 重新综合实现，WNS/WHS 同为 +0.281 / +0.032ns                                                                                                                                                                                                                                                                                     |
| 资源占用                                         | Slice LUT **948（5.39%）**、Slice Register 1491（4.24%）、**Block RAM Tile 0（0.00%）**、DSP **2（2.50%）**、Bonded IOB 2（2.00%）、BUFGCTRL 6（18.75%）、**MMCME2_ADV 2（100%，器件上限）**                                                                                                                                                                                                                    |
| XSA                                              | `build/vivado/dpm_system.xsa`（含 bitstream）；`python3 ps/tools/check_bd_uart.py build/vivado/dpm_system.xsa` 报 `PS_UART: UART1 base=0xE0000000 io=MIO 48 .. 49 baud=115200`                                                                                                                                                                                                                                  |
| 日志告警（清理前）                               | 0 ERROR；综合 133 条 WARNING、实现 106 条 WARNING（其中 100 条是 CRITICAL）。构成：100 × `[Synth 8-7129]`（xlconcat 写死的 128 个 `In*` 端口）、25 × `[Synth 8-7071]`（PS7 DDR/MIO 等未连接端口）、100 × `[Netlist 29-160]`（PS7 自带 XDC 在没有 board part 时设不上 MIO 引脚属性）、3 × `[Synth 8-7023]`、2 × `[Vivado 12-4739]`（顶层 XDC 里失效的 `*clk_wiz_src*` 兜底约束）、3 × `[Synth 8-4446/8-7023]` 等 |
| **日志告警（清理后）**                           | **综合 `58 Infos, 0 Warnings, 0 Critical Warnings and 0 Errors`；实现 `148 Infos, 0 Warnings, 0 Critical Warnings and 0 Errors`**。改名/改 BD 能消的已改（顶层 XDC 的时钟约束写法、`clk_wiz_0` 的 `USE_LOCKED`），厂商 IP 固有的按类屏蔽并写明理由（见「告警清理」）。唯一仍在 `open_project` 里打印的是 96 条 `[Board 49-26]` 板卡库扫描告警，它在脚本任何语句之前就输出，`set_msg_config` 拦不到              |

### 上一版（clk_wiz_src 方案）的实测记录

加入片内信号源（BD 里第二个 MMCM + 动态重配置口）并把手写 BUFGCE 门控换成 clk_wiz 自带的
Safe Clock Startup 之后重跑的脚本，均为实际运行结果（**BD 结构与本节的资源数字对应的是
`clk_wiz_src` 那一版，仅作对照**）：

| 步骤                    | 结果                                                                                                                                                                                                                                                                                                                |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `package_ip.tcl`        | 通过，`ipx::check_integrity: Integrity check passed`，0 ERROR / 0 CRITICAL WARNING；IP 对外 **5 个** bus interface：`s_axi_lite`（19 根 AXI4-Lite 信号全部推断成功）、`clock`、`dspClock`、`resetn`、`interrupt`；片内源的三根信号（`srcSig1` / `srcSig2` / `srcLocked`）是普通引脚                                 |
| `create_bd.tcl`         | 通过（`BD_DONE: dpm_system`），`validate_bd_design` 0 ERROR / 0 CRITICAL WARNING；BD 用 `axi_interconnect_0`（NUM_SI=1、NUM_MI=2，自动插入 `auto_pc` 做 AXI3→AXI4-Lite）、两个 MMCM（`clk_wiz_0` 出 DSP 域时钟、`clk_wiz_src` 带 DRP）；两段地址 `SEG_dpm_top_0_Reg 0x43C00000` 与 `SEG_clk_wiz_src_Reg 0x43C10000` |
| 综合 + 实现 + bitstream | 通过（`SYNTH_DONE` / `IMPL_DONE` / `XSA_DONE`，`write_bitstream completed successfully`）；**6 次独立实现的 WNS 全部为正：最差 +0.153ns、最好 +0.389ns**，WHS 均为 +0.025ns，TNS = THS = 0，**0 个失败端点**（每次的报告见 `impl_multi.tcl` 的输出）                                                                |
| 时钟                    | `clk_fpga_0` 50 MHz（FCLK_CLK0）、DSP 域 400 MHz（`clk_wiz_0` 的 MMCM 输出，2.5 ns）、片内源 2 × 25 MHz 且相位差 90°（`clk_wiz_src` 的 `clk_out1` / `clk_out2`，波形 `{0,20}` 与 `{10,30}` ns，即 90°）；**器件只有 2 个 MMCM，本设计两个都用掉了**                                                                 |
| 跨时钟域（CDC）         | 三类握手通路（慢↔快的请求/应答、慢→快的配置整字）都在有界 `set_max_delay -datapath_only 20ns` 下；片内源两路时钟到 DSP 域按异步输入 `set_false_path` 处理，`srcLocked` 用 `set_max_delay -datapath_only 20ns` 进慢域                                                                                                |
| 资源占用                | Slice LUT 2030（11.53%）、Slice Register 2743（7.79%）、**Block RAM Tile 0（0.00%）**、DSP 3（3.75%）、Bonded IOB 2（2.00%，即 W18/W19）、BUFGCTRL 6（18.75%）、**MMCME2_ADV 2（100%，器件上限）**                                                                                                                  |
| bitstream               | `build/vivado/bd_proj/dpm_bd.runs/impl_1/dpm_system_wrapper.bit`                                                                                                                                                                                                                                                    |
| XSA                     | `build/vivado/dpm_system.xsa`（含 bitstream）                                                                                                                                                                                                                                                                       |
| `xsim`                  | 顶层 IP 的 SV 激励在 [`../sim/`](../sim/)，通过（`==== 测试通过`）：片内源的两路信号由激励直接驱动（周期 640ns、相位差 160ns），测得 `phaseCnt/edgeCnt` 精确等于 32 个高速时钟周期；运行方式与覆盖范围见 [`../sim/README.md`](../sim/README.md)                                                                     |

未验证项：

- **本轮没有上板**：PL 侧只到「BD 校验通过 + 综合实现时序为正 + bitstream/XSA 生成成功」，
  没有在真板上验证功能；PS 侧软件与上板记录见 [`../ps/README.md`](../ps/README.md) 与
  [`../docs/onboard-ax7010.md`](../docs/onboard-ax7010.md)。
- **MMCM 的 DRP 通路没有仿真**：xsim 的 unisim MMCM 不支持 DRP 写（16 个单比特探针全部无效），
  因此「写 MMCM 寄存器镜像 → 写 LOAD → 输出真的改变」只有 DRP 从端模型
  （`build/analysis/drp_tb/`）与 `validate_bd_design` 的结构证据；上板后需要先确认这一点。

## 注意事项与已知限制

- **本仓库不绑定开发板**：`build_bitstream.tcl` 把 `NSTD-1`（IO 标准未指定）与 `UCIO-1`
  （引脚未约束）两条 DRC 降级为警告，否则 `sig1`/`sig2` 缺少引脚约束会让 `write_bitstream` 中断。
  使用具体开发板时，请加入板级约束文件并去掉这两条降级。
- 没有板级文件时，综合/实现的日志里原本会出现约 100 条
  `[Netlist 29-160] Cannot set property 'iostandard'/'PACKAGE_PIN'/'slew'/'PIO_DIRECTION' ...` 的
  **CRITICAL WARNING**，来源是 PS7 IP 自动生成的
  `dpm_system_processing_system7_0_0.xdc`：没有 board part 时它无法把 MIO 引脚解析成 package pin。
  MIO 的 IO 标准与方向由 PS 的配置（boot header / PS 配置字）在启动时生效，不影响 PL 侧的
  综合、实现与 bitstream 生成；加了板级约束后这些告警会消失。
  **现已由 [`hook_filter_messages.tcl`](hook_filter_messages.tcl) 屏蔽**（连同其余厂商 IP 固有告警，
  见上文「告警清理」）：这些语句落在「未使能因而在网表里不存在」的引脚上，改不动也不该改。
  若换成绑定了 board part 的工程，应当把该文件里对应的 `set_msg_config` 行删掉，
  让真正的引脚属性问题重新暴露出来。
- `sig1`/`sig2` 的 `PACKAGE_PIN`/`IOSTANDARD` 已按本仓库默认板级方案写死在
  [`constraints/dpm_top.xdc`](constraints/dpm_top.xdc)（`sig1 → W18`、`sig2 → W19`，LVCMOS33）。
  这两个引脚在 `xc7z010clg400-1` 上属于 bank 34 的差分对 `L22`（`IO_L22P_T3_34` / `IO_L22N_T3_34`），
  这里当两路独立单端输入用；换板卡时按实际引脚与 bank 供电（LVCMOS33 需要 VCCO=3.3V）改这两行。
  两个信号按异步输入约束（`set_false_path`），PL 内部由带 `ASYNC_REG` 的同步链采样。
  由于 MIO/DDR 等 PS 侧引脚在没有 board part 时仍无法解析，`NSTD-1`/`UCIO-1` 的降级 hook 依然保留。
- IP 的 AXI4-Lite 从口没有突发与传输宽度概念：每笔读写各一次握手，读/写响应恒为 `OKAY`，
  未映射地址读回 0、写被忽略。IP-XACT 里声明的 `MAX_BURST_LENGTH=1` 与「读写各一笔未完成事务」
  只是把能力如实写全；PS 侧经协议转换器发出的 AXI3 访问会被拆成单拍 AXI4-Lite 传输。
- 测量结果只有一份（`resultDepth = 1`）：新结果会覆盖尚未读走的旧结果，并把 `LOST_COUNT` 加一，
  软件用 `RESULT_SEQ` 跳变或 `LOST_COUNT` 判断是否漏读；`CTRL.clear`（bit2）是复位结果寄存器与
  标志位的正确方式。
