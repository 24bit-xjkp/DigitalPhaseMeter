# Vivado：IP 打包、PS↔PL 互联与实现

本目录把 PL 侧测量逻辑打包成 Vivado IP，并生成 PS7 + AXI 互联 + 两个 MMCM + 两个自定义 IP 的
block design（BD），完成综合、实现、bitstream 与 XSA 导出。工程已于 2026-10-19 完成验收
（450MHz DSP 域时序签核通过），上板步骤与 PS 侧软件见 [`../docs/bringup.md`](../docs/bringup.md)
与 [`../ps/README.md`](../ps/README.md)。

- 测量 IP（`dpm_top`）对外只有**一个 AXI4-Lite 从口** `s_axi_lite`（4KB 寄存器区）：控制/状态、
  只读测量结果（一次测量 4 个字）、片内源使能与输入选择都在这一段里；没有 BRAM、没有突发传输。
- 片内信号源由 BD 里的**裸 MMCM**（[`rtl/dpm_mmcm_src.v`](rtl/dpm_mmcm_src.v)，`MMCME2_ADV`
  薄封装）产生两路同频、相位差可调的信号。频率与相位差由 PS 算出 MMCM 的 16 位寄存器值，经第二个
  AXI4-Lite 从口 `dpm_src_drp`（极简 DRP 控制器，地址段 `0x43C1_0000`）搬进 MMCM 的 DRP 口。
- 寄存器偏移/位域的权威描述是 [`../ps/include/dpm_regs.h`](../ps/include/dpm_regs.h)（Chisel 侧对应 `ControlRegs.scala`）。

> 片内源旧方案（clk_wiz 的 `USE_DYN_RECONFIG + PHASE_DUTY_CONFIG`）已删除：它把「频率/相位 → MMCM
> 计数器位域」的定点换算综合成硬件（13,287 LUT site + 38 DSP48E1）。当前方案把换算放在 PS 软件，PL 侧只留 DRP 搬运状态机（编码实现见 `host/dpm_host/sig.py`，与 `dpm_regs.h` 逐位一致）。

## 前提与环境

1. 先用仓库根的 `./mill emit DigitalPhaseMeterTop MmcmDrpController` 生成两个 IP 需要的
   SystemVerilog（每个模块只有一份产物，仿真与综合共用；一条命令生成两个模块）。

2. Vivado 在 PATH 中（本项目在 **Vivado 2025.2** 上验证）；**固定器件 `xc7z010clg400-1`**
   （ALINX AX7010 的芯片）。`create_bd.tcl` 建工程后与 Python 流程的 build 阶段打开工程后
   都会回读校验 `part`，不一致直接报错退出。本仓库不再支持切换器件（要换就三处一起改并
   重跑全流程）。
3. Python `>= 3.12`。`vivado/flow/` 是根 uv workspace 的一个成员，根 `uv sync` 会把它装进
   **工程全局的 `.venv`**（不会为它单独建虚拟环境）；安装后可直接调用 `dpm-vivado-flow`。
4. `build/vivado/`、`build/chisel/` 与 `vivado/flow/.venv` 下都是构建/开发产物，不入库。

## 入口与执行顺序

官方入口是 Python 包 **`dpm-vivado-flow`**（源码在 [`flow/`](flow/)）。Tcl 脚本是它的执行层，
也保留为手工/排障入口；两者消费同一份 `build/chisel/` 产物。

| 入口 / 文件                   | 作用                                                                                                                                                                | 产物                                                                                                       |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `uv run dpm-vivado-flow`      | **官方增量入口**：Python 计算 sha256/stamp/run 指纹，决定复用/重跑；生成 Tcl 计划并调用 Vivado。默认跳过未变化的 IP 打包、BD 重建与 OOC 综合                        | `build/vivado/.flow/`（stamp/计划/结果/日志）、bit/XSA、时序/利用率/DRC/methodology 报告                   |
| `package_ip.tcl`              | 读入生成的 RTL，打包成 `xjkp.org:dpm:dpm_top:1.0`；按端口名推断 AXI4-Lite 从口，补齐时钟/复位/中断接口与 4KB 地址块                                                 | `build/vivado/ip/dpm_top/`                                                                                 |
| `package_src_ip.tcl`          | 把 `MmcmDrpController` 打包成 `xjkp.org:dpm:dpm_src_drp:1.0`（AXI4-Lite + DRP 口 + `locked`/`mmcm_reset`）                                                          | `build/vivado/ip/dpm_src_drp/`                                                                             |
| `create_bd.tcl`               | 建 PS7 + AXI 互联 + 两个 IP + 两个 MMCM 的 BD；连接时钟/复位/中断/信号，分配地址，配置 PS 串口，`validate_bd_design` 后生成 wrapper                                 | `build/vivado/bd_proj/`                                                                                    |
| `flow/tcl/run_build_stage.tcl`| build 阶段的 Tcl worker：按 Python 计划写策略/hook/reset 指定 run，launch/wait，报告、methodology 门禁与 XSA 导出                                                  | 报告、`dpm_system.xsa`、`results/build.txt`                                                                |
| `flow/tcl/refresh_bd.tcl`     | 原位 BD 刷新 worker：`update_ip_catalog` → `upgrade_ip` → `generate_target` → `validate_bd_design` → 地址检查 → 保存；失败由 Python 回退全量重建                     | `results/bd_refresh.txt`                                                                                   |
| `impl_stability.tcl`          | **签核用**：同一网表、不换策略重复 N 次独立实现，报 WNS/WHS 的最小/平均/最大值与失败次数；始终关闭增量实现                                                          | `impl_stability_results.csv`、`stability_a<N>_timing.rpt`                                                  |
| `impl_multi.tcl`              | **调优用**：换 place/route directive 扫策略（会覆盖官方策略，不作签核依据）；始终关闭增量实现                                                                       | `impl_multi_results.csv`、`multi_a<N>_timing.rpt`                                                          |
| `query_runs.tcl`              | 只读查询 `synth_1` / `impl_1` 的策略、全部 `STEPS.*` 参数与增量相关属性                                                                                             | 终端输出                                                                                                   |
| `constraints/dpm_top.xdc`     | 顶层约束：被测信号、片内源时钟、跨域握手、MMCM 启动移位寄存器、板级引脚                                                                                              | 源文件                                                                                                     |
| `hook_filter_messages.tcl`    | 综合（`SYNTH_DESIGN`）与实现（`INIT_DESIGN`）的 pre-hook：屏蔽厂商 IP 固有告警                                                                                       | 源文件                                                                                                     |
| `hook_methodology_waivers.tcl`| 实现 `ROUTE_DESIGN` 的 pre-hook：创建 TIMING-28/47 的 methodology waiver（每条写明理由的已知例外）                                                                  | 源文件                                                                                                     |
| `hook_filter_vendor_ip.tcl`   | 厂商 IP 的 OOC 综合 run 的 pre-hook：屏蔽厂商生成 RTL 固有的 `Netlist 29-345`（只挂厂商 run，自研 IP 的不挂）                                                       | 源文件                                                                                                     |
| `suppress_early_messages.tcl` | `open_project` / 原位 BD 刷新**之前**加载的早期过滤（Board 49-26、Project 1-5713、BD 41-1661 等）                                                                   | 源文件                                                                                                     |

```bash
# 一次性：把 dpm-vivado-flow 装进根 .venv（workspace 成员 + vivado 依赖组）
uv sync

# 日常增量流程（仓库根执行；激活 .venv 后也可直接写 dpm-vivado-flow）
uv run dpm-vivado-flow                 # 默认：只重跑变化的阶段/run
uv run dpm-vivado-flow --status        # 只读：stamp/指纹/DCP/报告/XSA 状态
uv run dpm-vivado-flow --dry-run       # 只生成并打印 Tcl 计划，不启动 Vivado
uv run dpm-vivado-flow --report-only   # 不改工程，只重出报告与 XSA
uv run dpm-vivado-flow --force         # 全量：重打包 + 重建 BD + 全量 reset

# 手工/排障：单独执行构造脚本（每次全量，不经过增量判断）
vivado -mode batch -source vivado/package_ip.tcl
vivado -mode batch -source vivado/package_src_ip.tcl
vivado -mode batch -source vivado/create_bd.tcl

# 签核/查询
vivado -mode batch -source vivado/impl_stability.tcl -tclargs 6
vivado -mode batch -source vivado/query_runs.tcl
```

**改了 RTL 之后**：先跑生成入口（`./mill emit DigitalPhaseMeterTop MmcmDrpController`），再在仓库根运行
`uv run dpm-vivado-flow`。流程会按内容哈希只重新打包变化的 IP、原位刷新 BD（接口不变时），并只重跑受影响的
OOC run / `synth_1` / `impl_1`；需要从零重建时用 `--force`。只手工重跑综合/实现而不重新
打包 IP 仍会用到旧 IP——官方增量入口会自动处理这种依赖顺序。

参数：

- `dpm-vivado-flow`：`--force`、`--dry-run`、`--status`、`--report-only`、`--strategy-only`、
  `--incremental-synth off|on|auto`、`--incremental-impl off|on|auto`、`--retiming 0|1`
  （默认 0=off）、`--dsp <个数>`（默认 2，0 表示不设）、`--uart uart0|uart1|none`（默认 uart1）、
  `--baud <波特率>`（默认 115200）、`--dsp-clk <MHz>`（默认 450）、`--repo-root`、`-v`。
  这些 BD 参数会通过 Python 生成的 Tcl 包装传给 `create_bd.tcl`。
- `create_bd.tcl [dsp_clk_mhz] [ps_fclk1_mhz] [-uart uart0|uart1|none] [-baud <波特率>]`：手工
  入口。第一个位置参数是 DSP 域频率（默认 **450**）；给出第二个位置参数时改用 PS 的
  `FCLK_CLK1` 直供 DSP 域（不建 MMCM，只有 ≤250MHz 才有意义，仅用于对比）；串口默认
  `uart1`（MIO48/49）、115200，配完回读校验，未生效直接报错。
- `impl_stability.tcl` / `impl_multi.tcl` 用 `-tclargs <N>` 给重复次数（默认 6）。**`-tclargs` 必须
  写在 Vivado 命令行最后**，否则其后的 `-log` / `-journal` 会被当作 Tcl 参数吞掉。

## 增量流程（dpm-vivado-flow）

```
dpm-vivado-flow（Python，根 .venv）
  ├─ sha256：filelist.f / 每个 RTL / 打包脚本 / BD 脚本 / MMCM 薄封装 / XDC / hook
  ├─ build/vivado/.flow/*.json：stage stamp、逐 run 输入指纹、Vivado 版本
  ├─ 决策：跳过 / 原位刷新 BD / 全量重建 / 逐 run reset / 增量编译
  ├─ 生成 plans/*.tcl + scripts/*.tcl
  └─ subprocess: vivado -mode batch -source <wrapper> → results/*.txt + logs/*.log
```

复用判据是**输入指纹匹配 + 输出 DCP 存在**，不依赖 run 的 `STATUS`（实测 `Out-of-date`
的厂商 IP run 的 DCP 仍可复用）。stamp/指纹只在阶段成功后原子写入；失败或中断不写，
下次保守重跑。所有状态在 `build/vivado/.flow/` 下，属于构建产物，删除它或 `--force`
都会回到全量路径。

| 变化                                   | 打包              | BD        | `synth_1` | IP OOC run        | `impl_1`             |
| -------------------------------------- | ----------------- | --------- | --------- | ----------------- | -------------------- |
| 无变化                                 | 复用              | 复用      | 复用      | 全部复用          | 复用（不启动 Vivado） |
| 仅 XDC                                 | 复用              | 复用      | 重跑      | 复用              | 重跑                 |
| 仅 `dpm_top` RTL                       | 只重打包 dpm_top  | 原位刷新  | 重跑      | 只 dpm_top        | 重跑                 |
| 仅 `dpm_src_drp` RTL                   | 只重打包该 IP     | 原位刷新  | 重跑      | 只该 IP           | 重跑                 |
| 共享 RTL（如 `Axi4LiteSlave`）         | 两个都重打包      | 原位刷新  | 重跑      | 两个自研 IP       | 重跑                 |
| `create_bd.tcl` / BD 参数 / MMCM 薄封装 | 按需              | 全量重建  | 全部      | 全部              | 重跑                 |
| 策略 / hook                            | 复用              | 复用      | 受影响    | dpm_top / 厂商 run | 重跑                 |
| `--force`                              | 全部              | 全量重建  | 全部      | 全部              | 重跑                 |
| `--report-only`                        | 复用              | 复用      | 复用      | 复用              | 只重出报告/XSA       |

原位刷新（`refresh_bd.tcl`）失败、`validate_bd_design` 出错或地址段不再是
`0x43C00000/4K` / `0x43C10000/4K` 时，Python 会删除工程并走全量重建（日志
`BD_REFRESH_FALLBACK`），不会静默复用坏工程。`generate_target` 可能顺带重建
`auto_pc` 的产物，导致该厂商 run 被动重综合；这是 Vivado 行为，属预期成本。

当前实测（Vivado 2025.2，本机，`uv run dpm-vivado-flow`）：

| 场景                         | 墙钟/结果                                                                                         |
| ---------------------------- | ------------------------------------------------------------------------------------------------- |
| 无改动重跑                   | **约 0.1–0.2 s**，不启动 Vivado；IP/BD/DCP/bit/XSA mtime 不变                                      |
| 单 IP（`dpm_top`）RTL 变化   | 只重打包 dpm_top + 原位刷新 BD + 重跑 dpm_top/auto_pc/synth_1/impl_1；**约 2 min 15 s**             |
| `--force` 全量               | **约 3–4 min**（8 个 IP OOC 与 impl 并行；时序见下方签核表）                                       |
| `--report-only`              | 约 13 s，只打开 `impl_1` 重出报告/XSA                                                              |

增量综合/实现的默认值是 `off`，实测结论：

- **增量实现**（同网表、`RuntimeOptimized`）：`report_incremental_reuse` 显示 cell/net 100% 复用，
  `impl_1 STATS.ELAPSED` 从约 **31 s 降到 26 s**（约 16%），WNS/WHS/WPWS 不变、日志 0 告警；
  但收益低于「≥20%」的默认开启判据，因此保持 opt-in。
- **增量综合**：IP 重打包/原位刷新后，`dpm_top` 与 `auto_pc` 的 OOC run 已被重建、没有同版本参考
  DCP，只有 `synth_1` 真正执行了 `read_checkpoint -incremental`；`synth_1` 耗时约 24 s（普通约
  22 s），没有收益。因此保持默认 off。
- 需要继续试验时用 `--incremental-synth/impl on|auto`；默认值只有在重新测量并通过判据后才改。
- 签核/调优脚本（`impl_stability.tcl` / `impl_multi.tcl`）每次都显式清空增量属性，始终做独立
  非增量实现。`impl_stability.tcl 6` 的 6/6 PASS 结论对应 2026-10-19 网表；RTL 改动后必须在
  新网表上重跑，不能沿用旧结论（单次实现通过也不等于签核通过）。


## DSP 域时钟（450MHz）与安全启动

- **为什么不用 PS 的 FCLK_CLK1**：Zynq-7000 的 FCLK 由 PS 的 IO PLL 分频，上限 250MHz，BD 里请求
  更高值会被**静默降到 250MHz**，所以 300MHz 以上必须在 PL 内用 MMCM 倍频；参考时钟取 `FCLK_CLK0`(50MHz)。
- `clk_wiz_0` 的参数：`PRIM_IN_FREQ=50`、请求 450MHz，Wizard 选小数倍频
  `MMCM_CLKFBOUT_MULT_F=20.250`、`MMCM_DIVCLK_DIVIDE=1`、`MMCM_CLKOUT0_DIVIDE_F=2.250`
  → VCO 1012.5MHz、输出 450MHz（450 = 50 × 9，不是整数倍频）；`clk_out1` 直接接
  `dpm_top_0/dspClock`。
- **`PRIM_SOURCE` 必须是 `No_buffer`**：`clk_in1` 由 fabric 上的 FCLK_CLK0 驱动（不是器件引脚）。
  用默认的 `Single_ended_clock_capable_pin` 时，Wizard 生成的 XDC 会在内部 pin
  `clk_wiz_0/inst/clk_in1` 上再 `create_clock` 一次——而该网络已由 PS7 的 XDC 定义主时钟
  `clk_fpga_0`，实现的方法学检查会报 `TIMING-4`/`TIMING-27` 两条 Critical Warning（主时钟在层次
  pin 上重复定义、覆盖 insertion delay）。改成 `No_buffer` 后 Wizard 不再生成这条 `create_clock`
  （模板中对应行被注释掉），DSP 时钟正确派生在 `clk_fpga_0` 的时钟树下。`create_bd.tcl` 对
  `PRIM_SOURCE` 做回读校验。
- **MMCM 锁定前不放时钟用 clk_wiz 自带的 Safe Clock Startup**（`USE_SAFE_CLOCK_STARTUP=true`）：
  Wizard 把 `CLKOUT1_DRIVES` 限定成 `BUFGCE`，`CE` 由同一 MMCM 输出经 `BUFH` 打拍的 8 bit 移位
  寄存器连续采到 8 拍 `locked` 后拉高，无毛刺放行。不要用「与门 + 组合逻辑」门控时钟（UG949 点名
  的反模式），也不需要外部门控模块。`USE_LOCKED=false`：该端口在本设计中没有消费者，关掉才能消掉
  `[Synth 8-7071] port 'locked' ... unconnected`。`clk_wiz` 的 `reset` 是**高有效**，接
  `proc_sys_reset_0/peripheral_reset`。
- **片内源是独立的第二个 MMCM**：`MMCME2_ADV` 不能直接做 BD cell（`create_bd_cell -type primitive`
  不支持，`-type module -reference MMCME2_ADV` 也解析不了），所以用 `rtl/dpm_mmcm_src.v` 薄封装，
  `add_files` 后以 module reference 例化。初始工作点 2 × 25MHz、相位差 90°（参考 50MHz、M=20、
  DIVCLK=1、CLKOUT 分频 40、VCO 1000MHz）。

## 综合 / 实现策略与签核

策略由 `dpm-vivado-flow` 的 build 阶段（Tcl worker
[`flow/tcl/run_build_stage.tcl`](flow/tcl/run_build_stage.tcl)）在启动 run **之前**写入
（`dpm-vivado-flow --strategy-only` 可只写不跑）：

| 侧                                              | 参数                                | 值                                | 说明                                                                                                                                         |
| ----------------------------------------------- | ----------------------------------- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| 综合（`synth_1` **与** IP 的 OOC run 两处都写） | `SYNTH_DESIGN.ARGS.DIRECTIVE`       | `PerformanceOptimized`            | 以时序为目标做跨层次优化                                                                                                                     |
| 综合                                            | `SYNTH_DESIGN.ARGS.GLOBAL_RETIMING` | `off`（默认；`--retiming 1` 打开） | 计数器写成 `when(clear)/.elsewhen(enable)`，正确映射是 DSP48 的 RSTP+CEP（使能由寄存器直接进控制脚）；打开重定时可能把使能寄存器折回 CARRYIN |
| 综合                                            | `SYNTH_DESIGN.ARGS.MAX_DSP`         | `2`                               | 两个计数器各 1 个 DSP48E1，并防止其它逻辑被搬进 DSP                                                                                          |
| 实现                                            | `impl_1` 的 `strategy`              | `Performance_Retiming`            | phys_opt 用 `AlternateFlowWithRetiming`（DSP 优化 + 重定时），正对「关键路径终点是 DSP 控制脚」                                              |

要点：

- **BD 工程里 IP 的 OOC 综合 run 不继承 `synth_1` 的参数**。真正含 450MHz 逻辑的是 `dpm_top` 的
  out-of-context run，只改 `synth_1` 等于没改。worker 按 IP 实例名匹配 run 并两处同时写；新建 BD
  工程刚打开时 IP run 还不存在，先 `launch_runs synth_1 -scripts_only` 把它物化出来（BD 管理的
  IP 不能用 `create_ip_run`）。
- **写 run 属性前先比较当前值**：写入相同的值也会让已完成的 run 变成过期状态、白跑一轮。Python
  侧的 run 指纹已经覆盖策略/hook；worker 在 reset 前再读一次当前值，发现漂移就把该 run 加入
  实际 reset 列表（结果里的 `STRATEGY_CHANGED` / `RESET_RUNS`）。
- **`reset_run` 会清掉步骤属性（含 hook）**：顺序是「materialize → 差异判定 → reset → 写策略/
  增量属性 → 挂 `TCL.PRE` → launch」。worker 只对本次实际 reset 的 run 写属性；未 reset 的 run
  不碰，避免把已完成 run 标成过期。厂商 IP 的 OOC run 也要单独挂 `hook_filter_vendor_ip.tcl`
  （它不继承 `synth_1` 的 `TCL.PRE`）。
- **物化与 reset 的顺序**：`launch_runs synth_1 -scripts_only` 只能在 reset 之前调用一次；原位
  BD 刷新后如果 `synth_1` 已被标成需要 reset，worker 会先 `reset_run synth_1` 再重试 materialize。
  不要在挂完 hook 之后再次 `scripts_only`（正式 launch 会报 `[Common 17-69]`）。
- **不要覆盖 `Performance_Retiming` 自带的步骤 directive**：该策略的收益来自 phys_opt 那一步。
- **同一工程不能并发开两个 Vivado**：`launch_runs` 会按当前工程重写 run 脚本，后启动的进程会覆盖
  先生效的策略；跑完用 `runme.log` 的 `Command:` 行核对实际 directive。

### 签核方式与结果

| 入口                   | 分工                                                                                                     |
| ---------------------- | -------------------------------------------------------------------------------------------------------- |
| `uv run dpm-vivado-flow` | 官方那一次：策略 → 综合 → 实现 → bitstream/XSA；`build/vivado/timing_summary.rpt` 是签核报告             |
| `impl_stability.tcl N` | 稳定性签核：不换策略、同网表重复 N 次独立实现；判据是 N 次全为正、0 失败端点；每轮显式关闭增量实现          |
| `impl_multi.tcl N`     | 调优：换 place/route directive 找更好的组合，会覆盖官方策略，**不作签核依据**；每轮显式关闭增量实现          |

place/route 有 run-to-run 差异，**单次通过不算签核**；反过来，多次实现数值完全相同说明该策略下
收敛是确定性的。用 `set_clock_uncertainty` 过约束只能用来逼工具，签核要清掉后重新出报告（本设计
不使用过约束）。

2026-10-26 的签核结果（Design State: Routed，`xc7z010clg400-1`；用 HEAD 的 RTL 增量重跑，单次实现）：

| 项                                                   | 结果                                                                                                                                                                                                               |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 全设计时序                                           | **WNS +0.271ns / WHS +0.060ns / WPWS +0.067ns**，TNS = THS = 0，**0 个失败端点**                                                                                                                                   |
| DSP 域（`clk_out1_dpm_system_clk_wiz_0_0`，2.222ns） | WNS +0.271ns / WHS +0.149ns / WPWS +0.067ns，0 个失败端点                                                                                                                                                          |
| 整体 WNS 的限制者                                    | 仍是 clk_wiz Safe Clock Startup 内部路径 `seq_reg1[7] → clkout1_buf/CE0`（+0.271）；**测量域自身最差 setup 路径余量 +0.553ns**（`dspReset` 释放链 → 边沿检测链复位脚；纯数据/控制脚路径 ≥ +0.65ns），DSP 域 hold 最差 +0.149ns |
| 稳定性                                               | 本次为**单次实现**（0 失败端点）；`impl_stability.tcl 6` 的 6/6 PASS 结论对应 2026-10-19 网表，RTL 改动后需按同样方式复跑                                                                                          |
| 其它时钟组                                           | 慢域（`clk_fpga_0`）WNS +12.611ns / WHS +0.060ns；跨域（`set_max_delay 20ns`）dsp→slow +18.116ns、slow→dsp +15.750ns                                                                                               |
| 资源                                                 | Slice LUT 878（4.99%）、Slice Register 1426（4.05%）、DSP 2（2.50%）、Block RAM 0、BUFGCTRL 6（18.75%）、MMCME2_ADV 2（100%）                                                                                      |
| 日志                                                 | IP 打包 / BD / 综合/实现主 run 与 8 个 IP OOC run **全部 0 Warnings / 0 Critical Warnings / 0 Errors**；实现 methodology 报告 `Checks found: 0 / Checks waived: 13`，**无 Critical Warning**                       |
| 产物                                                 | `write_bitstream` 成功，`build/vivado/dpm_system.xsa`（含 bitstream，2026-10-26 刷新）                                                                                                                              |

`xc7z010` 的 DSP48E1 全寄存器 FMAX 为 464.25MHz（DS187），450MHz 用掉约 97%；Vivado 时序分析
覆盖 DSP 内部与 CEP/RSTP 控制脚，实现后这些路径的余量见上表。

## Block design 结构

```text
processing_system7_0 ──M_AXI_GP0(AXI3)──► axi_interconnect_0 ──M00_AXI──► dpm_top_0/s_axi_lite   0x43C0_0000
        │                                      └──M01_AXI──► dpm_src_drp_0/s_axi_lite            0x43C1_0000
        │  FCLK_CLK0 50MHz ──► proc_sys_reset_0 ──► 互联与两个从口的主时钟/复位
        │                   ├──► clk_wiz_0（MMCM + Safe Clock Startup）──► dpm_top_0/dspClock（450MHz）
        │                   └──► dpm_mmcm_src_0（裸 MMCM）──► dpm_top_0/intSig_sig1、intSig_sig2、srcLocked（2×25MHz、90°）
        │                              ▲ DRP ◄── dpm_src_drp_0（MMCM 寄存器镜像搬运）
        └◄── IRQ_F2P[0] ◄── xlconcat_0 ◄── dpm_top_0/interrupt

extSig_sig1 / extSig_sig2：BD 外部端口 → dpm_top_0（顶层 XDC 约束到 W18 / W19）
PS 串口：默认 UART1 = MIO48/49（`-uart` 可换 UART0 = MIO14/15 或 none）
dpm_top_0 内部：外部引脚与片内源经 SignalInputMux 二选一，选择/使能寄存器同在主时钟域
```

要点：

- PS7 的 `M_AXI_GP0` 是 **AXI3**（ID 宽 12、最大突发 16），两个从口均为 AXI4-Lite，因此用
  `axi_interconnect` 负责地址译码并由它插入 `auto_pc` 完成协议转换（`ID_WIDTH=12`、
  `TRANSLATION_MODE=2`）；只有一个从口时用 `axi_protocol_converter` 更省。
- `M_AXI_GP0_ACLK` 必须显式接 `FCLK_CLK0`；中断是 `IRQ_F2P[0]`，`xlconcat` 的 `NUM_PORTS=16`
  只接 `In0`，未连接的输入由 IP 当作 0。
- 复位：IP 的 `resetn` 是低有效异步复位，接 `proc_sys_reset_0/peripheral_aresetn`。每个时钟域
  各有一个 `ResetSynchronizer` 生成域内高有效复位：主时钟域的在顶层，DSP 域的在 `Controller`
  内部（从 `mainReset` 再同步，输入取反后接入）。
- 地址用 `assign_bd_address -offset ... -range 4K [get_bd_addr_segs <ip>/s_axi_lite/Reg]
  -target_address_space /processing_system7_0/Data` 分配；收尾 `validate_bd_design` 必须
  0 ERROR / 0 CRITICAL WARNING。
- 片内源的两路 MMCM 输出直接作为被测信号进 `dpm_top`（不在测量域做数字分频），与外部引脚共用
  同一条异步采样链，由 `SIG_CTRL.srcSelect` 切换。

## 地址映射

| 从口                       | 基地址        | 范围 | 用途                                             |
| -------------------------- | ------------- | ---- | ------------------------------------------------ |
| `dpm_top_0/s_axi_lite`     | `0x43C0_0000` | 4KB  | 控制/状态 + 只读测量结果 + 片内源使能/输入选择   |
| `dpm_src_drp_0/s_axi_lite` | `0x43C1_0000` | 4KB  | MMCM 寄存器镜像（0x08..0x24 八个）+ LOAD（0x28） |

测量 IP 的寄存器偏移/位域以 [`../ps/include/dpm_regs.h`](../ps/include/dpm_regs.h) 为准（Vitis 里
对应 `XPAR_DPM_TOP_0_S_AXI_LITE_BASEADDR` / `XPAR_DPM_SRC_DRP_0_S_AXI_LITE_BASEADDR`）。DRP 控制器
寄存器：`0x00 ID`（0x4450_4D03）、`0x04 STATUS`（b0 locked、b1/b2 busy）、`0x08` 起 8 个 MMCM
寄存器镜像、`0x28 LOAD`（W1S）。

## PS 侧（裸机）访问示例

```c
#include "xil_io.h"
#include "dpm_regs.h"   /* 偏移/位域权威定义；对应 XPAR_* 宏见 xparameters.h */

static inline void dpm_write(u32 off, u32 v) { Xil_Out32(DPM_BASE + off, v); }
static inline u32  dpm_read(u32 off)         { return Xil_In32(DPM_BASE + off); }

/* 启动一次测量：CTRL 的 bit1 是普通读写位，必须与 start 一起写，否则会关掉中断使能 */
void dpm_start(u32 gate_target)
{
    dpm_write(DPM_REG_GATE_TARGET, gate_target);
    dpm_write(DPM_REG_CTRL, DPM_CTRL_IRQ_ENABLE | DPM_CTRL_START);
}

/* 选中片内信号源；频率/相位差由 PS 先写 MMCM 寄存器镜像、LOAD 并等 locked，再在这里使能并选择 */
void dpm_select_internal_source(void)
{
    dpm_write(DPM_REG_SIG_CTRL, DPM_SIG_ENABLE | DPM_SIG_SELECT);
    while ((dpm_read(DPM_REG_SIG_STATUS) & (DPM_SIGST_LOCKED | DPM_SIGST_READY)) !=
           (DPM_SIGST_LOCKED | DPM_SIGST_READY)) { }
}

void dpm_select_external_source(void) { dpm_write(DPM_REG_SIG_CTRL, 0u); }

/* 中断服务程序：清挂起位，读走 4 个结果寄存器（结果只有一份，不需要循环取多条记录） */
void dpm_irq_handler(void)
{
    dpm_write(DPM_REG_IRQ_STATUS, DPM_IRQ_PENDING);

    u32 gate_cnt  = dpm_read(DPM_REG_RESULT_GATE);
    u32 edge_cnt  = dpm_read(DPM_REG_RESULT_EDGE);
    u32 phase_cnt = dpm_read(DPM_REG_RESULT_PHASE);
    u32 seq       = dpm_read(DPM_REG_RESULT_SEQ);
    /* 相位差（弧度）≈ 2π × phase_cnt / (T_sig / T_dsp) / edge_cnt */
    (void)gate_cnt; (void)edge_cnt; (void)phase_cnt; (void)seq;

    dpm_write(DPM_REG_IRQ_STATUS, DPM_IRQ_RESULT_READ);   /* 写 b1：结果已读走 */
}
```

写 MMCM 寄存器镜像的流程：写 `0x08..0x24` 中要改的寄存器 → 写 `0x28 = 1` → 轮询 `0x04.b0` 等
重新锁定。**重配置期间两路输出停止**，只能在测量空闲时改；DRP 写要求 MMCM 处于复位（控制器已按
此实现）。相位编码（`HIGH_TIME`/`LOW_TIME`/`EDGE`/`NO_COUNT`/`PHASE_MUX`/`DELAY_TIME`）参考实现见
`host/dpm_host/sig.py` 与 `ps/include/dpm_regs.h`；相位网格是 1/8 VCO 周期（`45/D` 度），9 位相位码
的可表示上界随 `D` 缩小，越界请求由协议层显式拒绝（`dpm_mmcm_phase_code_fits`）。

## 顶层约束（XDC）要点

`constraints/dpm_top.xdc` 补充下面几类约束（时钟拓扑本身由 PS7 / clk_wiz 自带 XDC 建立；
`clk_wiz` 用 `No_buffer` 后 DSP 时钟派生在 `clk_fpga_0` 树下）：

1. **外部被测信号** `extSig_sig1/2`：异步输入，`set_false_path`（IP 内立即进 3 级 `ASYNC_REG`
   同步链，不需要建立/保持关系）。
2. **片内源两路 MMCM 输出**：名字在网表里叫 `clkout0_raw` / `clkout1_raw`，对 DSP 域
   `set_false_path`；`locked` 是准静态电平，用 `set_max_delay -datapath_only 20ns` 进慢域。
3. **慢↔快握手通路**：两个方向都写 `set_max_delay -datapath_only 20ns`。clk_wiz 为对齐相位把
   CLKOUT0 设成 `-T_dsp`；按逐沿跨时钟检查只留约 2.1ns（比真实布线还紧）。真实需求是「一个慢
   时钟周期内到达」（翻转电平 + 同步链），用有界 max_delay 表达。只写一个方向会让另一个方向
   变成新的最差路径。`No_buffer` 之后 DSP 时钟与 `clk_fpga_0` 在工具眼里是同步时钟对，这四组
   max_delay 会命中 `TIMING-47` 告警——属设计意图，由 `hook_methodology_waivers.tcl` 显式 waiver。
4. **片内源 MMCM 的 Safe Clock Startup 移位寄存器**：`dpm_mmcm_src.v` 的 `seq_reg1/2` 时钟来自
   MMCM 输出（经 BUFH），异步清零与 `locked` 输入都是准静态控制信号，用
   `set_false_path -to *dpm_mmcm_src_0/inst/seq_reg*`（与 clk_wiz 自带 XDC 的同类约束一致），
   不做逐沿检查。
5. **板级引脚**：`extSig_sig1 → W18`、`extSig_sig2 → W19`，LVCMOS33（bank 34 的差分对 L22 当
   两路独立单端输入，VCCO 需 3.3V）。换板卡时按实际引脚与供电改这两行。
6. **命名兼容**：取时钟一律用通配（如 `clkout*_raw`、`*clk_out1_dpm_system_clk_wiz_0_0*`），
   换实现方式后约束能自动跟上。这样写会命中 `TIMING-28`（按名字引用 auto-derived 时钟）的
   Warning，同样是刻意的取舍，由 `hook_methodology_waivers.tcl` waiver 并在报告中保留计数。

**写法要求：xdc 不是完整 tcl**，不支持 `foreach` / `if` 等流程控制（`[Designutils 20-1307]`）。
同一个 xdc 会被评估两次：综合启动时（BD 还是黑盒、时钟都不存在）与实现读网表时。因此取对象一律
「通配符 + `get_clocks -quiet`」、约束命令也带 `-quiet`：综合阶段安静地不适用，实现阶段正常生效。

复核：`check_timing` 无错误项，唯一非零项是 `extSig_sig1/2`「有 false path 但无 input delay」的 MEDIUM 提示（有意为之）。

## 综合 / 实现告警清理

目标：整个脚本流程（IP 打包 → BD → 综合 → 实现）日志 **0 Warnings / 0 Critical Warnings / 0 Errors**。
分三类处理：

### 1) 根因在本仓库的 → 改设计

- `clk_wiz_0` 的 `PRIM_SOURCE` 由默认的引脚类型改成 `No_buffer`：消除 `TIMING-4`/`TIMING-27`
  两条 **Critical Warning**（内部 pin 上重复定义主时钟），DSP 时钟树回到 `clk_fpga_0` 下。
- `vivado/rtl/dpm_mmcm_src.v`：`BUFGCE` 显式写 `SIM_DEVICE("7SERIES")`（消 `Netlist 29-345`）；
  `reset | drpReset` 先在 DCLK 域同步两拍再驱动 MMCM.RST 与启动移位寄存器 CLR（消 `LUTAR-1`）。
- `Axi4LiteSlave` 的 AXI4-Lite 端口固定 32 位，只把下游实际使用的地址/数据位交给命令总线
  （`addrUsedWidth`/`dataUsedWidth`），高位在从口处丢弃、不再零扩展：消掉**自研 IP** OOC 综合的
  `Synth 8-3936`×4（dpm_top 的 awAddr 32→6、dpm_src_drp 的 awAddr 32→12 / wData 32→16 / wStrb 4→2；
  厂商 IP 内部仍有 3 条 INFO 级同名提示，不是告警）。
- `clk_wiz_0` 的 `USE_LOCKED=false`（端口无消费者的既有修复）。
- IP/run 属性写错会**静默失效**，因此 `create_bd.tcl` 对 `part` / `PRIM_SOURCE` / `USE_LOCKED`
  回读校验，不生效直接 `error`。

### 2) 厂商 IP 固有、改不动的 → 显式屏蔽（每条在文件里写明理由）

| hook 文件                     | 挂载位置                                                                     | 屏蔽内容                                                                                                                                                                          |
| ----------------------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `hook_filter_messages.tcl`    | `synth_1/SYNTH_DESIGN`、`impl_1/INIT_DESIGN`                                 | `Synth 8-7129`×100（xlconcat 写死的 In*）、`8-7071`/`8-7023`（PS7/proc_sys_reset 未用端口）、`8-4446`、`Opt 31-32`、`Netlist 29-160`(CRITICAL)×~100（PS7 xdc 给未使能引脚设属性） |
| `hook_filter_vendor_ip.tcl`   | **厂商 IP 的 OOC 综合 run**（名称排除 dpm_top / dpm_src_drp / dpm_mmcm_src） | `Netlist 29-345`（clk_wiz 生成的 BUFGCE 未覆盖 Vivado 2025.2 的默认 `SIM_DEVICE`）                                                                                                |
| `suppress_early_messages.tcl` | 各脚本 `open_project` **之前** source                                        | `Board 49-26`×96、`Project 1-5713`、`Vivado 12-7122`、`Constraints 18-550`、`IP_Flow 19-3664`（OOC run reset 后的中间状态）                                                       |

早期过滤**必须在 `open_project` 之前**加载才有效（Board 49-26 等在 `open_project` 内部打印，放到它之后加载就来不及）。IP 的 OOC run 是独立进程、不继承 `synth_1` 的 `TCL.PRE`，必须
单独挂；且只有 reset 过的 run 才会在 `launch_runs` 时重新生成脚本、带上后挂的 hook（否则脚本
停在「物化」时生成的版本，实测 `clk_wiz` 的厂商过滤就这样漏过一次）。

### 3) 报告层的刻意例外 → methodology waiver

实现 `route_design` 步骤末尾会自动跑 `report_methodology`。以下两类是设计上刻意保留、改约束反而
更危险/更不语义化的告警，由 `hook_methodology_waivers.tcl`（挂 `impl_1/ROUTE_DESIGN.TCL.PRE`）
创建 waiver；实现 run 的 `*_methodology_drc_routed.rpt`（并复制为 `build/vivado/methodology.rpt`）显示 **Checks found: 0 / Checks waived: 13**，不再出现 Critical：

- `TIMING-28`×7：顶层 xdc 用「通配时钟名 + `-quiet`」引用 auto-derived 时钟。这是为了让同一份
  XDC 同时适配「综合黑盒阶段」与「实现阶段」并兼容 IP 改名；改成 `get_pins -of_objects` 会把
  约束绑定到更深的层次名、失效风险更大。
- `TIMING-47`×6：片内源 MMCM 运行期由 DRP 改相位/分频，与 `clk_fpga_0` 没有固定同步沿关系；
  慢↔DSP 握手与 `locked` 都用有界 `set_max_delay -datapath_only 20ns`（保留布线延迟检查），
  工具按「同步时钟对」方式提示，属预期。

waiver 是**报告条目**不是 message：`set_msg_config`/hook 屏蔽不了，必须在 run 进程里、
`report_methodology` 之前创建；`reset_run` 会清掉，所以 `impl_stability.tcl` / `impl_multi.tcl`
每轮 reset 之后都会重挂。`dpm-vivado-flow` 的 build worker 在实现完成后解析该 run 的
methodology 报告，若仍有未 waive 的 Critical Warning 会直接 `error`（签核门禁）。

### 关于 `[IP_Flow 19-5661]`

`package_ip.tcl` 里 `ipx::check_integrity` 会打印 `[IP_Flow 19-5661]`（`dspClock` 没有关联 bus
interface）。该提示的过滤规则**必须挂在 `check_integrity` 调用之前**：`ipx::package_project`
之后 ipx 会重置消息规则，挂在文件前部的同一规则不生效（实测）。`dpm_top` 的 `dspClock` 只驱动
测量逻辑、不驱动 AXI 总线，提示本身无害，`check_integrity` 通过。

> GUI 里手工打开工程仍会看到 96 条 `[Board 49-26]`（早期过滤只在脚本流程里生效）。另外同一工作
> 目录下连续跑多个 Vivado 脚本会互相覆盖默认的 `vivado.log`，排障时用 `-log <文件>` 区分
> （注意 `-log` / `-journal` 必须放在 `-tclargs` 之前）。

## Python 流程的测试

```bash
uv run pytest vivado/flow/tests -v      # 纯 Python：哈希/stamp/Tcl 计划/复用决策/日志解析
uv run black --check vivado/flow
uv run isort --check-only vivado/flow
```

单元测试不启动 Vivado；Vivado 相关的冷/热/单 IP 改动/`--force`/`--report-only` 场景按上面的
复用矩阵手工执行，并把实际耗时、WNS/WHS、所有 run 的 `runme.log` 告警计数与 methodology
`Checks found / Checks waived` 写进交付报告。

## 已知限制与注意事项

- **工程只绑定器件、不绑定 board part**：固定 `xc7z010clg400-1`（AX7010），Vivado 里没有该板的
  board 文件；board-store 的 `Board 49-26` 噪声由 `suppress_early_messages.tcl` 在脚本流程里过滤。
  引脚全部由顶层 xdc 给出（`extSig_sig1/2 = W18/W19, LVCMOS33`），不依赖板级文件。
  `write_bitstream` 的 DRC 实测 0 违例，不需要把 `NSTD-1`/`UCIO-1` 降级为警告。
- `extSig_sig1/2` 的引脚在顶层 xdc 里写死为 W18/W19、LVCMOS33；换板按实际引脚与 bank 供电改。
- 测量结果只有一份（`resultDepth = 1`）：新结果会覆盖未读走的旧结果并累加 `LOST_COUNT`；用
  `RESULT_SEQ` 跳变或 `LOST_COUNT` 判断漏读，`CTRL.clear` 是复位结果与标志的正确方式。
- IP 的 AXI4-Lite 从口没有突发、读写各一笔未完成事务，响应恒为 `OKAY`；未映射地址读回 0、写被忽略。
- **DRP 写必须让 MMCM 处于复位**：控制器时序是 `RST=1`（≥8 个 DCLK）→ 8 笔 DRP 写 → `RST=0`
  → 重新锁定；不拉 RST 时「分频字立即生效、相位字不生效」。重配置期间两路输出停止。
- 相位编码要点：`m = round(phase_mdeg × D / 45000)`（9 位），`DELAY_TIME = m >> 3`、
  `PHASE_MUX = m & 7`；`ClkReg1` 的 bit12 按实测生成值恒为 1；`divide == 1` 时看 `NO_COUNT`，
  不能把 HIGH/LOW_TIME 相加；分频硬上限 `D = 126`（`ps/include/dpm_calc.h::DPM_SIG_DIV_MAX`、
  `host/dpm_host/sig.py::DIV_MAX`、`spec/gen_golden.py::DIV_HARD_MAX` 三处一致）。
- `xc7z010` 只有 2 个 MMCM，本设计两个都用掉（DSP 域时钟 + 片内源），没有余量再加第三个时钟域。
- 工具细节与全部实测坑见 [`.agents/skills/dpm-vivado-ip/SKILL.md`](../.agents/skills/dpm-vivado-ip/SKILL.md)；
  同一工程不能并发开两个 Vivado，`-tclargs` 必须写在命令行最后（见上文）。
