# 为 DSP 域高速时钟（dspClock）配置综合/实现策略
#
# 用法：
#   vivado -mode batch -source vivado/apply_timing_strategy.tcl [retiming] [dsp_budget]
#
#   参数（都可省略）：retiming 取 0/1（默认 1），dsp_budget 为允许占用的 DSP 个数（默认 2）
#
# 前置条件：先跑 vivado/create_bd.tcl 生成工程（build/vivado/bd_proj）
#
# 为什么需要这个脚本：DSP 域（MMCM 倍频出的 dspClock）跑 400MHz 时，关键路径是
# 测量 FSM 与 DSP48 控制脚的组合逻辑。默认流程按面积与布线均衡，留不出这个频率
# 需要的余量；这里做两件事：
#   1. 综合：PerformanceOptimized directive + 全局寄存器重定时（GLOBAL_RETIMING）
#   2. 实现：换成内置的 Performance_Retiming 策略（它的 phys_opt 用
#      AlternateFlowWithRetiming：更激进的复制 + DSP/BRAM 优化 + 开启重定时）。
# **不要**再覆盖该策略自带的步骤 directive——实测覆盖后会掉回 +0.077ns 一档。
# 策略写进工程（dpm_bd.xpr），后续 build_bitstream.tcl 的 run 直接复用。

set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
set proj_dir [file join $root_dir build vivado bd_proj]
set proj_file [file join $proj_dir dpm_bd.xpr]

set retiming 1
if {[llength $argv] >= 1 && [lindex $argv 0] ne ""} { set retiming [lindex $argv 0] }
set dsp_budget 2
if {[llength $argv] >= 2 && [lindex $argv 1] ne ""} { set dsp_budget [lindex $argv 1] }

if {![file exists $proj_file]} {
    error "找不到工程 $proj_file，请先运行 vivado/create_bd.tcl"
}

cd [file join $root_dir build vivado]
open_project $proj_file

# ---- 综合 ----
# GLOBAL_RETIMING：跨寄存器重定时（默认 auto）。DSP48 的时钟使能/复位路径上如果
#   只有一层组合逻辑，重定时能把它收进 DSP 内部，直接消掉这一段 LUT + 布线。
# DIRECTIVE PerformanceOptimized：以时序为目标做跨层次优化与寄存器复制。
# MAX_DSP：把 DSP 用量钉在已知规模上，避免综合器为了别处的时序把 32 位计数器搬来搬去
#   （本设计只需要 2 个 DSP48）。
set_property STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING [expr {$retiming ? "on" : "off"}] [get_runs synth_1]
set_property STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE PerformanceOptimized [get_runs synth_1]
if {$dsp_budget > 0} {
    set_property STEPS.SYNTH_DESIGN.ARGS.MAX_DSP $dsp_budget [get_runs synth_1]
}

# ---- 实现：换成性能优先的内置策略 ----
# 选 Performance_Retiming 而不是 Performance_ExplorePostRoutePhysOpt：
# 前者的 phys_opt 用 AlternateFlowWithRetiming（官方描述：更激进的复制 + **DSP/BRAM 优化** +
# 开启重定时），正是本设计需要的——关键路径终点是 DSP48 的控制脚。
# 慢域 50MHz 参考 + DSP 400MHz 下实测（同一份综合网表、逐个内置策略跑布局布线）：
#   Performance_Retiming        WNS +0.270ns / WHS +0.095ns   ← 本仓库采用
#   Performance_RefinePlacement WNS +0.109ns / WHS +0.053ns
#   Performance_ExplorePostRoutePhysOpt（本仓库早期采用） WNS +0.077ns
#   Performance_Auto_1/2/3      WNS +0.061ns
#   Performance_ExtraTimingOpt / Performance_ExploreWithRemap（跑到 write_bitstream 失败，未采用）
set_property strategy Performance_Retiming [get_runs impl_1]

# 有意**不**覆盖 Performance_Retiming 自带的步骤 directive：
# 该策略的关键正是 phys_opt 的 AlternateFlowWithRetiming（DSP 优化 + 重定时），
# 覆盖成 AggressiveExplore 会把它换掉，实测 WNS 从 +0.270ns 掉到 +0.077ns 一档。
# 需要临时对比其它 directive 组合时，用 query_runs.tcl 看当前取值再改。

puts "STRATEGY_BEGIN"
puts "synth_1 strategy = [get_property strategy [get_runs synth_1]]"
puts "impl_1  strategy = [get_property strategy [get_runs impl_1]]"
puts "synth retiming    = [get_property STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING [get_runs synth_1]]"
puts "synth directive   = [get_property STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE [get_runs synth_1]]"
foreach step {OPT_DESIGN PLACE_DESIGN PHYS_OPT_DESIGN ROUTE_DESIGN POST_ROUTE_PHYS_OPT_DESIGN} {
    set en [get_property STEPS.${step}.IS_ENABLED [get_runs impl_1]]
    set di [get_property STEPS.${step}.ARGS.DIRECTIVE [get_runs impl_1]]
    puts "impl $step enabled=$en directive=$di"
}
puts "STRATEGY_END"
puts "STRATEGY_DONE"
