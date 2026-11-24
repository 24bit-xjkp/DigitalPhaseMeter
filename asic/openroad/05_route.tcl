# 阶段 5：全局布线、天线修复、详细布线、填充单元与最终报告
#
# 从 04_route_opt.tcl 产出的数据库开始。这个阶段在新的 OpenROAD 进程里做
# 全新全局布线，04 阶段插入/调整过的单元也会有完整的 route guide。
#
# sky130 原始 LEF 没有把天线二极管标成 ANTENNACELL，所以 repair_antennas 必须
# 显式给出二极管单元名；PDK 准备步骤同时补上了 ANTENNADIFFAREA。
# 详细布线后仍可能新增天线违规，因此按 OpenROAD 参考流程再做一次
# "check -> repair -> reroute" 循环。

source [file join [file dirname [info script]] config.tcl]

log_stage "读入布线前修复后的数据库"
read_db $ROUTE_OPT_DB
reload_timing
set_propagated_clock [all_clocks]
set_sky130_wire_rc
set_sky130_routing_layers

log_stage "全局布线"
pin_access
global_route -guide_file $ROUTE_GUIDE -congestion_iterations 100 -verbose
estimate_parasitics -global_routing

log_stage "布线前天线修复"
repair_antennas $DIODE_CELL -iterations 5
puts "布线前天线违规数：[check_antennas]"

log_stage "详细布线"
detailed_route -output_drc $DRC_RPT \
               -output_maze $MAZE_LOG \
               -no_pin_access \
               -verbose 0

log_stage "详细布线后天线修复"
set antenna_iters 0
while {[check_antennas] && $antenna_iters < 5} {
    incr antenna_iters
    puts "天线修复第 $antenna_iters 轮"
    repair_antennas $DIODE_CELL
    detailed_route -output_drc $DRC_RPT \
                   -output_maze $MAZE_LOG \
                   -no_pin_access \
                   -verbose 0
}
set antenna_errors [check_antennas]
puts "最终天线违规数：$antenna_errors"
report_drc_file

if {![design_is_routed]} {
    error "布线未完成：仍有未布通的网络"
}

log_stage "填充单元与最终时序"
filler_placement $FILLER_CELLS
check_placement -verbose
estimate_parasitics -global_routing

report_mcmm_timing
# 主场景（ss）再打一份完整路径，便于看最差 setup 的具体单元/走线
puts "\n主场景 $PRIMARY_SCENE 的 setup 完整路径："
report_checks -path_delay max -scenes $PRIMARY_SCENE -format full_clock_expanded \
              -fields {input_pin slew capacitance} -digits 3
report_stage "route"
report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3
report_clock_skew -digits 3
report_mcmm_power
report_floating_nets

write_def $FINAL_DEF
write_db $FINAL_DB
puts "阶段 5 完成：$FINAL_DEF / $FINAL_DB"
