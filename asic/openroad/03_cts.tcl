# 阶段 3：时钟树综合、CTS 后修复
#
# 设计有两个时钟（clock / dspClock）和 clockgate 插入的 ICG。TritonCTS 会把
# ICG 按 sink cluster 复制并重新驱动，再平衡两棵时钟树的延迟。
#
# 本阶段只建 ss 场景：MCMM 的三份同名 liberty 会让 TritonCTS 的 dummy-load 候选
# 选到未链接到 LEF master 的 tt/ff cell（CTS-0118），只读 ss 可以保留 dummy
# load 平衡；04/05 阶段会重新读三个 corner 做 MCMM 修复与报告。

source [file join [file dirname [info script]] config.tcl]

log_stage "读入 placement 数据库"
read_db $PLACE_DB
# CTS 阶段只读 ss（主场景）：MCMM 会把 ss/tt/ff 三份同名 liberty 都读进默认
# network，而 dbNetwork 只把先读入的 ss cell 链到 LEF master；TritonCTS 的
# dummy-load 候选会遍历所有 library，一旦选到 tt/ff 的同名 clkinv，staToDb()
# 拿不到物理 master，就会报 CTS-0118。只读 ss 时时钟树仍会插入 dummy load
# 平衡叶子负载，CTS 后修复也在这个场景完成；04/05 阶段会重新读三个 corner
# 做 MCMM 修复与报告。
read_liberty $SS_LIB
read_sdc -mode $SDC_MODE $SDC
define_scene ss -liberty $SS_LIB -mode $SDC_MODE
set_scene ss
set_sky130_wire_rc
set_sky130_routing_layers

set cts_args [list -root_buf $CTS_ROOT_BUF \
                   -buf_list $CTS_BUF_LIST \
                   -sink_clustering_enable \
                   -sink_clustering_max_diameter $CTS_CLUSTER_MAX_DIAMETER]
if {$USEFUL_SKEW} {
    lappend cts_args -useful_skew
}
log_stage "时钟树综合（useful_skew $USEFUL_SKEW）"
repair_clock_inverters
clock_tree_synthesis {*}$cts_args
repair_clock_nets
detailed_placement

log_stage "CTS 后时序 / 电气规则修复"
set_propagated_clock [all_clocks]
estimate_parasitics -placement
repair_design -slew_margin $SLEW_MARGIN -cap_margin $CAP_MARGIN
repair_tie_fanout -separation $TIE_SEPARATION $TIE_LO
repair_tie_fanout -separation $TIE_SEPARATION $TIE_HI
repair_timing {*}[repair_timing_options]
detailed_placement

# 本阶段只定义了 ss 场景，report_stage 会按 MCMM_SCENES 逐个取 slack；
# 临时收窄到 ss，避免访问未定义的 tt/ff 场景。
set ::MCMM_SCENES {ss}
report_stage "CTS"
report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3
report_clock_skew -digits 3

write_db $CTS_DB
puts "阶段 3 完成：$CTS_DB"
