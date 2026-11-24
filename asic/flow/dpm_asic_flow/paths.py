"""仓库路径、阶段顺序与固定文件名。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 默认工艺库根目录；可用 `--pdk` 或 `DPM_PDK` 覆盖。
DEFAULT_PDK = Path.home() / "skywater-pdk"

# 综合固定用 ss（最慢 corner）；OpenROAD 需要 ss/tt/ff 三个 corner 做 MCMM。
CORNERS = ("ss_100C_1v60", "tt_025C_1v80", "ff_100C_1v95")

# `all` 按顺序跑完其余阶段；其余每个值对应一次可单独执行的阶段。
STAGES = ("all", "synth", "floorplan", "place", "cts", "route-opt", "route", "gds")


class FlowError(RuntimeError):
    """流程可预期错误：打印后以非零码退出，不打印 traceback。"""


def _is_repo_root(candidate: Path) -> bool:
    return (candidate / "xmake.lua").is_file() and (candidate / "asic" / "synth" / "synth.ys").is_file()


def find_repo_root(explicit: Path | None = None) -> Path:
    """确定仓库根目录。

    优先级：显式参数 > 从当前目录向上找仓库标记 > 从包文件位置向上回溯
    （`asic/flow/dpm_asic_flow/paths.py` 向上即仓库根）。
    """

    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not _is_repo_root(root):
            raise FlowError(f"--repo-root 指向的目录不像本仓库：{root}")
        return root

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if _is_repo_root(candidate):
            return candidate

    for candidate in Path(__file__).resolve().parents:
        if _is_repo_root(candidate):
            return candidate

    raise FlowError("找不到仓库根目录：请在仓库内运行，或显式给出 --repo-root <仓库根>。")


@dataclass(frozen=True)
class RepoPaths:
    """从一个仓库根派生出的所有固定路径。"""

    root: Path

    @property
    def asic_dir(self) -> Path:
        return self.root / "asic"

    @property
    def flow_dir(self) -> Path:
        return self.asic_dir / "flow"

    @property
    def synth_script_dir(self) -> Path:
        return self.asic_dir / "synth"

    @property
    def openroad_script_dir(self) -> Path:
        return self.asic_dir / "openroad"

    @property
    def build_dir(self) -> Path:
        return self.root / "build" / "asic"

    @property
    def synth_dir(self) -> Path:
        return self.build_dir / "synth"

    @property
    def openroad_dir(self) -> Path:
        return self.build_dir / "openroad"

    @property
    def rtl_dir(self) -> Path:
        return self.root / "build" / "chisel" / "DigitalPhaseMeterTopA"
