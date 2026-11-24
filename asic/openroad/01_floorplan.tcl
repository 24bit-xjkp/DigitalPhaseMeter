# 阶段 1：floorplan + IO 摆放 + tapcell + PDN
#
# 关键点：
#   * initialize_floorplan 用 -utilization 60（百分比，即 0.6）；
#   * place_io_pins 把 161 个端口平均分到芯片四周（top/right/bottom/left）；
#   * 输出 01_floorplan.def 供 check_pins.py 验证四边都有 pin。

source [file join [file dirname [info script]] config.tcl]

log_stage "读入 sky130 HD 工艺库、ss/tt/ff 三个 corner 与综合网表"
read_lef $TECH_LEF
read_lef $CELL_LEF
read_mcmm_liberty
read_verilog $NETLIST
link_design $DESIGN
define_mcmm_scenes

log_stage "floorplan：利用率 $UTILIZATION%，四周摆 IO"
initialize_floorplan -utilization $UTILIZATION \
                     -aspect_ratio 1.0 \
                     -core_space $CORE_SPACE \
                     -site $SITE
source [file join $ASIC_DIR openroad tracks.tcl]
place_io_pins

log_stage "tapcell 与 PDN"
tapcell -distance $TAPCELL_DISTANCE -tapcell_master $TAPCELL_MASTER
source [file join $ASIC_DIR openroad pdn.tcl]
pdngen

report_stage "floorplan"

write_def $FLOORPLAN_DEF
write_db $FLOORPLAN_DB
puts "阶段 1 完成：$FLOORPLAN_DB"
