"""把 sky130 PDK 整理成 ASIC 流程直接可用的形式。

流程只从 ``DPM_PDK``（默认 ``~/skywater-pdk``）读取工艺文件，不在仓库里
保存 PDK 派生文件；脚本在 ``build/asic/`` 下生成：

* ``sky130_fd_sc_hd.tlef`` / ``sky130_fd_sc_hd__<corner>.lib``：指向 PDK 的符号链接。
  corner 由 ``--corner`` 以列表给出，默认准备 ss/tt/ff 三个（OpenROAD 的 MCMM
  需要全部三个）；OpenROAD 按 LEF 文件名区分 library，因此技术 LEF 与单元 LEF
  必须不同名。
* ``sky130_fd_sc_hd__<corner>_synth.lib``：去掉 ``lpflow_*`` 单元的综合用 liberty
  （见 :func:`filter_synth_liberty`）。
* ``sky130_fd_sc_hd_cells.lef``：把 ``cells/*/*.lef`` 合并成一个 library。合并时
  去掉 LEF 5.6 起废弃的 ``SOURCE`` 语句，并给天线二极管补上 OpenROAD 天线修复
  需要的 ``CLASS CORE ANTENNACELL`` 与 ``ANTENNADIFFAREA`` —— sky130 的原始 PDK
  LEF 没有这两项，``repair_antennas`` 会直接报错。
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .paths import CORNERS, DEFAULT_PDK, RepoPaths, find_repo_root

# sky130 HD 里需要补天线属性的单元。
DIODE = "sky130_fd_sc_hd__diode_2"


@dataclass(frozen=True)
class PreparedPdk:
    """一次 PDK 准备的输入/输出摘要，供调用方打印或测试断言。"""

    pdk: Path
    corner: str
    tech_lef: Path
    timing_lib: Path
    synth_lib: Path
    cells_lef: Path
    removed_cells: int
    merged_macros: int


def _link(src: Path, dst: Path) -> None:
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    dst.symlink_to(src)


def macro_block(path: Path) -> tuple[str, str]:
    """读出一个单元 LEF 里的 MACRO ... END <name> 文本。"""
    lines = path.read_text(errors="replace").splitlines()
    started = False
    name = ""
    out: list[str] = []
    for line in lines:
        if not started:
            match = re.match(r"^MACRO\s+(\S+)", line)
            if match:
                started = True
                name = match.group(1)
                out.append(line)
            continue
        if re.match(r"^\s*SOURCE\b", line):
            # LEF 5.6 起 SOURCE 已废弃，OpenROAD 只会刷警告，直接丢掉
            continue
        out.append(line)
        if line.strip() == f"END {name}":
            return name, "\n".join(out) + "\n"
    assert False, f"{path} 里没有完整的 MACRO 块"


def filter_synth_liberty(src: Path, dst: Path) -> int:
    """生成只给 Yosys 用的 liberty：去掉 lpflow_* 单元。

    lpflow 系列是低功耗隔离/电平保持单元，ABC 会把它们当普通组合逻辑用，
    但它们的输出驱动和 SLEEP 控制引脚不适合普通信号路径，容易在布线后出现
    max transition/capacitance 残余违例。OpenROAD 仍然读完整 liberty。
    """
    removed = 0
    skipping = False
    depth = 0
    out: list[str] = []
    for line in src.read_text(errors="replace").splitlines(keepends=True):
        if not skipping:
            if re.match(r'^\s*cell\s*\(\s*"sky130_fd_sc_hd__lpflow_', line):
                skipping = True
                depth = line.count("{") - line.count("}")
                removed += 1
                continue
            out.append(line)
        else:
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                skipping = False
    dst.write_text("".join(out))
    return removed


def _corner_files(pdk: Path, corner: str) -> tuple[Path, Path, Path]:
    """返回 (技术 LEF, 该 corner 的 timing liberty, 单元 LEF 目录)。"""
    lib_dir = pdk / "libraries" / "sky130_fd_sc_hd" / "latest"
    tech_lef = lib_dir / "tech" / "sky130_fd_sc_hd.tlef"
    timing_lib = lib_dir / "timing" / f"sky130_fd_sc_hd__{corner}.lib"
    cell_dir = lib_dir / "cells"
    for path in (tech_lef, timing_lib, cell_dir):
        assert path.exists(), f"PDK 文件不存在：{path}（用 --pdk 或 DPM_PDK 指定 skywater-pdk 根目录）"
    return tech_lef, timing_lib, cell_dir


def prepare(
    corner: str,
    pdk: Path | None = None,
    build_dir: Path | None = None,
) -> PreparedPdk:
    """生成 ``build_dir`` 下的技术 LEF、corner liberty、综合 liberty 与合并单元 LEF。"""
    if pdk is None:
        pdk = Path(os.environ.get("DPM_PDK", DEFAULT_PDK))
    pdk = Path(pdk).expanduser().resolve()
    if build_dir is None:
        build_dir = RepoPaths(find_repo_root()).build_dir
    build_dir = Path(build_dir)

    tech_lef, timing_lib, cell_dir = _corner_files(pdk, corner)
    build_dir.mkdir(parents=True, exist_ok=True)
    _link(tech_lef, build_dir / "sky130_fd_sc_hd.tlef")
    _link(timing_lib, build_dir / f"sky130_fd_sc_hd__{corner}.lib")

    synth_lib = build_dir / f"sky130_fd_sc_hd__{corner}_synth.lib"
    removed = filter_synth_liberty(timing_lib, synth_lib)

    cell_lefs = sorted(path for path in cell_dir.glob("*/*.lef") if not path.name.endswith(".magic.lef"))
    assert cell_lefs, f"{cell_dir} 下没有单元 LEF"

    blocks: list[str] = []
    patched_diode = False
    for path in cell_lefs:
        name, block = macro_block(path)
        if name == DIODE:
            block = block.replace("  CLASS CORE ;", "  CLASS CORE ANTENNACELL ;", 1)
            block = block.replace(
                "  PIN DIODE\n    ANTENNAGATEAREA",
                "  PIN DIODE\n    ANTENNADIFFAREA  0.434700 ;\n    ANTENNAGATEAREA",
                1,
            )
            patched_diode = "ANTENNADIFFAREA" in block
        blocks.append(block)

    assert patched_diode, f"合并后的 LEF 里没有找到天线单元 {DIODE}"

    merged = build_dir / "sky130_fd_sc_hd_cells.lef"
    with merged.open("w") as stream:
        stream.write('VERSION 5.7 ;\nBUSBITCHARS "[]" ;\nDIVIDERCHAR "/" ;\n\n')
        stream.write("\n".join(blocks))
        stream.write("\nEND LIBRARY\n")

    info = PreparedPdk(
        pdk=pdk,
        corner=corner,
        tech_lef=tech_lef,
        timing_lib=timing_lib,
        synth_lib=synth_lib,
        cells_lef=merged,
        removed_cells=removed,
        merged_macros=len(blocks),
    )
    print(f"[prepare_pdk] PDK      : {info.pdk}", flush=True)
    print(f"[prepare_pdk] corner   : {info.corner}", flush=True)
    print(f"[prepare_pdk] 技术 LEF : {info.tech_lef}", flush=True)
    print(f"[prepare_pdk] 时序库   : {info.timing_lib}", flush=True)
    print(f"[prepare_pdk] 综合库   : 去掉 {info.removed_cells} 个 lpflow_* 单元 -> {info.synth_lib}", flush=True)
    print(f"[prepare_pdk] 单元 LEF : {info.merged_macros} 个 MACRO -> {info.cells_lef}", flush=True)
    return info


def prepare_many(
    corners: Iterable[str],
    pdk: Path | None = None,
    build_dir: Path | None = None,
) -> list[PreparedPdk]:
    """按给定顺序准备多个 corner；重复项只准备一次。"""
    return [prepare(corner=corner, pdk=pdk, build_dir=build_dir) for corner in dict.fromkeys(corners)]


def default_corners() -> list[str]:
    """默认要准备的 corner 列表：``DPM_CORNER``（空格或逗号分隔）或全部 :data:`CORNERS`。"""
    raw = os.environ.get("DPM_CORNER", "").strip()
    if raw:
        return [item for item in raw.replace(",", " ").split() if item]
    return list(CORNERS)


def build_parser() -> argparse.ArgumentParser:
    corners = default_corners()
    pdk = Path(os.environ.get("DPM_PDK", "").strip() or DEFAULT_PDK).expanduser()
    parser = argparse.ArgumentParser(
        prog="dpm-prepare-pdk",
        description="准备 sky130 HD ASIC 流程使用的 PDK 派生文件",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--corner",
        dest="corners",
        nargs="+",
        metavar="CORNER",
        default=corners,
        help="要准备的 timing liberty corner，可给多个（DPM_CORNER 可覆盖，空格或逗号分隔）",
    )
    parser.add_argument("--pdk", type=Path, default=pdk, help="skywater-pdk 根目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prepare_many(args.corners, pdk=args.pdk)
    return 0
