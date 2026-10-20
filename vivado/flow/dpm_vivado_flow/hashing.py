"""内容哈希：只用 Python 标准库，保证跨机器/跨时间稳定。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from .paths import FlowError


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise FlowError(f"找不到文件：{path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_files(paths: Iterable[Path]) -> str:
    """对一组文件取哈希：包含规范化路径与内容，顺序无关。"""

    lines: list[str] = []
    for raw in paths:
        path = Path(raw).resolve()
        lines.append(f"{path}\t{sha256_file(path)}")
    lines.sort()
    return sha256_text("\n".join(lines))


def sha256_dir(directory: Path) -> str:
    """递归目录哈希：相对路径 + 内容，排序后组合。"""

    directory = Path(directory)
    if not directory.is_dir():
        raise FlowError(f"找不到目录：{directory}")
    lines: list[str] = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        rel = path.relative_to(directory).as_posix()
        lines.append(f"{rel}\t{sha256_file(path)}")
    return sha256_text("\n".join(lines))


def compose(*parts: object) -> str:
    """把若干可 JSON 序列化的片段组合成稳定哈希。"""

    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return sha256_text(payload)


def read_filelist(rtl_dir: Path) -> list[Path]:
    """读取 firtool 的 filelist.f，返回绝对文件列表（顺序即编译顺序）。"""

    rtl_dir = Path(rtl_dir)
    filelist = rtl_dir / "filelist.f"
    if not filelist.is_file():
        raise FlowError(f"找不到 {filelist}：请先运行 sbtn 生成对应的 SystemVerilog")
    files: list[Path] = []
    for line in filelist.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if item:
            files.append((rtl_dir / item).resolve())
    return files
