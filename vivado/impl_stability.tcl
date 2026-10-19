# 同一份综合网表、同一套实现策略上重复做 N 次独立布局布线，回答「能不能稳定通过」
#
# 用法：
#   vivado -mode batch -source vivado/impl_stability.tcl -tclargs 6
#
# 与 impl_multi.tcl 的分工：
#   - impl_multi.tcl  **换** place/route directive 扫策略，比较不同策略的余量（会覆盖官方策略）；
#   - 本脚本         **不换**策略，沿用工程当前的 impl_1 strategy（即 build_bitstream.tcl 写入的
#                    官方策略），只重复独立实现。它回答的是「同一策略下余量稳不稳」，而不是
#                    「哪个策略最好」。
#
# 签核判据（本仓库约定）：
#   - N 次实现的 WNS 与 WHS **全部为正**、0 个失败端点；
#   - 报告里写清 N、最小/中位/最大值，单次通过不能作为签核依据。
#   place/route 有可观的 run-to-run 差异（400MHz 轮次历史实测 WNS 在 +0.006 / -0.202 之间摆），
#   因此必须多次独立尝试（本脚本每次 reset_run impl_1 后重新 place/route）。
#
# 前置条件：
#   - 工程已建好（vivado/create_bd.tcl），综合网表已完整（先跑 vivado/build_bitstream.tcl，
#     或至少 launch_runs synth_1 到完成）；
#   - 运行前确认没有其它 Vivado 在跑（并发会互相覆盖同一工程的步骤参数，见 vivado/README.md）。
#
# 产物（都在 build/vivado/ 下，属于构建产物、不入库）：
#   impl_stability_results.csv   每次的 WNS/WHS/DSP 域余量/失败端点数
#   stability_a<N>_timing.rpt    每次的 report_timing_summary
#   控制台最后打印 STABILITY_BEST / STABILITY_DONE 汇总行

set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
set build_dir [file join $root_dir build vivado]
set proj_dir [file join $build_dir bd_proj]
set hook_bs [file normalize [file join $script_dir hook_write_bitstream.tcl]]
set hook_filter [file normalize [file join $script_dir hook_filter_messages.tcl]]
set dsp_clock_group "clk_out1_dpm_system_clk_wiz_0_0"

set attempts 6
if {[llength $argv] >= 1 && [lindex $argv 0] ne ""} { set attempts [lindex $argv 0] }
if {![string is integer -strict $attempts] || $attempts < 1} {
    error "attempts 必须是正整数（收到 '$attempts'）"
}

cd $build_dir
open_project [file join $proj_dir dpm_bd.xpr]

# 读工程当前策略，只打印不修改：本脚本的语义就是「按官方策略重复验证」。
# 如果这里不是预期的那套策略，说明 build_bitstream.tcl 还没跑过或策略被别的脚本改过。
puts "STABILITY_IMPL_STRATEGY: [get_property strategy [get_runs impl_1]]"
foreach step {PLACE_DESIGN PHYS_OPT_DESIGN ROUTE_DESIGN POST_ROUTE_PHYS_OPT_DESIGN} {
    puts "STABILITY_IMPL_STEP: $step directive=[get_property -quiet STEPS.${step}.ARGS.DIRECTIVE [get_runs impl_1]]"
}

set results_file [file join $build_dir impl_stability_results.csv]
set fh [open $results_file w]
puts $fh "attempt,wns,whs,wns_dsp,whs_dsp,fep_setup,fep_hold"
flush $fh

# 从 report_timing_summary 里取「Design Timing Summary」统计行（列序 WNS TNS FEP_SETUP ... WHS ...）。
# 不用「一条大正则 + 非贪婪」去套整张表：Tcl ARE 里 `.` 能匹配换行，会把数字吃错。
# 与 impl_multi.tcl 的解析函数保持一致。
proc parse_design_timing_summary {rpt_file} {
    set fh [open $rpt_file r]
    set text [read $fh]
    close $fh
    set lines [split $text "\n"]
    set idx -1
    for {set i 0} {$i < [llength $lines]} {incr i} {
        if {[string match "*Design Timing Summary*" [lindex $lines $i]]} {
            set idx $i
            break
        }
    }
    if {$idx < 0} { return [list "?" "?" "?" "?"] }
    for {set i [expr {$idx + 1}]} {$i < [llength $lines]} {incr i} {
        set fields [regexp -all -inline {\S+} [lindex $lines $i]]
        if {[llength $fields] >= 8 && [string is double -strict [lindex $fields 0]] &&
            [string is integer -strict [lindex $fields 2]]} {
            # 列序：WNS TNS TNS_FEP TNS_TEP WHS THS THS_FEP THS_TEP ...
            return [list [lindex $fields 0] [lindex $fields 4] [lindex $fields 2] [lindex $fields 6]]
        }
    }
    return [list "?" "?" "?" "?"]
}

set wns_list {}
set whs_list {}
set best_wns -99.0
set best ""
set fail_count 0

for {set i 1} {$i <= $attempts} {incr i} {
    puts "STABILITY_START attempt=$i/$attempts"
    # 每次独立实现：reset_run impl_1 会清掉步骤属性（含挂的 TCL.PRE hook），reset 后必须重挂。
    reset_run impl_1
    set_property STEPS.INIT_DESIGN.TCL.PRE $hook_filter [get_runs impl_1]
    set_property STEPS.WRITE_BITSTREAM.TCL.PRE $hook_bs [get_runs impl_1]
    launch_runs impl_1 -to_step write_bitstream -jobs 8
    wait_on_run impl_1
    if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
        puts "STABILITY_FAIL attempt=$i status=[get_property STATUS [get_runs impl_1]]"
        puts $fh "$i,,,,,failed"
        flush $fh
        incr fail_count
        continue
    }
    open_run impl_1
    set rpt [file join $build_dir "stability_a${i}_timing.rpt"]
    report_timing_summary -file $rpt
    lassign [parse_design_timing_summary $rpt] w wh fep_setup fep_hold
    # DSP 域余量用 get_timing_paths 直接取（比解析报告稳）
    set p_setup [get_timing_paths -quiet -group $dsp_clock_group -max_paths 1 -setup]
    set p_hold [get_timing_paths -quiet -group $dsp_clock_group -max_paths 1 -hold]
    set w_dsp "?"
    set wh_dsp "?"
    if {[llength $p_setup]} { set w_dsp [get_property SLACK $p_setup] }
    if {[llength $p_hold]} { set wh_dsp [get_property SLACK $p_hold] }
    puts "STABILITY_RESULT attempt=$i WNS=$w WHS=$wh DSP_WNS=$w_dsp DSP_WHS=$wh_dsp FEP_SETUP=$fep_setup FEP_HOLD=$fep_hold"
    puts $fh "$i,$w,$wh,$w_dsp,$wh_dsp,$fep_setup,$fep_hold"
    flush $fh
    if {$w ne "?"} { lappend wns_list [expr {double($w)}] }
    if {$wh ne "?"} { lappend whs_list [expr {double($wh)}] }
    if {$w ne "?" && [expr {double($w)}] > $best_wns} {
        set best_wns [expr {double($w)}]
        set best "attempt=$i WNS=$w WHS=$wh DSP_WNS=$w_dsp DSP_WHS=$wh_dsp"
        file copy -force $rpt [file join $build_dir best_timing.rpt]
    }
    close_design
}
close $fh

proc stats {values} {
    if {[llength $values] == 0} { return [list "?" "?" "?"] }
    set sorted [lsort -real $values]
    set n [llength $sorted]
    set sum 0.0
    foreach v $sorted { set sum [expr {$sum + $v}] }
    return [list [lindex $sorted 0] [lindex $sorted end] [expr {$sum / $n}]]
}

lassign [stats $wns_list] wns_min wns_max wns_avg
lassign [stats $whs_list] whs_min whs_max whs_avg
puts "STABILITY_SUMMARY attempts=$attempts fails=$fail_count"
puts "STABILITY_WNS_MIN=$wns_min WNS_AVG=$wns_avg WNS_MAX=$wns_max"
puts "STABILITY_WHS_MIN=$whs_min WHS_AVG=$whs_avg WHS_MAX=$whs_max"
puts "STABILITY_BEST $best"
puts "STABILITY_VERDICT [expr {$fail_count == 0 && [llength $wns_list] == $attempts && [lindex [lsort -real $wns_list] 0] > 0 ? {PASS} : {FAIL}}]"
puts "STABILITY_DONE 每次报告见 build/vivado/stability_a<N>_timing.rpt；最好一次另存为 build/vivado/best_timing.rpt。"
close_project
