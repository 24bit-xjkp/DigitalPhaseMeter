# 阶段 4：全局布线、GRT 寄生下的修复与预布线天线修复
#
# 本阶段对应 ORFS 的 global_route.tcl：full global_route 后用 GRT 寄生做
# repair_design / repair_timing，每次修复后用 -start_incremental /
# -end_incremental 把改动 net 的 guide 补齐，再做预布线天线修复，写出带完整
# guide 的数据库。05 阶段因此可以直接详细布线（ORFS 的 5_1_grt.odb ->
# 5_2_route.odb 切分），不需要再次全局布线。
#
# 不用“修完再全量 global_route”：同一进程里已经 global_route 过的设计再次
# 布线会走增量路径，repair_design/repair_timing 新建的 pin 可能拿不到 guide，
# 后续 estimate_parasitics -global_routing 会报大量 EST-0026。显式的
# -start_incremental/-end_incremental 只重布 repair 改动过的 net，guide 完整。
#
# 预布线天线修复必须完成在本阶段：05 从数据库重新读入时 GRT 的运行时状态
# （NDR 关闭、拥塞图）不会恢复，在 05 里直接 repair_antennas 会因残余拥塞报
# GRT-0232。

source [file join [file dirname [info script]] config.tcl]

log_stage "读入 CTS 数据库"
read_db $CTS_DB
reload_timing
set_propagated_clock [all_clocks]
set_sky130_wire_rc
set_sky130_routing_layers

log_stage "全局布线（critical_nets $GRT_CRITICAL_NETS%）"
pin_access
prepare_timing_driven_grt
global_route {*}[global_route_options $PREP_GUIDE]
estimate_parasitics -global_routing

log_stage "GRT 寄生下的电气规则修复"
repair_design -slew_margin $SLEW_MARGIN -cap_margin $CAP_MARGIN
# repair_design 可能插入/改动单元，只重布这些 net，保证新 pin 有 guide
global_route -start_incremental
detailed_placement
global_route -end_incremental
estimate_parasitics -global_routing

log_stage "GRT 寄生下的时序修复"
repair_timing {*}[repair_timing_options]
global_route -start_incremental
detailed_placement
check_placement -verbose
global_route -end_incremental
estimate_parasitics -global_routing

log_stage "布线前天线修复"
repair_antennas $DIODE_CELL -iterations 5
puts "布线前天线违规数：[check_antennas]"

# 天线修复可能新增二极管/jumper，最后统一写出与数据库一致的 guide
write_guides $PREP_GUIDE

report_stage "global route"
report_mcmm_timing
report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3

write_db $ROUTE_OPT_DB
puts "阶段 4 完成：$ROUTE_OPT_DB"
