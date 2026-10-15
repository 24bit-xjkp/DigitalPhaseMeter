# 让片内信号源支持**在线改相位**：给 clk_wiz_src 打开 CONFIG.PHASE_DUTY_CONFIG。
#
# 背景（实测结论，见 docs/verify-drp.md 与 spec/drp_semantics.json）：
#   当前 BD 里 clk_wiz_src 生成的 IP 虽然打开了 USE_DYN_RECONFIG，但 DRP 寄存器文件里的
#   相位字（clk_out1 → 0x20C、clk_out2 → 0x218，单位毫度）**没有接到 MMCM 配置重建逻辑**：
#   `*_mmcm_pll_drp.v` 里 `S2_CLKOUT1 = mmcm_pll_count_calc(S2_CLKOUT1_DIVIDE, S1_CLKOUT1_PHASE, ...)`
#   用的是静态 generic。xsim 实测 43 组：写相位字完全不改变 clk_out2 的相位（恒为 90°）。
#   打开 PHASE_DUTY_CONFIG 后，模板会多出 S2_CLKOUTx_PHASE/S2_CLKOUTx_DUTY 端口并把寄存器文件
#   的字接进重建逻辑，于是写 0x218 = 毫度值即可在线改相位（分辨率 45/D 度，D=40 时 1.125°）。
#
# 用法（仓库根目录）：
#   vivado -mode batch -source vivado/patch_enable_src_phase.tcl -tclargs 1      # dry run（不保存）
#   vivado -mode batch -source vivado/patch_enable_src_phase.tcl                 # 真正打补丁
#
# 打完补丁必须按顺序重建（README 的流水线）：
#   vivado -mode batch -source vivado/package_ip.tcl       # 重新打包 dpm_top（BD 会用到）
#   vivado -mode batch -source vivado/create_bd.tcl        # 重建 BD（clk_wiz_src 的新配置）
#   vivado -mode batch -source vivado/apply_timing_strategy.tcl
#   vivado -mode batch -source vivado/build_bitstream.tcl
# 然后复跑 DRP 语义实测（sim/run_drp_tb.sh）确认相位字真的生效，并更新
# spec/drp_semantics.json 的 phase_controllable。

set dry  [expr {$argc > 0 ? [lindex $argv 0] : "0"}]
set proj [expr {$argc > 1 ? [lindex $argv 1] : "build/vivado/bd_proj/dpm_bd.xpr"}]

if {![file exists $proj]} {
    puts "ERROR: 找不到 BD 工程 $proj（先跑 vivado/create_bd.tcl）"
    exit 1
}

open_project $proj
set bd_file [get_files -quiet *.bd]
if {$bd_file eq ""} { puts "ERROR: 工程里没有 .bd" ; exit 1 }
open_bd_design $bd_file

set src [get_bd_cells -quiet clk_wiz_src]
if {$src eq ""} { puts "ERROR: 找不到 clk_wiz_src" ; exit 1 }

set before [get_property -quiet CONFIG.PHASE_DUTY_CONFIG $src]
puts "PATCH: clk_wiz_src PHASE_DUTY_CONFIG 当前值 = '$before'"

# 允许“相位/占空比可配置”：生成模板会把寄存器文件的相位/占空比字接进 MMCM 重建逻辑
set_property -dict [list CONFIG.PHASE_DUTY_CONFIG {true}] $src
set after [get_property -quiet CONFIG.PHASE_DUTY_CONFIG $src]
puts "PATCH: 设置后 = '$after'"
if {$after ne "true" && $after ne "1"} {
    puts "ERROR: PHASE_DUTY_CONFIG 未生效（2025.2 该 IP 的属性名可能不同，"
    puts "       请在 GUI 里打开 clk_wiz_src → Dynamic Reconfiguration 页确认）"
    exit 1
}

# 校验：USE_DYN_RECONFIG 必须仍然打开
set dyn [get_property -quiet CONFIG.USE_DYN_RECONFIG $src]
puts "PATCH_CHECK: USE_DYN_RECONFIG='$dyn' PHASE_DUTY_CONFIG='$after'"
if {$dyn ne "true" && $dyn ne "1"} {
    puts "ERROR: USE_DYN_RECONFIG 被关掉了，动态重配置会失效"
    exit 1
}

if {$dry == 1 || $dry eq "dry"} {
    puts "PATCH_DRY_RUN: 属性设置成功、未保存（dry=1）"
    close_project
    exit 0
}

validate_bd_design
save_bd_design
generate_target all $bd_file
puts "PATCH_DONE: clk_wiz_src 已允许相位/占空比动态配置。"
puts "PATCH_NEXT: 依次重跑 package_ip.tcl → create_bd.tcl → apply_timing_strategy.tcl → build_bitstream.tcl，"
puts "            再跑 sim/run_drp_tb.sh 复验相位字是否生效（并更新 spec/drp_semantics.json）。"
close_project
