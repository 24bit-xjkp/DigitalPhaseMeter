# 综合、实现、生成 bitstream 与 XSA
#
# 用法：
#   vivado -mode batch -source vivado/build_bitstream.tcl
#
# 前置条件：先运行 vivado/package_ip.tcl 与 vivado/create_bd.tcl
#
# 产物（build/vivado/ 下）：
#   dpm_system.xsa            含 bitstream 的硬件平台（可用于 Vitis 软件工程）
#   bd_proj/dpm_bd.runs/      综合与实现报告、bit 文件

set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
set build_dir [file join $root_dir build vivado]
set proj_dir [file join $build_dir bd_proj]
set xdc_file [file join $script_dir constraints dpm_top.xdc]
set xsa_file [file join $build_dir dpm_system.xsa]

# 把 Vivado 的工作目录放到 build 下，避免在仓库根目录留下 .Xil 等临时文件
file mkdir $build_dir
cd $build_dir

set proj_file [file join $proj_dir dpm_bd.xpr]
if {![file exists $proj_file]} {
    error "找不到工程 $proj_file，请先运行 vivado/create_bd.tcl"
}

open_project $proj_file

# 加入顶层约束（重复运行时先移除旧副本）
foreach old [get_files -quiet -of_objects [get_filesets constrs_1] {*dpm_top.xdc}] {
    remove_files -quiet $old
}
add_files -fileset constrs_1 -norecurse $xdc_file
set_property target_language Verilog [current_project]

# 本仓库不绑定具体开发板：sig1/sig2 没有引脚位置与 IO 标准约束。
# 把「引脚未约束 / IO 标准未指定」两条 DRC 降级为警告，使 bitstream 仍能生成
# （时序与功能不受影响，只是这些引脚位置未指定）。降级必须放在 write_bitstream
# 步骤的 pre-hook 里：run 在独立进程中执行 DRC，主进程里设置 SEVERITY 不生效。
# 使用具体开发板时填好 vivado/constraints/dpm_top.xdc 里的引脚并去掉这个 hook。
set hook_file [file join $script_dir hook_write_bitstream.tcl]
if {![file exists $hook_file]} {
    error "找不到 $hook_file"
}

# ---- 综合 ----
# 已经完成过的 run 直接复用（例如只重跑 bitstream 步骤），否则重新启动
if {[string match "*Complete*" [get_property STATUS [get_runs synth_1]]]} {
    puts "SYNTH_REUSED"
} else {
    reset_run synth_1
    launch_runs synth_1 -jobs 8
    wait_on_run synth_1
}
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {
    error "综合失败：见 [get_property DIRECTORY [get_runs synth_1]]/runme.log"
}
puts "SYNTH_DONE"

# ---- 实现（布局布线 + bitstream）----
if {[string match "*Complete*" [get_property STATUS [get_runs impl_1]]]} {
    puts "IMPL_REUSED"
} else {
    reset_run impl_1
    # reset_run 之后再设置 pre-hook，确保它对本轮 write_bitstream 生效
    set_property STEPS.WRITE_BITSTREAM.TCL.PRE $hook_file [get_runs impl_1]
    launch_runs impl_1 -to_step write_bitstream -jobs 8
    wait_on_run impl_1
}
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
    error "实现或 bitstream 失败：见 [get_property DIRECTORY [get_runs impl_1]]/runme.log"
}
puts "IMPL_DONE"

open_run impl_1
report_timing_summary -file [file join $build_dir timing_summary.rpt]
report_utilization -file [file join $build_dir utilization.rpt]
report_drc -file [file join $build_dir drc.rpt]
puts "TIMING_BEGIN"
puts [get_property STATS.WNS [get_runs impl_1]]
puts [get_property STATS.WHS [get_runs impl_1]]
puts "TIMING_END"

# ---- 导出硬件平台 ----
write_hw_platform -fixed -include_bit -force -file $xsa_file
puts "XSA_DONE: $xsa_file"
