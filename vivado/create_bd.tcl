# 创建 PS7 + AXI 协议转换器 + MMCM + dpm_top IP 的 block design
#
# 用法：
#   vivado -mode batch -source vivado/create_bd.tcl [dsp_clk_mhz] [ps_fclk1_mhz]
#
#   dsp_clk_mhz  —— DSP 域时钟频率（MHz），默认 400（MMCM 倍频；本仓库实测在 xc7z010clg400-1
#                   上综合/实现收敛，域内 WNS ≥ +0.236ns，见 vivado/README.md）
#   ps_fclk1_mhz —— 给定时改用 PS 的 FCLK_CLK1 直供 DSP 域（不建 MMCM、不用 dsp_clk_mhz），
#                   用于对比「PS 直供」与「MMCM 倍频」两条路。FCLK_CLK1 上限 250MHz，
#                   因此这条路只能覆盖 250MHz 及以下。
#
# 前置条件：先运行 vivado/package_ip.tcl 生成 IP（build/vivado/ip/dpm_top）
#
# 结构（IP 只有一个 AXI4-Lite 从口，互联不再需要地址译码）：
#
#   processing_system7_0 ──M_AXI_GP0(AXI3)──► axi_protocol_converter_0 ──M_AXI(AXI4-Lite)──► dpm_top_0/s_axi_lite
#          │  FCLK_CLK0(50MHz) ──► proc_sys_reset_0 ─► 协议转换器与 IP 的主时钟/复位
#          │                    └► clk_wiz_0(MMCM + Safe Clock Startup) ─► dpm_top_0/dspClock
#          └◄── IRQ_F2P ◄── xlconcat_0 ◄── dpm_top_0/interrupt（IRQ_F2P[0]）
#
#   sig1 / sig2 作为 BD 外部端口引出，供顶层约束分配引脚
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
# 为什么用协议转换器而不是 axi_interconnect：从口只剩一个，没有地址译码需求，
# 只需要把 Zynq M_AXI_GP0 的 AXI3（ID 宽 12、最大突发 16）转成 IP 的 AXI4-Lite。
# axi_protocol_converter 正是 auto_pc 用的那个 IP，且不会引入互联的额外面积与延迟。
#
# 地址分配：s_axi_lite 0x43C0_0000（4KB 寄存器区，控制/状态寄存器 + 只读结果寄存器）

set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
set build_dir [file join $root_dir build vivado]
set proj_dir [file join $build_dir bd_proj]
set ip_repo [file join $build_dir ip]

set part "xc7z010clg400-1"

# DSP 域目标时钟频率（MHz），默认 400；默认走 PL 内的 MMCM（PS 的 FCLK 分频点够不到 300MHz 以上）。
set dsp_clk_mhz 400.0
if {[llength $argv] >= 1 && [lindex $argv 0] ne ""} { set dsp_clk_mhz [lindex $argv 0] }
set ps_fclk1_mhz ""
if {[llength $argv] >= 2 && [lindex $argv 1] ne ""} {
    set ps_fclk1_mhz [lindex $argv 1]
    set dsp_clk_mhz $ps_fclk1_mhz
}
set use_mmcm [expr {$ps_fclk1_mhz eq ""}]

# 把 Vivado 的工作目录放到 build 下，避免在仓库根目录留下 .Xil 等临时文件
file mkdir $build_dir
cd $build_dir
set bd_name "dpm_system"
set ip_vlnv "xjkp.org:dpm:dpm_top:1.0"
set lite_base "0x43C00000"

if {![file exists [file join $ip_repo dpm_top component.xml]]} {
    error "找不到打包好的 IP：$ip_repo/dpm_top，请先运行 vivado/package_ip.tcl"
}

file delete -force $proj_dir
create_project -force dpm_bd $proj_dir -part $part
set_property ip_repo_paths $ip_repo [current_project]
update_ip_catalog -rebuild

create_bd_design $bd_name
current_bd_design $bd_name

# ---- PS7：使能 M_AXI_GP0、FCLK 与 PL 中断 ----
# FCLK_CLK0 取 50MHz：既作 AXI/低速控制域时钟（闸门计数、FSM 都跑在这个域），
# 又作 MMCM 的参考时钟。50MHz 参考下 400MHz 输出的实际参数是 M=20、O=2.5、
# DIVCLK=1（VCO 1000MHz，在 7 系列 -1 的 600~1200MHz 范围内，见生成的 clk_wiz
# 例化）；AXI4-Lite 寄存器访问不需要更快的时钟。
create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 processing_system7_0
set ps7_config [list \
    CONFIG.PCW_USE_M_AXI_GP0 {1} \
    CONFIG.PCW_USE_M_AXI_GP1 {0} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {50} \
    CONFIG.PCW_USE_FABRIC_INTERRUPT {1} \
    CONFIG.PCW_IRQ_F2P_INTR {1}]
if {$use_mmcm} {
    # DSP 域时钟由 PL 内 MMCM 生成，FCLK_CLK1 不需要
    lappend ps7_config CONFIG.PCW_EN_CLK1_PORT {0}
} else {
    lappend ps7_config CONFIG.PCW_EN_CLK1_PORT {1}
    lappend ps7_config CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ $ps_fclk1_mhz
}
set_property -dict $ps7_config [get_bd_cells processing_system7_0]

# ---- 复位：把 PS 的 FCLK_RESET0_N 交给 proc_sys_reset 生成同步复位 ----
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_0

# ---- AXI3(PS M_AXI_GP0) → AXI4-Lite(IP) 的协议转换 ----
# ID_WIDTH 取 PS 的 12 位，让转换器把 ID 一起接上（ID_WIDTH=0 时从口根本没有 ID 信号，
# C_IGNORE_ID=1，PS 的 ID/BID/RID 会被悬空）；TRANSLATION_MODE=2 即转换到 AXI4-Lite，
# 与 Vivado 自动插入的 auto_pc 配置一致（MI_PROTOCOL=AXI4LITE 时它也是唯一合法值）。
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_protocol_converter:2.1 axi_protocol_converter_0
set_property -dict [list \
    CONFIG.SI_PROTOCOL {AXI3} \
    CONFIG.MI_PROTOCOL {AXI4LITE} \
    CONFIG.DATA_WIDTH {32} \
    CONFIG.ADDR_WIDTH {32} \
    CONFIG.ID_WIDTH {12} \
    CONFIG.TRANSLATION_MODE {2}] [get_bd_cells axi_protocol_converter_0]

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
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_protocol_converter_0/aclk]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_protocol_converter_0/aresetn]
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

# ---- AXI 数据通路：PS → 协议转换器 → IP 唯一的从口 ----
connect_bd_intf_net [get_bd_intf_pins processing_system7_0/M_AXI_GP0] [get_bd_intf_pins axi_protocol_converter_0/S_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_protocol_converter_0/M_AXI] [get_bd_intf_pins dpm_top_0/s_axi_lite]

# ---- 中断 ----
connect_bd_net [get_bd_pins dpm_top_0/interrupt] [get_bd_pins xlconcat_0/In0]
connect_bd_net [get_bd_pins xlconcat_0/dout] [get_bd_pins processing_system7_0/IRQ_F2P]

# ---- 被测信号 ----
connect_bd_net [get_bd_ports sig1] [get_bd_pins dpm_top_0/sig1]
connect_bd_net [get_bd_ports sig2] [get_bd_pins dpm_top_0/sig2]

# ---- 地址分配：只有一个 4KB 寄存器段 ----
assign_bd_address -quiet -target_address_space /processing_system7_0/Data \
    -offset $lite_base -range 4K [get_bd_addr_segs dpm_top_0/s_axi_lite/Reg] -force

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
