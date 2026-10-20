# 查询 block design 工程的综合 / 实现运行选项
#
# 用法：
#   vivado -mode batch -source vivado/query_runs.tcl
#
# 前置条件：先运行 vivado/create_bd.tcl 生成工程（build/vivado/bd_proj）
#
# 输出：synth_1 / impl_1 两个 run 的策略名、状态，以及所有 STEPS.*.* 参数
#   （即每一步 synth_design / opt_design / place_design / phys_opt_design /
#     route_design / write_bitstream 的真实取值，含用户 pre-hook）。
#   GUI 里等价的查看位置：Project Manager → Project Settings… → Synthesis/Implementation
#   （策略与常用选项），以及 Window → Design Runs 选中 run 后的属性；而每个 run 实际
#   执行的命令行记录在 build/vivado/bd_proj/dpm_bd.runs/<run>/dpm_system_wrapper.tcl
#   与同目录的 runme.log（`Command:` 行）。

set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
set proj_file [file join $root_dir build vivado bd_proj dpm_bd.xpr]

if {![file exists $proj_file]} {
    error "找不到工程 $proj_file，请先运行 vivado/create_bd.tcl"
}

# 把 Vivado 的工作目录放到 build 下，避免在仓库根目录留下 .Xil 等临时文件
cd [file join $root_dir build vivado]
# 早期消息过滤必须先于 open_project 加载（Board 49-26 / Project 1-5713 在 open_project 内部打印）
source [file join $script_dir suppress_early_messages.tcl]
open_project $proj_file

puts "RUN_QUERY_BEGIN"
puts "part             = [get_property part [current_project]]"
puts "top              = [get_property top [current_fileset]]"

foreach run_name {synth_1 impl_1} {
    set run [get_runs $run_name]
    puts "==== $run_name : strategy = [get_property strategy $run] / status = [get_property STATUS $run]"
    # STEPS.<步骤>.ARGS.<参数> 是该步骤的参数，STEPS.<步骤>.IS_ENABLED 控制步骤是否执行，
    # STEPS.<步骤>.TCL.PRE/POST 是插入的钩子脚本
    foreach prop [lsort [list_property $run]] {
        if {[string match "STEPS.*" $prop]} {
            set value [get_property $prop $run]
            if {$value ne ""} {
                puts "  $prop = $value"
            }
        }
    }
    # 增量综合/实现相关属性（只读）：确认复用/重跑决策依赖的 checkpoint 状态。
    # INCREMENTAL_MODE 是综合步骤参数，只在 synth_1 上存在；实现侧的增量由
    # INCREMENTAL_CHECKPOINT 决定。属性不存在时安静返回空串（不同版本可能有差异）。
    foreach {label prop} [list \
        AUTO_INCREMENTAL_CHECKPOINT AUTO_INCREMENTAL_CHECKPOINT \
        INCREMENTAL_CHECKPOINT INCREMENTAL_CHECKPOINT \
        INCREMENTAL_MODE STEPS.SYNTH_DESIGN.ARGS.INCREMENTAL_MODE \
        WRITE_INCREMENTAL_SYNTH_CHECKPOINT WRITE_INCREMENTAL_SYNTH_CHECKPOINT] {
        set value ""
        catch { set value [get_property -quiet $prop $run] }
        puts "  $label = $value"
    }
}
puts "RUN_QUERY_END"
