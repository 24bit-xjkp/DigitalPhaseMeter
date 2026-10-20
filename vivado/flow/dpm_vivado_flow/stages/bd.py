"""Block design 阶段：复用 / 原位刷新 / 全量重建。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from ..hashing import compose, sha256_dir, sha256_files
from ..paths import BD_NAME, CUSTOM_IPS, PART, FlowError, RepoPaths
from ..runner import VivadoRunner
from ..stamps import SCHEMA, clear_runs, load_json, save_json
from ..tclgen import write_source_wrapper


@dataclass(frozen=True)
class BdArgs:
    dsp_clk: float = 450.0
    uart: str = "uart1"
    baud: int = 115200


def bd_hashes(paths: RepoPaths, args: BdArgs, vivado: str) -> dict[str, str]:
    structure = compose(
        "bd_structure",
        sha256_files([paths.vivado_dir / "create_bd.tcl", paths.vivado_dir / "rtl" / "dpm_mmcm_src.v"]),
        SCHEMA,
        PART,
        vivado,
    )
    args_hash = compose("bd_args", PART, args.dsp_clk, args.uart, args.baud)
    ip_hashes: dict[str, str] = {}
    for ip, spec in CUSTOM_IPS.items():
        ip_dir = paths.ip_dir / spec["ip_dir"]
        ip_hashes[ip] = sha256_dir(ip_dir) if ip_dir.is_dir() else ""
    return {
        "structure": structure,
        "args": args_hash,
        "ip_dpm_top": ip_hashes["dpm_top"],
        "ip_dpm_src_drp": ip_hashes["dpm_src_drp"],
        "vivado": vivado,
        "part": PART,
    }


def _stamp_matches(stamp: dict[str, object] | None, current: dict[str, str]) -> bool:
    if not stamp or stamp.get("schema") != SCHEMA:
        return False
    return all(stamp.get(key) == value for key, value in current.items())


def _bd_file(paths: RepoPaths) -> str:
    return str(paths.proj_dir / "dpm_bd.srcs" / "sources_1" / "bd" / BD_NAME / f"{BD_NAME}.bd")


def bd_stage(
    paths: RepoPaths,
    runner: VivadoRunner,
    vivado: str,
    args: BdArgs,
    *,
    force: bool,
    dry_run: bool,
) -> dict[str, object]:
    current = bd_hashes(paths, args, vivado)
    stamp = load_json(paths.bd_stamp_file)
    project_exists = paths.proj_file.is_file()

    if not force and project_exists and _stamp_matches(stamp, current):
        print(f"BD_REUSED: structure={current['structure'][:12]} ip_top={current['ip_dpm_top'][:12]}")
        return current

    # 只有 IP 目录内容变化时，先在原工程内原位刷新。
    if (
        not force
        and project_exists
        and stamp is not None
        and stamp.get("schema") == SCHEMA
        and stamp.get("structure") == current["structure"]
        and stamp.get("args") == current["args"]
        and stamp.get("vivado") == current["vivado"]
        and stamp.get("part") == current["part"]
    ):
        changed = [ip for ip in CUSTOM_IPS if stamp.get(f"ip_{ip}") != current[f"ip_{ip}"]]
        if changed:
            upgrade_ips = [f"{BD_NAME}_{CUSTOM_IPS[ip]['cell']}" for ip in changed]
            print(f"BD_REFRESH: changed_ips={changed} upgrade_ips={upgrade_ips}")
            if dry_run:
                return current
            plan = {
                "stage": "refresh_bd",
                "project": str(paths.proj_file),
                "part": PART,
                "bd": _bd_file(paths),
                "upgrade_ips": upgrade_ips,
                "lite_base": "0x43C00000",
                "drp_base": "0x43C10000",
                "suppress_early": str(paths.suppress_early),
                "result_file": str(paths.result_file("bd_refresh")),
            }
            try:
                result = runner.run_stage("bd_refresh", paths.flow_tcl_dir / "refresh_bd.tcl", plan)
                runner.check(result, "BD 原位刷新")
            except FlowError as exc:
                print(f"BD_REFRESH_FALLBACK: {exc}")
            else:
                save_json(paths.bd_stamp_file, {"schema": SCHEMA, **current})
                print(f"BD_REFRESHED: upgraded={changed}")
                return current

    # 全量重建。
    if dry_run:
        print("BD_REBUILT: (dry-run) 将删除并重建 bd_proj")
        return current
    print("BD_REBUILT: 删除并重建 bd_proj")
    paths.bd_stamp_file.unlink(missing_ok=True)
    clear_runs(paths)
    shutil.rmtree(paths.proj_dir, ignore_errors=True)

    argv: list[object] = []
    dsp_clk = float(args.dsp_clk)
    if abs(dsp_clk - 450.0) > 1e-9:
        argv.append(int(dsp_clk) if dsp_clk.is_integer() else dsp_clk)
    if args.uart != "uart1" or args.baud != 115200:
        argv.extend(["-uart", args.uart, "-baud", str(args.baud)])

    wrapper = write_source_wrapper(
        paths.wrapper_file("create_bd"),
        paths.vivado_dir / "create_bd.tcl",
        argv=argv,
    )
    result = runner.run_script(wrapper, paths.log_file("create_bd"))
    runner.check_script(result, "create_bd")
    if not paths.proj_file.is_file():
        raise FlowError(f"create_bd 之后仍找不到工程：{paths.proj_file}")
    save_json(paths.bd_stamp_file, {"schema": SCHEMA, **current})
    print(f"BD_DONE: {paths.proj_file}")
    return current
