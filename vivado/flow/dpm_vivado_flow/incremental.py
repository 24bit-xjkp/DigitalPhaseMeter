"""增量综合/实现的参考 DCP 管理。"""

from __future__ import annotations

import shutil
from pathlib import Path

from .paths import FlowError, RepoPaths

VALID_MODES = ("off", "on", "auto")


def parse_mode(value: str) -> str:
    if value not in VALID_MODES:
        raise FlowError(f"--incremental-* 只接受 off/on/auto（收到 {value!r}）")
    return value


def enabled(mode: str, has_reference: bool) -> bool:
    if mode == "off":
        return False
    if mode == "on":
        return True
    return has_reference


def copy_reference(paths: RepoPaths, run: str, dcp: Path) -> Path | None:
    """把上一轮 DCP 复制到 .flow/ref/，返回参考路径；不存在则返回 None。"""

    dcp = Path(dcp)
    if not dcp.is_file():
        return None
    target = paths.ref_dcp(run)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dcp, target)
    return target
