"""自研 Tcl 执行后端：生成脚本，调用 `vivado -mode batch -source`，解析结果与日志。"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .paths import FlowError, RepoPaths
from .plan import parse_result_file
from .tclgen import write_plan, write_wrapper

_VERSION_RE = re.compile(r"vivado\s+v([0-9][0-9A-Za-z.]*)", re.IGNORECASE)
_MESSAGE_RE = re.compile(r"^\s*(CRITICAL WARNING|WARNING|ERROR):")
_MAX_REPORTED = 5


@dataclass
class RunResult:
    returncode: int
    log_path: Path
    result_path: Path | None
    result: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    critical_warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.critical_warnings and not self.errors and not self.warnings


class VivadoRunner:
    """唯一后端：所有 Vivado 交互都通过 batch 脚本完成。"""

    def __init__(self, paths: RepoPaths, *, vivado: str | None = None, verbose: bool = False):
        self.paths = paths
        self.vivado = vivado or os.environ.get("DPM_VIVADO") or "vivado"
        self.verbose = verbose

    # ---- 工具版本 ---------------------------------------------------------
    def query_version(self) -> str:
        try:
            proc = subprocess.run(
                [self.vivado, "-version"],
                cwd=self.paths.root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except FileNotFoundError as exc:
            raise FlowError(
                f"找不到 Vivado 可执行文件 '{self.vivado}'：请把 Vivado 加入 PATH，" "或设置 DPM_VIVADO 指向 vivado 可执行文件"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise FlowError("vivado -version 超时") from exc
        match = _VERSION_RE.search(proc.stdout + proc.stderr)
        if not match:
            raise FlowError(f"无法识别 Vivado 版本：{proc.stdout!r} {proc.stderr!r}")
        return match.group(1)

    # ---- 单次脚本执行 -----------------------------------------------------
    def run_script(
        self,
        script: Path,
        log_path: Path,
        *,
        timeout: float | None = None,
    ) -> RunResult:
        script = Path(script)
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.vivado,
            "-mode",
            "batch",
            "-nojournal",
            "-notrace",
            "-log",
            str(log_path),
            "-source",
            str(script),
        ]
        stdout = None if self.verbose else subprocess.DEVNULL
        stderr = None if self.verbose else subprocess.STDOUT
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.paths.root,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise FlowError(
                f"找不到 Vivado 可执行文件 '{self.vivado}'：请把 Vivado 加入 PATH，" "或设置 DPM_VIVADO 指向 vivado 可执行文件"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise FlowError(f"Vivado 脚本超时（{timeout}s）：{script}") from exc

        result = self._inspect_log(log_path)
        result.returncode = proc.returncode
        return result

    # ---- 计划 + worker ----------------------------------------------------
    def run_stage(
        self,
        stage: str,
        worker: Path,
        plan: dict[str, object],
    ) -> RunResult:
        plan_file = write_plan(self.paths.plan_file(stage), plan)
        wrapper = write_wrapper(self.paths.wrapper_file(stage), worker, plan_file)
        result = self.run_script(wrapper, self.paths.log_file(stage))
        result_path = self.paths.result_file(stage)
        result.result_path = result_path
        result.result = parse_result_file(result_path)
        return result

    # ---- 校验 -------------------------------------------------------------
    def _check_messages(self, result: RunResult, stage: str) -> None:
        if result.returncode != 0:
            raise FlowError(f"{stage} 失败（退出码 {result.returncode}）：{result.log_path}\n" f"{self.tail(result.log_path)}")
        if result.errors or result.critical_warnings:
            raise FlowError(
                f"{stage} 日志出现 ERROR/CRITICAL WARNING：{result.log_path}\n"
                + "\n".join((result.errors + result.critical_warnings)[:_MAX_REPORTED])
            )
        if result.warnings:
            raise FlowError(f"{stage} 日志出现 WARNING：{result.log_path}\n" + "\n".join(result.warnings[:_MAX_REPORTED]))

    def check_script(self, result: RunResult, stage: str) -> None:
        """校验一次性 source 脚本（打包/全量建 BD），不要求结果文件。"""

        self._check_messages(result, stage)

    def check(self, result: RunResult, stage: str) -> None:
        """校验计划 worker：除退出码/日志外，必须有结果文件。"""

        self._check_messages(result, stage)
        if result.result_path is None or not Path(result.result_path).is_file():
            raise FlowError(f"{stage} 没有生成结果文件：{result.result_path}")

    def check_allow_warnings(self, result: RunResult, stage: str) -> None:
        """用于只读查询/报告阶段：允许 WARNING，但 ERROR/CRITICAL 仍失败。"""

        if result.returncode != 0 or result.errors or result.critical_warnings:
            self._check_messages(result, stage)

    # ---- 辅助 -------------------------------------------------------------
    @staticmethod
    def _inspect_log(log_path: Path) -> RunResult:
        result = RunResult(returncode=0, log_path=log_path, result_path=None)
        if not log_path.is_file():
            result.errors.append(f"日志文件不存在：{log_path}")
            return result
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = _MESSAGE_RE.match(line)
            if not match:
                continue
            kind = match.group(1)
            if kind == "ERROR":
                result.errors.append(line.strip())
            elif kind == "CRITICAL WARNING":
                result.critical_warnings.append(line.strip())
            else:
                result.warnings.append(line.strip())
        return result

    @staticmethod
    def tail(path: Path, lines: int = 40) -> str:
        path = Path(path)
        if not path.is_file():
            return f"（日志不存在：{path}）"
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(text[-lines:])
