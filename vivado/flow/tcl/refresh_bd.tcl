# 原位 BD 刷新 stage 的 Tcl worker（Python 主控 dpm-vivado-flow 调用）
#
# 场景：vivado/package_ip.tcl / package_src_ip.tcl 重新打包了自研 IP，但 BD 工程
# （build/vivado/bd_proj/dpm_bd.xpr）里的 IP 还是旧版本。本 worker 不重建工程，而是
# 就地升级 IP、重生成 BD 产物并校验地址分配，避免整条 create_bd 流程重跑。
#
# 用法：由 dpm-vivado-flow 生成的包装脚本调用（CONTRACT.md §5）：
#   set dpm_plan_file {/abs/.../plans/refresh_bd.tcl}
#   source {/abs/.../vivado/flow/tcl/refresh_bd.tcl}
#
# 接口约定见 vivado/flow/CONTRACT.md §2.2（计划 key）与 §3.2（结果文件）。
# 失败一律 error（包装脚本负责 exit 1），本文件内部不调用 exit。

# ---------------------------------------------------------------------------
# 0. 读取计划文件（CONTRACT.md §2.2）
# ---------------------------------------------------------------------------
proc dpm_parse_plan_file {path} {
    # 兜底解析（同 run_build_stage.tcl）：优先 source 计划文件；若 Python 侧把 list 值
    # 写成了多个 Tcl word（source 时 set 报 wrong # args），按已知的
    # `set dpm_plan(<key>) <value>` 行格式逐行读取。
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
    error "refresh_bd.tcl 需要全局变量 dpm_plan_file 指向计划文件（CONTRACT.md §2）"
}
set _plan_src_err ""
if {[catch {source $dpm_plan_file} _plan_src_err]} {
    puts "PLAN_SOURCE_FALLBACK: source 计划文件失败（$_plan_src_err），按 set dpm_plan(...) 逐行读取"
    dpm_parse_plan_file $dpm_plan_file
} elseif {![info exists dpm_plan]} {
    error "计划文件没有定义 dpm_plan(...)：$dpm_plan_file"
}
foreach _key {
    stage project part bd upgrade_ips lite_base drp_base
    suppress_early result_file
} {
    if {![info exists dpm_plan($_key)]} { set dpm_plan($_key) "" }
}
if {$dpm_plan(stage) ne "refresh_bd"} {
    error "refresh_bd.tcl 只处理 stage=refresh_bd（计划里是 '$dpm_plan(stage)'）"
}
if {$dpm_plan(project) eq ""} { error "计划缺少 project（工程 .xpr 路径）" }
if {$dpm_plan(result_file) eq ""} { error "计划缺少 result_file（结果文件路径）" }
if {$dpm_plan(part) eq ""} { error "计划缺少 part" }
if {$dpm_plan(lite_base) eq ""} { error "计划缺少 lite_base" }
if {$dpm_plan(drp_base) eq ""} { error "计划缺少 drp_base" }

# 结果文件只在成功路径写；先删掉上一轮结果，避免失败后 Python 读到旧结果。
if {[file exists $dpm_plan(result_file)]} {
    file delete -force $dpm_plan(result_file)
}

# ---------------------------------------------------------------------------
# 1. 通用工具
# ---------------------------------------------------------------------------
proc dpm_get_prop {obj prop} {
    # 读属性；属性不存在时安静返回空串（不与 [Common 17-673] 这类错误混淆）。
    set value ""
    catch { set value [get_property -quiet $prop $obj] }
    return $value
}

proc dpm_norm_hex {value} {
    # 把 0x43C00000 / 0X43c00000 / 4390912 统一成小写、无前缀、无前导零的形式；
    # 地址段属性的返回格式随 Vivado 版本可能不同，用规范化后的字符串比较。
    set v [string trim $value]
    if {$v eq ""} { return "" }
    if {[string is integer -strict $v]} {
        # 纯十进制（有些版本会把 offset 归一成十进制），统一成无前缀的小写十六进制
        set v [string tolower [format "%x" $v]]
    } else {
        set v [string tolower $v]
        if {[string match "0x*" $v]} { set v [string range $v 2 end] }
    }
    while {[string length $v] > 1 && [string index $v 0] eq "0"} {
        set v [string range $v 1 end]
    }
    return $v
}

proc dpm_norm_range {value} {
    # 地址段的 range 在 2025.2 上返回 0x00001000（不是 "4K"/"4096"）；统一按 4K 识别。
    set v [string trim $value]
    if {[string equal -nocase $v "4K"]} { return "4K" }
    if {[string is integer -strict $v] && $v == 4096} { return "4K" }
    if {[string equal [dpm_norm_hex $v] "1000"]} { return "4K" }
    return $v
}

proc dpm_find_seg_at {segs base} {
    # 在地址段列表里找 offset==base 且 range==4K 的段；找不到返回空串。
    foreach seg $segs {
        if {[dpm_norm_hex [dpm_get_prop $seg offset]] eq [dpm_norm_hex $base] &&
            [dpm_norm_range [dpm_get_prop $seg range]] eq "4K"} {
            return $seg
        }
    }
    return ""
}

proc dpm_find_ip {name} {
    # 工程模式的 IP 对象（BD 里的 IP 实例在 get_ips 里以 <bd>_<cell>_0 命名）；
    # 精确名找不到时用名字子串过滤兜底。
    set ips [get_ips -quiet $name]
    if {[llength $ips] > 0} { return [lindex $ips 0] }
    set ips [get_ips -quiet -filter "NAME =~ *${name}*"]
    if {[llength $ips] > 0} { return [lindex $ips 0] }
    return ""
}

proc dpm_write_result {path lines} {
    if {$path eq ""} { error "计划没有给出 result_file" }
    file mkdir [file dirname $path]
    set fh [open $path w]
    foreach line $lines { puts $fh $line }
    close $fh
    puts "RESULT_FILE: $path"
}

proc dpm_addr_report {segs} {
    # 打印当前地址分配，便于日志排障（与 create_bd.tcl 的输出风格一致）。
    puts "BD_ADDRESS_MAP_BEGIN"
    foreach seg $segs {
        puts "  [dpm_get_prop $seg name] offset=[dpm_get_prop $seg offset] range=[dpm_get_prop $seg range]"
    }
    puts "BD_ADDRESS_MAP_END"
}

# ==== 以上为通用工具，以下为主流程 ====

# ---------------------------------------------------------------------------
# 2. 早期消息过滤、进入工程目录、打开工程、校验器件
# ---------------------------------------------------------------------------
# 原位刷新会打印若干「预期中间态」的消息（IP 版本落后于 catalog、重新生成时覆盖
# auto_pc 的 OOC 约束等），它们必须在 open_project 之前就被过滤掉；逐条理由见
# vivado/suppress_early_messages.tcl 末尾的「原位 BD 刷新」一节。
if {$dpm_plan(suppress_early) ne ""} {
    if {![file exists $dpm_plan(suppress_early)]} {
        error "找不到早期消息过滤脚本：$dpm_plan(suppress_early)"
    }
    source $dpm_plan(suppress_early)
}

set proj_file [file normalize $dpm_plan(project)]
if {![file exists $proj_file]} {
    error "找不到工程 $proj_file，请先运行 create_bd 阶段"
}
# 把工作目录放到工程目录下，避免在仓库根目录留下 .Xil 等临时文件。
set work_dir [file dirname $proj_file]
file mkdir $work_dir
cd $work_dir

open_project $proj_file

# 器件绑定校验：本工程固定 xc7z010clg400-1（AX7010 的芯片）。
set project_part [dpm_get_prop [current_project] part]
puts "PROJECT_PART: $project_part"
if {$project_part ne $dpm_plan(part)} {
    error "工程绑定的器件是 '$project_part'，计划要求 '$dpm_plan(part)'（本仓库固定 xc7z010clg400-1）"
}

# ---------------------------------------------------------------------------
# 3. 重扫 IP 仓库并升级计划里点名的 IP
# ---------------------------------------------------------------------------
# update_ip_catalog -rebuild：重新扫描工程 ip_repo_paths（package 脚本刚重新打过包），
# 让 catalog 里的 dpm_top / dpm_src_drp 版本号更新到最新。
puts "UPDATE_IP_CATALOG: rebuild"
update_ip_catalog -rebuild

# 取 BD 文件：优先用计划给出的路径；缺省时退回工程里唯一的 .bd。
set bd_file $dpm_plan(bd)
if {$bd_file eq ""} {
    set bd_files [get_files -quiet *.bd]
    if {[llength $bd_files] == 0} { error "工程里没有 .bd 文件" }
    set bd_file [lindex $bd_files 0]
    puts "BD_FILE_FALLBACK: $bd_file"
} else {
    set bd_file [file normalize $bd_file]
    if {![file exists $bd_file]} { error "找不到 BD 文件：$bd_file" }
}
puts "BD_FILE: $bd_file"

# 打开 BD 后 IP 实例才能被 get_ips / upgrade_ip 正确解析。
open_bd_design $bd_file

set upgraded {}
foreach ip_name $dpm_plan(upgrade_ips) {
    set ip_obj [dpm_find_ip $ip_name]
    if {$ip_obj eq ""} {
        error "upgrade_ips 里的 IP '$ip_name' 在工程里不存在（BD 是否已包含该实例？）"
    }
    puts "UPGRADE_IP: $ip_name -> [dpm_get_prop $ip_obj NAME]"
    # -quiet：IP 已经是最新时（update_ip_catalog -rebuild 已刷新 catalog）upgrade_ip 会打印
    # [Coretcl 2-1042]「No IP was identified for upgrade」；这是原位刷新的预期分支，不是
    # 工程缺陷。-quiet 只关闭该命令的消息，不改变返回；真正的升级/结构问题仍会由后面的
    # generate_target / validate_bd_design / 地址检查暴露。
    upgrade_ip -quiet $ip_obj
    lappend upgraded $ip_name
}
puts "UPGRADED: $upgraded"

# ---------------------------------------------------------------------------
# 4. 重生成 BD 产物并校验
# ---------------------------------------------------------------------------
puts "GENERATE_TARGET: $bd_file"
set bd_obj [get_files -quiet $bd_file]
if {[llength $bd_obj] == 0} {
    set bd_obj [get_files -quiet [file tail $bd_file]]
}
if {[llength $bd_obj] == 0} {
    error "get_files 找不到 BD 文件对象：$bd_file"
}
generate_target all $bd_obj

# validate_bd_design 的问题以消息形式打印（ERROR/CRITICAL WARNING 由 Python 侧日志门禁
# 判定，CONTRACT.md §6）；这里能保证的是命令本身不抛错。
puts "VALIDATE_BD: start"
validate_bd_design
puts "VALIDATE_BD: done"

# ---- 地址段检查：0x43C00000/4K（dpm_top）与 0x43C10000/4K（dpm_src_drp）----
# 升级 IP / 重生成 BD 之后地址分配理论上不变；这里仍显式回读，避免出现「刷新后
# PS 访问地址悄悄变了」这种只在板子上才暴露的问题。
# 必须取 PS7 的 Data 地址空间（主口视角）；get_bd_addr_spaces 不带参数时也会返回
# 从口自己的地址空间（如 s_axi_lite），那种空间的段没有 offset，会误判地址。
set addr_space ""
set ps_spaces [get_bd_addr_spaces -quiet processing_system7_0/Data]
if {[llength $ps_spaces] > 0} {
    set addr_space [lindex $ps_spaces 0]
}
if {$addr_space eq ""} {
    foreach space [get_bd_addr_spaces -quiet] {
        set space_name [dpm_get_prop $space name]
        if {[string match "*processing_system7_0*" $space_name] &&
            [string match "*Data*" $space_name]} {
            set addr_space $space
            break
        }
    }
}
if {$addr_space eq ""} {
    error "找不到 processing_system7_0/Data 地址空间，无法检查地址分配"
}
puts "BD_ADDR_SPACE: [dpm_get_prop $addr_space name]"

set segs [get_bd_addr_segs -quiet -of_objects $addr_space]
dpm_addr_report $segs

set lite_seg [dpm_find_seg_at $segs $dpm_plan(lite_base)]
set drp_seg [dpm_find_seg_at $segs $dpm_plan(drp_base)]

# 回退修复：地址段丢失/偏移被改动时，按已知的从口名字找回来并用计划里的偏移重分配
# （assign_bd_address -force），再校验一次；修不好就 error，绝不静默接受错误地址。
set repaired 0
if {$lite_seg eq ""} {
    foreach seg $segs {
        if {[string match "*dpm_top_0*s_axi_lite*" [dpm_get_prop $seg name]]} {
            puts "BD_ADDR_REPAIR: [dpm_get_prop $seg name] -> offset=$dpm_plan(lite_base) range=4K"
            assign_bd_address -quiet -target_address_space $addr_space \
                -offset $dpm_plan(lite_base) -range 4K $seg -force
            set repaired 1
            break
        }
    }
}
if {$drp_seg eq ""} {
    foreach seg $segs {
        if {[string match "*dpm_src_drp_0*s_axi_lite*" [dpm_get_prop $seg name]]} {
            puts "BD_ADDR_REPAIR: [dpm_get_prop $seg name] -> offset=$dpm_plan(drp_base) range=4K"
            assign_bd_address -quiet -target_address_space $addr_space \
                -offset $dpm_plan(drp_base) -range 4K $seg -force
            set repaired 1
            break
        }
    }
}
# 修复过地址就重新校验一次，保证 BD 内部状态与回读一致。
if {$repaired} {
    set segs [get_bd_addr_segs -quiet -of_objects $addr_space]
    puts "BD_ADDR_REVALIDATE: 地址修复后重新 validate_bd_design"
    validate_bd_design
    puts "BD_ADDR_REPAIRED: 1"
}
set lite_seg [dpm_find_seg_at $segs $dpm_plan(lite_base)]
set drp_seg [dpm_find_seg_at $segs $dpm_plan(drp_base)]
if {$lite_seg eq "" || $drp_seg eq ""} {
    dpm_addr_report $segs
    error "BD 地址段检查失败：期望 $dpm_plan(lite_base)/4K 与 $dpm_plan(drp_base)/4K，实际见上面的 BD_ADDRESS_MAP"
}
# 校验通过：结果文件回写计划里的地址（与工程实际值已按数值等价比较过），
# 避免不同 Vivado 版本对 offset 的字符串格式（大小写/前导零）影响 Python 侧比较。
set addr_lite $dpm_plan(lite_base)
set addr_drp $dpm_plan(drp_base)
puts "BD_ADDR_CHECK: lite=$addr_lite ([dpm_get_prop $lite_seg name]) drp=$addr_drp ([dpm_get_prop $drp_seg name])"

# ---------------------------------------------------------------------------
# 5. 保存 BD 并写结果文件（CONTRACT.md §3.2）
# ---------------------------------------------------------------------------
save_bd_design
puts "BD_SAVED: $bd_file"

dpm_write_result $dpm_plan(result_file) [list \
    "STAGE=$dpm_plan(stage)" \
    "UPGRADED=[join $upgraded { }]" \
    "VALIDATE=ok" \
    "ADDR_LITE=$addr_lite" \
    "ADDR_DRP=$addr_drp"]
puts "BD_REFRESH_DONE: upgraded=$upgraded"
