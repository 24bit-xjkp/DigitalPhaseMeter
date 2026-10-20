"""`dpm-vivado-flow` 命令行入口。"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .paths import PART, FlowError, RepoPaths, find_repo_root
from .runner import VivadoRunner
from .stages.bd import BdArgs, bd_hashes, bd_stage
from .stages.build import BuildOptions, build_stage, status
from .stages.package import package_stage
from .stamps import SCHEMA, load_state, require_stamp, save_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dpm-vivado-flow",
        description="DigitalPhaseMeter 的 Vivado 增量综合实现流程（Python 决策 + Tcl 执行）",
    )
    parser.add_argument("--repo-root", type=Path, help="仓库根目录（默认从当前目录/包位置自动识别）")
    parser.add_argument("--force", action="store_true", help="忽略 stamp/指纹，全量重打包 + 重建 BD + 全量 reset")
    parser.add_argument("--dry-run", action="store_true", help="只生成/打印计划，不启动 Vivado")
    parser.add_argument("--status", action="store_true", help="只打印阶段 stamp、run 指纹与输出状态")
    parser.add_argument("--report-only", action="store_true", help="不 reset/不 launch，只重出报告与 XSA")
    parser.add_argument("--strategy-only", action="store_true", help="只写综合/实现策略，不跑 run")
    parser.add_argument(
        "--incremental-synth",
        choices=["off", "on", "auto"],
        default="off",
        help="增量综合（默认 off；auto=有同版本参考 DCP 时启用）",
    )
    parser.add_argument(
        "--incremental-impl",
        choices=["off", "on", "auto"],
        default="off",
        help="增量实现（默认 off；auto=有同版本参考 DCP 时启用）",
    )
    parser.add_argument("--retiming", type=int, choices=[0, 1], default=0, help="综合 GLOBAL_RETIMING（默认 0=off）")
    parser.add_argument("--dsp", type=int, default=2, help="综合 MAX_DSP（默认 2；0 表示不设）")
    parser.add_argument("--uart", choices=["uart0", "uart1", "none"], default="uart1", help="PS 调试串口")
    parser.add_argument("--baud", type=int, default=115200, help="PS 调试串口波特率")
    parser.add_argument("--dsp-clk", type=float, default=450.0, help="DSP 域目标频率 MHz（默认 450）")
    parser.add_argument("-v", "--verbose", action="store_true", help="实时显示 Vivado 输出")
    return parser


def _ensure_version(paths: RepoPaths, runner: VivadoRunner, force: bool) -> str:
    state = load_state(paths)
    version = str(state.get("vivado", ""))
    if version and not force:
        return version
    version = runner.query_version()
    save_state(paths, {"schema": SCHEMA, "vivado": version, "part": PART})
    return version


def _status(
    paths: RepoPaths,
    runner: VivadoRunner,
    vivado: str,
    args: argparse.Namespace,
) -> None:
    current_bd: dict[str, str] | None = None
    try:
        current_bd = bd_hashes(
            paths,
            BdArgs(dsp_clk=args.dsp_clk, uart=args.uart, baud=args.baud),
            vivado,
        )
    except FlowError as exc:
        print(f"BD_CURRENT: 无法计算（{exc}）")
    status(paths, vivado, current_bd, retiming=args.retiming, dsp_budget=args.dsp)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    started = time.time()
    try:
        paths = RepoPaths(find_repo_root(args.repo_root))
        paths.ensure_flow_dirs()
        runner = VivadoRunner(paths, verbose=args.verbose)
        vivado = _ensure_version(paths, runner, args.force)
        print(f"FLOW: root={paths.root} vivado={vivado} part={PART}")

        if args.status:
            _status(paths, runner, vivado, args)
            return 0

        bd_args = BdArgs(dsp_clk=args.dsp_clk, uart=args.uart, baud=args.baud)
        options = BuildOptions(
            force=args.force,
            dry_run=args.dry_run,
            report_only=args.report_only,
            strategy_only=args.strategy_only,
            incremental_synth=args.incremental_synth,
            incremental_impl=args.incremental_impl,
            retiming=args.retiming,
            dsp_budget=args.dsp,
        )

        if args.report_only or args.strategy_only:
            bd_stamp = require_stamp(paths.bd_stamp_file, "BD stamp")
            build_stage(paths, runner, vivado, bd_stamp, options)
            print(f"FLOW_DONE: {time.time() - started:.1f}s")
            return 0

        package_stage(paths, runner, vivado, force=args.force, dry_run=args.dry_run)
        bd_stamp = bd_stage(
            paths,
            runner,
            vivado,
            bd_args,
            force=args.force,
            dry_run=args.dry_run,
        )
        build_stage(paths, runner, vivado, bd_stamp, options)
        print(f"FLOW_DONE: {time.time() - started:.1f}s")
        return 0
    except FlowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("INTERRUPTED", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
