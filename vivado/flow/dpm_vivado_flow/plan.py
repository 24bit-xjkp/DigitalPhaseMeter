"""阶段计划与结果的轻量数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StageStatus:
    """一个阶段的执行结果（用于打印与后续决策）。"""

    name: str
    reused: bool = False
    detail: str = ""
    data: dict[str, object] = field(default_factory=dict)


def parse_result_file(path: Path) -> dict[str, str]:
    """解析 `KEY=value` 结果文件；缺失返回空 dict。"""

    path = Path(path)
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result
