# 阶段 2：全局布局、修复、详细布局
#
# read_db 之后 STA 侧的 liberty / SDC 不会自动恢复，reload_timing 会重新读。

source [file join [file dirname [info script]] config.tcl]

log_stage "读入 floorplan 数据库"
read_db $FLOORPLAN_DB
reload_timing
set_sky130_wire_rc
set_sky130_routing_layers

log_stage "全局布局（routability driven, density $PLACE_DENSITY）"
global_placement -routability_driven -density $PLACE_DENSITY

log_stage "max slew / cap / fanout 修复与 tie cell 拆分"
estimate_parasitics -placement
repair_design -slew_margin $SLEW_MARGIN -cap_margin $CAP_MARGIN
repair_tie_fanout -separation $TIE_SEPARATION $TIE_LO
repair_tie_fanout -separation $TIE_SEPARATION $TIE_HI

set_placement_padding -global -left $DETAIL_PAD -right $DETAIL_PAD
detailed_placement

report_stage "placement"
report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3

write_db $PLACE_DB
puts "阶段 2 完成：$PLACE_DB"
