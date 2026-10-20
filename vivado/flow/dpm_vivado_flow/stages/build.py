"""综合/实现/报告阶段：逐 run 复用、选择性 reset、增量编译与签核门禁。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from ..fingerprint import impl_inputs, ip_synth_inputs, synth_inputs
from ..hashing import compose, sha256_dir, sha256_file
from ..incremental import copy_reference, enabled, parse_mode
from ..paths import BD_NAME, CUSTOM_IPS, PART, FlowError, RepoPaths
from ..plan import parse_result_file
from ..reports import (
    check_methodology,
    check_run_logs,
    count_log_messages,
    missing_build_outputs,
)
from ..runner import RunResult, VivadoRunner
from ..stamps import SCHEMA, load_json, load_runs, save_json, save_runs
from ..tclgen import write_plan, write_wrapper

SYNTH_MAIN = "synth_1"
IMPL_MAIN = "impl_1"
MMCM_RUN = f"{BD_NAME}_dpm_mmcm_src_0_0_synth_1"
CUSTOM_RUN_TO_IP = {f"{BD_NAME}_{spec['cell']}_synth_1": ip for ip, spec in CUSTOM_IPS.items()}
KNOWN_SYNTH_RUNS = {SYNTH_MAIN, MMCM_RUN, *CUSTOM_RUN_TO_IP}
DEFAULT_DSP_BUDGET = 2


@dataclass(frozen=True)
class BuildOptions:
    force: bool = False
    dry_run: bool = False
    report_only: bool = False
    strategy_only: bool = False
    incremental_synth: str = "off"
    incremental_impl: str = "off"
    retiming: int = 0
    dsp_budget: int = DEFAULT_DSP_BUDGET


@dataclass
class BuildDecision:
    runs: list[str]
    fingerprints: dict[str, dict[str, str]]
    reset_runs: list[str]
    materialize: bool
    report_only: bool
    strategy_targets: list[list[str]]
    hook_assignments: list[list[str]]
    incremental_synth: list[list[str]]
    incremental_impl: str
    warm: bool
    notes: list[str] = field(default_factory=list)


def _synth_props(retiming: int, dsp_budget: int) -> list[tuple[str, str]]:
    props = [
        ("STEPS.SYNTH_DESIGN.ARGS.GLOBAL_RETIMING", "on" if retiming else "off"),
        ("STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE", "PerformanceOptimized"),
    ]
    if dsp_budget > 0:
        props.append(("STEPS.SYNTH_DESIGN.ARGS.MAX_DSP", str(dsp_budget)))
    return props


def _synth_sig(retiming: int, dsp_budget: int) -> str:
    return compose("synth_strategy", _synth_props(retiming, dsp_budget))


def strategy_rows(retiming: int, dsp_budget: int) -> list[list[str]]:
    rows: list[list[str]] = []
    for run in (SYNTH_MAIN, f"{BD_NAME}_dpm_top_0_0_synth_1"):
        for prop, value in _synth_props(retiming, dsp_budget):
            rows.append([run, prop, value])
    rows.append([IMPL_MAIN, "strategy", "Performance_Retiming"])
    return rows


def discover_runs(paths: RepoPaths) -> list[str]:
    names: set[str] = set()
    if paths.runs_dir.is_dir():
        for path in paths.runs_dir.iterdir():
            if path.is_dir() and (path.name == SYNTH_MAIN or path.name.endswith("_synth_1")):
                names.add(path.name)
    state = load_runs(paths)
    names.update(state.get("runs", {}).keys())
    names.update(KNOWN_SYNTH_RUNS)
    synth = sorted(name for name in names if name == SYNTH_MAIN or name.endswith("_synth_1"))
    return synth


def vendor_runs(runs: list[str]) -> list[str]:
    return [run for run in runs if run != SYNTH_MAIN and run not in CUSTOM_RUN_TO_IP and run != MMCM_RUN and run.endswith("_synth_1")]


def hook_assignments(paths: RepoPaths, vendor: list[str]) -> list[list[str]]:
    rows = [
        [SYNTH_MAIN, "SYNTH_DESIGN", str(paths.hook_filter)],
        [IMPL_MAIN, "INIT_DESIGN", str(paths.hook_filter)],
        [IMPL_MAIN, "ROUTE_DESIGN", str(paths.hook_waivers)],
    ]
    rows.extend([run, "SYNTH_DESIGN", str(paths.hook_vendor_ip)] for run in vendor)
    return rows


def _verify_bd_matches_ips(paths: RepoPaths, bd_stamp: dict[str, object]) -> None:
    for ip, spec in CUSTOM_IPS.items():
        ip_dir = paths.ip_dir / spec["ip_dir"]
        if not ip_dir.is_dir():
            raise FlowError(f"IP 目录不存在：{ip_dir}")
        current = sha256_dir(ip_dir)
        if current != bd_stamp.get(f"ip_{ip}"):
            raise FlowError(
                f"IP 目录内容与 BD stamp 不一致（{ip}）：IP 已重新打包但 BD 未刷新；" "请先运行 BD 阶段（dpm-vivado-flow 会按阶段自动处理）"
            )


def _fallback_vendor_fp(bd_stamp: dict[str, object], hook_hash: str, vivado: str) -> str:
    return ip_synth_inputs(
        base=f"{bd_stamp.get('structure')}|{bd_stamp.get('args')}",
        strategy_sig="default",
        hook_hash=hook_hash,
        vivado=vivado,
    )


def expected_fingerprints(
    paths: RepoPaths,
    runs: list[str],
    bd_stamp: dict[str, object],
    vivado: str,
    retiming: int,
    dsp_budget: int,
) -> dict[str, dict[str, str]]:
    _verify_bd_matches_ips(paths, bd_stamp)
    if not paths.xdc_file.is_file():
        raise FlowError(f"找不到顶层约束：{paths.xdc_file}")
    xdc_hash = sha256_file(paths.xdc_file)
    hook_filter_hash = sha256_file(paths.hook_filter)
    hook_waivers_hash = sha256_file(paths.hook_waivers)
    hook_vendor_hash = sha256_file(paths.hook_vendor_ip)
    synth_sig = _synth_sig(retiming, dsp_budget)
    structure = str(bd_stamp.get("structure", ""))
    args = str(bd_stamp.get("args", ""))

    fps: dict[str, dict[str, str]] = {}
    for run in runs:
        if run == SYNTH_MAIN:
            value = synth_inputs(
                bd_structure=structure,
                bd_args=args,
                xdc_hash=xdc_hash,
                strategy_sig=synth_sig,
                hook_hash=hook_filter_hash,
                vivado=vivado,
            )
        elif run in CUSTOM_RUN_TO_IP:
            ip = CUSTOM_RUN_TO_IP[run]
            value = ip_synth_inputs(
                base=str(bd_stamp.get(f"ip_{ip}", "")),
                strategy_sig=synth_sig if ip == "dpm_top" else "default",
                hook_hash=None,
                vivado=vivado,
            )
        elif run == MMCM_RUN:
            value = ip_synth_inputs(
                base=structure,
                strategy_sig="default",
                hook_hash=None,
                vivado=vivado,
            )
        else:
            value = ip_synth_inputs(
                base=f"{structure}|{args}",
                strategy_sig="default",
                hook_hash=hook_vendor_hash,
                vivado=vivado,
            )
        fps[run] = {"kind": "synth", "inputs": value}

    fps[IMPL_MAIN] = {
        "kind": "impl",
        "inputs": impl_inputs(
            synth_fingerprint=fps[SYNTH_MAIN]["inputs"],
            strategy_sig="Performance_Retiming",
            init_hook_hash=hook_filter_hash,
            route_hook_hash=hook_waivers_hash,
            vivado=vivado,
        ),
    }
    return fps


def decide(
    paths: RepoPaths,
    vivado: str,
    bd_stamp: dict[str, object],
    options: BuildOptions,
) -> BuildDecision:
    runs = discover_runs(paths)
    fps = expected_fingerprints(paths, runs, bd_stamp, vivado, options.retiming, options.dsp_budget)
    stored = load_runs(paths).get("runs", {})
    stored_vivado = str(load_runs(paths).get("vivado", ""))
    notes: list[str] = []

    reset: list[str] = []
    for run in runs:
        dcp = paths.synth_run_dcp(run)
        stored_inputs = (stored.get(run) or {}).get("inputs")
        reusable = not options.force and stored_inputs == fps[run]["inputs"] and dcp.is_file()
        if not reusable:
            reset.append(run)

    # synth_1 必须覆盖所有 IP OOC run 的变化。
    if any(run != SYNTH_MAIN for run in reset) and SYNTH_MAIN not in reset:
        reset.append(SYNTH_MAIN)

    stored_impl = (stored.get(IMPL_MAIN) or {}).get("inputs")
    impl_reusable = (
        not options.force
        and SYNTH_MAIN not in reset
        and stored_impl == fps[IMPL_MAIN]["inputs"]
        and paths.impl_routed_dcp.is_file()
        and paths.impl_bit.is_file()
    )
    if not impl_reusable and IMPL_MAIN not in reset:
        reset.append(IMPL_MAIN)

    materialize = not bool(stored) or not paths.runs_dir.is_dir()
    for run in set(runs) | set(reset):
        if run != IMPL_MAIN and not paths.run_dir(run).is_dir():
            materialize = True

    missing_outputs = missing_build_outputs(paths)
    report_only = options.report_only or (not reset and bool(missing_outputs))
    if report_only and missing_outputs:
        notes.append("缺少构建输出，改为 report-only 重新生成：" + ", ".join(p.name for p in missing_outputs))

    # 增量编译参考 DCP：只在确实要重跑且旧 DCP 存在时复制。
    incremental_synth: list[list[str]] = []
    incremental_impl = ""
    compatible = stored_vivado == vivado
    synth_mode = parse_mode(options.incremental_synth)
    impl_mode = parse_mode(options.incremental_impl)
    if reset and not report_only:
        for run in reset:
            if run == IMPL_MAIN:
                continue
            old_dcp = paths.synth_run_dcp(run)
            has_ref = old_dcp.is_file() and compatible
            if enabled(synth_mode, has_ref) and has_ref:
                ref = copy_reference(paths, run, old_dcp)
                if ref is not None:
                    incremental_synth.append([run, str(ref)])
            elif synth_mode == "on" and not has_ref:
                notes.append(f"incremental_synth 跳过 {run}：没有同版本参考 DCP")
        has_impl_ref = paths.impl_routed_dcp.is_file() and compatible
        if IMPL_MAIN in reset and enabled(impl_mode, has_impl_ref) and has_impl_ref:
            ref = copy_reference(paths, IMPL_MAIN, paths.impl_routed_dcp)
            if ref is not None:
                incremental_impl = str(ref)
        elif IMPL_MAIN in reset and impl_mode == "on" and not has_impl_ref:
            notes.append("incremental_impl 跳过：没有同版本 routed DCP")

    warm = not reset and not report_only and not options.force
    return BuildDecision(
        runs=runs,
        fingerprints=fps,
        reset_runs=sorted(set(reset)),
        materialize=materialize,
        report_only=report_only,
        strategy_targets=strategy_rows(options.retiming, options.dsp_budget),
        hook_assignments=hook_assignments(paths, vendor_runs(runs)),
        incremental_synth=incremental_synth,
        incremental_impl=incremental_impl,
        warm=warm,
        notes=notes,
    )


def _build_plan(paths: RepoPaths, decision: BuildDecision, options: BuildOptions) -> dict[str, object]:
    launch_synth = SYNTH_MAIN in decision.reset_runs or options.strategy_only
    launch_impl = IMPL_MAIN in decision.reset_runs
    if options.report_only or decision.report_only:
        launch_synth = launch_impl = False
    if options.strategy_only:
        launch_synth = launch_impl = False
    # report_only 不得改策略/hook：否则会把已完成 run 标成过期。
    strategy_targets = [] if (options.report_only or decision.report_only) else decision.strategy_targets
    hook_rows = [] if (options.report_only or decision.report_only) else decision.hook_assignments
    return {
        "stage": "build",
        "project": str(paths.proj_file),
        "part": PART,
        "xdc": str(paths.xdc_file),
        "suppress_early": str(paths.suppress_early),
        "hook_filter": str(paths.hook_filter),
        "hook_waivers": str(paths.hook_waivers),
        "hook_vendor_ip": str(paths.hook_vendor_ip),
        "strategy_targets": strategy_targets,
        "hook_assignments": hook_rows,
        "reset_runs": decision.reset_runs if not (options.report_only or options.strategy_only) else [],
        "materialize": bool(decision.materialize and not (options.report_only or options.strategy_only)),
        "incremental_synth": decision.incremental_synth if not (options.report_only or options.strategy_only) else [],
        "incremental_impl": decision.incremental_impl if not (options.report_only or options.strategy_only) else "",
        "launch_synth": launch_synth,
        "launch_impl": launch_impl,
        "report_only": bool(options.report_only or decision.report_only),
        "strategy_only": bool(options.strategy_only),
        "xsa": str(paths.xsa_file),
        "build_dir": str(paths.build_dir),
        "result_file": str(paths.result_file("build")),
    }


def _update_runs(
    paths: RepoPaths,
    vivado: str,
    decision: BuildDecision,
    runs_seen: list[str],
    bd_stamp: dict[str, object],
    options: BuildOptions,
) -> None:
    hook_vendor_hash = sha256_file(paths.hook_vendor_ip)
    saved: dict[str, dict[str, str]] = {}
    for run in sorted(set(decision.runs) | set(runs_seen)):
        fp = decision.fingerprints.get(run)
        if fp is None and run.endswith("_synth_1") and run != SYNTH_MAIN:
            fp = {
                "kind": "synth",
                "inputs": _fallback_vendor_fp(bd_stamp, hook_vendor_hash, vivado),
            }
        if fp is not None:
            saved[run] = fp
    saved[IMPL_MAIN] = decision.fingerprints[IMPL_MAIN]
    save_runs(
        paths,
        {"schema": SCHEMA, "vivado": vivado, "part": PART, "runs": saved},
    )


def _print_stats(result: RunResult) -> None:
    res = result.result
    print(
        "BUILD_RESULT: "
        f"SYNTH_RAN={res.get('SYNTH_RAN', '0')} "
        f"SYNTH_PROGRESS={res.get('SYNTH_PROGRESS', 'NA')} "
        f"IMPL_RAN={res.get('IMPL_RAN', '0')} "
        f"IMPL_PROGRESS={res.get('IMPL_PROGRESS', 'NA')} "
        f"WNS={res.get('WNS', 'NA')} WHS={res.get('WHS', 'NA')} "
        f"FAILED_ENDPOINTS={res.get('FAILED_ENDPOINTS', 'NA')} "
        f"METHODOLOGY_FOUND={res.get('METHODOLOGY_FOUND', 'NA')} "
        f"METHODOLOGY_WAIVED={res.get('METHODOLOGY_WAIVED', 'NA')}"
    )
    if res.get("INCREMENTAL_SYNTH_CONFIRMED"):
        print(f"INCREMENTAL_SYNTH_CONFIRMED: {res['INCREMENTAL_SYNTH_CONFIRMED']}")
    if res.get("INCREMENTAL_IMPL_CONFIRMED"):
        print("INCREMENTAL_IMPL_CONFIRMED")


def build_stage(
    paths: RepoPaths,
    runner: VivadoRunner,
    vivado: str,
    bd_stamp: dict[str, object],
    options: BuildOptions,
) -> RunResult | None:
    decision = decide(paths, vivado, bd_stamp, options)
    for note in decision.notes:
        print(f"BUILD_NOTE: {note}")
    if decision.warm and not options.report_only and not options.strategy_only:
        print("SYNTH_REUSED")
        print("IMPL_REUSED")
        print(f"FLOW_REUSED: {len(decision.runs)} 个综合 run 与 impl_1 全部复用，未启动 Vivado")
        return None

    plan = _build_plan(paths, decision, options)
    if options.dry_run:
        plan_path = write_plan(paths.plan_file("build"), plan)
        wrapper = write_wrapper(paths.wrapper_file("build"), paths.flow_tcl_dir / "run_build_stage.tcl", plan_path)
        print(f"BUILD_DRY_RUN: plan={plan_path} wrapper={wrapper}")
        print(
            "BUILD_DECISION: "
            f"reset={decision.reset_runs} materialize={decision.materialize} "
            f"report_only={plan['report_only']} strategy_only={plan['strategy_only']} "
            f"incremental_synth={decision.incremental_synth} incremental_impl={bool(decision.incremental_impl)}"
        )
        return None

    started = time.time()
    result = runner.run_stage("build", paths.flow_tcl_dir / "run_build_stage.tcl", plan)
    runner.check(result, "综合/实现")
    _print_stats(result)

    if plan["report_only"] or plan["strategy_only"]:
        if plan["strategy_only"] and result.result.get("STRATEGY_CHANGED"):
            # strategy-only 改了属性但没有重跑 run：旧指纹不再代表工程实际状态，
            # 删掉 runs.json，强制下一次正常 build 重新判断/重跑。
            paths.runs_file.unlink(missing_ok=True)
            print(f"STRATEGY_ONLY_INVALIDATED: {result.result.get('STRATEGY_CHANGED')}")
        print(f"BUILD_DONE: {'report_only' if plan['report_only'] else 'strategy_only'}")
        return result

    res = result.result
    if res.get("SYNTH_RAN") == "1" and res.get("SYNTH_PROGRESS") != "100%":
        raise FlowError(f"综合未完成：PROGRESS={res.get('SYNTH_PROGRESS')}")
    if res.get("IMPL_RAN") == "1" and res.get("IMPL_PROGRESS") != "100%":
        raise FlowError(f"实现未完成：PROGRESS={res.get('IMPL_PROGRESS')}")
    if not paths.xsa_file.is_file():
        raise FlowError(f"没有生成 XSA：{paths.xsa_file}")

    found, waived = check_methodology(paths)
    print(f"METHODOLOGY_CHECKS_FOUND: {found}")
    print(f"METHODOLOGY_CHECKS_WAIVED: {waived}")

    runs_seen = [item for item in res.get("RUNS_SEEN", "").split() if item] or decision.runs
    counts = check_run_logs(paths, sorted(set(runs_seen)))
    for run, (warnings, critical, errors) in sorted(counts.items()):
        if warnings or critical or errors:
            raise FlowError(f"{run} 的 runme.log 存在告警：warnings={warnings} critical={critical} errors={errors}")
    _update_runs(paths, vivado, decision, runs_seen, bd_stamp, options)
    print(f"BUILD_ELAPSED: {time.time() - started:.1f}s")
    return result


def status(
    paths: RepoPaths,
    vivado: str,
    bd_stamp_current: dict[str, object] | None = None,
    *,
    retiming: int = 0,
    dsp_budget: int = DEFAULT_DSP_BUDGET,
) -> None:
    """只读打印阶段 stamp/指纹/DCP 状态。"""

    state = load_json(paths.state_file) or {}
    print(f"STATE: vivado={state.get('vivado', '?')} part={state.get('part', '?')}")
    for ip in CUSTOM_IPS:
        stamp = load_json(paths.package_stamp_file(ip)) or {}
        print(f"PACKAGE {ip}: inputs={str(stamp.get('inputs', '?'))[:12]} vivado={stamp.get('vivado', '?')}")
    bd = bd_stamp_current or load_json(paths.bd_stamp_file) or {}
    print(
        "BD: "
        f"structure={str(bd.get('structure', '?'))[:12]} args={str(bd.get('args', '?'))[:12]} "
        f"ip_dpm_top={str(bd.get('ip_dpm_top', '?'))[:12]} ip_dpm_src_drp={str(bd.get('ip_dpm_src_drp', '?'))[:12]}"
    )
    runs = load_runs(paths)
    stored = runs.get("runs", {})
    print(f"RUNS: {len(stored)} 条指纹（vivado={runs.get('vivado', '?')}）")
    for run in discover_runs(paths):
        dcp = paths.synth_run_dcp(run)
        print(f"  {run}: fp={'yes' if (stored.get(run) or {}).get('inputs') else 'no'} dcp={'yes' if dcp.is_file() else 'no'}")
    print(
        "  impl_1: "
        f"fp={'yes' if (stored.get(IMPL_MAIN) or {}).get('inputs') else 'no'} "
        f"routed={'yes' if paths.impl_routed_dcp.is_file() else 'no'} "
        f"bit={'yes' if paths.impl_bit.is_file() else 'no'} "
        f"xsa={'yes' if paths.xsa_file.is_file() else 'no'}"
    )
    missing = missing_build_outputs(paths)
    if missing:
        print("MISSING_OUTPUTS: " + ", ".join(str(path) for path in missing))
