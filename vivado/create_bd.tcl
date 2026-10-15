# 创建 PS7 + AXI 协议转换器 + MMCM + dpm_top IP 的 block design
#
# 用法：
#   vivado -mode batch -source vivado/create_bd.tcl [dsp_clk_mhz] [ps_fclk1_mhz] [-uart <uart0|uart1|none>] [-baud <波特率>]
#
#   dsp_clk_mhz  —— DSP 域时钟频率（MHz），默认 400（MMCM 倍频；本仓库实测在 xc7z010clg400-1
#                   上综合/实现收敛，域内 WNS ≥ +0.236ns，见 vivado/README.md）
#   ps_fclk1_mhz —— 给定时改用 PS 的 FCLK_CLK1 直供 DSP 域（不建 MMCM、不用 dsp_clk_mhz），
#                   用于对比「PS 直供」与「MMCM 倍频」两条路。FCLK_CLK1 上限 250MHz，
#                   因此这条路只能覆盖 250MHz 及以下。
#   -uart        —— 板载调试串口，默认 uart1：
#                     uart1 → MIO48(tx)/49(rx)（Bank1，本仓库 AX7010 的板载 CP2102N 接法）
#                     uart0 → MIO14(tx)/15(rx)（Bank0，多数板卡为 3.3V）
#                     none  → 不给 PS 配串口（MIO 保持全 unassigned）
#                   MIO 号与 bank 供电是板级信息，换板卡时按原理图选；改完脚本会回读校验。
#   -baud        —— 串口波特率，默认 115200
#
#   例：`create_bd.tcl 300` = 300MHz；`create_bd.tcl 0 150` = PS 直供 150MHz；
#       `create_bd.tcl -uart uart0` = 默认 400MHz + MIO14/15 串口。
#
# 前置条件：先运行 vivado/package_ip.tcl 与 vivado/package_src_ip.tcl 生成两个 IP
#   （build/vivado/ip/dpm_top、build/vivado/ip/dpm_src_drp）
#
# 综合/实现策略（DSP 域高速时钟需要的那些 run 参数）由 build_bitstream.tcl 在启动 run 前内联设置，
#   本脚本只负责把 BD 建对，不再需要单独的 apply_timing_strategy.tcl。
#
# 结构（两个 AXI4-Lite 从口：测量 IP 与片内信号源的 MMCM 动态重配置口）：
#
#   processing_system7_0 ──M_AXI_GP0(AXI3)──► axi_interconnect_0 ──M00_AXI(AXI4-Lite)──► dpm_top_0/s_axi_lite
#          │                                        └──M01_AXI(AXI4-Lite)──► dpm_src_drp_0/s_axi_lite（DRP）
#          │  FCLK_CLK0(50MHz) ──► proc_sys_reset_0 ─► 互联与两个从口的主时钟/复位
#          │                    ├► clk_wiz_0(MMCM + Safe Clock Startup) ─► dpm_top_0/dspClock
#          │                    └► dpm_mmcm_src_0(裸 MMCM) ─► dpm_top_0/srcSig1、srcSig2、srcLocked
#          │                          ▲
#          │                          └── DRP ◄── dpm_src_drp_0（极简 DRP 控制器，方案 A）
#          └◄── IRQ_F2P ◄── xlconcat_0 ◄── dpm_top_0/interrupt（IRQ_F2P[0]）
#
#   sig1 / sig2 作为 BD 外部端口引出，供顶层约束分配引脚
#
# 片内信号源（方案 A：裸 MMCM + 极简 DRP 控制器）：
#   - 两路输出同频（初始 25MHz）、相位差 90°，**频率与相位差都由 PS 写成 MMCM 的
#     16 位寄存器值**，再由 dpm_src_drp_0 逐条写进 MMCM 的 DRP 口；
#   - 两路输出送给 dpm_top_0，由 IP 在测量时钟域里采样边沿并做后分频（SIG_DIV）；
#   - 地址段 0x43C1_0000，寄存器映射见 vivado/package_src_ip.tcl 的说明与
#     build/analysis/golden/mmcm_encode.py（编码参考实现）。
#
# 不再使用 clk_wiz 的 `USE_DYN_RECONFIG + PHASE_DUTY_CONFIG`：那套配置会把
# 「毫度/百分比/MHz → MMCM 计数器位域」的定点换算综合成硬件，实测占 13,287 个
# 物理 LUT site 与 38 个 DSP48E1（整设计 96% 的逻辑 LUT）。
#
# 为什么互联改成 axi_interconnect（原先只有一个从口时用 axi_protocol_converter）：
#   从口多于一个时互联要负责地址译码，axi_interconnect 会自动插入 auto_pc 完成
#   AXI3(M_AXI_GP0) → AXI4-Lite 的协议转换，因此不再需要单独的协议转换器。
#
# 为什么 DSP 域时钟要过 MMCM，而不是直接用 PS 的 FCLK_CLK1：
#   Zynq-7000 的 FCLK_CLK1 由 PS 的 IO PLL 分频得到，频率被限制在 250MHz 以内
#   （Vivado 会把更高的请求静默降到 250MHz，不报错），因此 300MHz 以上的 DSP 域时钟
#   必须在 PL 内用 MMCM 倍频。MMCM 的参考时钟用 FCLK_CLK0（50MHz，与 AXI/控制域同源，
#   锁相稳定），倍频后作为 dpm_top_0/dspClock。
#
# 为什么不用「与门 + util_vector_logic」门控、也不用自写的 BUFGCE 模块：
#   组合逻辑门控时钟会让时钟走 fabric 布线（BUFG → LUT → 各时钟脚），7 系列上这是
#   UG949 点名的反模式。正确做法是用专用全局时钟使能缓冲 BUFGCE，而 clk_wiz 自带的
#   Safe Clock Startup（CONFIG.USE_SAFE_CLOCK_STARTUP，见下）本来就例化 BUFGCE 并
#   自动等 MMCM 锁定，因此不需要外部再加一级门控。
#
# 地址分配：
#   dpm_top_0/s_axi_lite   0x43C0_0000（4KB：控制/状态寄存器 + 只读结果寄存器 + 输入选择/后分频）
#   dpm_src_drp_0/s_axi_lite  0x43C1_0000（4KB：MMCM 寄存器镜像 + LOAD，见 package_src_ip.tcl）

set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
set build_dir [file join $root_dir build vivado]
set proj_dir [file join $build_dir bd_proj]
set ip_repo [file join $build_dir ip]

set part "xc7z010clg400-1"

# ---- 参数：前两个位置参数保持原义，串口用 -uart / -baud 具名给出 ----
# DSP 域目标时钟频率（MHz），默认 400；默认走 PL 内的 MMCM（PS 的 FCLK 分频点够不到 300MHz 以上）。
set dsp_clk_mhz 400.0
set ps_fclk1_mhz ""
set uart_sel "uart1"
set uart_baud 115200
set positional {}
for {set i 0} {$i < [llength $argv]} {incr i} {
    set arg [lindex $argv $i]
    switch -- $arg {
        -uart { incr i ; set uart_sel [lindex $argv $i] }
        -baud { incr i ; set uart_baud [lindex $argv $i] }
        default { lappend positional $arg }
    }
}
if {[llength $positional] >= 1 && [lindex $positional 0] ne ""} { set dsp_clk_mhz [lindex $positional 0] }
if {[llength $positional] >= 2 && [lindex $positional 1] ne ""} {
    set ps_fclk1_mhz [lindex $positional 1]
    set dsp_clk_mhz $ps_fclk1_mhz
}
set use_mmcm [expr {$ps_fclk1_mhz eq ""}]

# 串口选择：MIO 号与方向是板级信息（Zynq UART0 = MIO14(tx)/15(rx)、UART1 = MIO48(tx)/49(rx)）。
set uart ""
set uart_periph ""
set mio_tx -1
set mio_rx -1
switch -- $uart_sel {
    uart0 { set uart "UART0" ; set uart_periph "UART 0" ; set mio_tx 14 ; set mio_rx 15 }
    uart1 { set uart "UART1" ; set uart_periph "UART 1" ; set mio_tx 48 ; set mio_rx 49 }
    none  { }
    default { error "-uart 只接受 uart0 / uart1 / none（收到 '$uart_sel'）" }
}

# 把 Vivado 的工作目录放到 build 下，避免在仓库根目录留下 .Xil 等临时文件
file mkdir $build_dir
cd $build_dir
set bd_name "dpm_system"
set ip_vlnv "xjkp.org:dpm:dpm_top:1.0"
set lite_base "0x43C00000"
set drp_base "0x43C10000"

if {![file exists [file join $ip_repo dpm_top component.xml]]} {
    error "找不到打包好的 IP：$ip_repo/dpm_top，请先运行 vivado/package_ip.tcl"
}
if {![file exists [file join $ip_repo dpm_src_drp component.xml]]} {
    error "找不到打包好的 IP：$ip_repo/dpm_src_drp，请先运行 vivado/package_src_ip.tcl"
}
if {![file exists [file join $script_dir rtl dpm_mmcm_src.v]]} {
    error "找不到 MMCM 薄封装：vivado/rtl/dpm_mmcm_src.v"
}

file delete -force $proj_dir
create_project -force dpm_bd $proj_dir -part $part
set_property ip_repo_paths $ip_repo [current_project]
update_ip_catalog -rebuild

# 裸 MMCM 的薄封装：MMCME2_ADV 是 unisim 原语，create_bd_cell 不接受 -type primitive，
# 也不能用 -type module -reference 直接引用（实测 [filemgmt 56-587]），因此用 HDL 模块
# 包一层再加进工程，BD 里即可用 module reference 例化。
add_files -norecurse [file join $script_dir rtl dpm_mmcm_src.v]
set_property top dpm_mmcm_src [get_filesets sources_1]
update_compile_order -fileset sources_1

create_bd_design $bd_name
current_bd_design $bd_name

# ---- PS7：使能 M_AXI_GP0、FCLK、PL 中断与调试串口 ----
# FCLK_CLK0 取 50MHz：既作 AXI/低速控制域时钟（闸门计数、FSM 都跑在这个域），
# 又作 MMCM 的参考时钟。50MHz 参考下 400MHz 输出的实际参数是 M=20、O=2.5、
# DIVCLK=1（VCO 1000MHz，在 7 系列 -1 的 600~1200MHz 范围内，见生成的 clk_wiz
# 例化）；AXI4-Lite 寄存器访问不需要更快的时钟。
#
# 串口（上板调试用，由 -uart 选择）：默认 UART1 走 MIO48(tx)/49(rx)，115200 8N1。
#   - MIO48/49 属于 MIO Bank1，本板该 bank 供电 1.8V（LVCMOS 1.8V）；
#     MIO Bank0（MIO0..15）供电 3.3V，UART0（MIO14/15）通常就接在这边。
#     Zynq-7000 的 MIO I/O 标准由 bank 供电决定，不可在 PS7 里逐脚编程，因此这里只是
#     把 bank 电压写进 PRESET_*_VOLTAGE 属性，让 .hwh / BSP 与硬件实际供电一致
#     （声明性，不改 MIO 电气特性）。
#   - 使能某个 UART 不会释放另一个：未选中的那个保留为可选调试口（未分配 MIO）。
#   - MIO 树的 54 项字符串要显式改（只设 PCW_UARTx_UARTx_IO 不一定同步更新树），
#     见下面 "MIO 树" 一段，改完脚本会回读校验。
#   - `-uart none` 时下面这段整体跳过，PS 保持无串口（MIO 全 unassigned）。
create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 processing_system7_0
set ps7_config [list \
    CONFIG.PCW_USE_M_AXI_GP0 {1} \
    CONFIG.PCW_USE_M_AXI_GP1 {0} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {50} \
    CONFIG.PCW_USE_FABRIC_INTERRUPT {1} \
    CONFIG.PCW_IRQ_F2P_INTR {1}]
if {$uart ne ""} {
    lappend ps7_config \
        CONFIG.PCW_EN_$uart {1} \
        CONFIG.PCW_${uart}_PERIPHERAL_ENABLE {1} \
        CONFIG.PCW_${uart}_${uart}_IO "MIO $mio_tx .. $mio_rx" \
        CONFIG.PCW_${uart}_BAUD_RATE $uart_baud \
        CONFIG.PCW_MIO_${mio_tx}_PULLUP {enabled} \
        CONFIG.PCW_MIO_${mio_tx}_DIRECTION {out} \
        CONFIG.PCW_MIO_${mio_tx}_SLEW {slow} \
        CONFIG.PCW_MIO_${mio_rx}_PULLUP {enabled} \
        CONFIG.PCW_MIO_${mio_rx}_DIRECTION {in} \
        CONFIG.PCW_MIO_${mio_rx}_SLEW {slow}
}
if {$use_mmcm} {
    # DSP 域时钟由 PL 内 MMCM 生成，FCLK_CLK1 不需要
    lappend ps7_config CONFIG.PCW_EN_CLK1_PORT {0}
} else {
    lappend ps7_config CONFIG.PCW_EN_CLK1_PORT {1}
    lappend ps7_config CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ $ps_fclk1_mhz
}
set_property -dict $ps7_config [get_bd_cells processing_system7_0]

# ---- MIO bank 供电声明（本板：Bank0 = 3.3V、Bank1 = 1.8V）----
# Zynq-7000 的 MIO I/O 标准由 bank 供电轨决定，不能在 PS7 里逐脚编程；这两个
# PRESET 属性只是把供电事实写进 .hwh/BSP。它们只在导入 board preset 时才存在，
# 本工程没有绑板，因此用 catch 保护：设不上也不影响硬件，只提示。
foreach {vp vv} {BANK0 "LVCMOS 3.3V" BANK1 "LVCMOS 1.8V"} {
    if {[catch {set_property CONFIG.PCW_PRESET_${vp}_VOLTAGE $vv [get_bd_cells processing_system7_0]} err]} {
        puts "WARNING: 设置 PCW_PRESET_${vp}_VOLTAGE=$vv 失败（$err）——仅影响声明，不影响硬件"
    }
}

# ---- MIO 树：把选中的两个 MIO 标成对应 UART（格式为 54 项、'#' 分隔）----
# PS7 的 MIO 树是一个 54 项的字符串属性（每项是外设名 / 信号名），
# 只写 PCW_UARTx_UARTx_IO 时 Vivado 不保证同步更新它，因此这里显式改并回读校验。
set ps_cell [get_bd_cells processing_system7_0]
if {$uart ne ""} {
    set mio_periph [split [get_property CONFIG.PCW_MIO_TREE_PERIPHERALS $ps_cell] "#"]
    set mio_sig    [split [get_property CONFIG.PCW_MIO_TREE_SIGNALS $ps_cell] "#"]
    if {[llength $mio_periph] != 54 || [llength $mio_sig] != 54} {
        set mio_periph [lrepeat 54 "unassigned"]
        set mio_sig    [lrepeat 54 "unassigned"]
    }
    lset mio_periph $mio_tx $uart_periph
    lset mio_periph $mio_rx $uart_periph
    lset mio_sig    $mio_tx "${uart_sel}_tx"
    lset mio_sig    $mio_rx "${uart_sel}_rx"
    set_property -dict [list \
        CONFIG.PCW_MIO_TREE_PERIPHERALS [join $mio_periph "#"] \
        CONFIG.PCW_MIO_TREE_SIGNALS [join $mio_sig "#"]] $ps_cell
}

# ---- 回读校验：串口配置必须真的落到 BD 里 ----
set bank0 [get_property -quiet CONFIG.PCW_PRESET_BANK0_VOLTAGE $ps_cell]
set bank1 [get_property -quiet CONFIG.PCW_PRESET_BANK1_VOLTAGE $ps_cell]
if {$uart eq ""} {
    puts "PS7_UART_CHECK: -uart none（未给 PS 配串口）"
} else {
    set uart_en   [get_property CONFIG.PCW_EN_$uart $ps_cell]
    set uart_io   [get_property CONFIG.PCW_${uart}_${uart}_IO $ps_cell]
    set uart_rate [get_property CONFIG.PCW_${uart}_BAUD_RATE $ps_cell]
    set tree_p    [split [get_property CONFIG.PCW_MIO_TREE_PERIPHERALS $ps_cell] "#"]
    set tree_s    [split [get_property CONFIG.PCW_MIO_TREE_SIGNALS $ps_cell] "#"]
    puts "PS7_UART_CHECK: EN_$uart=$uart_en IO='$uart_io' BAUD=$uart_rate"
    puts "PS7_UART_CHECK: MIO$mio_tx=[lindex $tree_p $mio_tx]/[lindex $tree_s $mio_tx]"
    puts "PS7_UART_CHECK: MIO$mio_rx=[lindex $tree_p $mio_rx]/[lindex $tree_s $mio_rx]"
    if {$uart_en != 1} { error "PS7：$uart 未使能" }
    if {[lindex $tree_p $mio_tx] ne $uart_periph || [lindex $tree_p $mio_rx] ne $uart_periph} {
        error "PS7：MIO 树未把 MIO$mio_tx/$mio_rx 指到 $uart_periph"
    }
}
puts "PS7_UART_CHECK: BANK0='$bank0' BANK1='$bank1'"
if {$bank1 ne "LVCMOS 1.8V"} {
    puts "WARNING: PCW_PRESET_BANK1_VOLTAGE 回读为 '$bank1'（期望 LVCMOS 1.8V，仅影响 .hwh/BSP 声明）"
}

# ---- 复位：把 PS 的 FCLK_RESET0_N 交给 proc_sys_reset 生成同步复位 ----
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_0

# ---- 互联：PS 的 M_AXI_GP0 分发给两个 AXI4-Lite 从口 ----
# NUM_SI=1（PS 一个主口）、NUM_MI=2（测量 IP + 片内源的 MMCM 动态重配置口）。
# 互联会自动插入 auto_pc 把 AXI3 转成 AXI4-Lite（ID 宽 12、TRANSLATION_MODE=2），
# 与原先单独使用 axi_protocol_converter 时的配置一致。
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 axi_interconnect_0
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {2}] [get_bd_cells axi_interconnect_0]

# ---- DSP 域时钟 ----
# 两条路：
#   默认（use_mmcm=1）：PL 内 MMCM 用 FCLK_CLK0(50MHz) 倍频出 dsp_clk_mhz，
#     并由 clk_wiz 自带的 Safe Clock Startup 保证 MMCM 锁定前时钟不出来；
#   use_mmcm=0：直接用 PS 的 FCLK_CLK1（由 IO PLL 分频得到，频率点较粗）。
if {$use_mmcm} {
    # 注意：clk_wiz 的 reset 是「高有效」的（CONFIG.RESET_TYPE=ACTIVE_HIGH），接
    # proc_sys_reset 的 peripheral_reset（高有效），不能接 peripheral_aresetn。
    # USE_SAFE_CLOCK_STARTUP=true 即 Clocking Wizard 的 "Safe Clock Startup"：
    #   - 打开后 CLKOUTx_DRIVES 被 Wizard 自动限定成 BUFGCE（不再允许 BUFG 等）；
    #   - 生成的 HDL 为「MMCM 输出 → BUFGCE（CE = seq_reg1[7]）→ clk_out1」，
    #     seq_reg1 是由同一 MMCM 输出经 BUFH 打拍的 8 bit 移位寄存器，逐拍移入
    #     MMCM 的 locked，连续 8 拍锁定后才拉高 CE，从而无毛刺地放行时钟；
    #   - IP 自带的 XDC（<ip>.xdc 与 <ip>_late.xdc）已给 seq_reg 加 set_false_path，
    #     不需要外部约束。
    # 因此不再需要手写的 DpmClockGate.v：原先的链路是 MMCM → BUFG（clk_wiz 默认驱动）
    # → BUFGCE（薄封装模块），现在合成一级 MMCM → BUFGCE，少一级全局缓冲。
    # locked 端口仍保留（USE_LOCKED=true）供调试观察，Safe Clock Startup 内部用的是
    # MMCM 原语的 locked，不依赖这个端口，故 BD 里不再连接它。
    create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 clk_wiz_0
    set_property -dict [list \
        CONFIG.PRIMITIVE {MMCM} \
        CONFIG.PRIM_SOURCE {Single_ended_clock_capable_pin} \
        CONFIG.PRIM_IN_FREQ {50.000} \
        CONFIG.CLKOUT1_REQUESTED_OUT_FREQ $dsp_clk_mhz \
        CONFIG.CLKOUT1_USED {true} \
        CONFIG.CLKOUT2_USED {false} \
        CONFIG.CLKOUT3_USED {false} \
        CONFIG.CLKOUT4_USED {false} \
        CONFIG.CLKOUT5_USED {false} \
        CONFIG.CLKOUT6_USED {false} \
        CONFIG.CLKOUT7_USED {false} \
        CONFIG.USE_LOCKED {true} \
        CONFIG.USE_SAFE_CLOCK_STARTUP {true} \
        CONFIG.USE_RESET {true}] [get_bd_cells clk_wiz_0]
}

# ---- 片内信号源：裸 MMCM + 极简 DRP 控制器（方案 A）----
#
# 【为什么不再用 clk_wiz】
#   原来这里用 clk_wiz 的 `USE_DYN_RECONFIG + PHASE_DUTY_CONFIG`：它会把
#   「毫度/百分比/MHz → MMCM 计数器位域」的定点换算综合成硬件（mmcm_pll_count_calc
#   等函数），实测在 xc7z010clg400-1 上占 13,287 个物理 LUT site 与 38 个 DSP48E1
#   ——整设计 96% 的逻辑 LUT。而厂商 PG065 明确写了 "Enabling this option utilizes
#   DSP resources. By default, this option is deselected to optimize the design for area."
#
#   那套换算是纯整数算术，PS 侧软件完全算得出来。方案 A 把换算搬到软件，
#   PL 侧只留「把软件给的 16 位寄存器值按顺序写进 MMCM DRP 口」的状态机
#   （dpm_src_drp，实测 114 LUT / 182 FF / 0 DSP）。
#   寄存器位域与编码规则见 build/analysis/golden/mmcm_encode.py（已对 clk_wiz 的
#   Write DRP 生成值逐点验证）。
#
# 结构：
#   FCLK_CLK0(50MHz) ──► dpm_mmcm_src(clkIn) ──► clkOut1/clkOut2 ──► dpm_top_0/srcSig1,srcSig2
#                                          └──► locked ──► dpm_src_drp/locked、dpm_top_0/srcLocked
#   dpm_src_drp（AXI4-Lite 从口 @0x43C1_0000）──► DRP 口 ──► dpm_mmcm_src
#
# MMCM 的静态配置与原来 clk_wiz 的默认工作点一致（50MHz 参考、M=20、D=40、25MHz、
# 相位差 90°）；软件在初始化时通过 DRP 改写工作点。
create_bd_cell -type module -reference dpm_mmcm_src dpm_mmcm_src_0

# 极简 DRP 控制器（Chisel 生成 → vivado/package_src_ip.tcl 打包）
create_bd_cell -type ip -vlnv xjkp.org:dpm:dpm_src_drp:1.0 dpm_src_drp_0

# 旧方案（clk_wiz + PHASE_DUTY_CONFIG）保留为注释，便于对照与回退：
#   create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 clk_wiz_src
#   set_property -dict [list CONFIG.PRIMITIVE {MMCM} CONFIG.PRIM_IN_FREQ {50.000} \
#       CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {25.000} CONFIG.CLKOUT2_USED {true} \
#       CONFIG.CLKOUT2_REQUESTED_OUT_FREQ {25.000} CONFIG.CLKOUT2_REQUESTED_PHASE {90.000} \
#       CONFIG.USE_DYN_RECONFIG {true} CONFIG.PHASE_DUTY_CONFIG {true} \
#       CONFIG.USE_LOCKED {true} CONFIG.USE_RESET {true} \
#       CONFIG.USE_SAFE_CLOCK_STARTUP {true}] [get_bd_cells clk_wiz_src]

# ---- 自定义 IP ----
create_bd_cell -type ip -vlnv $ip_vlnv dpm_top_0

# ---- 中断汇聚：IRQ_F2P 为 16 位，用 xlconcat 把 PL 中断接到 bit0 ----
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 xlconcat_0
set_property -dict [list CONFIG.NUM_PORTS {16}] [get_bd_cells xlconcat_0]

# ---- 被测信号引出为外部端口 ----
create_bd_port -dir I sig1
create_bd_port -dir I sig2

# ---- 时钟与复位 ----
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins processing_system7_0/M_AXI_GP0_ACLK]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins proc_sys_reset_0/slowest_sync_clk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_RESET0_N] [get_bd_pins proc_sys_reset_0/ext_reset_in]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_interconnect_0/ACLK]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_interconnect_0/S00_ACLK]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_interconnect_0/M00_ACLK]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_interconnect_0/M01_ACLK]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_interconnect_0/ARESETN]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_interconnect_0/S00_ARESETN]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_interconnect_0/M00_ARESETN]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_interconnect_0/M01_ARESETN]
# 片内源的 MMCM：参考时钟与 AXI 时钟都用 FCLK_CLK0，复位用同域的高有效复位
# MMCM 参考时钟与 DRP 控制器的主时钟（同域：DRP 的 DCLK 直接用 FCLK_CLK0）
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins dpm_mmcm_src_0/clkIn]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins dpm_mmcm_src_0/drpDclk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins dpm_src_drp_0/clock]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins dpm_src_drp_0/resetn]
# MMCM 的复位：peripheral_reset 是高有效（与原来 clk_wiz_src 的 reset 接法一致）
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_reset] [get_bd_pins dpm_mmcm_src_0/reset]
# DRP 连线：控制器 → MMCM
connect_bd_net [get_bd_pins dpm_src_drp_0/drp_daddr] [get_bd_pins dpm_mmcm_src_0/drpDaddr]
connect_bd_net [get_bd_pins dpm_src_drp_0/drp_di] [get_bd_pins dpm_mmcm_src_0/drpDi]
connect_bd_net [get_bd_pins dpm_src_drp_0/drp_den] [get_bd_pins dpm_mmcm_src_0/drpDen]
connect_bd_net [get_bd_pins dpm_src_drp_0/drp_dwe] [get_bd_pins dpm_mmcm_src_0/drpDwe]
connect_bd_net [get_bd_pins dpm_mmcm_src_0/drpDout] [get_bd_pins dpm_src_drp_0/drp_dout]
connect_bd_net [get_bd_pins dpm_mmcm_src_0/drpDrdy] [get_bd_pins dpm_src_drp_0/drp_drdy]
connect_bd_net [get_bd_pins dpm_mmcm_src_0/locked] [get_bd_pins dpm_src_drp_0/locked]
# 两路源信号与锁定指示送给 IP
connect_bd_net [get_bd_pins dpm_mmcm_src_0/clkOut1] [get_bd_pins dpm_top_0/srcSig1]
connect_bd_net [get_bd_pins dpm_mmcm_src_0/clkOut2] [get_bd_pins dpm_top_0/srcSig2]
connect_bd_net [get_bd_pins dpm_mmcm_src_0/locked] [get_bd_pins dpm_top_0/srcLocked]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins dpm_top_0/clock]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins dpm_top_0/resetn]

# MMCM 路：50MHz 参考 → dsp_clk_mhz 输出；复位用 FCLK_CLK0 域的高有效复位。
# dspClock 直接接 clk_wiz 的 clk_out1：锁定前不放时钟由 Wizard 自己的 Safe Clock
# Startup（内部 BUFGCE）负责，BD 里不再有额外的门控单元。
if {$use_mmcm} {
    connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins clk_wiz_0/clk_in1]
    connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_reset] [get_bd_pins clk_wiz_0/reset]
    connect_bd_net [get_bd_pins clk_wiz_0/clk_out1] [get_bd_pins dpm_top_0/dspClock]
} else {
    connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK1] [get_bd_pins dpm_top_0/dspClock]
}

# ---- AXI 数据通路：PS → 互联 → 测量 IP 与片内源 MMCM 的动态重配置口 ----
connect_bd_intf_net [get_bd_intf_pins processing_system7_0/M_AXI_GP0] [get_bd_intf_pins axi_interconnect_0/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_interconnect_0/M00_AXI] [get_bd_intf_pins dpm_top_0/s_axi_lite]
connect_bd_intf_net [get_bd_intf_pins axi_interconnect_0/M01_AXI] [get_bd_intf_pins dpm_src_drp_0/s_axi_lite]

# ---- 中断 ----
connect_bd_net [get_bd_pins dpm_top_0/interrupt] [get_bd_pins xlconcat_0/In0]
connect_bd_net [get_bd_pins xlconcat_0/dout] [get_bd_pins processing_system7_0/IRQ_F2P]

# ---- 被测信号 ----
connect_bd_net [get_bd_ports sig1] [get_bd_pins dpm_top_0/sig1]
connect_bd_net [get_bd_ports sig2] [get_bd_pins dpm_top_0/sig2]

# ---- 地址分配：测量 IP 4KB + 片内源 MMCM 的动态重配置口 4KB ----
assign_bd_address -quiet -target_address_space /processing_system7_0/Data \
    -offset $lite_base -range 4K [get_bd_addr_segs dpm_top_0/s_axi_lite/Reg] -force
assign_bd_address -quiet -target_address_space /processing_system7_0/Data \
    -offset $drp_base -range 4K [get_bd_addr_segs dpm_src_drp_0/s_axi_lite/Reg] -force

regenerate_bd_layout
validate_bd_design
save_bd_design

# ---- 生成 BD 产物与顶层 wrapper ----
set bd_file [get_files ${bd_name}.bd]
generate_target all $bd_file
set wrapper [make_wrapper -files $bd_file -top]
add_files -norecurse $wrapper
set_property top [file rootname [file tail $wrapper]] [current_fileset]
update_compile_order -fileset sources_1

puts "BD_DONE: [get_property name [current_bd_design]]"
puts "BD_ADDRESS_MAP_BEGIN"
foreach seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces processing_system7_0/Data]] {
    puts "  [get_property name $seg] offset=[get_property offset $seg] range=[get_property range $seg]"
}
puts "BD_ADDRESS_MAP_END"
