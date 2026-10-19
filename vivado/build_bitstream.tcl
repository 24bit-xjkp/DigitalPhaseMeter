# 综合、实现、生成 bitstream 与 XSA（正常流水线的最后一步）
#
# 用法：
#   vivado -mode batch -source vivado/build_bitstream.tcl [-retiming 0|1] [-dsp <个数>] [-strategy-only]
#
#   -retiming       全局寄存器重定时，默认 0（off；当前 RTL 的 DSP 控制脚与两级流水结构不需要，
#                   打开反而可能把使能寄存器重新折进 DSP 的 CARRYIN 路径）
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
set retiming 0
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

# ---- 综合/实现策略（run 参数，必须在本次 launch_runs 之前写）----
# 【策略作用在哪个 run 上】本工程是 BD 工程：BD 顶层 wrapper 由 synth_1 综合，而 BD 里的 IP
#   默认各自做 out-of-context 综合（run 名 <ip>_synth_1）。**IP 的 OOC run 不继承 synth_1 的
#   STEPS.SYNTH_DESIGN.ARGS**：实测 dpm_top 的 OOC run 在写策略前是
#   DIRECTIVE=Default / GLOBAL_RETIMING=auto / MAX_DSP=-1，而真正包含 DSP 域 450MHz 逻辑的正是
#   dpm_top 这个自定义 IP（synth_1 只综合顶层 wrapper，几乎没有逻辑）。因此策略必须**同时**写到
#   synth_1 与自定义 IP 的 OOC run 上，否则等于没写（历史坑：此前只改了 synth_1）。
#   只处理本仓库打包的 dpm_top；厂商 IP（PS7 / clk_wiz / 互联等）保持默认，避免无关重跑与风险。
# 只按 IP 实例名匹配（前面不写 BD 名），这样 BD 改名后策略仍然落到位
set custom_ip_names {dpm_top_0_0}
set custom_ip_synth_runs {}
foreach ip_name $custom_ip_names {
    set runs [get_runs -quiet -filter "NAME =~ *${ip_name}_synth_1"]
    if {[llength $runs] == 0} {
        # 新建的 BD 工程刚 open 时**还没有** IP 的 OOC 综合 run：它们要等 launch_runs 才被建出来。
        # `launch_runs synth_1 -scripts_only` 只生成 run 脚本、不启动任何综合，正好用来把 IP run
        # 物化出来（实测；BD 管理的 IP 不能用 create_ip_run，会报 [Vivado 12-3424] IPI cores ...）。
        puts "IP_SYNTH_RUNS_MATERIALIZE: launch_runs synth_1 -scripts_only"
        launch_runs synth_1 -scripts_only
        set runs [get_runs -quiet -filter "NAME =~ *${ip_name}_synth_1"]
    }
    if {[llength $runs] > 0} {
        lappend custom_ip_synth_runs [get_property NAME [lindex $runs 0]]
    } else {
        # 不能静默跳过：策略没落到 450MHz 逻辑所在的 IP 上 = 没有策略（2026-10-19 的教训）
        error "找不到 $ip_name 的 OOC 综合 run：策略无法作用到高速域逻辑，检查 BD/IP 是否已正确建立"
    }
}
puts "CUSTOM_IP_SYNTH_RUNS: $custom_ip_synth_runs"

# GLOBAL_RETIMING：跨寄存器重定时，默认 off（可用 -retiming 1 打开）。当前 RTL 把两个计数器的
#   使能/清零写成 `when(clear){0}.elsewhen(enable){+1}`，让综合器映射成 DSP48E1 的 RSTP（同步
#   复位）+ CEP（时钟使能），使能由**寄存器直接**进 DSP 控制脚；打开重定时有可能把使能寄存器
#   重新折成 CARRYIN 的组合输入，反而把路径拉长（2026-10-19 之前的实现就是这种形态）。
# DIRECTIVE PerformanceOptimized：以时序为目标做跨层次优化与寄存器复制。
# MAX_DSP：DSP 用量上限，默认 2——两个计数器各占 1 个 DSP48E1，并防止综合器为别处时序再搬
#   逻辑进 DSP（DSP48E1 在 -1 速度等级下 450MHz 已用掉约 97% 的额定 FMAX）。给 0 表示不设。
# 实现选 Performance_Retiming 而不是 Performance_ExplorePostRoutePhysOpt：前者的 phys_opt 用
#   AlternateFlowWithRetiming（官方描述：更激进的复制 + **DSP/BRAM 优化** + 开启重定时），正是本设计
#   需要的——关键路径终点是 DSP48 的控制脚。
# 当前 450MHz 签核（2026-10-19，Design State: Routed）：全设计 WNS +0.291ns / WHS +0.052ns /
#   WPWS +0.067ns，TNS = THS = 0、0 个失败端点；同一策略用 impl_stability.tcl 做 6 次独立实现，
#   结果一致。策略对比调优用 impl_multi.tcl，签核以本脚本 + impl_stability 为准。
# 有意**不**覆盖 Performance_Retiming 自带的步骤 directive：该策略的关键正是 phys_opt 的
#   AlternateFlowWithRetiming，覆盖掉会明显损失余量。需要临时对比其它 directive 组合时，
#   用 query_runs.tcl 看当前取值再改。
set strategy_targets [list \
    [list synth_1 STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING [expr {$retiming ? "on" : "off"}]] \
    [list synth_1 STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE PerformanceOptimized] \
    [list impl_1 strategy Performance_Retiming]]
if {$dsp_budget > 0} {
    lappend strategy_targets [list synth_1 STEPS.SYNTH_DESIGN.ARGS.MAX_DSP $dsp_budget]
}
# 同一套综合策略也写到自定义 IP 的 OOC run 上（原因见本节开头）
foreach run $custom_ip_synth_runs {
    lappend strategy_targets [list $run STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING [expr {$retiming ? "on" : "off"}]]
    lappend strategy_targets [list $run STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE PerformanceOptimized]
    if {$dsp_budget > 0} {
        lappend strategy_targets [list $run STEPS.SYNTH_DESIGN.ARGS.MAX_DSP $dsp_budget]
    }
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
foreach run $custom_ip_synth_runs {
    puts "ip run $run: directive=[get_property STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE [get_runs $run]] \
retiming=[get_property STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING [get_runs $run]] \
max_dsp=[get_property STEPS.SYNTH_DESIGN.ARGS.MAX_DSP [get_runs $run]]"
}
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
if {!$synth_reuse} {
    # IP 的 OOC run 是 synth_1 的子 run，reset synth_1 不会连带 reset 它们；
    # 策略或 IP 内容变化后必须把 OOC run 一起 reset，否则会复用旧 checkpoint。
    foreach run $custom_ip_synth_runs { reset_run $run }
    reset_run synth_1
}
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
