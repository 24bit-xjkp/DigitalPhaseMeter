# 阶段 3：时钟树综合、CTS 后修复
#
# 设计有两个时钟（clock / dspClock）和 clockgate 插入的 ICG。TritonCTS 会把
# ICG 按 sink cluster 复制并重新驱动，再平衡两棵时钟树的延迟。

source [file join [file dirname [info script]] config.tcl]

log_stage "读入 placement 数据库"
read_db $PLACE_DB
reload_timing
set_sky130_wire_rc
set_sky130_routing_layers

log_stage "时钟树综合"
repair_clock_inverters
clock_tree_synthesis -root_buf $CTS_ROOT_BUF \
                     -buf_list $CTS_BUF_LIST \
                     -sink_clustering_enable \
                     -sink_clustering_max_diameter $CTS_CLUSTER_MAX_DIAMETER
repair_clock_nets
detailed_placement

log_stage "CTS 后时序 / 电气规则修复"
set_propagated_clock [all_clocks]
estimate_parasitics -placement
repair_design -slew_margin $SLEW_MARGIN -cap_margin $CAP_MARGIN
repair_tie_fanout -separation $TIE_SEPARATION $TIE_LO
repair_tie_fanout -separation $TIE_SEPARATION $TIE_HI
repair_timing -skip_gate_cloning
detailed_placement

report_stage "CTS"
report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3
report_clock_skew -digits 3

write_db $CTS_DB
puts "阶段 3 完成：$CTS_DB"
