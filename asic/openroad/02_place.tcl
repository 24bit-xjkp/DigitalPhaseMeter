# 阶段 2：全局布局、修复、详细布局
#
# read_db 之后 STA 侧的 liberty / SDC 不会自动恢复，reload_timing 会重新读。

source [file join [file dirname [info script]] config.tcl]

log_stage "读入 floorplan 数据库"
read_db $FLOORPLAN_DB
reload_timing
set_sky130_wire_rc
set_sky130_routing_layers

set gpl_args [list -routability_driven -density $PLACE_DENSITY]
if {$TIMING_DRIVEN_PLACE} {
    lappend gpl_args -timing_driven
    if {$TIMING_DRIVEN_REPAIR} {
        lappend gpl_args -timing_driven_repair_timing
    }
}
if {$VIRTUAL_CTS} {
    lappend gpl_args -virtual_cts -virtual_cts_max_skew_fraction $VIRTUAL_CTS_MAX_SKEW
}
log_stage "全局布局（routability driven, density $PLACE_DENSITY, timing_driven $TIMING_DRIVEN_PLACE, virtual_cts $VIRTUAL_CTS）"
global_placement {*}$gpl_args

log_stage "max slew / cap / fanout 修复与 tie cell 拆分"
estimate_parasitics -placement
repair_design -slew_margin $SLEW_MARGIN -cap_margin $CAP_MARGIN
repair_tie_fanout -separation $TIE_SEPARATION $TIE_LO
repair_tie_fanout -separation $TIE_SEPARATION $TIE_HI

set_placement_padding -global -left $DETAIL_PAD -right $DETAIL_PAD
detailed_placement

# 布局阶段迭代重综合：resynth 会替换关键路径上的逻辑 cloud 并插入新单元，
# 新单元没有位置；随后用 -incremental 的 global_placement 只摆放这些新单元，
# 再 detailed_placement 合法化。全量重布局会把已有布局打散，实测更差。
proc count_unplaced_instances {} {
    set block [ord::get_db_block]
    set count 0
    foreach inst [$block getInsts] {
        set status [$inst getPlacementStatus]
        if {$status eq "NONE" || $status eq "UNPLACED"} {
            incr count
        }
    }
    return $count
}

if {$PLACE_RESYNTH_ITERS > 0} {
    for {set iter 1} {$iter <= $PLACE_RESYNTH_ITERS} {incr iter} {
        log_stage "布局迭代 $iter/$PLACE_RESYNTH_ITERS：$PLACE_RESYNTH_MODE resynth"
        if {$PLACE_RESYNTH_MODE eq "anneal"} {
            set resynth_args [list -corner $PRIMARY_SCENE -iters $RESYNTH_ITERS \
                                   -initial_ops $RESYNTH_INITIAL_OPS \
                                   -revert_after $RESYNTH_REVERT_AFTER -seed $RESYNTH_SEED]
            if {$RESYNTH_TEMP ne ""} {
                lappend resynth_args -temp $RESYNTH_TEMP
            }
            resynth_annealing {*}$resynth_args
        } elseif {$PLACE_RESYNTH_MODE eq "genetic"} {
            resynth_genetic -corner $PRIMARY_SCENE -iters $RESYNTH_ITERS \
                -population_size $RESYNTH_POPULATION_SIZE \
                -initial_ops $RESYNTH_INITIAL_OPS -seed $RESYNTH_SEED
        } else {
            resynth -corner $PRIMARY_SCENE
        }

        if {[count_unplaced_instances] == 0} {
            puts "==> resynth 未插入新单元（候选端点 slack 已满足），提前结束布局迭代"
            break
        }

        estimate_parasitics -placement
        set inc_args [list -incremental -routability_driven -density $PLACE_DENSITY]
        if {$PLACE_RESYNTH_TIMING_DRIVEN} {
            lappend inc_args -timing_driven
        }
        log_stage "布局迭代 $iter：增量全局布局（timing_driven $PLACE_RESYNTH_TIMING_DRIVEN）"
        global_placement {*}$inc_args
        estimate_parasitics -placement
        detailed_placement
        report_stage "布局迭代 $iter"
    }
}

report_stage "placement"
report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3

write_db $PLACE_DB
puts "阶段 2 完成：$PLACE_DB"
