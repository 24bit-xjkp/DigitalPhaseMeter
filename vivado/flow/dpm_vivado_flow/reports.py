"""日志与报告门禁：Warnings/Critical/Errors、methodology、时序统计。"""

from __future__ import annotations

import re
from pathlib import Path

from .paths import FlowError, RepoPaths

_MESSAGE_RE = re.compile(r"^\s*(CRITICAL WARNING|WARNING|ERROR):")
_CHECKS_FOUND_RE = re.compile(r"Checks found:\s+([0-9]+)")
_CHECKS_WAIVED_RE = re.compile(r"Checks waived:\s+([0-9]+)")


def count_log_messages(path: Path) -> tuple[int, int, int]:
    """返回 (warnings, critical_warnings, errors)。"""

    path = Path(path)
    warnings = critical = errors = 0
    if not path.is_file():
        return 0, 0, 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _MESSAGE_RE.match(line)
        if not match:
            continue
        kind = match.group(1)
        if kind == "ERROR":
            errors += 1
        elif kind == "CRITICAL WARNING":
            critical += 1
        else:
            warnings += 1
    return warnings, critical, errors


def run_log_counts(paths: RepoPaths, run: str) -> tuple[int, int, int]:
    return count_log_messages(paths.run_dir(run) / "runme.log")


def check_run_logs(paths: RepoPaths, runs: list[str]) -> dict[str, tuple[int, int, int]]:
    """统计所有 run 的 runme.log；有 WARNING/CRITICAL/ERROR 即失败。"""

    result: dict[str, tuple[int, int, int]] = {}
    bad: list[str] = []
    for run in runs:
        counts = run_log_counts(paths, run)
        result[run] = counts
        if any(counts):
            bad.append(f"{run}: warnings={counts[0]} critical={counts[1]} errors={counts[2]}")
    if bad:
        raise FlowError("runme.log 存在未预期的告警/错误：\n" + "\n".join(bad))
    return result


def find_methodology_report(paths: RepoPaths) -> Path:
    candidates = sorted(paths.run_dir("impl_1").glob("*_methodology_drc_routed.rpt"))
    if not candidates:
        raise FlowError(f"找不到实现 methodology 报告：{paths.run_dir('impl_1')}/*_methodology_drc_routed.rpt")
    return candidates[0]


def check_methodology(paths: RepoPaths) -> tuple[int, int]:
    """返回 (checks found, checks waived)，并要求 found == 0 且无 Critical Warning。"""

    report = find_methodology_report(paths)
    text = report.read_text(encoding="utf-8", errors="replace")
    found_match = _CHECKS_FOUND_RE.search(text)
    if not found_match:
        raise FlowError(f"methodology 报告格式无法识别：{report}")
    found = int(found_match.group(1))
    waived_match = _CHECKS_WAIVED_RE.search(text)
    waived = int(waived_match.group(1)) if waived_match else 0
    if "Critical Warning" in text:
        raise FlowError(f"实现存在未 waive 的 methodology Critical Warning：{report}")
    if found != 0:
        raise FlowError(f"实现 methodology 仍有 {found} 条未 waive 的检查项：{report}")
    return found, waived


def report_timing_summary(paths: RepoPaths) -> Path:
    return paths.build_dir / "timing_summary.rpt"


def required_build_outputs(paths: RepoPaths) -> list[Path]:
    return [
        paths.xsa_file,
        paths.build_dir / "timing_summary.rpt",
        paths.build_dir / "utilization.rpt",
        paths.build_dir / "drc.rpt",
        paths.build_dir / "methodology.rpt",
    ]


def missing_build_outputs(paths: RepoPaths) -> list[Path]:
    return [path for path in required_build_outputs(paths) if not path.is_file()]
