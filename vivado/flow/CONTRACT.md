# Python 主控与 Tcl worker 的接口约定

本文件是 `vivado/flow/dpm_vivado_flow/`（Python）与 `vivado/flow/tcl/*.tcl`（Tcl）
之间的唯一接口约定。两侧修改接口时必须同步更新本文件。

## 1. 职责边界

- Python：内容哈希、stamp/run 指纹、复用/重跑决策、回退、生成计划与包装脚本、调用
  `vivado -mode batch`、解析结果与日志、报告门禁。
- Tcl worker：只执行计划中的线性 Vivado 命令；不做哈希/复用判断。允许根据「当前属性值与
  目标值不同」做必要的 reset（见 §4），并把实际 reset 的 run 写回结果。

## 2. 计划文件（Tcl sourceable）

Python 生成到 `build/vivado/.flow/plans/<stage>.tcl`，worker 通过全局变量
`dpm_plan_file` 拿到路径后 `source` 它。计划是一组 `set dpm_plan(<key>) <value>` 行，
值由 Python 按 Tcl list/word 规则转义（路径可以含空格，不能含未平衡的 `{}`/反斜杠）。

### 2.1 综合/实现 worker（`tcl/run_build_stage.tcl`）

| key | 类型 | 含义 |
| --- | --- | --- |
| `stage` | string | `build` |
| `project` | path | `build/vivado/bd_proj/dpm_bd.xpr` |
| `part` | string | `xc7z010clg400-1` |
| `xdc` | path | 顶层约束文件 |
| `suppress_early` | path | `suppress_early_messages.tcl` |
| `hook_filter` | path | `hook_filter_messages.tcl` |
| `hook_waivers` | path | `hook_methodology_waivers.tcl` |
| `hook_vendor_ip` | path | `hook_filter_vendor_ip.tcl` |
| `strategy_targets` | list of `{run property value}` | 需要写入的策略目标 |
| `hook_assignments` | list of `{run step hook}` | 需要挂的 `STEPS.<step>.TCL.PRE` |
| `reset_runs` | list | Python 判定必须重跑的 run |
| `materialize` | 0/1 | 是否先 `launch_runs synth_1 -scripts_only` 物化 IP OOC run |
| `incremental_synth` | list of `{run ref_dcp}` | 综合参考 DCP；空表示关闭 |
| `incremental_impl` | path | 实现参考 DCP；空表示关闭 |
| `launch_synth` | 0/1 | 是否启动 `synth_1` |
| `launch_impl` | 0/1 | 是否启动 `impl_1 -to_step write_bitstream` |
| `report_only` | 0/1 | 不 reset/不 launch，只重出报告与 XSA |
| `strategy_only` | 0/1 | 只写策略，不 reset/不 launch |
| `xsa` | path | `build/vivado/dpm_system.xsa` |
| `build_dir` | path | `build/vivado` |
| `result_file` | path | `build/vivado/.flow/results/build.txt` |

### 2.2 原位 BD 刷新 worker（`tcl/refresh_bd.tcl`）

| key | 类型 | 含义 |
| --- | --- | --- |
| `stage` | string | `refresh_bd` |
| `project` | path | 工程文件 |
| `part` | string | `xc7z010clg400-1` |
| `bd` | path | `dpm_system.bd` |
| `upgrade_ips` | list | 需要 `upgrade_ip` 的 IP 实例名（如 `dpm_system_dpm_top_0_0`） |
| `lite_base` | string | `0x43C00000` |
| `drp_base` | string | `0x43C10000` |
| `suppress_early` | path | 早期消息过滤 |
| `result_file` | path | `build/vivado/.flow/results/bd_refresh.txt` |

## 3. 结果文件（`KEY=value` 行）

Python 只解析第一个 `=` 之前作为 key，其余整行作为 value（value 可以为空，可以含空格）。
worker 只在成功路径写结果；失败时 `error`，包装脚本负责 `exit 1`。

### 3.1 `build.txt`

```
STAGE=build
RESET_RUNS=synth_1 dpm_system_dpm_top_0_0_synth_1
STRATEGY_CHANGED=synth_1
RUNS_SEEN=synth_1 dpm_system_dpm_top_0_0_synth_1 impl_1
SYNTH_RAN=1
SYNTH_PROGRESS=100%
SYNTH_STATUS=synth_design Complete!
IMPL_RAN=1
IMPL_PROGRESS=100%
IMPL_STATUS=write_bitstream Complete!
WNS=0.271
WHS=0.023
TNS=0.000
THS=0.000
FAILED_ENDPOINTS=0
METHODOLOGY_FOUND=0
METHODOLOGY_WAIVED=13
INCREMENTAL_SYNTH_CONFIRMED=dpm_system_dpm_top_0_0_synth_1
INCREMENTAL_IMPL_CONFIRMED=1
XSA=/abs/path/dpm_system.xsa
```

- `RUNS_SEEN` 是当前工程里所有 `synth_1`/`*_synth_1`/`impl_1` 的 run 名（空格分隔）。
- `*_RAN` 表示本次是否真的启动了该阶段（`0`/`1`）。
- `WNS/WHS/TNS/THS/FAILED_ENDPOINTS` 取自 `impl_1` 的 `STATS.*`；没有实现时为 `NA`。
- `INCREMENTAL_*_CONFIRMED`：确认 `runme.log` 里真的出现 `read_checkpoint -incremental`
  （综合可给空；实现给 `1`/空）。
- `METHODOLOGY_FOUND/WAIVED` 取自实现的 `*_methodology_drc_routed.rpt`。

### 3.2 `bd_refresh.txt`

```
STAGE=bd_refresh
UPGRADED=dpm_system_dpm_top_0_0
VALIDATE=ok
ADDR_LITE=0x43C00000
ADDR_DRP=0x43C10000
```

## 4. worker 允许的 reset 判定

为保证「Python 判定复用、但工程属性被手工改过」时不会静默复用旧结果，worker 可以：

1. 读每个 `strategy_targets` 的当前值；与目标不同则写入，并把对应 run 加入实际 reset 列表。
2. 读每个 `hook_assignments` 的 `TCL.PRE`；与目标不同则把对应 run 加入实际 reset 列表。
3. 实际 reset 列表 = `reset_runs` ∪ 上述新增。
4. **先 reset，再写策略/hook**（`reset_run` 会清步骤属性）；对未被 reset 的 run 不写任何属性，
   避免把已完成的 run 标成过期。
5. 所有实际 reset 的 run 在结果中写入 `RESET_RUNS`。

## 5. 包装脚本

Python 生成 `build/vivado/.flow/scripts/<stage>.tcl`。包装脚本负责：

```tcl
# 例：build
set dpm_plan_file {/abs/.../plans/build.tcl}
if {[catch {source {/abs/.../tcl/run_build_stage.tcl}} msg opts]} {
    puts "DPM_STAGE_ERROR: $msg"
    catch {puts [dict get $opts -errorinfo]}
    exit 1
}
exit 0
```

- 失败必须 `exit 1`；成功 `exit 0`。
- worker 内部不调用 `exit`。
- Python 以退出码 + 结果文件 + 日志三重判定。

## 6. 日志与告警门禁

- 包装脚本/worker 的 stdout 由 Vivado 写入 `-log` 指定的日志文件。
- Python 统计日志中行首 `WARNING:`、`CRITICAL WARNING:`、`ERROR:` 的数量；有任一未预期条目
  即该阶段失败（预期条目应已在对应 hook 中按理由过滤）。
- 各 run 的 `runme.log` 单独统计，作为交付报告的一部分。

## 7. 日志文件位置

```
build/vivado/.flow/
├── plans/<stage>.tcl
├── scripts/<stage>.tcl
├── results/<stage>.txt
└── logs/<stage>.log
```
