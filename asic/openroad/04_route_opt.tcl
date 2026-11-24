# 阶段 4：布线前电气规则修复（在 CTS 数据库上做一次 GRT 寄生评估）
#
# 布局阶段的 repair_design 用的是 placement 寄生估计，长线在布线后可能才暴露
# max transition/cap 违例。这里先做一次全局布线拿到 GRT 寄生，修完电气规则后
# 保存 04_route_opt.odb；下一阶段从该数据库**重新**全局布线，避免在同一个
# OpenROAD 进程里复用上一次的 route guide 导致新插入单元对应的 pin 没有布线。
#
# 拆分这两个阶段是为了让 05_route.tcl 的 global_route 在干净状态下运行：
# 同一进程里对已经 global_route 过的设计再次调用 global_route 时，OpenROAD 会
# 走增量路径，repair_design/repair_timing 新建的 pin 可能拿不到 guide，
# 后续 estimate_parasitics -global_routing 会报大量 EST-0026。

source [file join [file dirname [info script]] config.tcl]

log_stage "读入 CTS 数据库"
read_db $CTS_DB
reload_timing
set_propagated_clock [all_clocks]
set_sky130_wire_rc
set_sky130_routing_layers

log_stage "全局布线（用于 GRT 寄生评估）"
pin_access
global_route -guide_file $PREP_GUIDE -congestion_iterations 100
estimate_parasitics -global_routing

log_stage "GRT 寄生下的电气规则 / 时序修复"
repair_design -slew_margin $SLEW_MARGIN -cap_margin $CAP_MARGIN
repair_timing -skip_gate_cloning
detailed_placement

report_stage "route opt"
report_mcmm_timing
report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3

write_db $ROUTE_OPT_DB
puts "阶段 4 完成：$ROUTE_OPT_DB"
