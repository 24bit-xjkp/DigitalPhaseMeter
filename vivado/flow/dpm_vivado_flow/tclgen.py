"""生成 Tcl 计划文件与包装脚本。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .paths import FlowError

_SAFE_WORD = re.compile(r"^[A-Za-z0-9_./:+=,@%^*?~-]+$")


def tcl_word(value: object) -> str:
    """把 Python 值转成 Tcl 单字。

    - 简单值原样输出；含空格的路径/字符串用 `{}` 包住；
    - 值内部不允许出现未处理的 `{}`、反斜杠或换行（本项目的计划值都是受控的
      路径/属性名/属性值，不需要更复杂的转义）。
    """

    text = str(value)
    if text == "":
        return "{}"
    if _SAFE_WORD.match(text):
        return text
    if any(ch in text for ch in "{}\\\n\r"):
        raise FlowError(f"Tcl 计划值包含不支持的字符（花括号/反斜杠/换行）：{text!r}")
    return "{" + text + "}"


def tcl_list(values: Sequence[object]) -> str:
    if not values:
        return "{}"
    return " ".join(tcl_word(value) for value in values)


def tcl_nested_list(rows: Sequence[Sequence[object]]) -> str:
    """Tcl list of lists：每个子列表用一个 `{...}` 元素表示。"""

    if not rows:
        return "{}"
    return " ".join("{" + tcl_list(list(row)) + "}" for row in rows)


def _is_scalar(value: object) -> bool:
    return isinstance(value, (str, int, bool, float))


def _tcl_value(value: Any) -> str:
    """把一个计划值转成**单个 Tcl word**。

    `set dpm_plan(key) <value>` 的 value 只能是单个 word；list 值必须在外层再包一层
    `{}`（内层元素自己已经按 Tcl list 规则转义），否则 source 时 `set` 会收到多个参数。
    """

    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, str)):
        return tcl_word(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "{}"
        if all(_is_scalar(item) for item in value):
            return "{" + tcl_list(value) + "}"
        if all(isinstance(item, (list, tuple)) for item in value):
            return "{" + tcl_nested_list(value) + "}"  # type: ignore[arg-type]
        raise FlowError(f"不支持的 Tcl 计划列表混合类型：{value!r}")
    if value is None:
        return "{}"
    raise FlowError(f"不支持的 Tcl 计划值类型：{type(value).__name__}")


def write_plan(path: Path, plan: dict[str, Any]) -> Path:
    """写 `set dpm_plan(<key>) <value>` 形式的 Tcl 计划文件。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 由 dpm-vivado-flow 生成，请勿手工编辑。"]
    for key in sorted(plan):
        lines.append(f"set dpm_plan({key}) {_tcl_value(plan[key])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_wrapper(path: Path, worker: Path, plan_file: Path) -> Path:
    """生成 build/refresh worker 的包装脚本。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"""# 由 dpm-vivado-flow 生成，请勿手工编辑。
set dpm_plan_file {tcl_word(plan_file)}
if {{[catch {{source {tcl_word(worker)}}} msg opts]}} {{
    puts "DPM_STAGE_ERROR: $msg"
    catch {{puts [dict get $opts -errorinfo]}}
    exit 1
}}
exit 0
"""
    path.write_text(text, encoding="utf-8")
    return path


def write_source_wrapper(path: Path, script: Path, argv: Sequence[object] | None = None) -> Path:
    """生成「source 一个既有 Tcl 脚本」的包装（打包 / 全量建 BD）。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # `set argv` 同样只能接收一个 word；用 list 的外层花括号包住（空 list 即 `{}`）。
    argv_text = _tcl_value(list(argv or []))
    text = f"""# 由 dpm-vivado-flow 生成，请勿手工编辑。
set argv {argv_text}
if {{[catch {{source {tcl_word(script)}}} msg opts]}} {{
    puts "DPM_STAGE_ERROR: $msg"
    catch {{puts [dict get $opts -errorinfo]}}
    exit 1
}}
catch {{close_project}}
exit 0
"""
    path.write_text(text, encoding="utf-8")
    return path
