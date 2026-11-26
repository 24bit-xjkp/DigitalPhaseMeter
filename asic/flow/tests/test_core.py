"""`dpm-asic-flow` / `dpm-prepare-pdk` 的纯 Python 单元测试（不启动 Yosys/OpenROAD）。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest
from dpm_asic_flow import prepare_pdk
from dpm_asic_flow.cli import apply_options, build_parser, main, read_dsp_freq_mhz, write_synth_filelist
from dpm_asic_flow.paths import CORNERS, RepoPaths, find_repo_root

CORNER = "tt_025C_1v80"

# 所有会改变默认值的环境变量；解析器默认值会读取它们，因此断言默认值前先清空。
FLOW_ENV_KEYS = (
    "DPM_PDK",
    "DPM_UTILIZATION",
    "DPM_PLACE_DENSITY",
    "DPM_SDC",
    "DPM_RETIME",
    "DPM_TIMING_DRIVEN_PLACE",
    "DPM_TIMING_DRIVEN_REPAIR",
    "DPM_VIRTUAL_CTS",
    "DPM_VIRTUAL_CTS_MAX_SKEW",
    "DPM_GRT_CRITICAL_NETS",
    "DPM_USEFUL_SKEW",
    "DPM_REPAIR_SETUP_MARGIN",
    "DPM_GATE_CLONING",
    "DPM_PLACE_RESYNTH_ITERS",
    "DPM_PLACE_RESYNTH_MODE",
    "DPM_RESYNTH_ITERS",
    "DPM_RESYNTH_INITIAL_OPS",
    "DPM_RESYNTH_REVERT_AFTER",
    "DPM_RESYNTH_SEED",
    "DPM_RESYNTH_TEMP",
    "DPM_RESYNTH_POPULATION_SIZE",
    "DPM_THREADS",
    "DPM_DSP_FREQ_MHZ",
    "DPM_CORNER",
)


def clear_flow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in FLOW_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


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
    with pytest.raises(AssertionError, match="不像本仓库"):
        find_repo_root(not_repo)


def test_repo_paths_layout(tmp_path: Path) -> None:
    paths = RepoPaths(tmp_path)
    assert paths.build_dir == tmp_path / "build" / "asic"
    assert paths.synth_dir == paths.build_dir / "synth"
    assert paths.openroad_dir == paths.build_dir / "openroad"
    assert paths.final_netlist == paths.openroad_dir / "final.v"
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
    with pytest.raises(AssertionError, match="MACRO 块"):
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
    with pytest.raises(AssertionError, match="PDK 文件不存在"):
        prepare_pdk.prepare(corner=CORNER, pdk=tmp_path / "missing", build_dir=tmp_path / "build")


def test_parser_defaults_and_stage_choices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clear_flow_env(monkeypatch)
    parser = build_parser()
    assert parser.formatter_class is argparse.ArgumentDefaultsHelpFormatter

    args = parser.parse_args([])
    assert args.stage == "all"
    assert args.repo_root is None
    assert args.pdk == Path.home() / "skywater-pdk"
    assert args.utilization == 60
    assert args.place_density == 0.60
    assert args.sdc == Path("asic/synth/dpm_asic.sdc")
    assert args.retime is False
    assert args.timing_driven_place is False
    assert args.timing_driven_repair is False
    assert args.virtual_cts is True
    assert args.virtual_cts_max_skew_fraction == 0.05
    assert args.grt_critical_nets == 20
    assert args.useful_skew is False
    assert args.repair_setup_margin == 1.0
    assert args.gate_cloning is True
    assert args.place_resynth_iters == 2
    assert args.place_resynth_mode == "anneal"
    assert args.resynth_iters == 200
    assert args.resynth_initial_ops == 3
    assert args.resynth_revert_after == 50
    assert args.resynth_seed == 1
    assert args.resynth_temp is None
    assert args.resynth_population_size == 8
    assert args.threads == 0

    args = parser.parse_args(["route-opt", "--pdk", str(tmp_path), "--utilization", "55"])
    assert args.stage == "route-opt"
    assert args.pdk == tmp_path
    assert args.utilization == 55

    args = parser.parse_args(
        [
            "--retime",
            "--timing-driven-place",
            "--timing-driven-repair",
            "--virtual-cts",
            "--virtual-cts-max-skew-fraction",
            "0.1",
            "--grt-critical-nets",
            "5",
            "--useful-skew",
            "--repair-setup-margin",
            "0.1",
            "--gate-cloning",
            "--place-resynth-iters",
            "3",
            "--place-resynth-mode",
            "genetic",
            "--resynth-iters",
            "30",
            "--resynth-initial-ops",
            "5",
            "--resynth-revert-after",
            "10",
            "--resynth-seed",
            "3",
            "--resynth-temp",
            "1.5",
            "--resynth-population-size",
            "12",
            "--threads",
            "4",
        ]
    )
    assert args.retime is True
    assert args.timing_driven_place is True
    assert args.timing_driven_repair is True
    assert args.virtual_cts is True
    assert args.virtual_cts_max_skew_fraction == 0.1
    assert args.grt_critical_nets == 5
    assert args.useful_skew is True
    assert args.repair_setup_margin == 0.1
    assert args.gate_cloning is True
    assert args.place_resynth_iters == 3
    assert args.place_resynth_mode == "genetic"
    assert args.resynth_iters == 30
    assert args.resynth_initial_ops == 5
    assert args.resynth_revert_after == 10
    assert args.resynth_seed == 3
    assert args.resynth_temp == 1.5
    assert args.resynth_population_size == 12
    assert args.threads == 4

    # BooleanOptionalAction 的 --no-* 形式
    args = parser.parse_args(
        [
            "--no-retime",
            "--no-timing-driven-place",
            "--no-timing-driven-repair",
            "--no-virtual-cts",
            "--no-useful-skew",
            "--no-gate-cloning",
        ]
    )
    assert args.retime is False
    assert args.timing_driven_place is False
    assert args.timing_driven_repair is False
    assert args.virtual_cts is False
    assert args.useful_skew is False
    assert args.gate_cloning is False

    with pytest.raises(SystemExit):
        parser.parse_args(["--allow-gate-cloning"])
    with pytest.raises(SystemExit):
        parser.parse_args(["unknown-stage"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--place-resynth-mode", "unknown"])

    assert CORNERS == ("ss_100C_1v60", "tt_025C_1v80", "ff_100C_1v95")


def test_help_uses_argument_defaults_formatter(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_flow_env(monkeypatch)
    monkeypatch.setenv("COLUMNS", "200")
    text = build_parser().format_help()

    assert "(default: 0.05)" in text
    assert "(default: 2)" in text
    assert "(default: True)" in text
    assert "--gate-cloning" in text
    assert "--no-gate-cloning" in text
    assert "--allow-gate-cloning" not in text
    assert "默认开启" not in text
    assert "默认关闭" not in text


def test_dsp_freq_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DPM_DSP_FREQ_MHZ", raising=False)
    assert read_dsp_freq_mhz() == 450.0

    monkeypatch.setenv("DPM_DSP_FREQ_MHZ", " 450 ")
    assert read_dsp_freq_mhz() == 450.0

    for bad in ("0", "-1", "abc", "nan", "inf"):
        monkeypatch.setenv("DPM_DSP_FREQ_MHZ", bad)
        with pytest.raises(AssertionError):
            read_dsp_freq_mhz()


def test_apply_options_exports_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_flow_env(monkeypatch)
    pdk, freq = apply_options(build_parser().parse_args([]), RepoPaths(tmp_path))

    assert freq == 450.0
    assert pdk == (Path.home() / "skywater-pdk").resolve()
    assert os.environ["DPM_PDK"] == str(pdk)
    assert os.environ["DPM_DSP_FREQ_MHZ"] == "450"
    assert os.environ["DPM_UTILIZATION"] == "60"
    assert os.environ["DPM_PLACE_DENSITY"] == "0.6"
    assert os.environ["DPM_SDC"] == str((tmp_path / "asic/synth/dpm_asic.sdc").resolve())
    assert os.environ["DPM_RETIME"] == "0"
    assert os.environ["DPM_PLACE_RESYNTH_ITERS"] == "2"
    assert os.environ["DPM_VIRTUAL_CTS"] == "1"
    assert os.environ["DPM_VIRTUAL_CTS_MAX_SKEW"] == "0.05"
    assert os.environ["DPM_GRT_CRITICAL_NETS"] == "20"
    assert os.environ["DPM_REPAIR_SETUP_MARGIN"] == "1.0"
    assert os.environ["DPM_GATE_CLONING"] == "1"
    assert os.environ["DPM_RESYNTH_ITERS"] == "200"
    assert os.environ["DPM_RESYNTH_REVERT_AFTER"] == "50"
    # virtual CTS / 迭代 resynth 隐含 timing-driven 布局
    assert os.environ["DPM_TIMING_DRIVEN_PLACE"] == "1"
    assert "DPM_THREADS" not in os.environ
    assert "DPM_RESYNTH_TEMP" not in os.environ


def test_apply_options_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 显式 flag > 环境变量 > 内置默认值
    clear_flow_env(monkeypatch)
    paths = RepoPaths(tmp_path)
    monkeypatch.setenv("DPM_VIRTUAL_CTS", "0")
    apply_options(build_parser().parse_args([]), paths)
    assert os.environ["DPM_VIRTUAL_CTS"] == "0"
    apply_options(build_parser().parse_args(["--virtual-cts"]), paths)
    assert os.environ["DPM_VIRTUAL_CTS"] == "1"
    apply_options(build_parser().parse_args(["--no-virtual-cts"]), paths)
    assert os.environ["DPM_VIRTUAL_CTS"] == "0"

    monkeypatch.setenv("DPM_GATE_CLONING", "0")
    apply_options(build_parser().parse_args([]), paths)
    assert os.environ["DPM_GATE_CLONING"] == "0"
    apply_options(build_parser().parse_args(["--gate-cloning"]), paths)
    assert os.environ["DPM_GATE_CLONING"] == "1"
    apply_options(build_parser().parse_args(["--no-gate-cloning"]), paths)
    assert os.environ["DPM_GATE_CLONING"] == "0"

    monkeypatch.setenv("DPM_REPAIR_SETUP_MARGIN", "0.5")
    apply_options(build_parser().parse_args(["--repair-setup-margin", "0.2"]), paths)
    assert os.environ["DPM_REPAIR_SETUP_MARGIN"] == "0.2"


def test_apply_options_asserts_invalid_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_flow_env(monkeypatch)
    paths = RepoPaths(tmp_path)
    for argv in (
        ["--virtual-cts-max-skew-fraction", "2"],
        ["--grt-critical-nets", "101"],
        ["--repair-setup-margin", "-1"],
        ["--place-resynth-iters", "-1"],
        ["--resynth-iters", "0"],
        ["--resynth-initial-ops", "0"],
        ["--resynth-revert-after", "-1"],
        ["--resynth-population-size", "0"],
        ["--resynth-temp", "0"],
        ["--threads", "-1"],
    ):
        with pytest.raises(AssertionError):
            apply_options(build_parser().parse_args(argv), paths)


def test_write_synth_filelist_uses_paths_relative_to_the_filelist(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    paths = RepoPaths(root)
    rtl = paths.rtl_dir
    rtl.mkdir(parents=True)
    (rtl / "filelist.f").write_text("A.sv\nverification/assert/B.sv\nsub/C.sv\n", encoding="utf-8")
    paths.synth_dir.mkdir(parents=True)

    text = write_synth_filelist(paths).read_text(encoding="utf-8")

    assert text.splitlines() == [
        "../../chisel/DigitalPhaseMeterTopA/A.sv",
        "../../chisel/DigitalPhaseMeterTopA/sub/C.sv",
    ]


def test_prepare_pdk_parser_accepts_corner_list(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_flow_env(monkeypatch)
    parser = prepare_pdk.build_parser()
    assert parser.formatter_class is argparse.ArgumentDefaultsHelpFormatter
    assert parser.parse_args([]).corners == list(CORNERS)
    assert parser.parse_args([]).pdk == Path.home() / "skywater-pdk"
    args = parser.parse_args(["--corner", CORNERS[0], CORNERS[2]])
    assert args.corners == [CORNERS[0], CORNERS[2]]


def test_default_corners_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DPM_CORNER", "ff_100C_1v95, ss_100C_1v60")
    assert prepare_pdk.default_corners() == ["ff_100C_1v95", "ss_100C_1v60"]
    assert prepare_pdk.build_parser().parse_args([]).corners == ["ff_100C_1v95", "ss_100C_1v60"]

    monkeypatch.delenv("DPM_CORNER")
    assert prepare_pdk.default_corners() == list(CORNERS)


def test_main_rejects_bad_repo_root(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="不像本仓库"):
        main(["--repo-root", str(tmp_path)])
