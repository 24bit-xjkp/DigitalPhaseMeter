# 综合、实现、生成 bitstream 与 XSA（正常流水线的最后一步）
#
# 用法：
#   vivado -mode batch -source vivado/build_bitstream.tcl [-retiming 0|1] [-dsp <个数>] [-strategy-only]
#
#   -retiming       全局寄存器重定时，默认 1（on）
#   -dsp            综合允许占用的 DSP 个数，默认 2（给 0 表示不设，用综合器默认）
#   -strategy-only  只把综合/实现策略写进工程并打印当前取值，不启动 run
#                   （工程已建好、只想换策略不重建时用；原来独立的 apply_timing_strategy.tcl 就是这件事）
#
# 前置条件：先运行 vivado/package_ip.tcl、vivado/package_src_ip.tcl 与 vivado/create_bd.tcl
#
# 产物（build/vivado/ 下）：
#   dpm_system.xsa            含 bitstream 的硬件平台（可用于 Vitis 软件工程）
#   bd_proj/dpm_bd.runs/      综合与实现报告、bit 文件
#
# 【为什么策略写在本脚本里】DSP 域（MMCM 倍频出的 dspClock）跑 400MHz 时，关键路径是测量 FSM
# 与 DSP48 控制脚的组合逻辑，默认流程留不出这个频率需要的余量。这些 run 参数只在启动 run 前有意义，
# 所以内联在启动 run 的这一步（原来是一个必须单独运行的 apply_timing_strategy.tcl）。

set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
set build_dir [file join $root_dir build vivado]
set proj_dir [file join $build_dir bd_proj]
set xdc_file [file join $script_dir constraints dpm_top.xdc]
set xsa_file [file join $build_dir dpm_system.xsa]

# ---- 参数 ----
set retiming 1
set dsp_budget 2
set strategy_only 0
for {set i 0} {$i < [llength $argv]} {incr i} {
    switch -- [lindex $argv $i] {
        -retiming { incr i ; set retiming [lindex $argv $i] }
        -dsp { incr i ; set dsp_budget [lindex $argv $i] }
        -strategy-only { set strategy_only 1 }
        default { error "未知参数 '[lindex $argv $i]'（可用：-retiming 0|1、-dsp <个数>、-strategy-only）" }
    }
}

# 把 Vivado 的工作目录放到 build 下，避免在仓库根目录留下 .Xil 等临时文件
file mkdir $build_dir
cd $build_dir

set proj_file [file join $proj_dir dpm_bd.xpr]
if {![file exists $proj_file]} {
    error "找不到工程 $proj_file，请先运行 vivado/create_bd.tcl"
}

open_project $proj_file

# 加入顶层约束：**已经在工程里就不要动**。实测 `remove_files` 会把已完成的 run 立刻标记为
# `Out-of-date`，于是每次运行都会白跑一遍综合/实现，`SYNTH_REUSED` / `IMPL_REUSED` 永远不生效。
set xdc_norm [file normalize $xdc_file]
set xdc_registered 0
foreach f [get_files -quiet -of_objects [get_filesets constrs_1]] {
    if {[file normalize $f] eq $xdc_norm} { set xdc_registered 1 }
}
if {$xdc_registered} {
    puts "XDC_REUSED: $xdc_file"
} else {
    foreach old [get_files -quiet -of_objects [get_filesets constrs_1] {*dpm_top.xdc}] {
        remove_files -quiet $old
    }
    add_files -fileset constrs_1 -norecurse $xdc_file
}
# 同理：属性值相同就不要写（重复设置也可能让 run 过期）
if {[get_property target_language [current_project]] ne "Verilog"} {
    set_property target_language Verilog [current_project]
}

# ---- 综合/实现策略（run 参数，必须在本轮 launch_runs 之前写）----
# GLOBAL_RETIMING：跨寄存器重定时（默认 auto）。DSP48 的时钟使能/复位路径上如果只有一层组合逻辑，
#   重定时能把它收进 DSP 内部，直接消掉这一段 LUT + 布线。
# DIRECTIVE PerformanceOptimized：以时序为目标做跨层次优化与寄存器复制。
# MAX_DSP：把 DSP 用量钉在已知规模上，避免综合器为了别处的时序把 32 位计数器搬来搬去
#   （本设计只需要 2 个 DSP48）；给 0 表示不设。
# 实现选 Performance_Retiming 而不是 Performance_ExplorePostRoutePhysOpt：前者的 phys_opt 用
#   AlternateFlowWithRetiming（官方描述：更激进的复制 + **DSP/BRAM 优化** + 开启重定时），正是本设计
#   需要的——关键路径终点是 DSP48 的控制脚。慢域 50MHz 参考 + DSP 400MHz 下实测（同一份综合网表、
#   逐个内置策略跑布局布线）：
#     Performance_Retiming        WNS +0.270ns / WHS +0.095ns   ← 本仓库采用
#     Performance_RefinePlacement WNS +0.109ns / WHS +0.053ns
#     Performance_ExplorePostRoutePhysOpt（本仓库早期采用） WNS +0.077ns
#     Performance_Auto_1/2/3      WNS +0.061ns
#     Performance_ExtraTimingOpt / Performance_ExploreWithRemap（跑到 write_bitstream 失败，未采用）
# 有意**不**覆盖 Performance_Retiming 自带的步骤 directive：该策略的关键正是 phys_opt 的
#   AlternateFlowWithRetiming（DSP 优化 + 重定时），覆盖成 AggressiveExplore 会把它换掉，
#   实测 WNS 从 +0.270ns 掉到 +0.077ns 一档。需要临时对比其它 directive 组合时，
#   用 query_runs.tcl 看当前取值再改。
set strategy_targets [list \
    [list synth_1 STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING [expr {$retiming ? "on" : "off"}]] \
    [list synth_1 STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE PerformanceOptimized] \
    [list impl_1 strategy Performance_Retiming]]
if {$dsp_budget > 0} {
    lappend strategy_targets [list synth_1 STEPS.SYNTH_DESIGN.ARGS.MAX_DSP $dsp_budget]
}
# 逐个核对：**只有当前值与目标值不同才写**。对已完成的 run，即使写入相同的值也会让它变成
# 过期状态（STATUS 不再是 Complete），下一次就无法复用网表——因此这里先比较再决定是否写。
# 属性值的格式由 Vivado 决定，用字符串比较即可（实测 "on" / "PerformanceOptimized" /
# "Performance_Retiming" / "2" 都能与写入值一致）。
# 注意用 lassign 拆每个三元组：foreach 的多变量形式是按「元素」而不是按「词」分组的，
# 直接写 `foreach {run prop value} $strategy_targets` 会把每个子列表整体赋给一个变量。
set strategy_changed 0
foreach item $strategy_targets {
    lassign $item run prop value
    set before [get_property $prop [get_runs $run]]
    if {$before eq $value} { continue }
    set_property $prop $value [get_runs $run]
    puts "STRATEGY_SET: [get_property name [get_runs $run]] $prop: '$before' -> '[get_property $prop [get_runs $run]]'"
    incr strategy_changed
}
puts "STRATEGY_BEGIN"
puts "synth_1 strategy = [get_property strategy [get_runs synth_1]]"
puts "impl_1  strategy = [get_property strategy [get_runs impl_1]]"
puts "synth retiming    = [get_property STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING [get_runs synth_1]]"
puts "synth directive   = [get_property STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE [get_runs synth_1]]"
# 只读 ARGS.DIRECTIVE：STEPS.<步骤>.IS_ENABLED 在这些步骤上不是合法属性（读它会报 [Common 17-673]）
foreach step {OPT_DESIGN PLACE_DESIGN PHYS_OPT_DESIGN ROUTE_DESIGN POST_ROUTE_PHYS_OPT_DESIGN} {
    puts "impl $step directive=[get_property -quiet STEPS.${step}.ARGS.DIRECTIVE [get_runs impl_1]]"
}
puts "STRATEGY_CHANGED: $strategy_changed"
puts "STRATEGY_END"

if {$strategy_only} {
    puts "STRATEGY_ONLY: 策略已就位（见上面的 STRATEGY_BEGIN/END），未启动 run（工程 $proj_file）"
    close_project
    exit 0
}

# 策略刚变化过 → 已完成的 run 是旧参数跑出来的，必须重跑，否则会静默复用旧网表。
set force_rerun [expr {$strategy_changed > 0}]
if {$force_rerun} {
    puts "STRATEGY_FORCE_RERUN: 综合/实现参数刚变化，已完成的 run 将重跑"
}

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
# 已经完成过的 run 且策略没变时直接复用（例如只重跑 bitstream 步骤），否则重新启动
if {!$force_rerun && [string match "*Complete*" [get_property STATUS [get_runs synth_1]]]} {
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
if {!$force_rerun && [string match "*Complete*" [get_property STATUS [get_runs impl_1]]]} {
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
