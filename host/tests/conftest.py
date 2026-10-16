"""pytest 的路径兜底：把 `host/` 加进 `sys.path`。

仓库根的 `pyproject.toml` 已经配置了 `pythonpath = ["host"]`（相对 rootdir 解析），
所以「仓库根目录 `uv run pytest`」与「`host/` 目录 `uv run pytest`」两种调用都能
`import dpm_host`。本文件保留一段等价的兜底逻辑，用于 rootdir 不是仓库根目录的
非标准调用（例如直接 `pytest host/tests/test_protocol.py`）。
"""

from __future__ import annotations

import os
import sys

_HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HOST_DIR not in sys.path:
    sys.path.insert(0, _HOST_DIR)
