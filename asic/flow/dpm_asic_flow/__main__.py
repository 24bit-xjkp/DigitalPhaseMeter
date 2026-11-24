"""模块入口：`python -m dpm_asic_flow`。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
