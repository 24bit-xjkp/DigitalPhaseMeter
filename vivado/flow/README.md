# `dpm-vivado-flow`：Python 主控的 Vivado 增量流程

本目录是 `vivado/` 的官方入口实现：Python 负责内容哈希、stamp/run 指纹、复用/重跑决策、
原位 BD 刷新回退、报告与告警门禁；Tcl（[`tcl/`](tcl/)）只执行线性的 Vivado 命令。
接口约定见 [`CONTRACT.md`](CONTRACT.md)。

## 安装与入口

`vivado/flow` 是仓库根 uv workspace 的成员，**共用工程全局的根 `.venv`**（不单独建虚拟环境）：

```bash
# 仓库根，一次性
uv sync

# 官方入口
uv run dpm-vivado-flow --status
uv run dpm-vivado-flow
uv run dpm-vivado-flow --force

# 激活根 .venv 后也可以直接调用
. .venv/bin/activate
dpm-vivado-flow --status

# 调试入口
uv run python -m dpm_vivado_flow --help
```

根 `pyproject.toml` 通过 `[tool.uv.workspace]`、`vivado` 依赖组与
`[tool.uv.sources] dpm-vivado-flow = { workspace = true }` 把它装进根 `.venv`；
`vivado/flow/pyproject.toml` 只注册 console script，运行时无第三方依赖。

## 常用模式

| 命令 | 行为 |
| --- | --- |
| `dpm-vivado-flow` | 默认增量：未变化的 IP/BD/run 直接复用；全复用时不启动 Vivado |
| `--status` | 只读打印 stamp、run 指纹、DCP/bit/XSA/报告是否存在 |
| `--dry-run` | 只生成 `build/vivado/.flow/plans|scripts/*.tcl`，不启动 Vivado |
| `--report-only` | 不改工程，只打开 `impl_1` 重出报告与 XSA |
| `--strategy-only` | 只写综合/实现策略；如果策略有变化会删除 `runs.json`，强制下次重跑 |
| `--force` | 重打包 + 重建 BD + 全量 reset |
| `--incremental-synth off|on|auto` | 增量综合（默认 off；`auto`=有同版本参考 DCP 时启用） |
| `--incremental-impl off|on|auto` | 增量实现（默认 off） |
| `--retiming 0|1`、`--dsp N` | 综合策略（默认 0 / 2） |
| `--uart`、`--baud`、`--dsp-clk` | 建 BD 参数（默认 uart1 / 115200 / 450MHz） |
| `--repo-root` | 显式指定仓库根（默认从 CWD 向上找 `xmake.lua`+`vivado/package_ip.tcl`） |

## 状态目录

`build/vivado/.flow/`（构建产物，不入库）：

```text
state.json                 schema / Vivado 版本 / part
package_dpm_top.json       打包输入哈希 + component.xml 哈希
package_dpm_src_drp.json
bd.json                    BD structure/args + 两个 IP 目录哈希
runs.json                  逐 run 输入指纹（综合 + 实现）
plans/<stage>.tcl          Python 生成的 Tcl 计划
scripts/<stage>.tcl        包装脚本（失败 exit 1）
results/<stage>.txt        worker 写出的 KEY=value 结果
logs/<stage>.log           Vivado -log
ref/<run>.dcp              增量编译参考 DCP
```

任一 stamp 缺失/损坏、版本或器件不符都会保守重跑；`--force` 忽略全部指纹。

## 阶段与复用

- **package**：`package_ip.tcl` / `package_src_ip.tcl`，按 `filelist.f` 与文件内容哈希决定是否重打包。
- **BD**：结构/参数不变且仅 IP 内容变化时，`refresh_bd.tcl` 在原工程内 `upgrade_ip` +
  `generate_target` + `validate_bd_design` + 地址检查；失败自动删除工程并全量重建。
- **build**：`run_build_stage.tcl` 按计划 reset 指定 run、写策略/hook/增量属性、launch/wait，
  再出报告、methodology 门禁与 XSA。复用判据是「指纹匹配 + 输出 DCP 存在」，不依赖 run `STATUS`。

完整复用矩阵与实测数据见 [`../README.md`](../README.md) 的「增量流程」。

## 测试与格式

```bash
uv run pytest vivado/flow/tests -v
uv run black --check vivado/flow
uv run isort --check-only vivado/flow
```

单元测试不启动 Vivado：覆盖哈希/stamp、Tcl 计划可 source、复用矩阵、日志/报告解析等。
Vivado 集成按 `../README.md` 的场景矩阵手工执行。

## 增加新 run/阶段时

1. 在 Python 侧为 run 计算输入指纹（`fingerprint.py` / `stages/build.py`）；
2. 若需要新的 reset/策略/hook/增量输入，先在 [`CONTRACT.md`](CONTRACT.md) 定义 plan/result key，
   再同步修改 Python 与 Tcl worker；
3. 更新 `../README.md` 的复用矩阵与 skill 的实测坑；
4. 跑 `uv run pytest vivado/flow/tests` 与一次 `--force` 全量 + 一次 warm 复用验证。
