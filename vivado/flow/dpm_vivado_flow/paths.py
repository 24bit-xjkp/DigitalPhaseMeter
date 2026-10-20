"""仓库路径、器件与固定文件名。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 固定器件（ALINX AX7010 的芯片）；与三个打包/建 BD 脚本顶部写死的值保持一致。
PART = "xc7z010clg400-1"
BD_NAME = "dpm_system"
PROJECT_DIR_NAME = "bd_proj"
PROJECT_FILE_NAME = "dpm_bd.xpr"
TOP_NAME = "dpm_system_wrapper"
XSA_NAME = "dpm_system.xsa"

# 自研 IP 的 RTL 产物目录、打包脚本与 BD 实例名前缀。
# 实例名在 BD 里是 <bd>_<cell>_0；这里只写 cell 前缀，用于匹配 run 名。
CUSTOM_IPS: dict[str, dict[str, str]] = {
    "dpm_top": {
        "rtl": "DigitalPhaseMeterTop",
        "script": "package_ip.tcl",
        "ip_dir": "dpm_top",
        "cell": "dpm_top_0_0",
    },
    "dpm_src_drp": {
        "rtl": "MmcmDrpController",
        "script": "package_src_ip.tcl",
        "ip_dir": "dpm_src_drp",
        "cell": "dpm_src_drp_0_0",
    },
}


class FlowError(RuntimeError):
    """流程可预期错误：打印后以非零码退出，不打印 traceback。"""


def find_repo_root(explicit: Path | None = None) -> Path:
    """确定仓库根目录。

    优先级：显式参数 > 从当前目录向上找仓库标记 > 从包文件位置回溯
    （`vivado/flow/dpm_vivado_flow/paths.py` 向上三级即仓库根）。
    """

    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not (root / "vivado" / "package_ip.tcl").is_file():
            raise FlowError(f"--repo-root 指向的目录不像本仓库：{root}")
        return root

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "xmake.lua").is_file() and (candidate / "vivado" / "package_ip.tcl").is_file():
            return candidate

    package_root = Path(__file__).resolve().parents[3]
    if (package_root / "xmake.lua").is_file() and (package_root / "vivado" / "package_ip.tcl").is_file():
        return package_root

    raise FlowError("找不到仓库根目录：请在仓库内运行，或显式给出 --repo-root <仓库根>。")


@dataclass(frozen=True)
class RepoPaths:
    """从一个仓库根派生出的所有固定路径。"""

    root: Path

    @property
    def vivado_dir(self) -> Path:
        return self.root / "vivado"

    @property
    def flow_dir(self) -> Path:
        return self.vivado_dir / "flow"

    @property
    def flow_tcl_dir(self) -> Path:
        return self.flow_dir / "tcl"

    @property
    def chisel_dir(self) -> Path:
        return self.root / "build" / "chisel"

    @property
    def build_dir(self) -> Path:
        return self.root / "build" / "vivado"

    @property
    def flow_build_dir(self) -> Path:
        return self.build_dir / ".flow"

    @property
    def plans_dir(self) -> Path:
        return self.flow_build_dir / "plans"

    @property
    def scripts_dir(self) -> Path:
        return self.flow_build_dir / "scripts"

    @property
    def results_dir(self) -> Path:
        return self.flow_build_dir / "results"

    @property
    def logs_dir(self) -> Path:
        return self.flow_build_dir / "logs"

    @property
    def ref_dir(self) -> Path:
        return self.flow_build_dir / "ref"

    @property
    def ip_dir(self) -> Path:
        return self.build_dir / "ip"

    @property
    def proj_dir(self) -> Path:
        return self.build_dir / PROJECT_DIR_NAME

    @property
    def proj_file(self) -> Path:
        return self.proj_dir / PROJECT_FILE_NAME

    @property
    def runs_dir(self) -> Path:
        return self.proj_dir / "dpm_bd.runs"

    @property
    def xsa_file(self) -> Path:
        return self.build_dir / XSA_NAME

    @property
    def xdc_file(self) -> Path:
        return self.vivado_dir / "constraints" / "dpm_top.xdc"

    @property
    def suppress_early(self) -> Path:
        return self.vivado_dir / "suppress_early_messages.tcl"

    @property
    def hook_filter(self) -> Path:
        return self.vivado_dir / "hook_filter_messages.tcl"

    @property
    def hook_waivers(self) -> Path:
        return self.vivado_dir / "hook_methodology_waivers.tcl"

    @property
    def hook_vendor_ip(self) -> Path:
        return self.vivado_dir / "hook_filter_vendor_ip.tcl"

    @property
    def state_file(self) -> Path:
        return self.flow_build_dir / "state.json"

    @property
    def bd_stamp_file(self) -> Path:
        return self.flow_build_dir / "bd.json"

    @property
    def runs_file(self) -> Path:
        return self.flow_build_dir / "runs.json"

    def package_stamp_file(self, ip: str) -> Path:
        return self.flow_build_dir / f"package_{ip}.json"

    def result_file(self, stage: str) -> Path:
        return self.results_dir / f"{stage}.txt"

    def log_file(self, stage: str) -> Path:
        return self.logs_dir / f"{stage}.log"

    def plan_file(self, stage: str) -> Path:
        return self.plans_dir / f"{stage}.tcl"

    def wrapper_file(self, stage: str) -> Path:
        return self.scripts_dir / f"{stage}.tcl"

    def ref_dcp(self, run: str) -> Path:
        return self.ref_dir / f"{run}.dcp"

    def run_dir(self, run: str) -> Path:
        return self.runs_dir / run

    def synth_run_dcp(self, run: str) -> Path:
        """综合 run 的主 DCP 路径；约定见 CONTRACT.md。"""

        if run == "synth_1":
            return self.run_dir(run) / f"{TOP_NAME}.dcp"
        if run.endswith("_synth_1"):
            return self.run_dir(run) / f"{run[: -len('_synth_1')]}.dcp"
        raise FlowError(f"不是综合 run：{run}")

    @property
    def impl_routed_dcp(self) -> Path:
        return self.run_dir("impl_1") / f"{TOP_NAME}_routed.dcp"

    @property
    def impl_bit(self) -> Path:
        return self.run_dir("impl_1") / f"{TOP_NAME}.bit"

    def ensure_flow_dirs(self) -> None:
        for directory in (
            self.build_dir,
            self.flow_build_dir,
            self.plans_dir,
            self.scripts_dir,
            self.results_dir,
            self.logs_dir,
            self.ref_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
