# 综合 / 实现 stage 的 Tcl worker（Python 主控 dpm-vivado-flow 的执行后端）
#
# 用法：由 dpm-vivado-flow 生成的包装脚本调用（CONTRACT.md §5）：
#   set dpm_plan_file {/abs/.../build/vivado/.flow/plans/build.tcl}
#   source {/abs/.../vivado/flow/tcl/run_build_stage.tcl}
#
# 接口约定见 vivado/flow/CONTRACT.md：
#   - §1 职责边界：不做哈希/复用判断，只按计划执行线性 Vivado 命令；允许按 §4 根据
#        「当前属性值与目标值不同」把 run 加入实际 reset 列表；
#   - §2.1 计划 key；§3.1 结果文件；§4 reset 判定。
#   - 失败一律 error（包装脚本负责 exit 1），本文件内部不调用 exit。
#
# 本文件执行综合/实现策略、hook、reset、launch、报告、methodology 签核与 XSA 导出；
# 「是否复用 / 重跑哪个 run」由 Python 决定，并支持增量综合/实现与 materialize。

# ---------------------------------------------------------------------------
# 0. 读取计划文件（CONTRACT.md §2.1）
# ---------------------------------------------------------------------------
proc dpm_parse_plan_file {path} {
    # 兜底解析：计划文件是 `set dpm_plan(<key>) <value>` 的 Tcl sourceable 文件。优先
    # source；若 Python 侧把 list 值写成了多个 Tcl word（`set dpm_plan(k) a b c`，source
    # 时会报 wrong # args），按计划文件已知格式逐行读取（值仍按 Tcl list 语义解析）。
    # 正常情况下不会走到这里；这不改变接口，只是集成期的防御。
    global dpm_plan
    catch {unset dpm_plan}
    set fh [open $path r]
    set text [read $fh]
    close $fh
    foreach line [split $text "\n"] {
        set line [string trim $line]
        if {$line eq "" || [string index $line 0] eq "#"} { continue }
        if {![regexp {^set\s+dpm_plan\(([^)]+)\)\s*(.*)$} $line -> key rest]} {
            error "计划文件行格式无法识别：$line"
        }
        # `{}` 与空串都表示空 value（Python 侧空 list/空字符串都写成 {}）。
        if {$rest eq "" || $rest eq "{}"} {
            set dpm_plan($key) ""
        } else {
            set dpm_plan($key) [lrange $rest 0 end]
        }
    }
    if {![info exists dpm_plan]} {
        error "计划文件没有定义 dpm_plan(...)：$path"
    }
}

if {![info exists dpm_plan_file]} {
    error "run_build_stage.tcl 需要全局变量 dpm_plan_file 指向计划文件（CONTRACT.md §2）"
}
set _plan_src_err ""
if {[catch {source $dpm_plan_file} _plan_src_err]} {
    puts "PLAN_SOURCE_FALLBACK: source 计划文件失败（$_plan_src_err），按 set dpm_plan(...) 逐行读取"
    dpm_parse_plan_file $dpm_plan_file
} elseif {![info exists dpm_plan]} {
    error "计划文件没有定义 dpm_plan(...)：$dpm_plan_file"
}

# 计划 key 归一：Python 正常会全部给出，这里只把缺失 key 补成空串，
# 避免下面每取一个值都要写 info exists。
foreach _key {
    stage project part xdc suppress_early
    hook_filter hook_waivers hook_vendor_ip
    strategy_targets hook_assignments reset_runs materialize
    incremental_synth incremental_impl launch_synth launch_impl
    report_only strategy_only xsa build_dir result_file
} {
    if {![info exists dpm_plan($_key)]} { set dpm_plan($_key) "" }
}

if {$dpm_plan(stage) ne "build"} {
    error "run_build_stage.tcl 只处理 stage=build（计划里是 '$dpm_plan(stage)'）"
}
if {$dpm_plan(project) eq ""} { error "计划缺少 project（工程 .xpr 路径）" }
if {$dpm_plan(result_file) eq ""} { error "计划缺少 result_file（结果文件路径）" }
if {$dpm_plan(build_dir) eq ""} { error "计划缺少 build_dir" }
if {$dpm_plan(part) eq ""} { error "计划缺少 part" }

# 结果文件只在成功路径写；先删掉上一轮结果，避免失败后 Python 读到旧结果。
if {[file exists $dpm_plan(result_file)]} {
    file delete -force $dpm_plan(result_file)
}

# ---------------------------------------------------------------------------
# 1. 通用工具
#    写法与既有脚本保持一致：file join、get_property -quiet、catch。
# ---------------------------------------------------------------------------
proc dpm_get_prop {obj prop} {
    # 对对象（工程 / run / 文件 / 地址段 ……）读属性；属性不存在时返回空串，不把
    # [Common 17-673] 这类错误抛给调用方（需要区分「不存在」时用 dpm_find_run_prop）。
    set value ""
    catch { set value [get_property -quiet $prop $obj] }
    return $value
}

proc dpm_run_prop {run prop} {
    set value ""
    catch { set value [get_property -quiet $prop [get_runs $run]] }
    return $value
}

proc dpm_run_exists {name} {
    if {$name eq ""} { return 0 }
    return [expr {[llength [get_runs -quiet $name]] > 0}]
}

proc dpm_run_names {pattern} {
    # 返回当前工程里名字匹配 pattern 的 run 名（保持 get_runs 的顺序）。
    set names {}
    foreach run [get_runs -quiet] {
        set name [dpm_get_prop $run NAME]
        if {[string match $pattern $name]} { lappend names $name }
    }
    return $names
}

proc dpm_is_synth_run {name} {
    return [expr {$name eq "synth_1" || [string match "*_synth_1" $name]}]
}

proc dpm_flag {value} {
    # 计划里的 0/1（Python 写成 0 或 1；空串按 0 处理）。
    return [expr {$value ne "" && $value ne "0"}]
}

proc dpm_add_unique {listVar item} {
    upvar 1 $listVar target
    if {[lsearch -exact $target $item] < 0} { lappend target $item }
}

proc dpm_register_utils {path what} {
    # hook / 参考 DCP 必须登记进 utils_1 fileset，否则 Vivado 报
    # [Runs 36-537]「文件不属于工程文件集」。
    if {$path eq ""} { return }
    if {![file exists $path]} { error "找不到${what}：$path" }
    if {[llength [get_files -quiet -of_objects [get_filesets utils_1] $path]] == 0} {
        add_files -fileset utils_1 -norecurse $path
        puts "UTILS_FILE_ADDED: $path"
    }
}

proc dpm_materialize {} {
    # 新建 BD 工程刚 open 时，IP 的 OOC 综合 run 还不存在（BD 管理的 IP 不能用
    # create_ip_run，会报 [Vivado 12-3424]）。`-scripts_only` 只生成 run 脚本、
    # 不启动综合，正好把 IP run 物化出来（实测；见 dpm-vivado-ip）。
    # 注意：本命令只能在整个 reset 阶段之前调用一次，reset 之后正式 launch 时
    # 会重新生成脚本；再调 scripts_only 会让紧随其后的 launch 报 [Common 17-69]。
    puts "IP_SYNTH_RUNS_MATERIALIZE: launch_runs synth_1 -scripts_only"
    # 原位刷新（upgrade_ip / generate_target / save_bd_design）之后，synth_1 可能已被
    # 标成需要 reset；此时直接 scripts_only 会报 [Common 17-69]。先 reset 再重试一次。
    if {[catch {launch_runs synth_1 -scripts_only} _mat_err]} {
        puts "MATERIALIZE_RETRY: $_mat_err"
        reset_run synth_1
        launch_runs synth_1 -scripts_only
    }
}

proc dpm_find_run_prop {run prop} {
    # 在 run 的属性表里按大小写不敏感方式找到规范的属性名；不存在返回空串。
    # 用途：写 INCREMENTAL_MODE 这类「不同 Vivado 版本可能不存在」的属性时先确认。
    set target [string tolower $prop]
    foreach name [list_property [get_runs $run]] {
        if {[string tolower $name] eq $target} { return $name }
    }
    return ""
}

proc dpm_set_run_prop {run prop value} {
    # 安静地写 run 属性：属性不存在时打印跳过原因并返回 0（由调用方决定是否致命）。
    set actual [dpm_find_run_prop $run $prop]
    if {$actual eq ""} {
        puts "RUN_PROP_SKIP: $run 没有属性 $prop（当前 Vivado 版本可能不支持）"
        return 0
    }
    set_property $actual $value [get_runs $run]
    puts "RUN_PROP_SET: $run $actual = '$value'"
    return 1
}

proc dpm_run_log_has_incremental {run} {
    # CONTRACT.md §3.1：确认 runme.log 里真的出现 `read_checkpoint -incremental`。
    set dir ""
    catch { set dir [get_property -quiet DIRECTORY [get_runs $run]] }
    if {$dir eq ""} { return 0 }
    set log_file [file join $dir runme.log]
    if {![file isfile $log_file]} { return 0 }
    set fh [open $log_file r]
    set text [read $fh]
    close $fh
    return [regexp {read_checkpoint\s+-incremental} $text]
}

proc dpm_parse_timing_fep {rpt_file} {
    # 兜底：从 report_timing_summary 的 Design Timing Summary 里取失败端点数
    # （setup FEP + hold FEP）。列序 WNS TNS TNS_FEP TNS_TEP WHS THS THS_FEP THS_TEP。
    if {![file isfile $rpt_file]} { return "NA" }
    set fh [open $rpt_file r]
    set text [read $fh]
    close $fh
    set lines [split $text "\n"]
    set idx -1
    for {set i 0} {$i < [llength $lines]} {incr i} {
        if {[string match "*Design Timing Summary*" [lindex $lines $i]]} { set idx $i; break }
    }
    if {$idx < 0} { return "NA" }
    for {set i [expr {$idx + 1}]} {$i < [llength $lines]} {incr i} {
        set fields [regexp -all -inline {\S+} [lindex $lines $i]]
        if {[llength $fields] >= 8 &&
            [string is double -strict [lindex $fields 0]] &&
            [string is integer -strict [lindex $fields 2]]} {
            if {[string is integer -strict [lindex $fields 6]]} {
                return [expr {[lindex $fields 2] + [lindex $fields 6]}]
            }
            return [lindex $fields 2]
        }
    }
    return "NA"
}

proc dpm_stat {run prop} {
    set value [dpm_run_prop $run $prop]
    if {$value eq ""} { return "NA" }
    return $value
}

proc dpm_failed_endpoints {run build_dir} {
    # CONTRACT.md §3.1 要求取 impl_1 的 STATS.*；不同版本里失败端点数的属性名
    # 可能是 STATS.FAILED_ENDPOINTS 或 STATS.FAILED_NETS，两者都不存在时用本次
    # report_timing_summary 的统计行兜底（结果仍来自 impl_1 的数据）。
    foreach prop {STATS.FAILED_ENDPOINTS STATS.FAILED_NETS} {
        set value [dpm_run_prop $run $prop]
        if {$value ne ""} {
            puts "FAILED_ENDPOINTS_SOURCE: $prop"
            return $value
        }
    }
    set value [dpm_parse_timing_fep [file join $build_dir timing_summary.rpt]]
    puts "FAILED_ENDPOINTS_SOURCE: timing_summary.rpt（STATS.* 属性不可用，取报告统计行）"
    return $value
}

proc dpm_parse_methodology {impl_dir build_dir} {
    # 解析实现 run 自动生成的 methodology 报告（签核产物）。
    # route_design 末尾自动跑 report_methodology，waiver 由 ROUTE_DESIGN.TCL.PRE 在
    # run 进程里创建；主进程 open_run 时拿不到这些 waiver，所以直接读 run 目录里的报告。
    set rpts [glob -nocomplain -directory $impl_dir *_methodology_drc_routed.rpt]
    if {[llength $rpts] == 0} {
        error "找不到实现的 methodology 报告：$impl_dir/*_methodology_drc_routed.rpt"
    }
    set rpt [lindex $rpts 0]
    file copy -force $rpt [file join $build_dir methodology.rpt]
    set fh [open $rpt r]
    set text [read $fh]
    close $fh
    if {![regexp {Checks found:\s+([0-9]+)} $text -> found]} {
        error "methodology 报告格式无法识别：$rpt"
    }
    set waived 0
    if {[regexp {Checks waived:\s+([0-9]+)} $text -> waived]} {}
    if {[regexp {Critical Warning} $text]} {
        error "实现存在未 waive 的 methodology Critical Warning，见 $rpt"
    }
    if {$found != 0} {
        error "实现 methodology 仍有 $found 条未 waive 的检查项，见 $rpt"
    }
    puts "METHODOLOGY_CHECK: found=$found waived=$waived report=$rpt"
    return [list $found $waived]
}

proc dpm_write_result {path lines} {
    if {$path eq ""} { error "计划没有给出 result_file" }
    file mkdir [file dirname $path]
    set fh [open $path w]
    foreach line $lines { puts $fh $line }
    close $fh
    puts "RESULT_FILE: $path"
}

proc dpm_report_strategy_summary {custom_runs} {
    puts "STRATEGY_BEGIN"
    puts "synth_1 strategy = [dpm_run_prop synth_1 strategy]"
    puts "impl_1  strategy = [dpm_run_prop impl_1 strategy]"
    puts "synth retiming    = [dpm_run_prop synth_1 STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING]"
    puts "synth directive   = [dpm_run_prop synth_1 STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE]"
    foreach run $custom_runs {
        puts "ip run $run: directive=[dpm_run_prop $run STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE] \
retiming=[dpm_run_prop $run STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING] \
max_dsp=[dpm_run_prop $run STEPS.SYNTH_DESIGN.ARGS.MAX_DSP]"
    }
    # 只读 ARGS.DIRECTIVE：STEPS.<步骤>.IS_ENABLED 在这些步骤上不是合法属性
    # （读它会报 [Common 17-673]）。
    foreach step {OPT_DESIGN PLACE_DESIGN PHYS_OPT_DESIGN ROUTE_DESIGN POST_ROUTE_PHYS_OPT_DESIGN} {
        puts "impl $step directive=[dpm_run_prop impl_1 STEPS.${step}.ARGS.DIRECTIVE]"
    }
    puts "STRATEGY_END"
}

# ==== 以上为通用工具，以下为主流程 ====

# ---------------------------------------------------------------------------
# 2. 进入 build 目录、加载早期消息过滤、打开工程、校验器件
# ---------------------------------------------------------------------------
set build_dir $dpm_plan(build_dir)
set proj_file $dpm_plan(project)

# 早期过滤必须先于 open_project：Board 49-26 / Project 1-5713 / IP_Flow 19-3664 等
# 是在 open_project 内部打印的，过滤规则必须先加载才拦得住（实测）。
if {$dpm_plan(suppress_early) ne ""} {
    if {![file exists $dpm_plan(suppress_early)]} {
        error "找不到早期消息过滤脚本：$dpm_plan(suppress_early)"
    }
    source $dpm_plan(suppress_early)
}

# 把 Vivado 的工作目录放到 build 下，避免在仓库根目录留下 .Xil 等临时文件
file mkdir $build_dir
cd $build_dir

if {![file exists $proj_file]} {
    error "找不到工程 $proj_file，请先运行 create_bd 阶段"
}
open_project $proj_file

# 器件绑定校验：本工程固定 xc7z010clg400-1（AX7010 的芯片）。回读比计划里写的 part，
# 避免工程被意外换器件后仍然产出「看起来通过」的结果。
set project_part [dpm_get_prop [current_project] part]
puts "PROJECT_PART: $project_part"
if {$project_part ne $dpm_plan(part)} {
    error "工程绑定的器件是 '$project_part'，计划要求 '$dpm_plan(part)'（本仓库固定 xc7z010clg400-1）"
}

set report_only [dpm_flag $dpm_plan(report_only)]
set strategy_only [dpm_flag $dpm_plan(strategy_only)]
if {$report_only && $strategy_only} {
    # Python 侧 --report-only 与 --strategy-only 同时给出时，report_only 优先：
    # 计划里 strategy_targets 已被置空，只重出报告与 XSA。
    puts "REPORT_STRATEGY_CONFLICT: report_only 优先，忽略 strategy_only"
    set strategy_only 0
}

# 主流程状态（结果文件用）
set reset_order {}
set strategy_changed {}
set runs_seen {}
set synth_ran 0
set impl_ran 0
set incremental_synth_targets {}
set incremental_impl_ref ""
set custom_ip_synth_runs {}
set meth_found "NA"
set meth_waived "NA"
set WNS "NA"
set WHS "NA"
set TNS "NA"
set THS "NA"
set FAILED_ENDPOINTS "NA"
set xsa_value ""

if {$report_only} {
    # report_only：不 reset、不写属性、不 launch，只重出报告与 XSA（CONTRACT.md §2.1）。
    puts "REPORT_ONLY: 只重出报告与 XSA（不 reset / 不写属性 / 不 launch）"
} else {
    # -----------------------------------------------------------------------
    # 3. 工程级准备：hook 文件登记、顶层约束、target_language
    # -----------------------------------------------------------------------
    # 所有 hook 都要加进 utils_1 fileset，否则 Vivado 会抱怨 hook 脚本不属于工程
    # （[Runs 36-537]）。hook 文件本身不在这里修改。
    dpm_register_utils $dpm_plan(hook_filter) "综合/实现消息过滤 hook"
    dpm_register_utils $dpm_plan(hook_waivers) "methodology waiver hook"
    dpm_register_utils $dpm_plan(hook_vendor_ip) "厂商 IP OOC 过滤 hook"

    # 加入顶层约束：**已经在工程里就不要动**。实测 remove_files 会把已完成的 run
    # 立刻标记为 Out-of-date，于是每次运行都会白跑一遍综合/实现。
    if {$dpm_plan(xdc) ne ""} {
        if {![file exists $dpm_plan(xdc)]} {
            error "找不到顶层约束文件：$dpm_plan(xdc)"
        }
        set xdc_norm [file normalize $dpm_plan(xdc)]
        set xdc_registered 0
        foreach f [get_files -quiet -of_objects [get_filesets constrs_1]] {
            if {[file normalize $f] eq $xdc_norm} { set xdc_registered 1 }
        }
        if {$xdc_registered} {
            puts "XDC_REUSED: $dpm_plan(xdc)"
        } else {
            # 同名的旧约束（例如工程从旧目录迁移过来）先删掉，再登记计划里的这份。
            set xdc_pattern "*[file tail $dpm_plan(xdc)]"
            foreach old [get_files -quiet -of_objects [get_filesets constrs_1] $xdc_pattern] {
                remove_files -quiet $old
            }
            add_files -fileset constrs_1 -norecurse $dpm_plan(xdc)
            puts "XDC_ADDED: $dpm_plan(xdc)"
        }
    } else {
        puts "XDC_SKIP: 计划没有给出 xdc"
    }

    # 属性值相同就不要写（重复设置也可能让 run 过期）
    if {[dpm_get_prop [current_project] target_language] ne "Verilog"} {
        set_property target_language Verilog [current_project]
    }

    # -----------------------------------------------------------------------
    # 4. materialize：把 IP 的 OOC 综合 run 物化出来
    # -----------------------------------------------------------------------
    set synth_will_run [dpm_flag $dpm_plan(launch_synth)]
    foreach r $dpm_plan(reset_runs) {
        if {[dpm_is_synth_run $r]} { set synth_will_run 1 }
    }

    set materialized 0
    if {[dpm_flag $dpm_plan(materialize)] && ($synth_will_run || $strategy_only)} {
        dpm_materialize
        set materialized 1
    }
    # 兜底：计划要启动综合，但目标里提到的 `*_synth_1` run（含自研 dpm_top 的
    # OOC run）还不存在时，补一次 scripts_only。
    if {!$materialized && ($synth_will_run || $strategy_only)} {
        set need_materialize 0
        if {[llength [dpm_run_names "*dpm_top_0_0*"]] == 0} { set need_materialize 1 }
        if {!$need_materialize} {
            foreach item [concat $dpm_plan(strategy_targets) $dpm_plan(hook_assignments)] {
                lassign $item r
                if {$r eq "synth_1"} { continue }
                if {[string match "*_synth_1" $r] && ![dpm_run_exists $r]} {
                    set need_materialize 1
                    break
                }
            }
        }
        if {$need_materialize} {
            dpm_materialize
            set materialized 1
        }
    }

    # 自研 IP 的 OOC 综合 run：策略必须写到这里（450MHz 逻辑就在这个 run 里）。
    # 找不到它会直接 error（「不能静默跳过」）。
    set custom_ip_synth_runs [dpm_run_names "*dpm_top_0_0*"]
    if {[llength $dpm_plan(strategy_targets)] > 0 && [llength $custom_ip_synth_runs] > 0} {
        set custom_covered 0
        foreach item $dpm_plan(strategy_targets) {
            lassign $item run prop value
            if {[lsearch -exact $custom_ip_synth_runs $run] >= 0} { set custom_covered 1 }
        }
        if {!$custom_covered} {
            error "计划的 strategy_targets 没有作用到自研 IP 的 OOC 综合 run（$custom_ip_synth_runs）：\
450MHz 逻辑就在这个 run 里，策略不落位等于没有策略（见 dpm-vivado-ip）"
        }
    }

    if {$strategy_only} {
        # -------------------------------------------------------------------
        # 4a. strategy_only：只写策略，不 reset / 不 launch
        #     （属性值相同不写，避免让已完成的 run 过期。）
        # -------------------------------------------------------------------
        foreach item $dpm_plan(strategy_targets) {
            if {[llength $item] != 3} {
                error "strategy_targets 条目格式应为 {run property value}：{$item}"
            }
            lassign $item run prop value
            if {![dpm_run_exists $run]} {
                error "strategy_targets 里的 run '$run' 在工程里不存在"
            }
            set before [dpm_run_prop $run $prop]
            if {$before eq $value} { continue }
            set_property $prop $value [get_runs $run]
            puts "STRATEGY_SET: $run $prop: '$before' -> '[dpm_run_prop $run $prop]'"
            dpm_add_unique strategy_changed $run
        }
        dpm_report_strategy_summary $custom_ip_synth_runs
        puts "STRATEGY_ONLY: 策略已就位（未启动任何 run；工程 $proj_file）"
    } else {
        # -------------------------------------------------------------------
        # 4b. 正常 build：读当前值 → 差异决定实际 reset 列表 → 先 reset 再写属性
        #     → launch
        # -------------------------------------------------------------------
        # 厂商 IP 的 OOC 综合 run：独立进程，不继承 synth_1 的 TCL.PRE，需要单独挂
        # 厂商过滤器。排除 synth_1 与自研 IP/模块（dpm_top / dpm_src_drp / dpm_mmcm_src），
        # 自研代码的告警必须留在日志里，不能被厂商过滤器掩盖。
        set vendor_runs {}
        foreach name [dpm_run_names "*_synth_1"] {
            if {$name eq "synth_1"} { continue }
            if {[string match "*dpm_top_0_0*" $name] ||
                [string match "*dpm_src_drp_0_0*" $name] ||
                [string match "*dpm_mmcm_src_0_0*" $name]} { continue }
            lappend vendor_runs $name
        }
        puts "VENDOR_IP_SYNTH_RUNS: $vendor_runs"

        # ---- 策略目标：读当前值，不同才把 run 加入实际 reset 列表（CONTRACT.md §4）----
        set strategy_targets {}
        set pending_reset {}
        set reset_from_plan {}
        foreach item $dpm_plan(strategy_targets) {
            if {[llength $item] != 3} {
                error "strategy_targets 条目格式应为 {run property value}：{$item}"
            }
            lassign $item run prop value
            if {![dpm_run_exists $run]} {
                error "strategy_targets 里的 run '$run' 在工程里不存在（物化后仍找不到）"
            }
            lappend strategy_targets [list $run $prop $value]
            set before [dpm_run_prop $run $prop]
            if {$before ne $value} {
                puts "STRATEGY_DIFF: $run $prop: '$before' -> '$value'"
                dpm_add_unique strategy_changed $run
                dpm_add_unique pending_reset $run
            }
        }

        # ---- hook 目标（计划给出的 + 厂商 IP OOC run 自动补的）----
        set hook_targets {}
        foreach item $dpm_plan(hook_assignments) {
            if {[llength $item] != 3} {
                error "hook_assignments 条目格式应为 {run step hook}：{$item}"
            }
            lassign $item run step hook
            if {![dpm_run_exists $run]} {
                error "hook_assignments 里的 run '$run' 在工程里不存在"
            }
            lappend hook_targets [list $run $step $hook]
            set explicit_hook("$run|$step") 1
        }
        # 首次构建时 Python 还不知道物化后出现的厂商 IP run 名（runs.json 为空），
        # 所以这里统一给「非 synth_1、非自研」的 *_synth_1 run 挂厂商过滤器；
        # 计划里显式给了同一 (run, step) 的则尊重计划，不覆盖。
        if {$dpm_plan(hook_vendor_ip) ne ""} {
            foreach run $vendor_runs {
                if {[info exists explicit_hook("$run|SYNTH_DESIGN")]} { continue }
                lappend hook_targets [list $run SYNTH_DESIGN $dpm_plan(hook_vendor_ip)]
            }
        }
        foreach item $hook_targets {
            lassign $item run step hook
            set prop "STEPS.${step}.TCL.PRE"
            set before [dpm_run_prop $run $prop]
            if {$before ne $hook} {
                puts "HOOK_DIFF: $run $prop: '$before' -> '$hook'"
                dpm_add_unique pending_reset $run
            }
        }

        # ---- 计划判定必须重跑的 run ----
        foreach r $dpm_plan(reset_runs) {
            if {![dpm_run_exists $r]} {
                error "reset_runs 里的 run '$r' 在工程里不存在（工程被重建过？见 dpm-vivado-flow）"
            }
            dpm_add_unique reset_from_plan $r
            dpm_add_unique pending_reset $r
        }

        # ---- 增量综合/实现：参考 DCP 登记 + 属性目标 + 差异判定 ----
        foreach item $dpm_plan(incremental_synth) {
            if {[llength $item] != 2} {
                error "incremental_synth 条目格式应为 {run ref_dcp}：{$item}"
            }
            lassign $item run ref_dcp
            if {![dpm_run_exists $run]} {
                error "incremental_synth 里的 run '$run' 在工程里不存在"
            }
            if {![file exists $ref_dcp]} {
                error "找不到增量综合参考 DCP：$ref_dcp"
            }
            lappend incremental_synth_targets [list $run $ref_dcp]
            dpm_register_utils $ref_dcp "增量综合参考 DCP"
            set before [dpm_run_prop $run INCREMENTAL_CHECKPOINT]
            set same 0
            if {$before eq $ref_dcp} {
                set same 1
            } elseif {$before ne "" && [file normalize $before] eq [file normalize $ref_dcp]} {
                set same 1
            }
            if {!$same} {
                puts "INCREMENTAL_SYNTH_DIFF: $run checkpoint: '$before' -> '$ref_dcp'"
                dpm_add_unique pending_reset $run
            }
        }
        set incremental_impl_ref $dpm_plan(incremental_impl)
        if {$incremental_impl_ref ne ""} {
            if {![file exists $incremental_impl_ref]} {
                error "找不到实现增量参考 DCP：$incremental_impl_ref"
            }
            dpm_register_utils $incremental_impl_ref "实现增量参考 DCP"
            set before [dpm_run_prop impl_1 INCREMENTAL_CHECKPOINT]
            set same 0
            if {$before eq $incremental_impl_ref} {
                set same 1
            } elseif {$before ne "" && [file normalize $before] eq [file normalize $incremental_impl_ref]} {
                set same 1
            }
            if {!$same} {
                puts "INCREMENTAL_IMPL_DIFF: impl_1 checkpoint: '$before' -> '$incremental_impl_ref'"
                dpm_add_unique pending_reset impl_1
            }
        }

        # ---- 计划要求启动的阶段必须先 reset（对已完成的 run 直接 launch 会失败）----
        if {[dpm_flag $dpm_plan(launch_synth)]} { dpm_add_unique pending_reset synth_1 }
        if {[dpm_flag $dpm_plan(launch_impl)]} { dpm_add_unique pending_reset impl_1 }

        # ---- 依赖级联：任一综合 run 要重跑 → synth_1 与 impl_1 都必须重跑 ----
        # synth_1 汇总 IP OOC run 的产物；实现依赖 synth_1 的网表。Python 正常会给出
        # 一致的 reset_runs，这里兜底「worker 按属性差异自己发现的变化」。
        set any_synth_reset 0
        foreach r $pending_reset {
            if {[dpm_is_synth_run $r]} { set any_synth_reset 1 }
        }
        if {$any_synth_reset} {
            dpm_add_unique pending_reset synth_1
            dpm_add_unique pending_reset impl_1
        }
        foreach r {synth_1 impl_1} {
            if {![dpm_run_exists $r]} { error "工程里缺少 run '$r'" }
        }

        # ---- 实际 reset 列表（CONTRACT.md §4 第 3/5 条）：synth_1 → 其他 *_synth_1 → impl_1 ----
        if {[lsearch -exact $pending_reset synth_1] >= 0} { lappend reset_order synth_1 }
        foreach name [dpm_run_names "*_synth_1"] {
            if {$name eq "synth_1"} { continue }
            if {[lsearch -exact $pending_reset $name] >= 0} { lappend reset_order $name }
        }
        if {[lsearch -exact $pending_reset impl_1] >= 0} { lappend reset_order impl_1 }
        foreach r $pending_reset {
            if {[lsearch -exact $reset_order $r] < 0} { lappend reset_order $r }
        }
        puts "RESET_RUNS: $reset_order"
        if {[llength $reset_from_plan] > 0} {
            puts "RESET_FROM_PLAN: $reset_from_plan"
        }

        # ---- 先 reset：reset_run 会把步骤属性恢复成策略默认值（含 TCL.PRE），
        #      所以策略/hook 的写入必须全部放在 reset 之后 ----
        foreach r $reset_order {
            puts "RESET_RUN: $r"
            reset_run $r
        }

        # ---- 非增量 reset run：清掉可能残留的增量参考 checkpoint ----
        # reset_run 不会清 run 级的 INCREMENTAL_CHECKPOINT / AUTO_INCREMENTAL_CHECKPOINT；
        # 上一次 --incremental-* 实验留下的设置会泄漏到本次普通 build（实测 synth_1 在
        # 未指定增量时仍执行了 read_checkpoint -incremental）。这里只对本次 reset 的 run、
        # 且不在计划增量目标里的 run 清空。
        set _incremental_synth_runs {}
        foreach item $incremental_synth_targets {
            lassign $item _inc_run _inc_ref
            dpm_add_unique _incremental_synth_runs $_inc_run
        }
        foreach r $reset_order {
            if {[lsearch -exact $_incremental_synth_runs $r] >= 0} { continue }
            dpm_set_run_prop $r INCREMENTAL_CHECKPOINT ""
            dpm_set_run_prop $r AUTO_INCREMENTAL_CHECKPOINT 0
        }
        if {[lsearch -exact $reset_order impl_1] >= 0 && $incremental_impl_ref eq ""} {
            dpm_set_run_prop impl_1 INCREMENTAL_CHECKPOINT ""
            dpm_set_run_prop impl_1 AUTO_INCREMENTAL_CHECKPOINT 0
        }

        # ---- 再写策略（只写在本次实际 reset 的 run 上；未 reset 的 run 不碰，
        #      避免把已完成的 run 标成过期）。先写 run 级 strategy，再写步骤参数，
        #      因为给 run 换 strategy 会把步骤参数重置回该 strategy 的预设。 ----
        foreach item $strategy_targets {
            lassign $item run prop value
            if {[lsearch -exact $reset_order $run] < 0} { continue }
            if {[string tolower $prop] ne "strategy"} { continue }
            set before [dpm_run_prop $run $prop]
            set_property $prop $value [get_runs $run]
            puts "STRATEGY_SET: $run $prop: '$before' -> '[dpm_run_prop $run $prop]'"
        }
        foreach item $strategy_targets {
            lassign $item run prop value
            if {[lsearch -exact $reset_order $run] < 0} { continue }
            if {[string tolower $prop] eq "strategy"} { continue }
            set before [dpm_run_prop $run $prop]
            set_property $prop $value [get_runs $run]
            puts "STRATEGY_SET: $run $prop: '$before' -> '[dpm_run_prop $run $prop]'"
        }
        dpm_report_strategy_summary $custom_ip_synth_runs

        # ---- 增量属性（必须在 reset 之后写；reset 会把步骤属性清回默认值）----
        foreach item $incremental_synth_targets {
            lassign $item run ref_dcp
            if {[lsearch -exact $reset_order $run] < 0} { continue }
            # AUTO_INCREMENTAL_CHECKPOINT：关掉「自动沿用上一轮 checkpoint」，
            # 只用计划显式给出的参考 DCP。
            if {![dpm_set_run_prop $run AUTO_INCREMENTAL_CHECKPOINT 0]} {
                puts "INCREMENTAL_AUTO_SKIP: $run 没有 AUTO_INCREMENTAL_CHECKPOINT 属性"
            }
            if {![dpm_set_run_prop $run INCREMENTAL_CHECKPOINT $ref_dcp]} {
                error "run '$run' 没有 INCREMENTAL_CHECKPOINT 属性，当前 Vivado 版本无法启用增量综合"
            }
            # INCREMENTAL_MODE：off/quick/default/aggressive；default = 做完整逻辑优化
            # （含边界优化）。属性不存在时提示但不失败（工具可能不需要它）。
            if {![dpm_set_run_prop $run STEPS.SYNTH_DESIGN.ARGS.INCREMENTAL_MODE default]} {
                puts "INCREMENTAL_SYNTH_MODE_SKIP: $run 没有 STEPS.SYNTH_DESIGN.ARGS.INCREMENTAL_MODE"
            }
        }
        if {$incremental_impl_ref ne "" && [lsearch -exact $reset_order impl_1] >= 0} {
            if {![dpm_set_run_prop impl_1 AUTO_INCREMENTAL_CHECKPOINT 0]} {
                puts "INCREMENTAL_AUTO_SKIP: impl_1 没有 AUTO_INCREMENTAL_CHECKPOINT 属性"
            }
            if {![dpm_set_run_prop impl_1 INCREMENTAL_CHECKPOINT $incremental_impl_ref]} {
                error "run 'impl_1' 没有 INCREMENTAL_CHECKPOINT 属性，当前 Vivado 版本无法启用增量实现"
            }
            # 实现侧没有独立的 INCREMENTAL_MODE 步骤参数：增量由上面的参考 checkpoint
            # 决定（run 脚本会注入 read_checkpoint -incremental），是否真的启用由
            # runme.log 的确认检查负责（见结果文件 INCREMENTAL_IMPL_CONFIRMED）。
        }

        # ---- 最后挂 hook（同样只在本次实际 reset 的 run 上）----
        foreach item $hook_targets {
            lassign $item run step hook
            if {[lsearch -exact $reset_order $run] < 0} { continue }
            set_property STEPS.${step}.TCL.PRE $hook [get_runs $run]
        }
        puts "HOOK_APPLIED: reset_runs=$reset_order"
        foreach item $hook_targets {
            lassign $item run step hook
            puts "HOOK_CHECK: $run STEPS.${step}.TCL.PRE = '[dpm_run_prop $run STEPS.${step}.TCL.PRE]'"
        }

        # ---- 启动（结果里的 *_RAN 以这里实际启动为准）----
        set launch_synth [expr {[lsearch -exact $reset_order synth_1] >= 0}]
        set launch_impl [expr {[lsearch -exact $reset_order impl_1] >= 0}]
        puts "LAUNCH_PLAN: synth=$launch_synth impl=$launch_impl"

        if {$launch_synth} {
            launch_runs synth_1 -jobs 8
            wait_on_run synth_1
            set synth_ran 1
        }
        set synth_progress [dpm_run_prop synth_1 PROGRESS]
        if {$synth_progress ne "100%"} {
            error "综合未完成（PROGRESS='$synth_progress'，STATUS='[dpm_run_prop synth_1 STATUS]'）：\
见 [dpm_run_prop synth_1 DIRECTORY]/runme.log"
        }

        if {$launch_impl} {
            launch_runs impl_1 -to_step write_bitstream -jobs 8
            wait_on_run impl_1
            set impl_ran 1
        }
        set impl_progress [dpm_run_prop impl_1 PROGRESS]
        if {$launch_impl && $impl_progress ne "100%"} {
            error "实现或 bitstream 失败（PROGRESS='$impl_progress'，STATUS='[dpm_run_prop impl_1 STATUS]'）：\
见 [dpm_run_prop impl_1 DIRECTORY]/runme.log"
        }
        # 未要求启动实现时允许实现尚未完成（synth-only 场景），结果里 WNS 等写 NA。
        puts "SYNTH_RAN: $synth_ran PROGRESS=$synth_progress"
        puts "IMPL_RAN: $impl_ran PROGRESS=$impl_progress"
    }
}

# ---------------------------------------------------------------------------
# 5. 报告、methodology 签核与 XSA
#    - 正常 build / report_only 走这里；strategy_only 不出报告（原 -strategy-only 语义）。
#    - 实现未完成时 WNS/WHS/... 写 NA（CONTRACT.md §3.1），不强行 open_run。
# ---------------------------------------------------------------------------
if {!$strategy_only} {
    set impl_complete [expr {[dpm_run_prop impl_1 PROGRESS] eq "100%"}]
    if {$report_only && !$impl_complete} {
        error "report_only: impl_1 未完成（PROGRESS='[dpm_run_prop impl_1 PROGRESS]'），无法重出报告"
    }
    if {$impl_complete} {
        open_run impl_1
        file mkdir $build_dir
        report_timing_summary -file [file join $build_dir timing_summary.rpt]
        report_utilization -file [file join $build_dir utilization.rpt]
        report_drc -file [file join $build_dir drc.rpt]

        # 方法学签核：解析实现 run 自动生成的 methodology 报告（见 dpm_parse_methodology）。
        set impl_dir [dpm_run_prop impl_1 DIRECTORY]
        lassign [dpm_parse_methodology $impl_dir $build_dir] meth_found meth_waived
        puts "METHODOLOGY_CHECKS_FOUND: $meth_found"
        puts "METHODOLOGY_CHECKS_WAIVED: $meth_waived"
        puts "METHODOLOGY_CLEAN: 无未 waive 的 methodology Critical Warning"

        # 时序统计取自 impl_1 的 STATS.*（CONTRACT.md §3.1）。
        set WNS [dpm_stat impl_1 STATS.WNS]
        set WHS [dpm_stat impl_1 STATS.WHS]
        set TNS [dpm_stat impl_1 STATS.TNS]
        set THS [dpm_stat impl_1 STATS.THS]
        set FAILED_ENDPOINTS [dpm_failed_endpoints impl_1 $build_dir]
        puts "TIMING_BEGIN"
        puts "WNS=$WNS WHS=$WHS TNS=$TNS THS=$THS FAILED_ENDPOINTS=$FAILED_ENDPOINTS"
        puts "TIMING_END"

        # 导出硬件平台（含 bitstream）
        if {$dpm_plan(xsa) ne ""} {
            write_hw_platform -fixed -include_bit -force -file $dpm_plan(xsa)
            set xsa_value $dpm_plan(xsa)
            puts "XSA_DONE: $xsa_value"
        } else {
            puts "XSA_SKIP: 计划没有给出 xsa"
        }
    } else {
        puts "IMPL_NOT_COMPLETE: 未生成报告与 XSA，结果里 WNS/WHS/TNS/THS/FAILED_ENDPOINTS 写 NA"
    }
}

# ---------------------------------------------------------------------------
# 6. 写结果文件（CONTRACT.md §3.1）
# ---------------------------------------------------------------------------
# RUNS_SEEN 用 get_runs 的实际列表（只取 synth_1 / *_synth_1 / impl_1）。
foreach name [dpm_run_names "*"] {
    if {$name eq "synth_1" || $name eq "impl_1" || [string match "*_synth_1" $name]} {
        lappend runs_seen $name
    }
}

# INCREMENTAL_*_CONFIRMED：确认 runme.log 里真的出现 read_checkpoint -incremental。
# 综合侧写「确认到的 run 名」（可空，空格分隔）；实现侧写 1/空（CONTRACT.md §3.1）。
set inc_synth_conf {}
if {!$report_only && !$strategy_only} {
    foreach item $incremental_synth_targets {
        lassign $item run ref_dcp
        if {[dpm_run_log_has_incremental $run]} {
            dpm_add_unique inc_synth_conf $run
        }
    }
}
set inc_impl_conf ""
if {!$report_only && !$strategy_only && $incremental_impl_ref ne ""} {
    if {[dpm_run_log_has_incremental impl_1]} { set inc_impl_conf 1 }
}

puts "RESULT_BEGIN"
dpm_write_result $dpm_plan(result_file) [list \
    "STAGE=$dpm_plan(stage)" \
    "RESET_RUNS=[join $reset_order { }]" \
    "STRATEGY_CHANGED=[join $strategy_changed { }]" \
    "RUNS_SEEN=[join $runs_seen { }]" \
    "SYNTH_RAN=$synth_ran" \
    "SYNTH_PROGRESS=[dpm_run_prop synth_1 PROGRESS]" \
    "SYNTH_STATUS=[dpm_run_prop synth_1 STATUS]" \
    "IMPL_RAN=$impl_ran" \
    "IMPL_PROGRESS=[dpm_run_prop impl_1 PROGRESS]" \
    "IMPL_STATUS=[dpm_run_prop impl_1 STATUS]" \
    "WNS=$WNS" \
    "WHS=$WHS" \
    "TNS=$TNS" \
    "THS=$THS" \
    "FAILED_ENDPOINTS=$FAILED_ENDPOINTS" \
    "METHODOLOGY_FOUND=$meth_found" \
    "METHODOLOGY_WAIVED=$meth_waived" \
    "INCREMENTAL_SYNTH_CONFIRMED=[join $inc_synth_conf { }]" \
    "INCREMENTAL_IMPL_CONFIRMED=$inc_impl_conf" \
    "XSA=$xsa_value"]
puts "RESULT_END"
