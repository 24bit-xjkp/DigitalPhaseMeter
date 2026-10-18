# 综合、实现、生成 bitstream 与 XSA（正常流水线的最后一步）
#
# 用法：
#   vivado -mode batch -source vivado/build_bitstream.tcl [-retiming 0|1] [-dsp <个数>] [-strategy-only]
#
#   -retiming       全局寄存器重定时，默认 1（on）
#   -dsp            综合允许占用的 DSP 个数，默认 2（给 0 表示不设，用综合器默认）
#   -strategy-only  只把综合/实现策略写进工程并打印当前取值，不启动 run
#                   （工程已建好、只想换策略不重建时用；原来独立的 apply_timing_strategy.tcl 就是这件事）
#
# 前置条件：先运行 vivado/package_ip.tcl、vivado/package_src_ip.tcl 与 vivado/create_bd.tcl
#
# 产物（build/vivado/ 下）：
#   dpm_system.xsa            含 bitstream 的硬件平台（可用于 Vitis 软件工程）
#   bd_proj/dpm_bd.runs/      综合与实现报告、bit 文件
#
# 【为什么策略写在本脚本里】DSP 域（MMCM 倍频出的 dspClock）跑 450MHz 时，关键路径是测量 FSM
# 与 DSP48 控制脚的组合逻辑，默认流程留不出这个频率需要的余量。这些 run 参数只在启动 run 前有意义，
# 所以内联在启动 run 的这一步（原来是一个必须单独运行的 apply_timing_strategy.tcl）。

set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
set build_dir [file join $root_dir build vivado]
set proj_dir [file join $build_dir bd_proj]
set xdc_file [file join $script_dir constraints dpm_top.xdc]
set xsa_file [file join $build_dir dpm_system.xsa]

# ---- 参数 ----
set retiming 1
set dsp_budget 2
set strategy_only 0
for {set i 0} {$i < [llength $argv]} {incr i} {
    switch -- [lindex $argv $i] {
        -retiming { incr i ; set retiming [lindex $argv $i] }
        -dsp { incr i ; set dsp_budget [lindex $argv $i] }
        -strategy-only { set strategy_only 1 }
        default { error "未知参数 '[lindex $argv $i]'（可用：-retiming 0|1、-dsp <个数>、-strategy-only）" }
    }
}

# 把 Vivado 的工作目录放到 build 下，避免在仓库根目录留下 .Xil 等临时文件
file mkdir $build_dir
cd $build_dir

set proj_file [file join $proj_dir dpm_bd.xpr]
if {![file exists $proj_file]} {
    error "找不到工程 $proj_file，请先运行 vivado/create_bd.tcl"
}

open_project $proj_file

# ---- 打开工程后才出现的两条告警：本工程刻意不绑开发板 ----
# `open_project` 发生在主进程里、run 的 TCL.PRE hook 之前，所以 hook_filter_messages.tcl 管不到，
# 这里补上。两条都源于「不绑定 board part」这一既定选择（引脚由 vivado/constraints/dpm_top.xdc 直接给出）：
#   [Project 1-5713]    × 1：工程没有 board part，Vivado 把 BoardPart 属性置空。
#   [Vivado 12-7122]    × 1：没有参考 checkpoint，自动增量编译不启用（本工程本来就每轮全跑）。
#   [Constraints 18-550]× 1：随 clk_wiz 生成的 xdc 给「由 fabric（FCLK_CLK0）驱动、不是顶层引脚」
#                       的输入网络设 IBUF_LOW_PWR——与 run 内那条同类，理由见 hook_filter_messages.tcl。
#
# 不在这里屏蔽 [Board 49-26]：那 96 条在 `open_project` **内部**就打印了，晚于本脚本的任何语句，
# 因此 set_msg_config 对它无效（实测：过滤在 open_project 之后加载，96 条照旧）。
# 它是 Vivado 扫描 XilinxBoardStore 时逐个报告「board 声明的 part 未安装」，与本设计无关；
# 绑定了具体开发板的工程不会看到。见 vivado/README.md 的「告警清理」。
foreach early_pattern {
    {Project 1-5713}
    {Vivado 12-7122}
    {Constraints 18-550}
} {
    set_msg_config -id $early_pattern -suppress
}
puts "EARLY_FILTER_LOADED: 已屏蔽工程级告警（board part 未绑定、增量编译未启用、IBUF_LOW_PWR）"

# ---- hook 文件（必须在下面第一次引用之前定义）----
# 1) hook_write_bitstream.tcl：把「引脚未约束 / IO 标准未指定」两条 DRC 降级为警告，使
#    bitstream 仍能生成（本仓库不绑定具体开发板）。降级必须放在 write_bitstream 步骤的
#    pre-hook 里：run 在独立进程中执行 DRC，主进程里设置 SEVERITY 不生效。
#    使用具体开发板时填好 vivado/constraints/dpm_top.xdc 里的引脚并去掉这个 hook。
# 2) hook_filter_messages.tcl：用 set_msg_config 屏蔽「厂商 IP 固有」告警（xlconcat 写死的
#    128 个输入端口、PS7 未使能的 DDR/MIO 端口、proc_sys_reset 无消费者的输出等）。
#    逐条理由写在该文件里；它们挂在 run 步骤的 TCL.PRE 上：
#      - SYNTH_DESIGN.TCL.PRE → 综合（8-7129 / 8-7071 / 8-7023 / 8-4446 都出在这一步）
#      - INIT_DESIGN.TCL.PRE  → 实现（link_design 时出现的告警都归这一步）
#    加进 utils_1 fileset，否则 Vivado 会抱怨 hook 脚本不属于工程（[Runs 36-537]）。
#    两个 hook 都是本脚本挂上去的 TCL.PRE，因此都要登记。
set hook_bs [file join $script_dir hook_write_bitstream.tcl]
set hook_filter [file join $script_dir hook_filter_messages.tcl]
foreach h [list $hook_bs $hook_filter] {
    if {![file exists $h]} {
        error "找不到 $h"
    }
    if {[llength [get_files -quiet -of_objects [get_filesets utils_1] $h]] == 0} {
        add_files -fileset utils_1 -norecurse $h
    }
}

# **apply_run_hooks 必须在所有 reset_run 之后调用**：reset_run 会把步骤属性恢复成策略默认值，
# 先设后 reset 等于没设（实测如此）。调用点在下面「先决策、后 reset、再统一挂 hook」那一段。
# 属性值没变就不写，避免让已完成的 run 过期（过期会导致下一次无法复用网表）。
proc apply_run_hooks {hook_filter hook_bs} {
    set changed 0
    foreach {run step} [list synth_1 SYNTH_DESIGN impl_1 INIT_DESIGN impl_1 WRITE_BITSTREAM] {
        set prop "STEPS.${step}.TCL.PRE"
        set want [expr {$step eq "WRITE_BITSTREAM" ? $hook_bs : $hook_filter}]
        if {[get_property $prop [get_runs $run]] ne $want} {
            set_property $prop $want [get_runs $run]
            incr changed
        }
    }
    return $changed
}

# 加入顶层约束：**已经在工程里就不要动**。实测 `remove_files` 会把已完成的 run 立刻标记为
# `Out-of-date`，于是每次运行都会白跑一遍综合/实现，`SYNTH_REUSED` / `IMPL_REUSED` 永远不生效。
set xdc_norm [file normalize $xdc_file]
set xdc_registered 0
foreach f [get_files -quiet -of_objects [get_filesets constrs_1]] {
    if {[file normalize $f] eq $xdc_norm} { set xdc_registered 1 }
}
if {$xdc_registered} {
    puts "XDC_REUSED: $xdc_file"
} else {
    foreach old [get_files -quiet -of_objects [get_filesets constrs_1] {*dpm_top.xdc}] {
        remove_files -quiet $old
    }
    add_files -fileset constrs_1 -norecurse $xdc_file
}
# 同理：属性值相同就不要写（重复设置也可能让 run 过期）
if {[get_property target_language [current_project]] ne "Verilog"} {
    set_property target_language Verilog [current_project]
}

# ---- 综合/实现策略（run 参数，必须在本轮 launch_runs 之前写）----
# GLOBAL_RETIMING：跨寄存器重定时（默认 auto）。DSP48 的时钟使能/复位路径上如果只有一层组合逻辑，
#   重定时能把它收进 DSP 内部，直接消掉这一段 LUT + 布线。
# DIRECTIVE PerformanceOptimized：以时序为目标做跨层次优化与寄存器复制。
# MAX_DSP：把 DSP 用量钉在已知规模上，避免综合器为了别处的时序把 32 位计数器搬来搬去
#   （本设计只需要 2 个 DSP48）；给 0 表示不设。
# 实现选 Performance_Retiming 而不是 Performance_ExplorePostRoutePhysOpt：前者的 phys_opt 用
#   AlternateFlowWithRetiming（官方描述：更激进的复制 + **DSP/BRAM 优化** + 开启重定时），正是本设计
#   需要的——关键路径终点是 DSP48 的控制脚。
# 【400MHz 轮次的历史实测】慢域 50MHz 参考 + DSP 400MHz 下（同一份综合网表、逐个内置策略跑
#   布局布线）；数字是历史记录，当前设计已是 450MHz（见下面「450MHz 轮次」）：
#     Performance_Retiming        WNS +0.270ns / WHS +0.095ns   ← 本仓库采用（策略沿用至今）
#     Performance_RefinePlacement WNS +0.109ns / WHS +0.053ns
#     Performance_ExplorePostRoutePhysOpt（本仓库早期采用） WNS +0.077ns
#     Performance_Auto_1/2/3      WNS +0.061ns
#     Performance_ExtraTimingOpt / Performance_ExploreWithRemap（跑到 write_bitstream 失败，未采用）
# 【450MHz 轮次（当前设计）】沿用同一套策略，2026-10-18 的完整综合/实现（Design State: Routed，
#   本轮未重跑 impl_multi.tcl）：
#   全设计 WNS +0.291ns / WHS +0.032ns / WPWS +0.067ns，TNS = THS = 0、0 个失败端点；
#   DSP 域内（clk_out1_dpm_system_clk_wiz_0_0，周期 2.222ns）WNS +0.291ns / WHS +0.163ns、0 失败端点。
#   来源 build/vivado/timing_summary.rpt，XSA 已生成。
# 有意**不**覆盖 Performance_Retiming 自带的步骤 directive：该策略的关键正是 phys_opt 的
#   AlternateFlowWithRetiming（DSP 优化 + 重定时），覆盖成 AggressiveExplore 会把它换掉，
#   实测（400MHz 轮次）WNS 从 +0.270ns 掉到 +0.077ns 一档。需要临时对比其它 directive 组合时，
#   用 query_runs.tcl 看当前取值再改。
set strategy_targets [list \
    [list synth_1 STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING [expr {$retiming ? "on" : "off"}]] \
    [list synth_1 STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE PerformanceOptimized] \
    [list impl_1 strategy Performance_Retiming]]
if {$dsp_budget > 0} {
    lappend strategy_targets [list synth_1 STEPS.SYNTH_DESIGN.ARGS.MAX_DSP $dsp_budget]
}
# 逐个核对：**只有当前值与目标值不同才写**。对已完成的 run，即使写入相同的值也会让它变成
# 过期状态（STATUS 不再是 Complete），下一次就无法复用网表——因此这里先比较再决定是否写。
# 属性值的格式由 Vivado 决定，用字符串比较即可（实测 "on" / "PerformanceOptimized" /
# "Performance_Retiming" / "2" 都能与写入值一致）。
# 注意用 lassign 拆每个三元组：foreach 的多变量形式是按「元素」而不是按「词」分组的，
# 直接写 `foreach {run prop value} $strategy_targets` 会把每个子列表整体赋给一个变量。
set strategy_changed 0
foreach item $strategy_targets {
    lassign $item run prop value
    set before [get_property $prop [get_runs $run]]
    if {$before eq $value} { continue }
    set_property $prop $value [get_runs $run]
    puts "STRATEGY_SET: [get_property name [get_runs $run]] $prop: '$before' -> '[get_property $prop [get_runs $run]]'"
    incr strategy_changed
}
puts "STRATEGY_BEGIN"
puts "synth_1 strategy = [get_property strategy [get_runs synth_1]]"
puts "impl_1  strategy = [get_property strategy [get_runs impl_1]]"
puts "synth retiming    = [get_property STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING [get_runs synth_1]]"
puts "synth directive   = [get_property STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE [get_runs synth_1]]"
# hook 文件位置（TCL.PRE 的实际挂载在 reset_run 之后由 apply_run_hooks 完成）
puts "synth hook = $hook_filter"
puts "impl  hook = $hook_filter (INIT_DESIGN) / $hook_bs (WRITE_BITSTREAM)"
# 只读 ARGS.DIRECTIVE：STEPS.<步骤>.IS_ENABLED 在这些步骤上不是合法属性（读它会报 [Common 17-673]）
foreach step {OPT_DESIGN PLACE_DESIGN PHYS_OPT_DESIGN ROUTE_DESIGN POST_ROUTE_PHYS_OPT_DESIGN} {
    puts "impl $step directive=[get_property -quiet STEPS.${step}.ARGS.DIRECTIVE [get_runs impl_1]]"
}
puts "STRATEGY_CHANGED: $strategy_changed"
puts "STRATEGY_END"

if {$strategy_only} {
    puts "STRATEGY_ONLY: 策略已就位（见上面的 STRATEGY_BEGIN/END），未启动 run（工程 $proj_file）"
    close_project
    exit 0
}

# 策略刚变化过 → 已完成的 run 是旧参数跑出来的，必须重跑，否则会静默复用旧网表。
set force_rerun [expr {$strategy_changed > 0}]
if {$force_rerun} {
    puts "STRATEGY_FORCE_RERUN: 综合/实现参数刚变化，已完成的 run 将重跑"
}

# ---- 先决定两个 run 是否复用，并把要重跑的 run 全部 reset 掉 ----
# reset_run 会把该 run 所有步骤的属性恢复成策略默认值（包括我们挂的 TCL.PRE hook），
# 因此 hook 必须在**所有** reset_run 之后统一挂一遍，不能夹在两次 reset 中间。
# 决策只看 run 当前状态，不受 reset 影响，所以先决策、后 reset。
set synth_reuse [expr {!$force_rerun && [string match "*Complete*" [get_property STATUS [get_runs synth_1]]]}]
set impl_reuse [expr {!$force_rerun && [string match "*Complete*" [get_property STATUS [get_runs impl_1]]]}]
if {!$synth_reuse} { reset_run synth_1 }
if {!$impl_reuse} { reset_run impl_1 }
set hook_changed [apply_run_hooks $hook_filter $hook_bs]
puts "HOOK_APPLIED: changed=$hook_changed"
foreach {run step} [list synth_1 SYNTH_DESIGN impl_1 INIT_DESIGN impl_1 WRITE_BITSTREAM] {
    puts "HOOK_CHECK: $run STEPS.${step}.TCL.PRE = '[get_property -quiet STEPS.${step}.TCL.PRE [get_runs $run]]'"
}

# ---- 综合 ----
if {$synth_reuse} {
    puts "SYNTH_REUSED"
} else {
    launch_runs synth_1 -jobs 8
    wait_on_run synth_1
}
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {
    error "综合失败：见 [get_property DIRECTORY [get_runs synth_1]]/runme.log"
}
puts "SYNTH_DONE"

# ---- 实现（布局布线 + bitstream）----
if {$impl_reuse} {
    puts "IMPL_REUSED"
} else {
    launch_runs impl_1 -to_step write_bitstream -jobs 8
    wait_on_run impl_1
}
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
    error "实现或 bitstream 失败：见 [get_property DIRECTORY [get_runs impl_1]]/runme.log"
}
puts "IMPL_DONE"

open_run impl_1
report_timing_summary -file [file join $build_dir timing_summary.rpt]
report_utilization -file [file join $build_dir utilization.rpt]
report_drc -file [file join $build_dir drc.rpt]
puts "TIMING_BEGIN"
puts [get_property STATS.WNS [get_runs impl_1]]
puts [get_property STATS.WHS [get_runs impl_1]]
puts "TIMING_END"

# ---- 导出硬件平台 ----
write_hw_platform -fixed -include_bit -force -file $xsa_file
puts "XSA_DONE: $xsa_file"
