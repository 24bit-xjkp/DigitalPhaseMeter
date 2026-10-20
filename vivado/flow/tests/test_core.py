"""核心模块的纯 Python 单元测试（不启动 Vivado）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from dpm_vivado_flow import hashing, tclgen
from dpm_vivado_flow.paths import BD_NAME, PART, FlowError, RepoPaths, find_repo_root
from dpm_vivado_flow.plan import parse_result_file
from dpm_vivado_flow.stages.build import SYNTH_MAIN, strategy_rows
from dpm_vivado_flow.stamps import SCHEMA, load_json, matches, save_json


def test_sha256_files_is_order_independent(tmp_path: Path) -> None:
    first = tmp_path / "a.sv"
    second = tmp_path / "b.sv"
    first.write_text("module a; endmodule\n", encoding="utf-8")
    second.write_text("module b; endmodule\n", encoding="utf-8")
    assert hashing.sha256_files([first, second]) == hashing.sha256_files([second, first])
    second.write_text("module b2; endmodule\n", encoding="utf-8")
    assert hashing.sha256_files([first, second]) != hashing.sha256_files([first])


def test_sha256_dir_detects_nested_changes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    file = tmp_path / "src" / "x.v"
    file.write_text("a", encoding="utf-8")
    before = hashing.sha256_dir(tmp_path)
    file.write_text("b", encoding="utf-8")
    assert hashing.sha256_dir(tmp_path) != before


def test_compose_is_stable() -> None:
    assert hashing.compose("a", 1, ["b", 2]) == hashing.compose("a", 1, ["b", 2])
    assert hashing.compose("a", 1) != hashing.compose("a", 2)


def test_tcl_word_and_plan(tmp_path: Path) -> None:
    assert tclgen.tcl_word("plain/path") == "plain/path"
    assert tclgen.tcl_word("a b") == "{a b}"
    assert tclgen.tcl_word("a$b") == "{a$b}"
    with pytest.raises(FlowError):
        tclgen.tcl_word("bad{brace")
    plan = {
        "stage": "build",
        "reset_runs": ["synth_1", "x_synth_1"],
        "strategy_targets": [["synth_1", "P", "V"], ["impl_1", "strategy", "S"]],
        "empty": "",
    }
    path = tmp_path / "plan.tcl"
    tclgen.write_plan(path, plan)
    text = path.read_text(encoding="utf-8")
    assert "set dpm_plan(stage) build" in text
    assert "set dpm_plan(reset_runs) {synth_1 x_synth_1}" in text
    assert "{{synth_1 P V} {impl_1 strategy S}}" in text
    assert "set dpm_plan(empty) {}" in text


def test_plan_is_sourceable_by_tclsh(tmp_path: Path) -> None:
    tclsh = shutil.which("tclsh")
    if not tclsh:
        pytest.skip("tclsh 不可用")
    plan = {
        "stage": "build",
        "reset_runs": ["synth_1", "x_synth_1"],
        "strategy_targets": [["synth_1", "P", "V"], ["impl_1", "strategy", "S"]],
        "incremental_synth": [["x_synth_1", "/tmp/ref a.dcp"]],
        "empty": [],
    }
    path = tmp_path / "plan.tcl"
    tclgen.write_plan(path, plan)
    script = tmp_path / "check.tcl"
    script.write_text(
        f"source {{{path}}}\n"
        'puts "STAGE=$dpm_plan(stage)"\n'
        'puts "RESET=[llength $dpm_plan(reset_runs)]"\n'
        'puts "TARGETS=[llength $dpm_plan(strategy_targets)]"\n'
        'puts "INCR=[lindex $dpm_plan(incremental_synth) 0 1]"\n'
        'puts "EMPTY=[llength $dpm_plan(empty)]"\n',
        encoding="utf-8",
    )
    proc = subprocess.run([tclsh, str(script)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "STAGE=build" in proc.stdout
    assert "RESET=2" in proc.stdout
    assert "TARGETS=2" in proc.stdout
    assert "INCR=/tmp/ref a.dcp" in proc.stdout
    assert "EMPTY=0" in proc.stdout


def test_wrapper_has_error_exit(tmp_path: Path) -> None:
    path = tmp_path / "wrapper.tcl"
    tclgen.write_wrapper(path, tmp_path / "worker.tcl", tmp_path / "plan.tcl")
    text = path.read_text(encoding="utf-8")
    assert "catch {source" in text
    assert "exit 1" in text
    assert "set dpm_plan_file" in text


def test_stamp_roundtrip_and_match(tmp_path: Path) -> None:
    path = tmp_path / "stamp.json"
    data = {"schema": SCHEMA, "inputs": "abc", "vivado": "2025.2", "part": PART}
    save_json(path, data)
    loaded = load_json(path)
    assert loaded == data
    assert matches(loaded, {"inputs": "abc", "vivado": "2025.2", "part": PART}, ["inputs", "vivado", "part"])
    assert not matches(loaded, {"inputs": "def", "vivado": "2025.2", "part": PART}, ["inputs", "vivado", "part"])
    assert load_json(tmp_path / "missing.json") is None


def test_parse_result_file(tmp_path: Path) -> None:
    path = tmp_path / "result.txt"
    path.write_text("STAGE=build\nWNS=0.271\nEMPTY=\n", encoding="utf-8")
    assert parse_result_file(path) == {"STAGE": "build", "WNS": "0.271", "EMPTY": ""}
    assert parse_result_file(tmp_path / "none.txt") == {}


def test_repo_paths_and_root(tmp_path: Path) -> None:
    (tmp_path / "xmake.lua").write_text("", encoding="utf-8")
    (tmp_path / "vivado").mkdir()
    (tmp_path / "vivado" / "package_ip.tcl").write_text("", encoding="utf-8")
    assert find_repo_root(tmp_path) == tmp_path.resolve()
    paths = RepoPaths(tmp_path)
    assert paths.proj_file.name == "dpm_bd.xpr"
    assert paths.plan_file("build").parent.name == "plans"
    assert paths.synth_run_dcp("synth_1").name == "dpm_system_wrapper.dcp"
    assert paths.synth_run_dcp(f"{BD_NAME}_dpm_top_0_0_synth_1").name == f"{BD_NAME}_dpm_top_0_0.dcp"
    assert paths.impl_routed_dcp.name == "dpm_system_wrapper_routed.dcp"


def test_strategy_rows_include_synth_impl_and_ip() -> None:
    rows = strategy_rows(0, 2)
    runs = {row[0] for row in rows}
    assert SYNTH_MAIN in runs
    assert f"{BD_NAME}_dpm_top_0_0_synth_1" in runs
    assert "impl_1" in runs
    assert ["impl_1", "strategy", "Performance_Retiming"] in rows
