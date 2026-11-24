# DigitalPhaseMeterTop ASIC 流程公共配置（OpenROAD Tcl）
#
# 由 01_floorplan.tcl / 02_place.tcl / 03_cts.tcl / 04_route_opt.tcl / 05_route.tcl 依次 source。
# 所有阶段都从仓库根执行，构建产物写到 build/asic/openroad/。

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
set OR_DIR   [file normalize [file dirname [info script]]]
set ASIC_DIR [file normalize [file join $OR_DIR ..]]
set ROOT     [file normalize [file join $ASIC_DIR ..]]
set BUILD    [file join $ROOT build asic]
set OR_BUILD [file join $BUILD openroad]
file mkdir $OR_BUILD

# PDK 根目录：dpm-asic-flow 会导出 DPM_PDK，单独跑某个阶段时用下面的默认值
if {[info exists ::env(DPM_PDK)]} {
    set PDK $::env(DPM_PDK)
} else {
    set PDK /home/luo/skywater-pdk
}
set PDK_LIB [file join $PDK libraries sky130_fd_sc_hd latest]

set TECH_LEF [file join $BUILD sky130_fd_sc_hd.tlef]
set CELL_LEF [file join $BUILD sky130_fd_sc_hd_cells.lef]

# ---------------------------------------------------------------------------
# MCMM：ss/tt/ff 三个 corner
# ---------------------------------------------------------------------------
# 三个 liberty 同时读入，ss/tt/ff 各建一个 scene。ss 是默认（优化主）场景，
# 也是 300MHz setup 的最慢 corner；tt/ff 覆盖 typical/fast 与 hold 检查。
# 三个 corner 的库由 `dpm-asic-flow synth` 调用 `dpm-prepare-pdk` 准备（默认全部三个）；
# 单独跑 OpenROAD 阶段时若缺库，下面有 file exists 检查给出明确错误。
set SS_LIB [file join $BUILD sky130_fd_sc_hd__ss_100C_1v60.lib]
set TT_LIB [file join $BUILD sky130_fd_sc_hd__tt_025C_1v80.lib]
set FF_LIB [file join $BUILD sky130_fd_sc_hd__ff_100C_1v95.lib]
set MCMM_SCENES {ss tt ff}
set PRIMARY_SCENE ss
# 三个 scene 共用一个 SDC mode：约束只读一次，set_scene 切场景时都生效
set SDC_MODE dpm
set NETLIST  [file join $BUILD synth dpm_asic_synth.v]
set SDC      [file join $ASIC_DIR synth dpm_asic.sdc]

set FLOORPLAN_DB  [file join $OR_BUILD 01_floorplan.odb]
set FLOORPLAN_DEF [file join $OR_BUILD 01_floorplan.def]
set PLACE_DB      [file join $OR_BUILD 02_place.odb]
set CTS_DB        [file join $OR_BUILD 03_cts.odb]
set PREP_GUIDE    [file join $OR_BUILD 04_route_opt.guide]
set ROUTE_OPT_DB  [file join $OR_BUILD 04_route_opt.odb]
set ROUTE_GUIDE   [file join $OR_BUILD 05_route.guide]
set DRC_RPT       [file join $OR_BUILD 05_route_drc.rpt]
set MAZE_LOG      [file join $OR_BUILD 05_route_maze.log]
set FINAL_DEF     [file join $OR_BUILD final.def]
set FINAL_DB      [file join $OR_BUILD final.odb]
set FINAL_GDS     [file join $OR_BUILD dpm_asic.gds]

# ---------------------------------------------------------------------------
# 设计 / 工艺参数
# ---------------------------------------------------------------------------
set DESIGN      DigitalPhaseMeterTop
set SITE        unithd
# initialize_floorplan 的 -utilization 单位是百分比：60 即 0.6
set UTILIZATION 60
set CORE_SPACE  2.0

# IO 沿芯片四周平均分成四组；hor 层给上下边，ver 层给左右边
set IO_HOR_LAYER met3
set IO_VER_LAYER met2
set IO_SIDES     {top right bottom left}

# 布局：目标密度略高于 floorplan 的 60%，给修复缓冲留一点空间
set PLACE_DENSITY 0.60
set SLEW_MARGIN   0.4
set CAP_MARGIN    0.3
set DETAIL_PAD    1

# 评估/调参用的环境变量覆盖（不设置时就是上面的交付默认值）：
#   DPM_UTILIZATION    floorplan 利用率百分比（60 即 0.6）
#   DPM_PLACE_DENSITY  global_placement -density（0.60）
#   DPM_SDC            替换默认 SDC（例如换 dspClock 周期的评估约束）
if {[info exists ::env(DPM_UTILIZATION)]} {
    set UTILIZATION $::env(DPM_UTILIZATION)
}
if {[info exists ::env(DPM_PLACE_DENSITY)]} {
    set PLACE_DENSITY $::env(DPM_PLACE_DENSITY)
}
if {[info exists ::env(DPM_SDC)]} {
    set SDC $::env(DPM_SDC)
}

# 三个 corner 的 liberty 由 `dpm-prepare-pdk` 生成；缺失时给出明确提示，
# 避免跑完综合才发现 OpenROAD 找不到 tt/ff。
foreach lib [list $SS_LIB $TT_LIB $FF_LIB] {
    if {![file exists $lib]} {
        error "缺少 MCMM 时序库 $lib。请先运行 `uv run dpm-asic-flow synth`，或直接运行 `uv run dpm-prepare-pdk` 生成三个 corner 的库。"
    }
}
if {![file exists $SDC]} {
    error "缺少 SDC：$SDC"
}

# 布线层与 RC
set SIGNAL_LAYERS met1-met5
set CLOCK_LAYERS  met3-met5

# 常用单元
set TAPCELL_MASTER  sky130_fd_sc_hd__tapvpwrvgnd_1
set TAPCELL_DISTANCE 14
set TIE_LO  sky130_fd_sc_hd__conb_1/LO
set TIE_HI  sky130_fd_sc_hd__conb_1/HI
set DIODE_CELL      sky130_fd_sc_hd__diode_2
set FILLER_CELLS    sky130_fd_sc_hd__fill_*
set CTS_ROOT_BUF    sky130_fd_sc_hd__clkbuf_8
set CTS_BUF_LIST    {sky130_fd_sc_hd__clkbuf_4 sky130_fd_sc_hd__clkbuf_8 sky130_fd_sc_hd__clkbuf_16}
set CTS_CLUSTER_MAX_DIAMETER 100

set TIE_SEPARATION 5

# ---------------------------------------------------------------------------
# 通用设置
# ---------------------------------------------------------------------------
set_thread_count [exec nproc]
# sta 的多线程在部分版本上不稳定，回归里固定单线程
sta::set_thread_count 1

# sky130 原始 LEF / liberty 已知的无害告警
suppress_message STA 1171
suppress_message ODB 0220
suppress_message DRT 0349

proc log_stage {message} {
    puts "\n========== $message =========="
}

# 读入 ss/tt/ff 三个 corner 的 liberty。
# read_db 不会恢复 STA 侧状态，02/03/04/05 阶段重新读时必须走这里。
proc read_mcmm_liberty {} {
    foreach lib [list $::SS_LIB $::TT_LIB $::FF_LIB] {
        read_liberty $lib
    }
}

# 建 ss/tt/ff 三个 scene 并读 SDC。
# 三个 scene 共用同一个 SDC mode：read_sdc -mode 只写一次，set_scene 切场景后
# 时钟/输入输出延迟/异步时钟组等约束在三个 scene 上都生效。
proc define_mcmm_scenes {} {
    read_sdc -mode $::SDC_MODE $::SDC
    define_scene ss -liberty $::SS_LIB -mode $::SDC_MODE
    define_scene tt -liberty $::TT_LIB -mode $::SDC_MODE
    define_scene ff -liberty $::FF_LIB -mode $::SDC_MODE
    set_scene $::PRIMARY_SCENE
}

# read_db 之后 STA 的 liberty / scene / SDC 都不会自动恢复，重新读一遍。
proc reload_timing {} {
    read_mcmm_liberty
    define_mcmm_scenes
}

# 单场景 WNS/TNS。report_worst_slack / report_tns 在 MCMM 下给的是全场景最差值，
# 需要逐场景数值时用 OpenSTA 带 -scene 的接口。
proc scene_slack {scene min_max} {
    return [sta::worst_slack -scene $scene -$min_max]
}

proc scene_tns {scene min_max} {
    return [sta::total_negative_slack -scene $scene -$min_max]
}

proc set_sky130_wire_rc {} {
    source [file join $::ASIC_DIR openroad rc.tcl]
    set_wire_rc -signal -layer met2
    set_wire_rc -clock -layer met5
}

proc set_sky130_routing_layers {} {
    set_routing_layers -signal $::SIGNAL_LAYERS -clock $::CLOCK_LAYERS
    foreach layer {met1 met2 met3 met4 met5} {
        set_global_routing_layer_adjustment $layer 0.4
    }
}

# 把顶层端口按名字顺序平均分成四组，分别约束到 top/right/bottom/left。
# 不这样约束时 place_pins 会把所有 pin 压到两三条边上，达不到"四周摆放"。
proc place_io_pins {} {
    set ports {}
    foreach port [get_ports *] {
        lappend ports [get_property $port full_name]
    }
    set count [llength $ports]
    if {$count == 0} {
        error "place_io_pins: 设计没有端口"
    }
    set per_side [expr {int(ceil($count / 4.0))}]
    set side_index 0
    foreach side $::IO_SIDES {
        set lo [expr {$side_index * $per_side}]
        set hi [expr {min(($side_index + 1) * $per_side - 1, $count - 1)}]
        incr side_index
        if {$lo > $count - 1} {
            break
        }
        set group [lrange $ports $lo $hi]
        set_io_pin_constraint -pin_names $group -region ${side}:*
        puts "  IO $side: [llength $group] pins"
    }
    place_pins -hor_layers $::IO_HOR_LAYER \
               -ver_layers $::IO_VER_LAYER \
               -corner_avoidance 1.0
}

proc report_drc_file {} {
    if {![file exists $::DRC_RPT]} {
        return
    }
    set stream [open $::DRC_RPT r]
    set text [read $stream]
    close $stream
    if {[string trim $text] eq ""} {
        puts "详细布线 DRC：0 条（报告为空）"
    } else {
        puts "详细布线 DRC：报告 $::DRC_RPT 非空，请检查"
    }
}

proc report_stage {name} {
    log_stage "$name 结果"
    report_design_area
    puts "MCMM 各场景 WNS/TNS（ns）："
    foreach scene $::MCMM_SCENES {
        puts [format "  %-3s setup_wns %8.3f hold_wns %8.3f setup_tns %8.3f hold_tns %8.3f" \
                  $scene \
                  [scene_slack $scene max] [scene_slack $scene min] \
                  [scene_tns $scene max] [scene_tns $scene min]]
    }
    set_scene $::PRIMARY_SCENE
}

# 对 ss/tt/ff 分别报告 setup/hold 路径：summary 格式给出 startpoint/endpoint/slack。
# 04（布线前）与 05（最终）都用它；05 另外对主场景再打一份完整路径。
proc report_mcmm_timing {} {
    log_stage "MCMM 时序：ss/tt/ff 的 setup/hold"
    foreach scene $::MCMM_SCENES {
        puts [format "\n--- scene %s: setup WNS %+.3f ns, hold WNS %+.3f ns ---" \
                  $scene [scene_slack $scene max] [scene_slack $scene min]]
        puts "setup paths:"
        report_checks -path_delay max -scenes $scene -format summary -digits 3
        puts "hold paths:"
        report_checks -path_delay min -scenes $scene -format summary -digits 3
    }
    set_scene $::PRIMARY_SCENE
}

# report_power 在 MCMM 下必须显式给 -scene，逐场景输出（leakage 随 corner 变化）。
proc report_mcmm_power {} {
    log_stage "MCMM 各场景功耗"
    foreach scene $::MCMM_SCENES {
        puts "\n--- scene $scene ---"
        report_power -scene $scene
    }
    set_scene $::PRIMARY_SCENE
}

