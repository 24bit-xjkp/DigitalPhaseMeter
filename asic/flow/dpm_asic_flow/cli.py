"""`dpm-asic-flow` 命令行入口：DigitalPhaseMeterTop 的 Yosys + OpenROAD ASIC 流程（sky130 HD）。

用法::

    uv run dpm-asic-flow                 # 综合 + floorplan + place + CTS + route + GDS
    uv run dpm-asic-flow synth           # 只跑综合
    uv run dpm-asic-flow floorplan       # 从综合网表开始，floorplan + 四边 IO + tapcell + PDN
    uv run dpm-asic-flow place
    uv run dpm-asic-flow cts
    uv run dpm-asic-flow route-opt       # 只做布线前电气规则修复（04_route_opt.odb）
    uv run dpm-asic-flow route           # 布线前修复 + 布线，输出 final.def / final.odb
    uv run dpm-asic-flow gds             # 从 final.def 导出 GDS（需要 KLayout Python 模块）

工艺库默认 ``~/skywater-pdk``，可用 ``--pdk`` 覆盖。利用率、布局密度和 SDC 分别用
``--utilization``、``--place-density``、``--sdc`` 覆盖；不传时使用
``asic/openroad/config.tcl`` 与 ``asic/synth/dpm_asic.sdc`` 里的交付默认值。

本入口只负责按阶段调用现有工具，不重新实现综合/布局算法；每个阶段的日志写到
``build/asic/`` 下，便于和 OpenROAD 的数据库一起检查。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import CORNERS, DEFAULT_PDK, STAGES, FlowError, RepoPaths, find_repo_root
from .prepare_pdk import prepare_many


def log(message: str) -> None:
    print(f"==> {message}", flush=True)


def need_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"缺少工具：{name}")


def need_file(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"缺少 {path}，请先运行前一个阶段（uv run dpm-asic-flow <stage>）")


def stream(command: list[str], cwd: Path, log_path: Path | None = None) -> None:
    """在 ``cwd`` 下执行命令，同时把输出写到终端和 ``log_path``。"""
    printable = " ".join(str(part) for part in command)
    print(f"+ {printable}", flush=True)

    stream_out = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream_out = log_path.open("w", encoding="utf-8")

    returncode = 1
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=os.environ,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if stream_out is not None:
                stream_out.write(line)
                stream_out.flush()
        returncode = process.wait()
    finally:
        if stream_out is not None:
            stream_out.close()

    if returncode != 0:
        raise SystemExit(f"命令失败（退出码 {returncode}）：{printable}")


def openroad(paths: RepoPaths, script: str, log_path: Path) -> None:
    stream(
        ["openroad", "-no_init", "-exit", str(paths.openroad_script_dir / script)],
        paths.root,
        log_path,
    )


def run_synth(paths: RepoPaths, pdk: Path) -> None:
    log("生成 SystemVerilog（./mill emit DigitalPhaseMeterTopA）")
    stream([str(paths.root / "mill"), "emit", "DigitalPhaseMeterTopA"], paths.root)

    # Yosys 用 ss 做时序感知综合；OpenROAD 需要 ss/tt/ff 三个 corner 做 MCMM。
    log(f"准备 PDK 派生文件（{' / '.join(CORNERS)}）")
    prepare_many(CORNERS, pdk=pdk, build_dir=paths.build_dir)

    # ASIC 入口只生成一份异步复位的 DigitalPhaseMeterTop（没有平台分支文件）；
    # verification/ 下的 assert 不参与综合。filelist.f 里的路径相对
    # build/chisel/DigitalPhaseMeterTopA/，这里改写成绝对路径，避免 read_slang
    # 按当前工作目录解析。
    top_dir = paths.rtl_dir
    top_filelist = top_dir / "filelist.f"
    if not top_filelist.is_file():
        raise SystemExit(f"找不到 {top_filelist}")
    lines: list[str] = []
    for line in top_filelist.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name or name.startswith("verification/"):
            continue
        lines.append(str(top_dir / name))
    paths.synth_dir.mkdir(parents=True, exist_ok=True)
    (paths.synth_dir / "filelist.f").write_text("\n".join(lines) + "\n", encoding="utf-8")

    log("Yosys 综合（read_slang + clockgate + ABC 时序映射）")
    stream(
        [
            "yosys",
            "-l",
            str(paths.synth_dir / "yosys.log"),
            "-s",
            str(paths.synth_script_dir / "synth.ys"),
        ],
        paths.root,
    )


def run_floorplan(paths: RepoPaths) -> None:
    need_file(paths.synth_dir / "dpm_asic_synth.v")
    log("OpenROAD floorplan（利用率 60%，IO 沿四周摆放）")
    openroad(paths, "01_floorplan.tcl", paths.openroad_dir / "01_floorplan.log")
    stream(
        [
            sys.executable,
            str(paths.openroad_script_dir / "check_pins.py"),
            str(paths.openroad_dir / "01_floorplan.def"),
        ],
        paths.root,
    )


def run_place(paths: RepoPaths) -> None:
    need_file(paths.openroad_dir / "01_floorplan.odb")
    log("OpenROAD placement")
    openroad(paths, "02_place.tcl", paths.openroad_dir / "02_place.log")


def run_cts(paths: RepoPaths) -> None:
    need_file(paths.openroad_dir / "02_place.odb")
    log("OpenROAD CTS")
    openroad(paths, "03_cts.tcl", paths.openroad_dir / "03_cts.log")


def run_route_opt(paths: RepoPaths) -> None:
    need_file(paths.openroad_dir / "03_cts.odb")
    log("OpenROAD 布线前电气规则修复（含一次 GRT 寄生评估）")
    openroad(paths, "04_route_opt.tcl", paths.openroad_dir / "04_route_opt.log")


def run_route(paths: RepoPaths) -> None:
    need_file(paths.openroad_dir / "04_route_opt.odb")
    log("OpenROAD routing")
    openroad(paths, "05_route.tcl", paths.openroad_dir / "05_route.log")
    stream(
        [
            sys.executable,
            str(paths.openroad_script_dir / "check_pins.py"),
            str(paths.openroad_dir / "final.def"),
        ],
        paths.root,
    )


def run_gds(paths: RepoPaths, pdk: Path) -> None:
    need_file(paths.openroad_dir / "final.def")
    if importlib.util.find_spec("klayout") is None:
        print("==> 跳过 GDS：当前 Python 没有 KLayout 的 klayout 包", flush=True)
        return

    log("KLayout：DEF + PDK 单元 GDS -> dpm_asic.gds")
    # 这里故意用 PDK 里的真实技术 LEF 路径：KLayout 的 LEF/DEF 读取器对指向
    # 技术 LEF 的符号链接解析有偏差，会把 via 定义当成未知名字。
    stream(
        [
            sys.executable,
            str(paths.openroad_script_dir / "06_gds.py"),
            "--def",
            str(paths.openroad_dir / "final.def"),
            "--output",
            str(paths.openroad_dir / "dpm_asic.gds"),
            "--tech-lef",
            str(pdk / "libraries" / "sky130_fd_sc_hd" / "latest" / "tech" / "sky130_fd_sc_hd.tlef"),
            "--cells-lef",
            str(paths.build_dir / "sky130_fd_sc_hd_cells.lef"),
            "--cells-dir",
            str(pdk / "libraries" / "sky130_fd_sc_hd" / "latest" / "cells"),
        ],
        paths.root,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dpm-asic-flow",
        description="DigitalPhaseMeterTop 的 Yosys + OpenROAD ASIC 流程（sky130 HD）",
    )
    parser.add_argument("stage", nargs="?", default="all", choices=STAGES, help="要运行的阶段")
    parser.add_argument("--repo-root", type=Path, help="仓库根目录（默认从当前目录/包位置自动识别）")
    parser.add_argument("--pdk", type=Path, help="skywater-pdk 根目录（默认 DPM_PDK 或 ~/skywater-pdk）")
    parser.add_argument("--utilization", type=int, help="floorplan 利用率百分比（默认 60，即 0.6）")
    parser.add_argument("--place-density", type=float, help="global_placement -density（默认 0.60）")
    parser.add_argument(
        "--sdc",
        type=Path,
        help="替换默认 SDC（OpenROAD 时序约束；ABC 的 -D 按域写在 synth.ys 的 --clock 参数里，改频率需同步修改）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        paths = RepoPaths(find_repo_root(args.repo_root))
    except FlowError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1

    pdk = (args.pdk if args.pdk is not None else Path(os.environ.get("DPM_PDK", DEFAULT_PDK))).expanduser().resolve()
    # OpenROAD 的 config.tcl 与 06_gds.py 仍从环境读工艺库根目录。
    os.environ["DPM_PDK"] = str(pdk)
    if args.utilization is not None:
        os.environ["DPM_UTILIZATION"] = str(args.utilization)
    if args.place_density is not None:
        os.environ["DPM_PLACE_DENSITY"] = str(args.place_density)
    if args.sdc is not None:
        os.environ["DPM_SDC"] = str(args.sdc.expanduser().resolve())

    for tool in ("yosys", "openroad", "python3"):
        need_tool(tool)

    paths.synth_dir.mkdir(parents=True, exist_ok=True)
    paths.openroad_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.stage in ("all", "synth"):
            run_synth(paths, pdk)
        if args.stage in ("all", "floorplan"):
            run_floorplan(paths)
        if args.stage in ("all", "place"):
            run_place(paths)
        if args.stage in ("all", "cts"):
            run_cts(paths)
        if args.stage in ("all", "route", "route-opt"):
            run_route_opt(paths)
        if args.stage in ("all", "route"):
            run_route(paths)
        if args.stage in ("all", "gds"):
            run_gds(paths, pdk)
    except FlowError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1

    print(flush=True)
    print("完成。结果目录：", flush=True)
    print(f"  综合网表 : {paths.synth_dir / 'dpm_asic_synth.v'}", flush=True)
    print(f"  Yosys 日志: {paths.synth_dir / 'yosys.log'}", flush=True)
    print(f"  Floorplan : {paths.openroad_dir / '01_floorplan.def'}", flush=True)
    print(f"  最终 DEF  : {paths.openroad_dir / 'final.def'}", flush=True)
    print(f"  最终 DB   : {paths.openroad_dir / 'final.odb'}", flush=True)
    print(f"  最终 GDS  : {paths.openroad_dir / 'dpm_asic.gds'}（如果 KLayout 可用）", flush=True)
    print(f"  图层配置  : {paths.openroad_dir / 'dpm_asic.lyp'}（与 GDS 同时生成）", flush=True)
    return 0
