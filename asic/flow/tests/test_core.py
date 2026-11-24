"""`dpm-asic-flow` / `dpm-prepare-pdk` 的纯 Python 单元测试（不启动 Yosys/OpenROAD）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from dpm_asic_flow import prepare_pdk
from dpm_asic_flow.cli import build_parser, main
from dpm_asic_flow.paths import CORNERS, FlowError, RepoPaths, find_repo_root

CORNER = "tt_025C_1v80"


def make_repo(tmp_path: Path) -> Path:
    """造一个带仓库标记的最小目录树。"""
    root = tmp_path / "repo"
    (root / "asic" / "synth").mkdir(parents=True)
    (root / "xmake.lua").write_text("", encoding="utf-8")
    (root / "asic" / "synth" / "synth.ys").write_text("", encoding="utf-8")
    return root


def make_pdk(tmp_path: Path) -> Path:
    """造一个够 prepare() 用的最小 sky130 HD 目录树。"""
    pdk = tmp_path / "skywater-pdk"
    lib = pdk / "libraries" / "sky130_fd_sc_hd" / "latest"
    (lib / "tech").mkdir(parents=True)
    (lib / "timing").mkdir()
    cells = lib / "cells"
    cells.mkdir()
    (lib / "tech" / "sky130_fd_sc_hd.tlef").write_text("VERSION 5.7 ;\nEND LIBRARY\n", encoding="utf-8")
    for corner in CORNERS:
        (lib / "timing" / f"sky130_fd_sc_hd__{corner}.lib").write_text(
            "library (sky130_fd_sc_hd) {\n"
            '  cell ("sky130_fd_sc_hd__inv_1") {\n'
            "    pin (A) { }\n"
            "  }\n"
            '  cell ("sky130_fd_sc_hd__lpflow_isobufsrc1_1") {\n'
            "    pin (A) { }\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
    (cells / "inv_1").mkdir()
    (cells / "inv_1" / "inv_1.lef").write_text(
        "MACRO sky130_fd_sc_hd__inv_1\n"
        "  CLASS CORE ;\n"
        "  SOURCE USER ;\n"
        "  PIN A\n"
        "    DIRECTION INPUT ;\n"
        "  END A\n"
        "END sky130_fd_sc_hd__inv_1\n",
        encoding="utf-8",
    )
    (cells / "diode").mkdir()
    (cells / "diode" / "diode.lef").write_text(
        f"MACRO {prepare_pdk.DIODE}\n"
        "  CLASS CORE ;\n"
        "  SOURCE USER ;\n"
        "  PIN DIODE\n"
        "    ANTENNAGATEAREA  0.4347 ;\n"
        "    DIRECTION INOUT ;\n"
        "  END DIODE\n"
        f"END {prepare_pdk.DIODE}\n",
        encoding="utf-8",
    )
    return pdk


def test_find_repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_repo(tmp_path)
    assert find_repo_root(root) == root.resolve()

    nested = root / "asic" / "synth"
    monkeypatch.chdir(nested)
    assert find_repo_root() == root.resolve()

    not_repo = tmp_path / "empty"
    not_repo.mkdir()
    with pytest.raises(FlowError):
        find_repo_root(not_repo)


def test_repo_paths_layout(tmp_path: Path) -> None:
    paths = RepoPaths(tmp_path)
    assert paths.build_dir == tmp_path / "build" / "asic"
    assert paths.synth_dir == paths.build_dir / "synth"
    assert paths.openroad_dir == paths.build_dir / "openroad"
    assert paths.rtl_dir == tmp_path / "build" / "chisel" / "DigitalPhaseMeterTopA"


def test_macro_block_drops_source_and_rejects_truncated(tmp_path: Path) -> None:
    lef = tmp_path / "a.lef"
    lef.write_text(
        "VERSION 5.7 ;\n" "MACRO sky130_fd_sc_hd__inv_1\n" "  CLASS CORE ;\n" "  SOURCE USER ;\n" "END sky130_fd_sc_hd__inv_1\n",
        encoding="utf-8",
    )
    name, block = prepare_pdk.macro_block(lef)
    assert name == "sky130_fd_sc_hd__inv_1"
    assert "SOURCE" not in block
    assert "CLASS CORE ;" in block

    lef.write_text("MACRO broken\n  CLASS CORE ;\n", encoding="utf-8")
    with pytest.raises(FlowError):
        prepare_pdk.macro_block(lef)


def test_prepare_builds_derived_files(tmp_path: Path) -> None:
    pdk = make_pdk(tmp_path)
    build = tmp_path / "build" / "asic"

    info = prepare_pdk.prepare(corner=CORNER, pdk=pdk, build_dir=build)

    assert info.pdk == pdk.resolve()
    assert info.corner == CORNER
    assert info.removed_cells == 1
    assert info.merged_macros == 2
    assert info.synth_lib.is_file()
    assert "lpflow_" not in info.synth_lib.read_text(encoding="utf-8")
    assert "sky130_fd_sc_hd__inv_1" in info.synth_lib.read_text(encoding="utf-8")

    tech_link = build / "sky130_fd_sc_hd.tlef"
    corner_link = build / f"sky130_fd_sc_hd__{CORNER}.lib"
    assert tech_link.is_symlink() and tech_link.resolve() == info.tech_lef
    assert corner_link.is_symlink() and corner_link.resolve() == info.timing_lib

    merged = info.cells_lef.read_text(encoding="utf-8")
    assert merged.startswith("VERSION 5.7 ;")
    assert "SOURCE" not in merged
    assert "CLASS CORE ANTENNACELL ;" in merged
    assert "ANTENNADIFFAREA  0.434700 ;" in merged
    assert merged.endswith("END LIBRARY\n")


def test_prepare_many_prepares_each_corner_once(tmp_path: Path) -> None:
    pdk = make_pdk(tmp_path)
    build = tmp_path / "build" / "asic"

    infos = prepare_pdk.prepare_many([CORNERS[2], CORNERS[0], CORNERS[2]], pdk=pdk, build_dir=build)

    assert [info.corner for info in infos] == [CORNERS[2], CORNERS[0]]
    for corner in (CORNERS[0], CORNERS[2]):
        assert (build / f"sky130_fd_sc_hd__{corner}.lib").is_symlink()


def test_prepare_reports_missing_pdk(tmp_path: Path) -> None:
    with pytest.raises(FlowError, match="PDK 文件不存在"):
        prepare_pdk.prepare(corner=CORNER, pdk=tmp_path / "missing", build_dir=tmp_path / "build")


def test_parser_defaults_and_stage_choices(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.stage == "all"
    assert args.pdk is None
    assert args.utilization is None

    args = parser.parse_args(["route-opt", "--pdk", str(tmp_path), "--utilization", "55"])
    assert args.stage == "route-opt"
    assert args.pdk == tmp_path
    assert args.utilization == 55

    with pytest.raises(SystemExit):
        parser.parse_args(["unknown-stage"])

    assert CORNERS == ("ss_100C_1v60", "tt_025C_1v80", "ff_100C_1v95")


def test_prepare_pdk_parser_accepts_corner_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DPM_CORNER", raising=False)
    parser = prepare_pdk.build_parser()
    assert parser.parse_args([]).corners == list(CORNERS)
    args = parser.parse_args(["--corner", CORNERS[0], CORNERS[2]])
    assert args.corners == [CORNERS[0], CORNERS[2]]


def test_default_corners_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DPM_CORNER", "ff_100C_1v95, ss_100C_1v60")
    assert prepare_pdk.default_corners() == ["ff_100C_1v95", "ss_100C_1v60"]
    assert prepare_pdk.build_parser().parse_args([]).corners == ["ff_100C_1v95", "ss_100C_1v60"]

    monkeypatch.delenv("DPM_CORNER")
    assert prepare_pdk.default_corners() == list(CORNERS)


def test_main_rejects_bad_repo_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--repo-root", str(tmp_path)]) == 1
    assert "不像本仓库" in capsys.readouterr().err
