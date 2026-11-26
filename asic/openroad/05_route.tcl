# 阶段 5：详细布线、天线修复循环、填充单元、最终报告与门级网表
#
# 从 04_route_opt.tcl 产出的数据库开始。该数据库已经完成全局布线（guide 完整）、
# GRT 寄生下的 repair_design/repair_timing 和预布线天线修复，本阶段对应 ORFS 的
# detail_route.tcl：不再重新全局布线，直接在已有 guide 上做详细布线。
#
# sky130 原始 LEF 没有把天线二极管标成 ANTENNACELL，所以 repair_antennas 必须
# 显式给出二极管单元名；详细布线后仍可能新增天线违规，因此按参考流程再做一次
# "check -> repair -> reroute" 循环。预布线天线修复在 04 阶段完成（从数据库重载
# 后 GRT 运行时状态不完整，在这里修会报 GRT-0232）。
#
# detailed_route 不传 -no_pin_access：04 阶段插入的天线二极管等新单元需要 DRT
# 自己补 pin access，这与 ORFS detail_route.tcl 一致。

source [file join [file dirname [info script]] config.tcl]

log_stage "读入已全局布线的数据库"
read_db $ROUTE_OPT_DB
reload_timing
set_propagated_clock [all_clocks]
set_sky130_wire_rc
set_sky130_routing_layers

log_stage "详细布线"
detailed_route -output_drc $DRC_RPT \
               -output_maze $MAZE_LOG \
               -verbose 0

log_stage "详细布线后天线修复"
set antenna_iters 0
while {[check_antennas] && $antenna_iters < 5} {
    incr antenna_iters
    puts "天线修复第 $antenna_iters 轮"
    repair_antennas $DIODE_CELL
    detailed_route -output_drc $DRC_RPT \
                   -output_maze $MAZE_LOG \
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

# 最终门级网表：保留综合阶段的可读信号名与布局/CTS/修复插入的单元名，
# 去掉 filler/tapcell 等纯物理单元。网表是布局布线后的真实连接关系，
# 可与 dpm_asic_synth.v 按信号名对照。
log_stage "写出最终门级网表"
write_verilog -remove_cells $PHYSICAL_CELLS $FINAL_NETLIST
puts "最终网表：$FINAL_NETLIST（已去掉 filler/tapcell）"
puts "阶段 5 完成：$FINAL_DEF / $FINAL_DB / $FINAL_NETLIST"
