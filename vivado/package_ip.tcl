# 把 DigitalPhaseMeterTop 打包成 Vivado IP
#
# 用法（vivado 在 PATH 中，或写全路径）：
#   vivado -mode batch -source vivado/package_ip.tcl
#
# 前置条件：先用 sbtn 生成 SystemVerilog：
#   sbtn -batch "runMain DigitalPhaseMeter.EmitDigitalPhaseMeterTop"
#
# 产物：
#   build/vivado/ip/dpm_top/     IP 目录（component.xml + 导入的 RTL 源码），可被工程直接引用
#   build/vivado/ip_proj/        打包过程使用的临时工程
#
# IP 对外接口（只有一个 AXI 从口）：
#   s_axi_lite    AXI4-Lite 从口，控制/状态寄存器 + 测量结果寄存器（4KB 寄存器区）
#   clock         主时钟（低速/AXI 域，PS FCLK_CLK0，50MHz），关联 s_axi_lite
#   dspClock      高速测量时钟（DSP 域，由 BD 内 MMCM 从 FCLK_CLK0 倍频，默认 450MHz）
#   resetn        异步复位输入，低有效，与主时钟同步后供 PL 内部使用
#   interrupt     测量完成中断，高有效电平
#   extSig_sig1 / extSig_sig2   被测信号（sig1 为参考信号）
#
# 数据通道说明：一次测量的结果只有 4 个字，结果直接锁存在 ControlRegs 的只读结果
# 寄存器里（RESULT_GATE/EDGE/PHASE/SEQ），PS 收到中断后读 4 个寄存器即可。
# 因此 IP 内没有 BRAM、没有第二个 AXI 从口，也没有任何突发传输。

# 本脚本可能被 dpm-vivado-flow 的包装脚本 source（包装脚本会设置 argv）；
# 直接 source 时 argv 可能不存在，这里在参数解析前兜底，保证脚本可被 source。
if {![info exists argv]} { set argv {} }

set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
# 生成产物在 <Name>/ 下（单一产物：复位同步用纯 RTL 的 ResetSynchronizer，仿真与综合共用同一份）
set rtl_dir [file join $root_dir build chisel DigitalPhaseMeterTop]
set build_dir [file join $root_dir build vivado]
set ip_root [file join $build_dir ip]
set proj_dir [file join $build_dir ip_proj]

# 目标器件：Zynq-7000（打包本身与器件无关，这里只是给临时工程一个 part）
set part "xc7z010clg400-1"

# 把 Vivado 的工作目录放到 build 下，避免在仓库根目录留下 .Xil 等临时文件
file mkdir $build_dir
cd $build_dir
set vendor "xjkp.org"
set library "dpm"
set ip_name "dpm_top"
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
create_project -force dpm_ip_pack $proj_dir -part $part
read_verilog -sv $sources

# ---- 打包流程的提示级告警过滤（在本仓库无法/不值得修，逐条给出理由）----
#   [IP_Flow 19-5101] 用 SystemVerilog 顶层打包「不完全支持」的提示：本 IP 就是 SV 产物，
#                     ipx::check_integrity 通过、BD 可正常使用，属已知限制。
#   [IP_Flow 19-11888] 组件 description 默认与 name 相同：打包器在 package_project 时就检查，
#                     而真实描述只能在拿到 core 之后设置（下一行就设置了），这条是时序噪声。
#   [IP_Flow 19-11770] 时钟接口还没有 FREQ_HZ：FREQ_HZ 在接口推断之后才补（见下方时钟接口段），
#                     推断时会先报一次。
#   [IP_Flow 19-2187] 没有 Product Guide 文件：IP 用法在本仓库 README/skill 中说明，
#                     不额外维护 IP-XACT 的产品文档。
#   注意：[IP_Flow 19-5661]（dspClock 没有关联总线）由 ipx::check_integrity 内部打印，而
#   package_project 之后 ipx 会重置消息规则——挂在文件前部的同一规则不生效，必须放到
#   check_integrity 调用之前（见文件末尾）。
foreach id {
    {IP_Flow 19-5101}
    {IP_Flow 19-11888}
    {IP_Flow 19-11770}
    {IP_Flow 19-2187}
} {
    set_msg_config -id $id -suppress
}

# 只删自己这个 IP 的目录：ip_root 下还有 package_src_ip.tcl 打的 dpm_src_drp，
# 整个 ip_root 删掉会让「先 package_ip、后 package_src_ip」的顺序变成隐式陷阱
file delete -force [file join $ip_root $ip_name]
# 按 Xilinx 约定把 IP 放在 <ip_root>/<ip_name>/ 下
ipx::package_project -root_dir [file join $ip_root $ip_name] -vendor $vendor -library $library -taxonomy "/UserIP" \
    -import_files -set_current true
set_property company_url "https://github.com/24bit-xjkp/DigitalPhaseMeter" [ipx::current_core]
set core [ipx::current_core]
set_property name $ip_name $core
set_property version $ip_version $core
set_property display_name "DigitalPhaseMeter 数字相位计顶层" $core
set_property description "PS 与 PL 互联的数字相位计 IP：AXI4-Lite 控制/状态与测量结果寄存器（4KB 寄存器区）、测量完成中断。测量结果直接锁存在只读结果寄存器中（GPIO 式数据通道），PS 收到中断后读回，不使用片内存储或突发传输。主时钟域复位由顶层同步链从 resetn 生成。" $core

# ---- AXI 从口：按端口名推断 ----
ipx::infer_bus_interfaces "xilinx.com:interface:aximm_rtl:1.0" $core

# 突发长度与未完成事务数按实现能力声明：寄存器口无突发（每次传输 1 拍），
# 读、写各只支持一笔未完成事务
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

# ---- 时钟、复位与中断接口 ----
ipx::infer_bus_interface clock "xilinx.com:signal:clock_rtl:1.0" $core
ipx::infer_bus_interface dspClock "xilinx.com:signal:clock_rtl:1.0" $core
ipx::infer_bus_interface resetn "xilinx.com:signal:reset_rtl:1.0" $core
ipx::infer_bus_interface interrupt "xilinx.com:signal:interrupt_rtl:1.0" $core

set clock_bif [ipx::get_bus_interfaces clock -of_objects $core]
set dsp_clock_bif [ipx::get_bus_interfaces dspClock -of_objects $core]
set reset_bif [ipx::get_bus_interfaces resetn -of_objects $core]
set interrupt_bif [ipx::get_bus_interfaces interrupt -of_objects $core]

# 主时钟关联唯一的 AXI 从口与复位；高速时钟只驱动测量核心
set param [ipx::add_bus_parameter -quiet "FREQ_HZ" $clock_bif]
set_property value 50000000 $param
set_property value_resolve_type user $param
set param [ipx::add_bus_parameter -quiet "ASSOCIATED_BUSIF" $clock_bif]
set_property value "s_axi_lite" $param
set param [ipx::add_bus_parameter -quiet "ASSOCIATED_RESET" $clock_bif]
set_property value "resetn" $param

# dspClock 的实际频率由 BD 里的 MMCM 决定（create_bd.tcl 的 dsp_clk_mhz，默认 450MHz）。
# 这里声明的 FREQ_HZ 只是 IP 侧的默认/声明值，与 create_bd.tcl 的默认频率保持一致；
# 改了 create_bd.tcl 的目标频率时，此处可同步改（不影响 BD 里的 MMCM 输出）。
set param [ipx::add_bus_parameter -quiet "FREQ_HZ" $dsp_clock_bif]
set_property value 450000000 $param
set_property value_resolve_type user $param
# 高速时钟域不需要外部复位：它的复位由 IP 内部从主复位同步得到。
# 若保留打包器自动加上的 ASSOCIATED_RESET，BD 会认为同一个 resetn 同时服务于
# 两个不同频率的时钟域并给出 CRITICAL WARNING。
set param [ipx::get_bus_parameters -quiet "ASSOCIATED_RESET" -of_objects $dsp_clock_bif]
if {$param ne ""} {ipx::remove_bus_parameter "ASSOCIATED_RESET" $dsp_clock_bif}

# 复位接口只声明极性：总线关联统一写在时钟接口的 ASSOCIATED_BUSIF / ASSOCIATED_RESET 上
# （与 Xilinx 自带 IP 一致）。若复位接口也写 ASSOCIATED_BUSIF，BD 会把 resetn 当成第二个
# 时钟关联，报 [BD 41-1732] 总线与多个时钟引脚关联。
set param [ipx::add_bus_parameter -quiet "POLARITY" $reset_bif]
set_property value "ACTIVE_LOW" $param

set param [ipx::add_bus_parameter -quiet "SENSITIVITY" $interrupt_bif]
set_property value "LEVEL_HIGH" $param

# 关联 AXI 从口与主时钟（同时生成 CLK_DOMAIN 参数）
ipx::associate_bus_interfaces -busif "s_axi_lite" -clock "clock" $core
ipx::associate_bus_interfaces -busif "interrupt" -clock "clock" $core

# ---- 地址块 ----
# 只有一个从口：4KB 寄存器区（控制/状态寄存器 + 只读测量结果寄存器）
# 打包器默认会给每个从口加一个 4GB 的 reg0 地址块，这里删掉，只保留真实的地址块
set mm [ipx::add_memory_map -quiet "s_axi_lite" $core]
set_property slave_memory_map_ref "s_axi_lite" [ipx::get_bus_interfaces s_axi_lite -of_objects $core]
set reg0 [ipx::get_address_blocks -quiet "reg0" -of_objects $mm]
if {$reg0 ne ""} {ipx::remove_address_block "reg0" $mm}
set ab [ipx::add_address_block -quiet "Reg" $mm]
set_property range 4K $ab
set_property width 32 $ab
set_property usage register $ab
set_property access read-write $ab

ipx::create_xgui_files $core
ipx::update_checksums $core
# [IP_Flow 19-5661]（dspClock 没有关联总线）由 check_integrity 内部打印；过滤必须挂在这里——
# package_project 之后 ipx 会重置消息规则，挂在文件前部的同一规则不生效（实测）。dspClock 只
# 驱动测量逻辑、不驱动 AXI 总线，该提示无害，check_integrity 仍会通过。
set_msg_config -id "IP_Flow 19-5661" -suppress
ipx::check_integrity $core
ipx::save_core $core

puts "IP_PACKAGE_DONE: [file join $ip_root $ip_name]"
