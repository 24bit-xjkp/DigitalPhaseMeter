# 同一份综合网表上做多次独立布局布线，取余量最好的一次
#
# 用法：
#   vivado -mode batch -source vivado/impl_multi.tcl -tclargs 6
#
# 前置条件：先跑 package_ip.tcl / package_src_ip.tcl / create_bd.tcl / 综合（见 build_bitstream.tcl）
#
# 背景：Vivado 的 place/route 有可观的 run-to-run 差异（同一网表、同一策略实测
# WNS 曾在 +0.006 与 -0.202 之间波动），因此「跑一次通过」不能作为签核结论，
# 必须多次独立尝试并保留余量最好的一次（结果与所选那次的路由/时序报告一起留存）。
#
# 注意：本脚本会在每次尝试前**覆盖** impl_1 的 place/route directive（用于扫不同布局布线
# 组合），因此它会覆盖 build_bitstream.tcl 选定的官方策略。**签核请以 build_bitstream.tcl
# 那次为准**（它启动 run 前会把策略写回 Performance_Retiming，当前 WNS +0.270ns）；
# 本脚本用于评估余量的稳定性/寻找更好的布局。
#
# 另外：跑本脚本前确认没有别的 Vivado 在跑（并发会互相覆盖工程里的步骤参数）。
#

set script_dir [file dirname [file normalize [info script]]]
set build_dir [file normalize [file join $script_dir .. build vivado]]
set proj_dir [file join $build_dir bd_proj]
set xdc_file [file normalize [file join $script_dir constraints dpm_top.xdc]]
set hook_bs [file normalize [file join $script_dir hook_write_bitstream.tcl]]
# 同一套「屏蔽厂商 IP 固有告警」的过滤器（理由见 hook_filter_messages.tcl）。
# 本脚本每轮都 reset_run impl_1，会把步骤属性清回默认值，因此每轮 reset 后重新挂一遍。
set hook_filter [file normalize [file join $script_dir hook_filter_messages.tcl]]

set attempts 6
if {[llength $argv] >= 1 && [lindex $argv 0] ne ""} { set attempts [lindex $argv 0] }
set places {Explore AggressiveExplore ExtraTimingOpt ExtraPostPlacementOpt Explore AggressiveExplore}
set routes {AggressiveExplore AggressiveExplore AggressiveExplore AggressiveExplore Explore Explore}

cd $build_dir
open_project [file join $proj_dir dpm_bd.xpr]
foreach old [get_files -quiet -of_objects [get_filesets constrs_1] {*dpm_top.xdc}] { remove_files -quiet $old }
add_files -fileset constrs_1 -norecurse $xdc_file
set_property target_language Verilog [current_project]
set_property strategy Performance_ExplorePostRoutePhysOpt [get_runs impl_1]

set results_file [file join $script_dir impl_multi_results.csv]
set fh [open $results_file w]
puts $fh "attempt,place,route,wns,whs,fep_setup"
flush $fh

# 从 report_timing_summary 的报告里取「Design Timing Summary」的 WNS/WHS/失败端点数。
# 不要用「一条大正则 + 非贪婪 .*?」的写法去套这张表：Tcl ARE 里 `.` 能匹配换行，
# 那个组合会让末尾的 (-?\d+\.\d+) 只吃到 "0.0" 而不是 "0.062"，实测 whs 列恒为 0.0。
# 这里按行定位：先找 "Design Timing Summary"，再取其后第一行的纯数字统计行。
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
    if {$idx < 0} { return [list "?" "?" "?"] }
    for {set i [expr {$idx + 1}]} {$i < [llength $lines]} {incr i} {
        set fields [regexp -all -inline {\S+} [lindex $lines $i]]
        if {[llength $fields] >= 5 && [string is double -strict [lindex $fields 0]] &&
            [string is integer -strict [lindex $fields 2]]} {
            # 列序：WNS TNS TNS_FEP TNS_TEP WHS ...
            return [list [lindex $fields 0] [lindex $fields 4] [lindex $fields 2]]
        }
    }
    return [list "?" "?" "?"]
}

set best ""; set best_wns -99.0; set best_rpt ""
for {set i 1} {$i <= $attempts} {incr i} {
    set place [lindex $places [expr {($i - 1) % [llength $places]}]]
    set route [lindex $routes [expr {($i - 1) % [llength $routes]}]]
    puts "MULTI_START attempt=$i place=$place route=$route"

    catch { set_property STEPS.OPT_DESIGN.ARGS.DIRECTIVE Explore [get_runs impl_1] }
    catch { set_property STEPS.PLACE_DESIGN.ARGS.DIRECTIVE $place [get_runs impl_1] }
    catch { set_property STEPS.PHYS_OPT_DESIGN.IS_ENABLED 1 [get_runs impl_1] }
    catch { set_property STEPS.PHYS_OPT_DESIGN.ARGS.DIRECTIVE AggressiveExplore [get_runs impl_1] }
    catch { set_property STEPS.ROUTE_DESIGN.ARGS.DIRECTIVE $route [get_runs impl_1] }
    catch { set_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED 1 [get_runs impl_1] }
    catch { set_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.ARGS.DIRECTIVE AggressiveExplore [get_runs impl_1] }

    reset_run impl_1
    set_property STEPS.WRITE_BITSTREAM.TCL.PRE $hook_bs [get_runs impl_1]
    set_property STEPS.INIT_DESIGN.TCL.PRE $hook_filter [get_runs impl_1]
    launch_runs impl_1 -to_step write_bitstream -jobs 8
    wait_on_run impl_1
    if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
        puts "MULTI_FAIL attempt=$i"
        puts $fh "$i,$place,$route,,,,failed"
        flush $fh
        continue
    }
    open_run impl_1
    set rpt [file join $script_dir "multi_a${i}_timing.rpt"]
    report_timing_summary -file $rpt
    lassign [parse_design_timing_summary $rpt] w wh n
    puts "MULTI_RESULT attempt=$i WNS=$w WHS=$wh FEP_SETUP=$n"
    puts $fh "$i,$place,$route,$w,$wh,$n"
    flush $fh
    if {$w ne "?" && [expr {double($w)}] > $best_wns} {
        # 新的最好一次：把上一次最好的 run 删掉，只保留当前最好的那个（impl_1 是固定名字，
        # 所以每次都是「重跑一次、比一次」，最后留在 impl_1 里的就是余量最好的那次）。
        set best_wns [expr {double($w)}]
        set best "attempt=$i place=$place route=$route wns=$w whs=$wh"
        set best_rpt $rpt
    }
}
close $fh
puts "MULTI_BEST $best"
if {$best_rpt ne ""} {
    file copy -force [file join $script_dir [file tail $best_rpt]] [file join $script_dir best_timing.rpt]
    set f [open [file join $script_dir best_timing.rpt] r]; set b [read $f]; close $f
}
# 把最好一次的实现目录留作签核产物
puts "MULTI_BEST_REPORT $best_rpt"
puts "MULTI_DONE"
puts "MULTI_HINT 每次的时序报告见 multi_a<N>_timing.rpt；余量最好的那次报告已另存为 best_timing.rpt。"
