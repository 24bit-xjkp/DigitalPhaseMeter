"""pytest 的路径兜底：把 `host/` 加进 `sys.path`，这样从仓库根目录
`python3 -m pytest host/tests -q` 也能 `import dpm_host`（本机没有 pytest，
本文件对 `python3 -m unittest` 无影响）。
"""

from __future__ import annotations

import os
import sys

_HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HOST_DIR not in sys.path:
    sys.path.insert(0, _HOST_DIR)
