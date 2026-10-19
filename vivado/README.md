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

1. 先用 sbtn 生成两个 IP 需要的 SystemVerilog（每个模块只有一份产物，仿真与综合共用）：
   `sbtn -batch "runMain DigitalPhaseMeter.EmitDigitalPhaseMeterTop"`、
   `sbtn -batch "runMain DigitalPhaseMeter.EmitMmcmDrpController"`。

2. Vivado 在 PATH 中（本项目在 **Vivado 2025.2** 上验证）；默认器件 `xc7z010clg400-1`，写在
   `package_ip.tcl` / `package_src_ip.tcl` / `create_bd.tcl` 顶部的 `part` 变量里，换器件时三处同步改。
3. `build/vivado/` 与 `build/chisel/` 下都是构建产物，不入库。

## 脚本与执行顺序

| 脚本 | 作用 | 产物 |
| ---- | ---- | ---- |
| `package_ip.tcl` | 读入生成的 RTL，打包成 `xjkp.org:dpm:dpm_top:1.0`；按端口名推断 AXI4-Lite 从口，补齐时钟/复位/中断接口与 4KB 地址块 | `build/vivado/ip/dpm_top/` |
| `package_src_ip.tcl` | 把 `MmcmDrpController` 打包成 `xjkp.org:dpm:dpm_src_drp:1.0`（AXI4-Lite + DRP 口 + `locked`/`mmcm_reset`） | `build/vivado/ip/dpm_src_drp/` |
| `create_bd.tcl` | 建 PS7 + AXI 互联 + 两个 IP + 两个 MMCM 的 BD；连接时钟/复位/中断/信号，分配地址，配置 PS 串口，`validate_bd_design` 后生成 wrapper | `build/vivado/bd_proj/` |
| `build_bitstream.tcl` | 先写综合/实现策略（`synth_1` 与 IP 的 OOC run 两处），再综合、实现、导出 bitstream/XSA；`-strategy-only` 只写策略不跑 run | `dpm_system.xsa`、`timing_summary.rpt`、`utilization.rpt`、`drc.rpt` |
| `impl_stability.tcl` | **签核用**：同一网表、不换策略重复 N 次独立实现，报 WNS/WHS 的最小/平均/最大值与失败次数 | `impl_stability_results.csv`、`stability_a<N>_timing.rpt` |
| `impl_multi.tcl` | **调优用**：换 place/route directive 扫策略（会覆盖官方策略，不作签核依据） | `impl_multi_results.csv`、`multi_a<N>_timing.rpt` |
| `query_runs.tcl` | 只读查询 `synth_1` / `impl_1` 的策略与全部 `STEPS.*` 参数 | 终端输出 |
| `constraints/dpm_top.xdc` | 顶层约束：被测信号、片内源时钟、跨域握手、板级引脚 | 源文件 |
| `hook_write_bitstream.tcl` | `write_bitstream` 的 pre-hook：把引脚未约束的两条 DRC 降级为警告（本仓库工程不绑 board part，默认保留） | 源文件 |
| `hook_filter_messages.tcl` | 综合（`SYNTH_DESIGN`）与实现（`INIT_DESIGN`）的 pre-hook：屏蔽厂商 IP 固有告警 | 源文件 |
| `build.sh` | 依次执行前四个脚本的一键封装 | — |

```bash
vivado -mode batch -source vivado/package_ip.tcl         # 顶层测量 IP
vivado -mode batch -source vivado/package_src_ip.tcl     # 片内源 DRP 控制器 IP
vivado -mode batch -source vivado/create_bd.tcl          # 默认 450MHz + uart1
vivado -mode batch -source vivado/build_bitstream.tcl    # 策略 → 综合/实现/bitstream/XSA
vivado -mode batch -source vivado/impl_stability.tcl -tclargs 6   # 同策略 6 次独立实现（签核）
vivado -mode batch -source vivado/query_runs.tcl         # 查看当前 run 参数（只读）
```

**改了 RTL 必须从打包开始按顺序重跑**（两个 IP → BD → 综合实现）；只重跑 `build_bitstream.tcl`
会继续用旧 IP。`create_bd.tcl` 会删除并重建 `build/vivado/bd_proj/`。

参数（与各脚本头部注释一致）：

- `create_bd.tcl [dsp_clk_mhz] [ps_fclk1_mhz] [-uart uart0|uart1|none] [-baud <波特率>]`：第一个
  位置参数是 DSP 域频率（默认 **450**）；给出第二个位置参数时改用 PS 的 `FCLK_CLK1` 直供 DSP 域
  （不建 MMCM，只有 ≤250MHz 才有意义，仅用于对比）；串口默认 `uart1`（MIO48/49）、115200，配完
  回读校验，未生效直接报错。
- `build_bitstream.tcl [-retiming 0|1] [-dsp <个数>] [-strategy-only]`：`-retiming` 默认 **0（off）**；
  `-dsp` 默认 2（给 0 表示不设）；`-strategy-only` 只写策略并打印当前取值，不启动 run。
- `impl_stability.tcl` / `impl_multi.tcl` 用 `-tclargs <N>` 给重复次数（默认 6）。**`-tclargs` 必须
  写在 Vivado 命令行最后**，否则其后的 `-log` / `-journal` 会被当作 Tcl 参数吞掉。

## DSP 域时钟（450MHz）与安全启动

- **为什么不用 PS 的 FCLK_CLK1**：Zynq-7000 的 FCLK 由 PS 的 IO PLL 分频，上限 250MHz，BD 里请求
  更高值会被**静默降到 250MHz**，所以 300MHz 以上必须在 PL 内用 MMCM 倍频；参考时钟取 `FCLK_CLK0`(50MHz)。
- `clk_wiz_0` 的参数：`PRIM_IN_FREQ=50`、请求 450MHz，Wizard 选小数倍频
  `MMCM_CLKFBOUT_MULT_F=20.250`、`MMCM_DIVCLK_DIVIDE=1`、`MMCM_CLKOUT0_DIVIDE_F=2.250`
  → VCO 1012.5MHz、输出 450MHz（450 = 50 × 9，不是整数倍频）；`clk_out1` 直接接
  `dpm_top_0/dspClock`。
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

策略由 [`build_bitstream.tcl`](build_bitstream.tcl) 在启动 run **之前**写入（`-strategy-only` 可只写
不跑）：

| 侧 | 参数 | 值 | 说明 |
| -- | ---- | -- | ---- |
| 综合（`synth_1` **与** IP 的 OOC run 两处都写） | `SYNTH_DESIGN.ARGS.DIRECTIVE` | `PerformanceOptimized` | 以时序为目标做跨层次优化 |
| 综合 | `SYNTH_DESIGN.ARGS.GLOBAL_RETIMING` | `off`（默认；`-retiming 1` 打开） | 计数器写成 `when(clear)/.elsewhen(enable)`，正确映射是 DSP48 的 RSTP+CEP（使能由寄存器直接进控制脚）；打开重定时可能把使能寄存器折回 CARRYIN |
| 综合 | `SYNTH_DESIGN.ARGS.MAX_DSP` | `2` | 两个计数器各 1 个 DSP48E1，并防止其它逻辑被搬进 DSP |
| 实现 | `impl_1` 的 `strategy` | `Performance_Retiming` | phys_opt 用 `AlternateFlowWithRetiming`（DSP 优化 + 重定时），正对「关键路径终点是 DSP 控制脚」 |

要点：

- **BD 工程里 IP 的 OOC 综合 run 不继承 `synth_1` 的参数**。真正含 450MHz 逻辑的是 `dpm_top` 的
  out-of-context run，只改 `synth_1` 等于没改。脚本按 IP 实例名匹配 run 并两处同时写；新建 BD
  工程刚打开时 IP run 还不存在，先 `launch_runs synth_1 -scripts_only` 把它物化出来（BD 管理的
  IP 不能用 `create_ip_run`）。
- **写 run 属性前先比较当前值**：写入相同的值也会让已完成的 run 变成过期状态、白跑一轮。脚本据此
  统计 `STRATEGY_CHANGED`，非 0 时打印 `STRATEGY_FORCE_RERUN` 并强制重跑。
- **`reset_run` 会清掉步骤属性（含 hook）**：顺序必须是「先决策 → 全部 `reset_run` → 再统一挂
  hook」，见 `build_bitstream.tcl` 的 `apply_run_hooks`；同时要 reset IP 的 OOC run（它不是
  `synth_1` 的子步骤）。
- **不要覆盖 `Performance_Retiming` 自带的步骤 directive**：该策略的收益来自 phys_opt 那一步。
- **同一工程不能并发开两个 Vivado**：`launch_runs` 会按当前工程重写 run 脚本，后启动的进程会覆盖
  先生效的策略；跑完用 `runme.log` 的 `Command:` 行核对实际 directive。

### 签核方式与结果

| 脚本 | 分工 |
| ---- | ---- |
| `build_bitstream.tcl` | 官方那一次：写策略 → 综合 → 实现 → bitstream/XSA；`build/vivado/timing_summary.rpt` 是签核报告 |
| `impl_stability.tcl N` | 稳定性签核：不换策略、同网表重复 N 次独立实现；判据是 N 次全为正、0 失败端点 |
| `impl_multi.tcl N` | 调优：换 place/route directive 找更好的组合，会覆盖官方策略，**不作签核依据** |

place/route 有 run-to-run 差异，**单次通过不算签核**；反过来，多次实现数值完全相同说明该策略下
收敛是确定性的。用 `set_clock_uncertainty` 过约束只能用来逼工具，签核要清掉后重新出报告（本设计
不使用过约束）。

2026-10-19 的签核结果（Design State: Routed，`xc7z010clg400-1`）：

| 项 | 结果 |
| -- | ---- |
| 全设计时序 | **WNS +0.291ns / WHS +0.052ns / WPWS +0.067ns**，TNS = THS = 0，**0 个失败端点** |
| DSP 域（`clk_out1_dpm_system_clk_wiz_0_0`，2.222ns） | WNS +0.291ns / WHS +0.122ns / WPWS +0.067ns，0 个失败端点 |
| 整体 WNS 的限制者 | clk_wiz Safe Clock Startup 内部路径 `seq_reg1[7] → clkout1_buf/CE0`（数据 0.837ns，其中布线 0.418ns）；**测量域自身最差路径余量 +0.583ns**（`dspReset` → `phaseCntEnable`），其后是 DSP 累加器的 CEP/RSTP 控制路径（+0.616 / +0.669 / +0.733ns） |
| 稳定性 | `impl_stability.tcl 6`：**6/6 PASS**，6 次数值完全相同（WNS/WHS/DSP WNS/DSP WHS = +0.291 / +0.052 / +0.291 / +0.122） |
| 其它时钟组 | 慢域（`clk_fpga_0`）WNS +11.470ns / WHS +0.052ns；跨域（`set_max_delay 20ns`）dsp→slow +18.209ns、slow→dsp +16.992ns |
| 资源 | Slice LUT 893（5.07%）、Slice Register 1458（4.14%）、DSP 2（2.50%）、Block RAM 0、BUFGCTRL 6（18.75%）、MMCME2_ADV 2（100%） |
| 日志 | 综合 `0 Warnings / 0 Critical Warnings / 0 Errors`，实现同样为 0（`synth_1` / `impl_1` 的 `runme.log`；IP 的 OOC 综合 run 另有 1 条良性 `[Synth 8-3936]`——AXI 32 位地址寄存器只用到低 6 位被 trim） |
| 产物 | `write_bitstream` 成功，`build/vivado/dpm_system.xsa`（含 bitstream） |

`xc7z010` 的 DSP48E1 全寄存器 FMAX 为 464.25MHz（DS187），450MHz 用掉约 97%；Vivado 时序分析
覆盖 DSP 内部与 CEP/RSTP 控制脚，实现后这些路径的余量见上表。

## Block design 结构

```text
processing_system7_0 ──M_AXI_GP0(AXI3)──► axi_interconnect_0 ──M00_AXI──► dpm_top_0/s_axi_lite   0x43C0_0000
        │                                      └──M01_AXI──► dpm_src_drp_0/s_axi_lite            0x43C1_0000
        │  FCLK_CLK0 50MHz ──► proc_sys_reset_0 ──► 互联与两个从口的主时钟/复位
        │                   ├──► clk_wiz_0（MMCM + Safe Clock Startup）──► dpm_top_0/dspClock（450MHz）
        │                   └──► dpm_mmcm_src_0（裸 MMCM）──► dpm_top_0/srcSig1、srcSig2、srcLocked（2×25MHz、90°）
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

| 从口 | 基地址 | 范围 | 用途 |
| ---- | ------ | ---- | ---- |
| `dpm_top_0/s_axi_lite` | `0x43C0_0000` | 4KB | 控制/状态 + 只读测量结果 + 片内源使能/输入选择 |
| `dpm_src_drp_0/s_axi_lite` | `0x43C1_0000` | 4KB | MMCM 寄存器镜像（0x08..0x24 八个）+ LOAD（0x28） |

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

`constraints/dpm_top.xdc` 补充下面几类约束（时钟拓扑本身由 PS7 / clk_wiz 自带 XDC 建立）：

1. **外部被测信号** `extSig_sig1/2`：异步输入，`set_false_path`（IP 内立即进 3 级 `ASYNC_REG`
   同步链，不需要建立/保持关系）。
2. **片内源两路 MMCM 输出**：名字在网表里叫 `clkout0_raw` / `clkout1_raw`，对 DSP 域
   `set_false_path`；`locked` 是准静态电平，用 `set_max_delay -datapath_only 20ns` 进慢域。
3. **慢↔快握手通路**：两个方向都写 `set_max_delay -datapath_only 20ns`。clk_wiz 为对齐相位把
   CLKOUT0 设成 `-T_dsp`，工具按逐沿跨时钟只留约 2.1ns（比真实布线还紧）；真实需求是「一个慢
   时钟周期内到达」（翻转电平 + 同步链），用有界 max_delay 表达。只写一个方向会让另一个方向
   变成新的最差路径。
4. **板级引脚**：`extSig_sig1 → W18`、`extSig_sig2 → W19`，LVCMOS33（bank 34 的差分对 L22 当
   两路独立单端输入，VCCO 需 3.3V）。换板卡时按实际引脚与供电改这两行。
5. **命名兼容**：取时钟一律用通配（如 `clkout*_raw`、`*clk_out1_dpm_system_clk_wiz_0_0*`），
   换实现方式后约束能自动跟上。

**写法要求：xdc 不是完整 tcl**，不支持 `foreach` / `if` 等流程控制（`[Designutils 20-1307]`）。
同一个 xdc 会被评估两次：综合启动时（BD 还是黑盒、时钟都不存在）与实现读网表时。因此取对象一律
「通配符 + `get_clocks -quiet`」、约束命令也带 `-quiet`：综合阶段安静地不适用，实现阶段正常生效。

复核：`check_timing` 无错误项，唯一非零项是 `extSig_sig1/2`「有 false path 但无 input delay」的 MEDIUM 提示（有意为之）。

## 综合 / 实现告警清理

目标（`synth_1` / `impl_1` 主 run）：综合与实现各步骤日志 `0 Warnings / 0 Critical Warnings / 0 Errors`。分两类处理：

1. **根因在本仓库的改设计**：顶层 xdc 里失效的硬取时钟名 → 改成「通配 + `-quiet`」；`clk_wiz_0`
   的 `USE_LOCKED` 改 `false`（端口无消费者）。IP 属性写错会**静默失效**，所以 `create_bd.tcl`
   回读校验并在不生效时 `error` 退出。
2. **厂商 IP 固有、改不动的显式屏蔽**：写进 [`hook_filter_messages.tcl`](hook_filter_messages.tcl)
   （每条注明理由），挂在 `STEPS.SYNTH_DESIGN.TCL.PRE`（综合）与 `STEPS.INIT_DESIGN.TCL.PRE`
   （实现读网表）：
   - `[Synth 8-7129]` × 100：`xlconcat` 的 128 个 `In*` 端口是 IP 内部 HDL 写死的，本设计只用 `In0`；
   - `[Synth 8-7071]` / `[Synth 8-7023]`：PS7 未使能的 DDR/MIO 端口、`proc_sys_reset` 无消费者的输出；
   - `[Synth 8-4446]`：顶层 wrapper 无输出引脚（对外通路全在 PS 侧）；
   - `[Opt 31-32]`：clk_wiz 的参考时钟来自 fabric，内部 IBUFG 被优化；
   - `[Netlist 29-160]`（CRITICAL）× ~100：PS7 自带 xdc 给不存在的 MIO/DDR 引脚设属性。

屏蔽只针对厂商 IP、不屏蔽自研 IP；`validate_bd_design` 与 xsim 顶层用例仍是接口漏连的守门人。

hook 必须 `add_files -fileset utils_1` 登记（否则报 `[Runs 36-537]`），且要在所有 `reset_run`
之后挂（`reset_run` 会恢复步骤属性）。`write_bitstream` 的 DRC 降级挂在
`STEPS.WRITE_BITSTREAM.TCL.PRE`（run 在独立进程中做 DRC，主进程里设 SEVERITY 不生效）。

仍在日志里的：`open_project` 内部的 96 条 `[Board 49-26]`（未安装 board 的扫描提示）早于脚本
任何语句，`set_msg_config` 拦不到，只能接受（绑定 board part 的工程不会出现）；IP 打包阶段的
`[IP_Flow 19-*]` 是提示级，不影响接口推断与地址块，`ipx::check_integrity` 通过即正常。

## 已知限制与注意事项

- **脚本工程不绑定 board part**（引脚全部由顶层 xdc 直接给出，不依赖板级文件）：`hook_write_bitstream.tcl`
  因此把 `NSTD-1`（IO 标准未指定）/ `UCIO-1`（引脚未约束）两条 DRC 降级为警告；换用其它板卡时按
  实际补板级约束，并删掉该 hook 与 `build_bitstream.tcl` 里的挂载。
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
