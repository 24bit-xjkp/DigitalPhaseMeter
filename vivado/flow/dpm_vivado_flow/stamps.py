"""JSON stamp/指纹的读写与匹配。"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .paths import PART, FlowError, RepoPaths

SCHEMA = 1


def load_json(path: Path) -> dict[str, Any] | None:
    """读取 JSON；缺失/损坏/非对象一律返回 None（视为不匹配）。"""

    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return data if isinstance(data, dict) else None


def save_json(path: Path, data: dict[str, Any]) -> None:
    """原子写 JSON（临时文件 + os.replace）。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def matches(stamp: dict[str, Any] | None, expected: dict[str, Any], keys: Iterable[str]) -> bool:
    """stamp 与 expected 在给定 key 上完全一致，且 schema 匹配。"""

    if not stamp:
        return False
    if stamp.get("schema") != SCHEMA:
        return False
    for key in keys:
        if stamp.get(key) != expected.get(key):
            return False
    return True


def load_state(paths: RepoPaths) -> dict[str, Any]:
    data = load_json(paths.state_file)
    if not data or data.get("schema") != SCHEMA:
        return {"schema": SCHEMA, "vivado": "", "part": PART}
    return data


def save_state(paths: RepoPaths, state: dict[str, Any]) -> None:
    state = dict(state)
    state["schema"] = SCHEMA
    state["part"] = PART
    save_json(paths.state_file, state)


def load_runs(paths: RepoPaths) -> dict[str, Any]:
    data = load_json(paths.runs_file)
    if not data or data.get("schema") != SCHEMA:
        return {"schema": SCHEMA, "vivado": "", "part": PART, "runs": {}}
    data.setdefault("runs", {})
    return data


def save_runs(paths: RepoPaths, data: dict[str, Any]) -> None:
    data = dict(data)
    data["schema"] = SCHEMA
    data["part"] = PART
    save_json(paths.runs_file, data)


def clear_runs(paths: RepoPaths) -> None:
    try:
        paths.runs_file.unlink()
    except FileNotFoundError:
        pass


def require_stamp(path: Path, what: str) -> dict[str, Any]:
    data = load_json(path)
    if not data or data.get("schema") != SCHEMA:
        raise FlowError(f"缺少有效的 {what}（{path}）；请先运行前置阶段或使用 --force")
    return data
