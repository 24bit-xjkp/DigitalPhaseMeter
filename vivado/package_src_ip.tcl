# 把 MmcmDrpController（极简 MMCM DRP 控制器）打包成 Vivado IP
#
# 用法（vivado 在 PATH 中，或写全路径）：
#   vivado -mode batch -source vivado/package_src_ip.tcl
#
# 前置条件：先用 sbtn 生成综合产物 SystemVerilog：
#   sbtn -batch "runMain DigitalPhaseMeter.EmitMmcmDrpController --type=syn"
#
# 产物：
#   build/vivado/ip/dpm_src_drp/   IP 目录（component.xml + 导入的 RTL 源码）
#   build/vivado/src_ip_proj/      打包过程使用的临时工程
#
# 背景（见 build/analysis/RESOURCE_ANALYSIS.md）：
#   原来片内信号源用 clk_wiz 的 `USE_DYN_RECONFIG + PHASE_DUTY_CONFIG`，它会把
#   「毫度/百分比/MHz → MMCM 计数器位域」的定点换算综合成硬件，实测占
#   13,287 个物理 LUT site 与 38 个 DSP48E1。本 IP 把换算搬到 PS 软件，
#   PL 侧只留「把软件给的 16 位寄存器值按顺序写进 MMCM DRP 口」的状态机，
#   实测 114 LUT / 182 FF / 0 DSP。
#
# IP 对外接口：
#   s_axi_lite   AXI4-Lite 从口（4KB）：11 个寄存器（ID/STATUS/8 个 MMCM 寄存器/LOAD）
#   clock        主时钟（AXI 与 DRP 同域，接 PS FCLK_CLK0 = 50MHz），关联 s_axi_lite
#   resetn       异步复位输入，低有效
#   drp_daddr / drp_di / drp_den / drp_dwe / drp_dout / drp_drdy
#                MMCM 动态重配置口，接 vivado/rtl/dpm_mmcm_src.v 的对应端口
#   locked       MMCM 锁定指示（异步电平，IP 内同步后进 STATUS.b0）

set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
# 综合产物（--type=syn）在 <Name>/syn/ 下（见 dpm-chisel-rtl 的 sim/syn 分目录约定）
set rtl_dir [file join $root_dir build chisel MmcmDrpController syn]
set build_dir [file join $root_dir build vivado]
set ip_root [file join $build_dir ip]
set proj_dir [file join $build_dir src_ip_proj]

set part "xc7z010clg400-1"

file mkdir $build_dir
cd $build_dir
set vendor "xjkp.org"
set library "dpm"
set ip_name "dpm_src_drp"
set ip_version "1.0"

if {![file exists [file join $rtl_dir filelist.f]]} {
    error "找不到 $rtl_dir/filelist.f，请先运行 sbtn 生成入口（见文件头注释）"
}

# 读取 filelist.f（firtool 的拓扑顺序即编译顺序）
set sources {}
set fp [open [file join $rtl_dir filelist.f] r]
foreach line [split [read $fp] "\n"] {
    set line [string trim $line]
    if {$line ne ""} {
        lappend sources [file join $rtl_dir $line]
    }
}
close $fp

file delete -force $proj_dir
create_project -force dpm_src_ip_pack $proj_dir -part $part
read_verilog -sv $sources

file delete -force [file join $ip_root $ip_name]
ipx::package_project -root_dir [file join $ip_root $ip_name] -vendor $vendor -library $library -taxonomy "/UserIP" \
    -import_files -set_current true
set_property company_url "https://github.com/24bit-xjkp/DigitalPhaseMeter" [ipx::current_core]
set core [ipx::current_core]
set_property name $ip_name $core
set_property version $ip_version $core
set_property display_name "片内信号源 MMCM 动态重配置控制器" $core
set_property description "极简 MMCM DRP 控制器：把 PS 软件算好的 16 位 MMCM 寄存器值按固定顺序写进 MMCM 的 DRP 口（POWER/CLKFBOUT/CLKOUT0/CLKOUT1/DIVCLK 共 8 笔），硬件不含任何定点算术。替代 clk_wiz 在 USE_DYN_RECONFIG + PHASE_DUTY_CONFIG 下生成的重配置参考设计（后者实测 13287 LUT + 38 DSP，本 IP 114 LUT + 0 DSP）。寄存器映射：0x00 ID、0x04 STATUS、0x08..0x24 八个 MMCM 寄存器、0x28 LOAD(W1S)。" $core

# ---- AXI 从口：按端口名推断（s_axi_lite_*）----
ipx::infer_bus_interfaces "xilinx.com:interface:aximm_rtl:1.0" $core
foreach {ifname max_burst} {s_axi_lite 1} {
    set bif [ipx::get_bus_interfaces $ifname -of_objects $core]
    if {$bif eq ""} {
        error "接口推断失败：找不到 $ifname"
    }
    foreach {param value} [list MAX_BURST_LENGTH $max_burst NUM_READ_OUTSTANDING 1 NUM_WRITE_OUTSTANDING 1] {
        set bifparam [ipx::add_bus_parameter -quiet $param $bif]
        set_property value $value $bifparam
        set_property value_source constant $bifparam
    }
}

# ---- 时钟与复位接口 ----
ipx::infer_bus_interface clock "xilinx.com:signal:clock_rtl:1.0" $core
ipx::infer_bus_interface resetn "xilinx.com:signal:reset_rtl:1.0" $core

set clock_bif [ipx::get_bus_interfaces clock -of_objects $core]
set reset_bif [ipx::get_bus_interfaces resetn -of_objects $core]

# 主时钟关联唯一的 AXI 从口与复位
set param [ipx::add_bus_parameter -quiet "FREQ_HZ" $clock_bif]
set_property value 50000000 $param
set_property value_resolve_type user $param
set param [ipx::add_bus_parameter -quiet "ASSOCIATED_BUSIF" $clock_bif]
set_property value "s_axi_lite" $param
set param [ipx::add_bus_parameter -quiet "ASSOCIATED_RESET" $clock_bif]
set_property value "resetn" $param

# 复位接口只声明极性（不要写 ASSOCIATED_BUSIF，否则 BD 报 41-1732）
set param [ipx::add_bus_parameter -quiet "POLARITY" $reset_bif]
set_property value "ACTIVE_LOW" $param

# 关联 AXI 从口与主时钟（同时生成 CLK_DOMAIN 参数）
ipx::associate_bus_interfaces -busif "s_axi_lite" -clock "clock" $core

# ---- 地址块：删掉打包器默认的 reg0（4GB），换成真实的 4KB ----
set mm [ipx::get_memory_maps s_axi_lite -of_objects $core]
if {[ipx::get_address_blocks reg0 -of_objects $mm] ne ""} {
    ipx::remove_address_block reg0 $mm
}
ipx::add_address_block Reg $mm
set_property -dict [list "BASE_ADDRESS" {0x43C10000} "RANGE" {4K} "WIDTH" {32}] \
    [ipx::get_address_blocks Reg -of_objects $mm]

ipx::create_xgui_files $core
ipx::update_checksums $core
ipx::check_integrity $core
ipx::save_core $core

puts "PACKAGE_SRC_IP_DONE: $ip_root/$ip_name"
close_project
