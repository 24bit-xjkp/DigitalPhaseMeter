"""`dpm-asic-flow` 命令行入口：DigitalPhaseMeterTop 的 Yosys + OpenROAD ASIC 流程（sky130 HD）。

用法::

    uv run dpm-asic-flow                 # 综合 + floorplan + place + CTS + route + GDS
    uv run dpm-asic-flow synth           # 只跑综合
    uv run dpm-asic-flow floorplan       # 从综合网表开始，floorplan + 左右两边 IO + tapcell + PDN
    uv run dpm-asic-flow place
    uv run dpm-asic-flow cts
    uv run dpm-asic-flow route-opt       # 全局布线 + GRT 寄生修复 + 预布线天线修复（04_route_opt.odb）
    uv run dpm-asic-flow route           # 详细布线 + 填充，输出 final.def / final.odb / final.v
    uv run dpm-asic-flow gds             # 从 final.def 导出 GDS（需要 KLayout Python 模块）

工艺库、利用率、布局密度、SDC 与 PnR 调优项见 ``uv run dpm-asic-flow --help``；
默认值由 :class:`argparse.ArgumentDefaultsHelpFormatter` 打印。开关项统一用
``--foo`` / ``--no-foo`` 成对给出（例如 ``--virtual-cts`` / ``--no-virtual-cts``）。

所有选项都会转成 ``DPM_*`` 环境变量，单独跑某个 Tcl 阶段时也可以直接 export。
优先级：显式命令行参数 > 环境变量 > 内置默认值。

dspClock 的目标频率由环境变量 ``DPM_DSP_FREQ_MHZ`` 决定（单位 MHz，默认 450）。
`synth` 把规范化后的值导出给 Yosys 内置 Tcl（``asic/synth/abc_targets.tcl``），
由 Tcl 读环境变量、算出整数 ps 目标并执行分模块 abc；OpenROAD 的
``asic/synth/dpm_asic.sdc`` 也读同一个变量，两边用同一套取整规则。
只跑 OpenROAD 阶段时记得 export 与综合时相同的值，例如降到 300MHz：

    DPM_DSP_FREQ_MHZ=300 uv run dpm-asic-flow all

450MHz 收敛所用的 PnR 调优默认开启：布局阶段 2 次 anneal resynth、virtual CTS
（skew 上限 0.05）、GRT critical nets 20%、``repair_timing -setup_margin 1.0``
与 gate cloning，resynth 搜索 200 次。需要回到未调优配置时，用对应的 ``--no-*``
或零值逐项关闭（例如 ``--no-virtual-cts --place-resynth-iters 0``）。

本入口只负责按阶段调用现有工具，不重新实现综合/布局算法；每个阶段的日志写到
``build/asic/`` 下，便于和 OpenROAD 的数据库一起检查。
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import CORNERS, DEFAULT_PDK, STAGES, RepoPaths, find_repo_root
from .prepare_pdk import prepare_many

# dspClock 的默认目标频率（MHz）；可用环境变量 DPM_DSP_FREQ_MHZ 覆盖。
DEFAULT_DSP_FREQ_MHZ = 450.0

RESYNTH_MODES = ("plain", "anneal", "genetic")


def _env_text(name: str, default: str) -> str:
    """读取环境变量；未设置或为空字符串时返回默认值。"""
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default: int) -> int:
    text = _env_text(name, "")
    if not text:
        return default
    assert text.lstrip("+-").isdigit(), f"{name} 必须是整数，收到 {text!r}"
    return int(text)


def _env_float(name: str, default: float | None) -> float | None:
    text = _env_text(name, "")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        raise AssertionError(f"{name} 必须是浮点数，收到 {text!r}") from None


def _env_bool(name: str, default: bool) -> bool:
    text = _env_text(name, "")
    if not text:
        return default
    assert text in ("0", "1"), f"{name} 必须是 0 或 1，收到 {text!r}"
    return text == "1"


def _env_path(name: str, default: str | Path) -> Path:
    return Path(_env_text(name, str(default))).expanduser()


def read_dsp_freq_mhz() -> float:
    """从环境变量 ``DPM_DSP_FREQ_MHZ`` 读取 dspClock 频率，未设置时用默认值。"""
    text = os.environ.get("DPM_DSP_FREQ_MHZ", "").strip()
    if not text:
        return DEFAULT_DSP_FREQ_MHZ
    try:
        freq_mhz = float(text)
    except ValueError:
        raise AssertionError(f"DPM_DSP_FREQ_MHZ 必须是数字（单位 MHz），收到 {text!r}") from None
    assert math.isfinite(freq_mhz) and freq_mhz > 0.0, f"DPM_DSP_FREQ_MHZ 必须是正数（单位 MHz），收到 {text!r}"
    return freq_mhz


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


def write_synth_filelist(paths: RepoPaths) -> Path:
    """把 Chisel filelist 过滤成 ``build/asic/synth/filelist.f``。

    去掉 ``verification/`` 下的断言，并写成相对 ``filelist.f`` 所在目录的路径；
    synth.ys 用 ``read_slang -F`` 读取，Yosys 会按该文件位置解析这些路径。
    """
    top_dir = paths.rtl_dir
    top_filelist = top_dir / "filelist.f"
    if not top_filelist.is_file():
        raise SystemExit(f"找不到 {top_filelist}")
    paths.synth_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for line in top_filelist.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name or name.startswith("verification/"):
            continue
        relative = os.path.relpath(top_dir / name, paths.synth_dir)
        lines.append(Path(relative).as_posix())
    path = paths.synth_dir / "filelist.f"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_synth(paths: RepoPaths, pdk: Path, freq_mhz: float) -> None:
    log("生成 SystemVerilog（./mill emit DigitalPhaseMeterTopA）")
    stream([str(paths.root / "mill"), "emit", "DigitalPhaseMeterTopA"], paths.root)

    # Yosys 用 ss 做时序感知综合；OpenROAD 需要 ss/tt/ff 三个 corner 做 MCMM。
    log(f"准备 PDK 派生文件（{' / '.join(CORNERS)}）")
    prepare_many(CORNERS, pdk=pdk, build_dir=paths.build_dir)

    # ASIC 入口只生成一份异步复位的 DigitalPhaseMeterTop（没有平台分支文件）；
    # verification/ 下的 assert 不参与综合。filelist 写成相对路径，synth.ys 用
    # read_slang -F 按 filelist.f 所在目录解析。
    write_synth_filelist(paths)

    # dspClock 频率来自 DPM_DSP_FREQ_MHZ；abc_targets.tcl 在 Yosys 里读同一个
    # 环境变量并算出 ABC 的整数 ps 目标，与 OpenROAD SDC 使用同一套规则。
    # retime 用 DPM_RETIME=1 选择另一份 synth 脚本：CE/SRST 摊成 mux、异步复位 FF
    # 先 dfflibmap，abc -dff 只重定时无控制端 FF；DspCounter 流水线带 keep，脚本会
    # 在 ABC 前把它先映射成库单元，避免 dretime 把分段流水线拆平。默认仍走交付基线。
    retime = os.environ.get("DPM_RETIME") == "1"
    synth_script = "synth_retime.ys" if retime else "synth.ys"
    log(f"Yosys 综合（read_slang + clockgate + 分模块 ABC；retime {'on' if retime else 'off'}；" f"dspClock {freq_mhz:g}MHz）")
    stream(
        [
            "yosys",
            "-l",
            str(paths.synth_dir / "yosys.log"),
            "-s",
            str(paths.synth_script_dir / synth_script),
        ],
        paths.root,
    )


def run_floorplan(paths: RepoPaths) -> None:
    need_file(paths.synth_dir / "dpm_asic_synth.v")
    log(f"OpenROAD floorplan（利用率 60%，IO 沿 {os.environ.get('DPM_IO_SIDES', 'left right')} 摆放）")
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
    log("OpenROAD 全局布线 + GRT 寄生修复 + 预布线天线修复")
    openroad(paths, "04_route_opt.tcl", paths.openroad_dir / "04_route_opt.log")


def run_route(paths: RepoPaths) -> None:
    need_file(paths.openroad_dir / "04_route_opt.odb")
    log("OpenROAD 详细布线（输出 final.def / final.odb / final.v）")
    openroad(paths, "05_route.tcl", paths.openroad_dir / "05_route.log")
    need_file(paths.final_netlist)
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
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("stage", nargs="?", default="all", choices=STAGES, help="要运行的阶段")
    parser.add_argument("--repo-root", type=Path, help="仓库根目录；省略时从当前目录或包位置向上查找")
    parser.add_argument("--pdk", type=Path, default=_env_path("DPM_PDK", DEFAULT_PDK), help="skywater-pdk 根目录")
    parser.add_argument("--utilization", type=int, default=_env_int("DPM_UTILIZATION", 60), help="floorplan 利用率百分比")
    parser.add_argument(
        "--place-density", type=float, default=_env_float("DPM_PLACE_DENSITY", 0.60), help="global_placement -density（0..1）"
    )
    parser.add_argument("--sdc", type=Path, default=_env_path("DPM_SDC", "asic/synth/dpm_asic.sdc"), help="OpenROAD 时序约束文件")
    parser.add_argument(
        "--retime",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("DPM_RETIME", False),
        help=(
            "实验性：开启 ABC 跨寄存器 retime（使用 synth_retime.ys）。"
            "异步复位/置位 FF 与 DspCounter 流水线会先映射成库单元，避免 ABC 破坏复位链或拆平流水线"
        ),
    )
    parser.add_argument(
        "--timing-driven-place",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("DPM_TIMING_DRIVEN_PLACE", False),
        help="global_placement -timing_driven（virtual CTS 与布局迭代 resynth 会自动打开）",
    )
    parser.add_argument(
        "--timing-driven-repair",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("DPM_TIMING_DRIVEN_REPAIR", False),
        help="global_placement -timing_driven_repair_timing（隐含 --timing-driven-place）",
    )
    parser.add_argument(
        "--virtual-cts",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("DPM_VIRTUAL_CTS", True),
        help="global_placement -virtual_cts（隐含 --timing-driven-place）",
    )
    parser.add_argument(
        "--virtual-cts-max-skew-fraction",
        type=float,
        metavar="F",
        default=_env_float("DPM_VIRTUAL_CTS_MAX_SKEW", 0.05),
        help="virtual_cts 最大插入延迟占周期比例（0..1）",
    )
    parser.add_argument(
        "--grt-critical-nets",
        type=int,
        metavar="P",
        default=_env_int("DPM_GRT_CRITICAL_NETS", 20),
        help="global_route -critical_nets_percentage（0..100，0 关闭）",
    )
    parser.add_argument(
        "--useful-skew",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("DPM_USEFUL_SKEW", False),
        help="clock_tree_synthesis -useful_skew",
    )
    parser.add_argument(
        "--repair-setup-margin",
        type=float,
        metavar="NS",
        default=_env_float("DPM_REPAIR_SETUP_MARGIN", 1.0),
        help="repair_timing -setup_margin（ns）",
    )
    parser.add_argument(
        "--gate-cloning",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("DPM_GATE_CLONING", True),
        help="允许 repair_timing 做 gate cloning",
    )
    parser.add_argument(
        "--place-resynth-iters",
        type=int,
        metavar="N",
        default=_env_int("DPM_PLACE_RESYNTH_ITERS", 2),
        help="布局阶段在初始全量布局后重复 resynth+增量布局的次数（0 关闭，隐含 --timing-driven-place）",
    )
    parser.add_argument(
        "--place-resynth-mode",
        choices=RESYNTH_MODES,
        default=_env_text("DPM_PLACE_RESYNTH_MODE", "anneal"),
        help="布局迭代 resynth 模式",
    )
    parser.add_argument(
        "--resynth-iters",
        type=int,
        metavar="N",
        default=_env_int("DPM_RESYNTH_ITERS", 200),
        help="每次 resynth 搜索的 ABC 脚本迭代数",
    )
    parser.add_argument(
        "--resynth-initial-ops",
        type=int,
        metavar="N",
        default=_env_int("DPM_RESYNTH_INITIAL_OPS", 3),
        help="初始 ABC 脚本的操作数",
    )
    parser.add_argument(
        "--resynth-revert-after",
        type=int,
        metavar="N",
        default=_env_int("DPM_RESYNTH_REVERT_AFTER", 50),
        help="退火连续变差多少次后回退到当前最优",
    )
    parser.add_argument(
        "--resynth-seed",
        type=int,
        metavar="N",
        default=_env_int("DPM_RESYNTH_SEED", 1),
        help="resynth 搜索随机种子",
    )
    parser.add_argument(
        "--resynth-temp",
        type=float,
        metavar="T",
        default=_env_float("DPM_RESYNTH_TEMP", None),
        help="退火初始温度；不给时用最差端点的 required arrival time",
    )
    parser.add_argument(
        "--resynth-population-size",
        type=int,
        metavar="N",
        default=_env_int("DPM_RESYNTH_POPULATION_SIZE", 8),
        help="遗传算法种群大小",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=_env_int("DPM_THREADS", 0),
        help="OpenROAD 线程数（0 表示自动）",
    )
    return parser


def apply_options(args: argparse.Namespace, paths: RepoPaths | None = None) -> tuple[Path, float]:
    """把命令行选项转成 ``DPM_*`` 环境变量，返回 (PDK 路径, dspClock 频率 MHz)。

    优先级：显式命令行参数 > 环境变量 > 内置默认值；非法参数直接 assert。
    ``paths`` 省略时按当前目录解析相对 ``--sdc``。
    """
    pdk = args.pdk.expanduser().resolve()
    os.environ["DPM_PDK"] = str(pdk)

    skew_fraction = args.virtual_cts_max_skew_fraction
    assert 0 <= skew_fraction <= 1, f"--virtual-cts-max-skew-fraction 必须在 0..1，收到 {skew_fraction}"
    assert 0 <= args.grt_critical_nets <= 100, f"--grt-critical-nets 必须在 0..100，收到 {args.grt_critical_nets}"
    assert args.repair_setup_margin >= 0, f"--repair-setup-margin 必须 >= 0，收到 {args.repair_setup_margin}"
    assert args.place_resynth_iters >= 0, f"--place-resynth-iters 必须 >= 0，收到 {args.place_resynth_iters}"
    mode = args.place_resynth_mode
    assert mode in RESYNTH_MODES, f"--place-resynth-mode 必须是 {'/'.join(RESYNTH_MODES)}，收到 {mode!r}"
    assert args.resynth_iters >= 1, f"--resynth-iters 必须 >= 1，收到 {args.resynth_iters}"
    assert args.resynth_initial_ops >= 1, f"--resynth-initial-ops 必须 >= 1，收到 {args.resynth_initial_ops}"
    assert args.resynth_revert_after >= 0, f"--resynth-revert-after 必须 >= 0，收到 {args.resynth_revert_after}"
    assert args.resynth_population_size >= 1, f"--resynth-population-size 必须 >= 1，收到 {args.resynth_population_size}"
    assert args.resynth_temp is None or args.resynth_temp > 0, f"--resynth-temp 必须 > 0，收到 {args.resynth_temp}"
    assert args.threads >= 0, f"--threads 必须 >= 0，收到 {args.threads}"

    os.environ["DPM_UTILIZATION"] = str(args.utilization)
    os.environ["DPM_PLACE_DENSITY"] = str(args.place_density)
    sdc = args.sdc.expanduser()
    sdc_root = paths.root if paths is not None else Path.cwd()
    os.environ["DPM_SDC"] = str((sdc if sdc.is_absolute() else sdc_root / sdc).resolve())
    os.environ["DPM_RETIME"] = "1" if args.retime else "0"
    os.environ["DPM_TIMING_DRIVEN_PLACE"] = "1" if args.timing_driven_place else "0"
    os.environ["DPM_TIMING_DRIVEN_REPAIR"] = "1" if args.timing_driven_repair else "0"
    os.environ["DPM_VIRTUAL_CTS"] = "1" if args.virtual_cts else "0"
    os.environ["DPM_VIRTUAL_CTS_MAX_SKEW"] = str(skew_fraction)
    os.environ["DPM_GRT_CRITICAL_NETS"] = str(args.grt_critical_nets)
    os.environ["DPM_USEFUL_SKEW"] = "1" if args.useful_skew else "0"
    os.environ["DPM_REPAIR_SETUP_MARGIN"] = str(args.repair_setup_margin)
    os.environ["DPM_GATE_CLONING"] = "1" if args.gate_cloning else "0"
    os.environ["DPM_PLACE_RESYNTH_ITERS"] = str(args.place_resynth_iters)
    os.environ["DPM_PLACE_RESYNTH_MODE"] = mode
    os.environ["DPM_RESYNTH_ITERS"] = str(args.resynth_iters)
    os.environ["DPM_RESYNTH_INITIAL_OPS"] = str(args.resynth_initial_ops)
    os.environ["DPM_RESYNTH_REVERT_AFTER"] = str(args.resynth_revert_after)
    os.environ["DPM_RESYNTH_SEED"] = str(args.resynth_seed)
    if args.resynth_temp is None:
        os.environ.pop("DPM_RESYNTH_TEMP", None)
    else:
        os.environ["DPM_RESYNTH_TEMP"] = str(args.resynth_temp)
    os.environ["DPM_RESYNTH_POPULATION_SIZE"] = str(args.resynth_population_size)
    if args.threads:
        os.environ["DPM_THREADS"] = str(args.threads)
    else:
        os.environ.pop("DPM_THREADS", None)

    # dspClock 频率：环境变量是唯一来源；把规范化后的值写回环境，OpenROAD
    # （SDC 读同一个变量）和 Yosys 生成的 ABC 目标因此始终一致。
    dsp_freq_mhz = read_dsp_freq_mhz()
    os.environ["DPM_DSP_FREQ_MHZ"] = f"{dsp_freq_mhz:g}"

    # timing-driven repair 隐含 timing-driven place；virtual CTS 与布局迭代 resynth
    # 也只在 timing-driven 布局里生效。config.tcl 里有同样的隐含规则，这里同步一份，
    # 保证 CLI 导出的环境自洽。
    if args.timing_driven_repair or args.virtual_cts or args.place_resynth_iters > 0:
        os.environ["DPM_TIMING_DRIVEN_PLACE"] = "1"
    return pdk, dsp_freq_mhz


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = RepoPaths(find_repo_root(args.repo_root))
    pdk, dsp_freq_mhz = apply_options(args, paths)

    for tool in ("yosys", "openroad", "python3"):
        need_tool(tool)

    paths.synth_dir.mkdir(parents=True, exist_ok=True)
    paths.openroad_dir.mkdir(parents=True, exist_ok=True)

    if args.stage in ("all", "synth"):
        run_synth(paths, pdk, dsp_freq_mhz)
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

    print(flush=True)
    print("完成。结果目录：", flush=True)
    print(f"  综合网表 : {paths.synth_dir / 'dpm_asic_synth.v'}", flush=True)
    print(f"  Yosys 日志: {paths.synth_dir / 'yosys.log'}", flush=True)
    print(f"  Floorplan : {paths.openroad_dir / '01_floorplan.def'}", flush=True)
    print(f"  最终 DEF  : {paths.openroad_dir / 'final.def'}", flush=True)
    print(f"  最终 DB   : {paths.openroad_dir / 'final.odb'}", flush=True)
    print(f"  最终网表  : {paths.final_netlist}（门级，已去掉 filler/tapcell）", flush=True)
    print(f"  最终 GDS  : {paths.openroad_dir / 'dpm_asic.gds'}（如果 KLayout 可用）", flush=True)
    print(f"  图层配置  : {paths.openroad_dir / 'dpm_asic.lyp'}（与 GDS 同时生成）", flush=True)
    return 0
