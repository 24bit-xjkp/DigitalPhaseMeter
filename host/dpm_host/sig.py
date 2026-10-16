"""片内信号源：MMCM 工作点求解与寄存器镜像配置字（`docs/ps-protocol.md` 第 6 节）。

本模块与 `ps/include/dpm_calc.h`（`dpm_sig_solve` / `dpm_sig_drp_words`）和
`ps/include/dpm_regs.h`（`DPM_DRP_*` / `DPM_MMCM_ENCODE_CLKOUT`）保持同一契约。

约束（冻结）
    - `f_in = 50MHz`、`DIVCLK = 1`、`MULT = 12..24`（整数）、不使用小数分频；
    - `VCO = f_in * MULT / DIVCLK`，且 `600MHz <= VCO <= 1200MHz`；
    - 输出分频 `D = VCO / f_sig`，`1 <= D <= 127`（字段能存 255，规则取 127 为硬上限）；
    - `clk_out1` 相位恒 0，相位差只加在 `clk_out2` 上。

「方案 A」的配置路径（当前实现）：PL 侧是极简 DRP 控制器 `dpm_src_drp`（AXI 段
`0x43C1_0000`，4KB），它只把 8 个 16 位寄存器**镜像**按顺序搬进 MMCM 的 DRP 口，
不做任何算术；**频率与相位都在这里编码成 MMCM 的原始寄存器值**::

    ClkReg1 = {PHASE_MUX[15:13], 1'b1, HIGH_TIME[11:6], LOW_TIME[5:0]}
    ClkReg2 = {EDGE[7], NO_COUNT[6], DELAY_TIME[5:0]}

相位被拆成 `DELAY_TIME`（整数 VCO 周期，6 位）+ `PHASE_MUX`（1/8 VCO 周期，3 位），
拆分方式依据厂商实现 `mmcm_pll_drp_func_7s_mmcm.vh` 的 `mmcm_pll_phase`：相位码
`m = round(phase_mdeg × D / 45000)` 以 **1/8 VCO 周期**为单位（9 位），于是
`DELAY_TIME = m >> 3`（取整数部分）、`PHASE_MUX = m & 7`（取小数部分）。
因此**可实现的相位网格 = 1/8 VCO 周期 = `45°/D`**（D=40 → 1.125°、D=125 → 0.36°），
而不是 45°——22.5° 这类请求在 D=40/50/80/100/125 下都能精确表示。

`spec/drp_semantics.json` 是旧 `clk_wiz_src` 方案的实测记录，本模块只从它读两个结论：
`phase_controllable`（相位是否真的能由 DRP 改写）与 `phase_static_deg_milli`（不可控时的
固定相位）。**单位模式/相位字偏移那些键属于旧方案，方案 A 不再使用**。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

# ------------------------------------------------------------- 冻结约束

#: MMCM 输入频率（= 低速时钟）Hz。
F_IN_HZ = 50_000_000
#: DIVCLK 固定为 1。
DIVCLK_DEFAULT = 1
#: MULT 范围（整数）。
MULT_MIN = 12
MULT_MAX = 24
#: VCO 范围（-1 速度等级）。
VCO_MIN_HZ = 600_000_000
VCO_MAX_HZ = 1_200_000_000
#: 输出分频 D 的硬上限：字段虽然能到 255，但冻结规则把 D 限制在 127 以内
#: （`ps/include/dpm_calc.h` 的 `DPM_SIG_DIV_MAX = 127`，与 docs/ps-protocol.md 第 6 节一致）。
DIV_MIN = 1
#: 输出分频硬上限：分频在 MMCM 里编码成 HIGH_TIME + LOW_TIME（各 6 位），
#: 因此可表示的最大值是 63 + 63 = **126**，不是 127（127 的 LOW_TIME=64 会被截断成 63）。
DIV_MAX = 126
#: 相位字段最大值（8 位）；历史字段，已由毫度相位字取代。
PHASE_FIELD_MAX = 0xFF
#: 相位字的满量程（毫度）：0..359999。
FULL_SCALE_MILLI_DEG = 360_000

# --------------------------------------------------- DRP 寄存器镜像（方案 A）

#: PL 的 `dpm_src_drp` 寄存器镜像基偏移（BD 映射在 AXI 段 0x43C1_0000 的 4KB 页内）。
DRP_OFF_BASE = 0x08
#: 只读 ID（固定 `0x44504D03`，"DPM3"）。
DRP_OFF_ID = 0x00
#: 状态：bit0 = MMCM locked、bit1/bit2 = 重配置进行中。
DRP_OFF_STATUS = 0x04
#: 8 个 MMCM 16 位寄存器镜像（PL 状态机按这个顺序写进 DRP 口）。
DRP_OFF_CLKFBOUT = 0x08
DRP_OFF_CLKFBOUT2 = 0x0C
DRP_OFF_CLKOUT0 = 0x10
DRP_OFF_CLKOUT0_2 = 0x14
DRP_OFF_CLKOUT1 = 0x18
DRP_OFF_CLKOUT1_2 = 0x1C
DRP_OFF_DIVCLK = 0x20
DRP_OFF_POWER = 0x24
#: LOAD：写 1 触发一次完整重配置（W1S）。
DRP_OFF_LOAD = 0x28
DRP_ID_VALUE = 0x44504D03
DRP_STATUS_LOCKED = 1 << 0
DRP_STATUS_BUSY = 1 << 1
#: POWER 寄存器：使能相位 mux 与 delay time（复位值）。
DRP_POWER_ENABLE_PHASE = 0xFFFF
#: LOAD 触发值（写 1 = 触发；旧 clk_wiz 方案的 0x07/0x03 已不适用）。
DRP_LOAD_TRIGGER = 0x01
#: 同一组寄存器镜像的 MMCM DRP 地址（诊断用，PL 侧状态机内部使用）。
DRP_DADDR_ORDER: tuple[int, ...] = (0x14, 0x15, 0x08, 0x09, 0x0A, 0x0B, 0x16, 0x28)

#: 相位网格分子：网格 = `45000 / D` 毫度 = `45 / D` 度（1/8 VCO 周期）。
PHASE_GRID_MDEG_NUM = 45_000
#: 相位码位数（DELAY_TIME 6 位 + PHASE_MUX 3 位）。
PHASE_CODE_MAX = 0x1FF
#: 相位上界公式的分子（毫度·分频）：`m <= 511` ⟺ `phase_mdeg × D <= 23017499`。
#:
#: 推导：`m = floor((phase_mdeg × D + 22500) / 45000) <= 511`
#: ⟺ `phase_mdeg × D + 22500 < 512 × 45000` ⟺ `phase_mdeg × D <= 23017499`。
#: 与 `ps/include/dpm_regs.h::DPM_MMCM_PHASE_MAX_MDEG_NUM` 同一个数。
PHASE_MAX_MDEG_NUM = (PHASE_CODE_MAX + 1) * PHASE_GRID_MDEG_NUM - PHASE_GRID_MDEG_NUM // 2 - 1
#: ClkReg1 的 bit12（手册标 RSVD，clk_wiz 生成值恒为 1）。
CLKREG1_MARK = 0x1000


def mmcm_phase_code(phase_mdeg: int, div: int) -> int:
    """相位（毫度）→ 9 位 MMCM 相位码 `round(phase_mdeg × div / 45000)`（单位 1/8 VCO 周期）。

    与 `ps/include/dpm_regs.h::dpm_mmcm_phase_code` 逐位一致（整数四舍五入，半点向上）。
    """
    div = int(div)
    if div <= 0:
        raise ValueError(f"D 必须为正数，实际 {div}")
    return (int(phase_mdeg) * div * 8 + 180_000) // 360_000


def mmcm_phase_code_to_mdeg(code: int, div: int) -> int:
    """相位码 → 可实现相位（毫度，0..359999）。与 :func:`mmcm_phase_code` 互逆。"""
    div = int(div)
    if div <= 0:
        raise ValueError(f"D 必须为正数，实际 {div}")
    return ((int(code) * PHASE_GRID_MDEG_NUM + div // 2) // div) % FULL_SCALE_MILLI_DEG


def phase_max_milli_deg(div: int) -> int:
    """给定输出分频 D 时可**请求**的最大相位（毫度，含）。与 C 侧逐位一致。

    MMCM 的相位码 `m` 只有 9 位（DELAY_TIME 6 位 + PHASE_MUX 3 位），因此可表示的
    相位上界**与 D 有关**：`m <= 511` ⟺ `phase_mdeg × D <= 23017499`，再被满量程
    `359999`（360° 与 0° 同义，满量程不接收）截断：

    - D=40（25MHz）→ 359999（整个 0~360° 都能表示，`m = 320` 时回到 0°）；
    - D=100（10MHz）→ 230174（229.95° = `m = 511`）；
    - D=120（5MHz）→ 191812（吸附后 191.625°）；
    - D=125（8MHz）→ 184139（吸附后 183.960° = `m = 511`）。
    """
    div = int(div)
    if div <= 0:
        raise ValueError(f"D 必须为正数，实际 {div}")
    return min(PHASE_MAX_MDEG_NUM // div, FULL_SCALE_MILLI_DEG - 1)


def phase_code_fits(phase_milli_deg: int, div: int) -> bool:
    """相位请求是否落在 9 位相位码范围内（越界 = MMCM 会截断回绕，协议应回 ERR_PARAM）。

    与 `ps/include/dpm_regs.h::dpm_mmcm_phase_code_fits` 同一判据：
    `requested < 360000` 且 `round(requested × D / 45000) <= 511`。
    `D <= 0` 显式抛 :class:`ValueError`（与本模块其余函数一致；C 侧对 D=0 返回「不可请求」）。
    """
    requested = int(phase_milli_deg)
    return 0 <= requested < FULL_SCALE_MILLI_DEG and mmcm_phase_code(requested, div) <= PHASE_CODE_MAX


def phase_code_top_milli_deg(div: int) -> int:
    """相位码上界点（`m = 511`，即 DELAY_TIME=63 + PHASE_MUX=7）的**网格相位**（毫度）。

    这是硬件能表示的最大延迟对应的相位，恒落在 `45°/D` 网格上。D ≥ 103 时它也是该频点
    唯一能 >180° 的点（225° 需要 `5D ≤ 511`），因此验证脚本用它兜底保证 >180° 覆盖。

    注意：D ≤ 63 时这个值会超过满量程（如 D=40 → 574.875°）——那是相位码的上界而不是
    请求上界，请求上界请用 :func:`phase_max_milli_deg`。
    """
    d = int(div)
    if d <= 0:
        raise ValueError(f"D 必须为正数，实际 {div}")
    return (PHASE_CODE_MAX * PHASE_GRID_MDEG_NUM + d // 2) // d


def mmcm_encode_clkout(divide: int, phase_mdeg: int = 0) -> tuple[int, int]:
    """由分频比与相位（毫度）算 MMCM 的 `(ClkReg1, ClkReg2)`。

    与 `ps/include/dpm_regs.h::DPM_MMCM_ENCODE_CLKOUT` 逐位一致。
    """
    d = int(divide)
    if d <= 0:
        raise ValueError(f"分频比必须为正数，实际 {divide}")
    high = d // 2
    low = d - high
    code = mmcm_phase_code(phase_mdeg, d)
    # D == 1 是特例：NO_COUNT 置位、HIGH/LOW_TIME 是占位值，EDGE 也按厂商做法清 0
    edge = 0 if d == 1 else (d & 1)
    no_count = 1 if d == 1 else 0
    clkreg1 = CLKREG1_MARK | ((code & 0x7) << 13) | ((high & 0x3F) << 6) | (low & 0x3F)
    clkreg2 = (edge << 7) | (no_count << 6) | ((code >> 3) & 0x3F)
    return clkreg1, clkreg2


def mmcm_encode_divclk(divclk: int = 1) -> int:
    """DIVCLK 寄存器（MMCM 0x16）：`{2'b00, EDGE[13], NO_COUNT[12], HIGH_TIME[11:6], LOW_TIME[5:0]}`。

    与 `ps/include/dpm_regs.h::dpm_mmcm_encode_divclk` 逐位一致：
    `divide == 1` 时 HIGH/LOW_TIME 是占位的 1、NO_COUNT 置位（权威值 `0x1041`）。
    注意这里 bit12 是 NO_COUNT，**不是** CLKOUTx 的 MARK 位。
    """
    d = int(divclk)
    if d <= 0:
        raise ValueError(f"DIVCLK 必须为正数，实际 {divclk}")
    high = 1 if d == 1 else d // 2
    low = 1 if d == 1 else d - d // 2
    edge = 0 if d == 1 else (d & 1)
    no_count = 1 if d == 1 else 0
    return (edge << 13) | (no_count << 12) | ((high & 0x3F) << 6) | (low & 0x3F)


def mmcm_decode_divide(clkreg1: int) -> int:
    """从 ClkReg1 反推分频比（bit12 == 0 视为 divide == 1，与 C 侧一致）。"""
    word = int(clkreg1)
    if (word >> 12) & 1 == 0:
        return 1
    return ((word >> 6) & 0x3F) + (word & 0x3F)


def mmcm_decode_divclk(word: int) -> int:
    """从 DIVCLK 寄存器反推 DIVCLK_DIVIDE（bit12 是 NO_COUNT，divide==1 时为 1）。"""
    value = int(word)
    if (value >> 12) & 1:
        return 1
    return ((value >> 6) & 0x3F) + (value & 0x3F)


def mmcm_decode_phase_mdeg(clkreg1: int, clkreg2: int, divide: int) -> int:
    """从一对 CLKOUTx 寄存器反推相位（毫度）。与 :func:`mmcm_encode_clkout` 互逆。"""
    code = ((int(clkreg2) & 0x3F) << 3) | ((int(clkreg1) >> 13) & 0x7)
    return mmcm_phase_code_to_mdeg(code, divide)


# ---------------------------------------------------------- 相位可控性语义


@dataclass(frozen=True)
class DrpSemantics:
    """相位可控性结论（来自 `spec/drp_semantics.json`，方案 A 只需两个键）。

    方案 A 下相位由 PS 直接编码进 MMCM 的 PHASE_MUX/DELAY_TIME，硬件网格固定是
    `45°/D`（1/8 VCO 周期），因此不再有「相位字段单位」这个未定稿项。
    """

    #: 当前 bitstream 的 clk_out2 相位是否真的能由 DRP 改写（缺省 True）。
    phase_controllable: bool = True
    #: 相位不可控时输出固定相位（BD 静态配置的 90°，单位 1e-3 度）。
    phase_static_milli_deg: int = 90_000
    #: 结论文件路径（回退时为 ``None``）。
    path: str | None = None
    #: 数据来源说明（写进 README / CLI 输出）。
    source: str = "fallback:方案A（相位可写，网格 45°/D）"
    #: JSON 里实际用到的键。
    keys: tuple[str, ...] = ()

    @property
    def is_fallback(self) -> bool:
        """是否使用了回退语义（没有实测结论文件）。"""
        return self.path is None

    def describe(self) -> str:
        """一行描述，供 CLI / 日志输出。"""
        if not self.phase_controllable:
            return f"相位不可控：clk_out2 固定 {self.phase_static_milli_deg / 1000:.3f}°" f"（写 DRP 不改变输出）；来源：{self.source}"
        return f"相位可控：写 MMCM 的 PHASE_MUX/DELAY_TIME，网格 45°/D（1/8 VCO 周期）；" f"来源：{self.source}"


def repo_root() -> Path:
    """仓库根目录（`host/dpm_host/sig.py` → 上溯三级）。"""
    return Path(__file__).resolve().parents[2]


def default_semantics_path() -> Path:
    """结论文件的默认路径：环境变量 `DPM_DRP_SEMANTICS` 优先，否则 `<repo>/spec/drp_semantics.json`。"""
    env = os.environ.get("DPM_DRP_SEMANTICS")
    if env:
        return Path(env)
    return repo_root() / "spec" / "drp_semantics.json"


def _fallback_semantics(note: str = "") -> DrpSemantics:
    """回退语义：方案 A（相位可写，网格 45°/D）。"""
    src = "fallback:方案A（相位可写，网格 45°/D = 1/8 VCO 周期）"
    if note:
        src += f"；{note}"
    return DrpSemantics(phase_controllable=True, source=src)


def parse_semantics_json(data: dict[str, Any], path: str | None) -> DrpSemantics:
    """解析结论 JSON，只取方案 A 需要的两个结论。

    - `phase_controllable`（缺省 true）：相位能不能由 DRP 改写；
    - `phase_static_deg_milli`（可缺省，默认 90000 = BD 静态配置的 90°）。

    JSON 里其余键（`phase_unit_deg_milli`、`phase_word_index.*` 等）是旧 `clk_wiz_src`
    方案的实测记录，方案 A 不再使用，这里显式忽略，避免把旧语义带进新路径。
    """
    scope: dict[str, Any] = dict(data)
    sub = data.get("phase")
    if isinstance(sub, dict):
        for key, value in sub.items():
            scope.setdefault(key, value)

    source = str(data.get("source") or data.get("measured_by") or data.get("generated_by") or (path or "spec/drp_semantics.json"))
    controllable = bool(scope.get("phase_controllable", True))
    static_mdeg = int(scope.get("phase_static_deg_milli", scope.get("phase_static_mdeg", 90_000)) or 90_000)
    return DrpSemantics(
        phase_controllable=controllable,
        phase_static_milli_deg=static_mdeg,
        path=path,
        source=source,
        keys=("phase_controllable",),
    )


_semantics_cache: DrpSemantics | None = None
_semantics_loaded_from: tuple[str, bool] | None = None  # (路径, 文件是否存在)


def load_semantics(path: str | os.PathLike[str] | None = None, *, force: bool = False) -> DrpSemantics:
    """加载相位字段语义（带缓存）。

    - `path=None` 时用 :func:`default_semantics_path`；
    - 文件不存在 → 回退候选 B（不报错，符合「实测结论未落盘时两侧按候选 B 落地」）；
    - 文件存在但 JSON 非法 → 抛 :class:`ValueError`（配置错误要显式失败，而不是悄悄回退）。
    """
    global _semantics_cache, _semantics_loaded_from
    target = Path(path) if path is not None else default_semantics_path()
    key = (str(target), target.exists())
    if _semantics_cache is not None and not force and _semantics_loaded_from == key:
        return _semantics_cache

    if not target.exists():
        sem = _fallback_semantics(f"{target} 不存在")
    else:
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:  # pragma: no cover - 配置错误
            raise ValueError(f"{target} 不是合法 JSON：{exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{target} 的顶层必须是 JSON 对象")
        sem = parse_semantics_json(raw, str(target))
    _semantics_cache = sem
    _semantics_loaded_from = key
    return sem


def reload_semantics(path: str | os.PathLike[str] | None = None) -> DrpSemantics:
    """重新从磁盘读相位字段语义（实测结论落盘后调用，不需要重启进程）。"""
    return load_semantics(path, force=True)


def reset_semantics_cache() -> None:
    """清空语义缓存（主要给单元测试用；下次访问会重新读盘）。"""
    global _semantics_cache, _semantics_loaded_from
    _semantics_cache = None
    _semantics_loaded_from = None


def current_semantics() -> DrpSemantics:
    """取当前生效的相位字段语义。"""
    return load_semantics()


def phase_supported(phase_milli_deg: int, semantics: DrpSemantics | None = None) -> tuple[bool, str]:
    """给定相位是否**真的能被当前 bitstream 实现**（供 CLI/GUI 提示，不做拒绝）。

    返回 ``(supported, reason)``。方案 A 下相位可控（写 MMCM 的 PHASE_MUX/DELAY_TIME），
    因此只要相位可控就返回 True；只有在 `phase_controllable=false`（旧 bitstream 行为）时，
    才要求「请求值 == 静态固定相位」。
    """
    sem = semantics or current_semantics()
    requested = int(phase_milli_deg)
    if sem.phase_controllable:
        return True, ""
    if requested == int(sem.phase_static_milli_deg):
        return True, f"相位固定为 {sem.phase_static_milli_deg / 1000:.3f}°（与请求值相同，无需改相位）"
    return (
        False,
        f"当前 bitstream 不支持在线改相位：clk_out2 相位固定 {sem.phase_static_milli_deg / 1000:.3f}°，"
        f"请求 {requested / 1000:.3f}° 不会生效（见 spec/drp_semantics.json）",
    )


def mmcm_phase_grid_milli_deg(div: int) -> Fraction:
    """MMCM 相位网格的物理值：`45° / D`，单位毫度（分数）。

    硬件依据：MMCM 的相位最小步长是 1/8 VCO 周期，折算到输出（周期 = D 个 VCO 周期）即
    `45°/D`（见 `ps/include/dpm_regs.h` 的相位编码说明）。
    """
    if int(div) <= 0:
        raise ValueError(f"D 必须为正数，实际 {div}")
    return Fraction(PHASE_GRID_MDEG_NUM, int(div))


def phase_grid_milli_deg(div: int, semantics: DrpSemantics | None = None) -> Fraction | None:
    """相位网格的精确值（1e-3 度，分数）：`45° / D`；相位不可控时为 ``None``。

    D=40 → 1.125°（1125 毫度，整数）、D=80 → 0.5625°（562.5 毫度，**非整数**，
    所以用分数表示避免网格畸变）。
    """
    sem = semantics or current_semantics()
    if not sem.phase_controllable:
        return None
    if int(div) <= 0:
        return None
    return mmcm_phase_grid_milli_deg(div)


def phase_quantum_milli_deg(div: int, semantics: DrpSemantics | None = None) -> int | None:
    """相位网格的整数毫度近似（显示用）；相位不可控时返回 ``None``。"""
    grid = phase_grid_milli_deg(div, semantics)
    if grid is None:
        return None
    return round_half_up_int(grid.numerator, grid.denominator)


def phase_code_value(
    phase_milli_deg: int,
    div: int,
    vco_hz: int = 0,
    semantics: DrpSemantics | None = None,
) -> tuple[int, int, int | None, tuple[str, ...]]:
    """算 MMCM 相位码与**可实现**相位。

    返回 ``(相位码 m, 实际可实现相位 mdeg, 量化步长 mdeg 或 None, 提示)``：

    - 相位可控（方案 A，当前实现）：`m = round(请求 × D / 45000)`（9 位，单位 1/8 VCO 周期），
      实际相位 = `m × 45000 / D`，即吸附到 `45°/D` 网格上的最近格点；
    - 相位不可控（旧 bitstream）：照写请求值（无害，`docs/ps-protocol.md` 6.2），
      但实际相位是静态固定值（BD 初值 90°），此时第一个返回值无硬件含义。

    请求超出 9 位相位码（`m > 511`，上界见 :func:`phase_max_milli_deg`）时**不是吸附而是回绕**：
    MMCM 的 PHASE_MUX/DELAY_TIME 字段会按位截断。这时 `notes` 里会给出越界说明，且第一个
    返回值仍是**未截断**的相位码（便于调用方判断越界）；协议层应当用 :func:`phase_code_fits`
    先行拒绝这类请求。
    """
    sem = semantics or current_semantics()
    requested = max(0, min(int(phase_milli_deg), FULL_SCALE_MILLI_DEG - 1))
    notes: list[str] = []
    grid = phase_grid_milli_deg(div, sem)
    del vco_hz  # 相位码与 VCO 无关：网格只由输出分频 D 决定
    if grid is None:
        actual = int(sem.phase_static_milli_deg)
        notes.append(f"相位不可控（phase_controllable=false）：写 DRP 不改变输出，" f"实际相位恒为 {actual / 1000:.3f}°")
        return 0, actual, None, tuple(notes)
    code = mmcm_phase_code(requested, div)
    if code > PHASE_CODE_MAX:
        notes.append(
            f"相位越界：{requested / 1000:.3f}° 在 D={div} 下需要相位码 {code}，超出 9 位字段"
            f"（上限 {PHASE_CODE_MAX}）；MMCM 会截断回绕而不是吸附，"
            f"协议的请求上界是 {phase_max_milli_deg(div) / 1000:.3f}°，越界应回 ERR_PARAM"
        )
    actual = mmcm_phase_code_to_mdeg(code, div)
    if actual != requested:
        notes.append(
            f"相位量化：请求 {requested / 1000:.3f}°，网格 {float(grid) / 1000:.4f}°（45/D，D={div}），" f"实际 {actual / 1000:.3f}°"
        )
    return code, actual, phase_quantum_milli_deg(div, sem), tuple(notes)


def phase_word_value(
    phase_milli_deg: int,
    div: int,
    vco_hz: int = 0,
    semantics: DrpSemantics | None = None,
) -> tuple[int, int, int | None, tuple[str, ...]]:
    """:func:`phase_code_value` 的历史别名（旧方案里第一个返回值是「毫度相位字」）。

    方案 A 下相位不再是独立寄存器，第一个返回值是 MMCM 的 9 位相位码；
    保留本名字是为了不破坏已有调用点。
    """
    return phase_code_value(phase_milli_deg, div, vco_hz, semantics)


def round_half_up_int(num: int, den: int) -> int:
    """整数四舍五入（.5 向上），用于相位网格量化与 D 的取整。"""
    num, den = int(num), int(den)
    if den <= 0:
        raise ValueError(f"den 必须为正数，实际 {den}")
    if num >= 0:
        return (2 * num + den) // (2 * den)
    return -((-2 * num + den) // (2 * den))


def _round_half_up(value: Fraction) -> int:
    """对 :class:`Fraction` 做四舍五入（.5 向上），返回整数。"""
    num, den = value.numerator, value.denominator
    if den < 0:
        num, den = -num, -den
    if num >= 0:
        return (2 * num + den) // (2 * den)
    return -((-2 * num + den) // (2 * den))


# --------------------------------------------------------------- 求解


@dataclass(frozen=True)
class SigSolution:
    """片内信号源工作点（对应 `dpm_sig_t` + `dpm_sig_solve_t`）。"""

    ok: bool
    #: 目标频率（Hz）。
    freq_hz: int
    #: 实际可实现频率（Hz），`VCO / D` 的整数结果。
    actual_freq_hz: int
    #: |实际频率 - 目标频率|，单位 1e-3 Hz。
    error_milli_hz: int
    #: 目标相位差（1e-3 度）。
    phase_milli_deg: int
    #: **可实现**的相位差（1e-3 度，已吸附到 `45°/D` 网格）。
    phase_actual_milli_deg: int
    vco_hz: int
    mult: int
    divclk: int
    div: int
    #: 协议响应里的 `phaseField(u8)`：MMCM 的 `DELAY_TIME` 字段（整数 VCO 周期，0..63）
    #: ＝ 相位码 `>> 3`；细调部分在 `PHASE_MUX` 里，由 :attr:`phase_actual_milli_deg` 体现。
    phase_field: int = 0
    #: MMCM 的 9 位相位码（单位 1/8 VCO 周期）：`DELAY_TIME = code >> 3`、`PHASE_MUX = code & 7`。
    phase_code: int = 0
    #: 相位量化网格（45/D 度，单位毫度）；相位不可控时为 None。
    phase_quantum_milli_deg: int | None = None
    #: 当前 bitstream 相位是否可控。
    phase_controllable: bool = True
    #: VCO 相位步长对应的时间（ps，= 1/8 VCO 周期）。
    phase_step_ps: Fraction = Fraction(0)
    #: 提示信息（频率/相位量化、回退语义等）。
    notes: tuple[str, ...] = ()
    #: 使用的相位可控性语义来源。
    semantics_source: str = ""
    #: 求解规则来源（便于报告里注明）。
    rule: str = "冻结规则：最小 |VCO/D-f|（整数精确比较）→ cost=|VCO-1000MHz|/1MHz+|D-40| 最小"

    @property
    def exact(self) -> bool:
        """频率是否精确实现（误差为 0）。"""
        return self.ok and self.error_milli_hz == 0

    @property
    def actual_freq_milli_hz(self) -> int:
        """实际输出频率，单位 1e-3 Hz（`VCO/D` 的有理值四舍五入，比 :attr:`actual_freq_hz` 精确）。"""
        if not self.ok or self.div == 0:
            return 0
        return _round_half_up(Fraction(self.vco_hz * 1000, self.div))

    @property
    def divide_field(self) -> int:
        """DRP 分频字段：低字节直接是 D。"""
        return self.div & 0xFF

    def describe(self) -> str:
        if not self.ok:
            return f"无法求解：目标 {self.freq_hz / 1e6:.6f} MHz 在约束内没有可用工作点"
        return (
            f"VCO={self.vco_hz / 1e6:.1f}MHz MULT={self.mult} DIVCLK={self.divclk} D={self.div} "
            f"→ f_out={self.actual_freq_hz / 1e6:.6f}MHz"
            + ("" if self.exact else f"（误差 {self.error_milli_hz / 1000:.3f}Hz）")
            + f"，相位码 {self.phase_code}（DELAY_TIME={self.phase_field}、PHASE_MUX={self.phase_code & 0x7}，"
            f"实际 {self.phase_actual_milli_deg / 1000:.3f}°）"
        )


#: BD 初始工作点（选择规则的参考点）。
BD_VCO_HZ = 1_000_000_000
BD_DIV = 40


def select_cost(vco_hz: int, div: int) -> Fraction:
    """冻结规则的代价：`|VCO-1000MHz|/1MHz + |D-40|`（精确有理数，不用浮点）。"""
    return Fraction(abs(int(vco_hz) - BD_VCO_HZ), 1_000_000) + abs(int(div) - BD_DIV)


def freq_error_exact(vco_hz: int, div: int, target_hz: int) -> Fraction:
    """精确频率误差 `|VCO/D - f|`。

    用整数交叉相乘的等价形式比较，避免浮点在 `1000/160` 这类点上因二进制表示误差判错。
    """
    return Fraction(abs(int(vco_hz) - int(target_hz) * int(div)), int(div))


def solve_sig(
    freq_hz: int,
    phase_milli_deg: int = 0,
    slow_hz: int = F_IN_HZ,
    semantics: DrpSemantics | None = None,
    *,
    div_max: int = DIV_MAX,
) -> SigSolution:
    """为给定目标频率与相位差求解 MMCM 工作点。

    冻结规则（三处必须一致：`spec/gen_golden.py::solve_sig`、`ps/src/dpm_sig.c::dpm_sig_solve`、
    本函数）：

    1. `DIVCLK = 1`、`MULT = 12..24`、`VCO = 50MHz × MULT ∈ [600,1200]MHz`、`D ∈ 1..127`（硬上限）、
       不使用小数分频；
    2. 先最小化频率误差 `|VCO/D - f|`（用整数交叉相乘的精确比较，不用浮点）；
    3. 误差相同时（含多个精确解）取 `cost = |VCO - 1000MHz|/1MHz + |D - 40|` 最小者。

    规则输出（= `spec/golden/vectors.json` 的冻结表）::

        5.000MHz → 600/12/120      6.250MHz → 750/15/120    8.000MHz → 1000/20/125
        10.000MHz → 1000/20/100    12.500MHz → 1000/20/80   20.000MHz → 1000/20/50
        25.000MHz → 1000/20/40
    """
    sem = semantics or current_semantics()
    target = int(freq_hz)
    if target <= 0 or slow_hz <= 0:
        return SigSolution(
            ok=False,
            freq_hz=target,
            actual_freq_hz=0,
            error_milli_hz=0,
            phase_milli_deg=int(phase_milli_deg),
            phase_actual_milli_deg=0,
            vco_hz=0,
            mult=0,
            divclk=DIVCLK_DEFAULT,
            div=0,
            phase_field=0,
            phase_step_ps=Fraction(0),
            notes=("目标频率必须为正数",),
            semantics_source=sem.source,
        )

    best: tuple[tuple, int, int, int, int] | None = None  # (排序键, vco, mult, div, divclk)
    for mult in range(MULT_MIN, MULT_MAX + 1):
        vco = slow_hz * mult // DIVCLK_DEFAULT
        if not (VCO_MIN_HZ <= vco <= VCO_MAX_HZ):
            continue
        for div in range(DIV_MIN, min(int(div_max), DIV_MAX) + 1):
            err = freq_error_exact(vco, div, target)
            key = (err, select_cost(vco, div), vco, div, DIVCLK_DEFAULT)
            if best is None or key < best[0]:
                best = (key, vco, mult, div, DIVCLK_DEFAULT)

    if best is None:
        return SigSolution(
            ok=False,
            freq_hz=target,
            actual_freq_hz=0,
            error_milli_hz=0,
            phase_milli_deg=int(phase_milli_deg),
            phase_actual_milli_deg=0,
            vco_hz=0,
            mult=0,
            divclk=DIVCLK_DEFAULT,
            div=0,
            phase_field=0,
            phase_step_ps=Fraction(0),
            notes=("约束内没有 (MULT, D) 组合",),
            semantics_source=sem.source,
        )

    _, vco, mult, div, divclk = best
    err = freq_error_exact(vco, div, target)
    actual_freq = _round_half_up(Fraction(vco, div))
    error_milli_hz = _round_half_up(err * 1000)
    step_ps = Fraction(10**12, 8 * vco)
    phase_code, phase_actual, phase_quantum, notes = phase_code_value(phase_milli_deg, div, vco, sem)

    notes_list = list(notes)
    if err != 0:
        achieved_milli = _round_half_up(Fraction(vco * 1000, div))
        notes_list.append(
            f"频率不能精确实现：目标 {target / 1e6:.6f} MHz，最接近 {achieved_milli / 1e6:.6f} MHz"
            f"（误差 {error_milli_hz / 1000:.3f} Hz）"
        )
    if Fraction(vco, div).denominator != 1:
        notes_list.append("该工作点的 VCO/D 不是整数 Hz，报告按四舍五入后的整数 Hz 给出（MMCM 实际输出为 VCO/D）")

    return SigSolution(
        ok=True,
        freq_hz=target,
        actual_freq_hz=actual_freq,
        error_milli_hz=error_milli_hz,
        phase_milli_deg=int(phase_milli_deg),
        phase_actual_milli_deg=phase_actual,
        vco_hz=vco,
        mult=mult,
        divclk=divclk,
        div=div,
        # phaseField = DELAY_TIME（相位码 >> 3），与 PS 侧回包口径一致
        phase_field=(phase_code >> 3) & 0x3F,
        phase_code=phase_code,
        phase_quantum_milli_deg=phase_quantum,
        phase_controllable=sem.phase_controllable,
        phase_step_ps=step_ps,
        notes=tuple(notes_list),
        semantics_source=sem.source,
    )


# ------------------------------------------------- 寄存器镜像配置字（方案 A）


@dataclass(frozen=True)
class DrpWords:
    """一次 `SET_SIG` 要写的 8 个 MMCM 寄存器镜像 + LOAD（方案 A）。

    PL 侧 `dpm_src_drp`（AXI 段 `0x43C1_0000`）按固定顺序把这 8 个 16 位值搬进 MMCM 的
    DRP 口，因此这里给出的就是**MMCM 的原始寄存器值**（编码见 :func:`mmcm_encode_clkout`）：

    - `clkfbout`/`clkfbout2`（0x08/0x0C）→ MMCM 0x14/0x15；
    - `clkout0`/`clkout0_2`（0x10/0x14）→ MMCM 0x08/0x09（参考输出，相位 0）；
    - `clkout1`/`clkout1_2`（0x18/0x1C）→ MMCM 0x0A/0x0B（移相输出，相位差在这里）；
    - `divclk`（0x20）→ MMCM 0x16；`power`（0x24）→ MMCM 0x28（使能相位 mux/delay）；
    - `load`（0x28）：写 1 触发一次完整重配置。
    """

    clkfbout: int
    clkfbout2: int
    clkout0: int
    clkout0_2: int
    clkout1: int
    clkout1_2: int
    divclk: int
    power: int = DRP_POWER_ENABLE_PHASE
    load: int = DRP_LOAD_TRIGGER
    #: 9 位相位码（单位 1/8 VCO 周期）与它拆出的两个 MMCM 字段。
    phase_code: int = 0
    phase_field: int = 0  # DELAY_TIME（整数 VCO 周期）
    phase_mux: int = 0  # PHASE_MUX（1/8 VCO 周期）

    def as_pairs(self) -> tuple[tuple[int, int], ...]:
        """`(偏移, 值)` 序列，按 PS 侧写入顺序（8 个寄存器镜像 + LOAD）。"""
        return (
            (DRP_OFF_CLKFBOUT, self.clkfbout),
            (DRP_OFF_CLKFBOUT2, self.clkfbout2),
            (DRP_OFF_CLKOUT0, self.clkout0),
            (DRP_OFF_CLKOUT0_2, self.clkout0_2),
            (DRP_OFF_CLKOUT1, self.clkout1),
            (DRP_OFF_CLKOUT1_2, self.clkout1_2),
            (DRP_OFF_DIVCLK, self.divclk),
            (DRP_OFF_POWER, self.power),
            (DRP_OFF_LOAD, self.load),
        )


def drp_words(sol: SigSolution, semantics: DrpSemantics | None = None) -> DrpWords:
    """组装要写进 PL 寄存器镜像的 8 个 MMCM 寄存器值 + LOAD（方案 A）。

    写入顺序（与 PS 侧 `dpm_sig_apply` / PL 状态机一致）：
    CLKFBOUT(0x08) → CLKFBOUT2(0x0C) → CLKOUT0(0x10) → CLKOUT0_2(0x14) →
    CLKOUT1(0x18) → CLKOUT1_2(0x1C) → DIVCLK(0x20) → POWER(0x24) → LOAD(0x28)=1。

    相位不再有独立寄存器：它被拆进 CLKOUT1 的 `PHASE_MUX`（ClkReg1）与 `DELAY_TIME`
    （ClkReg2），由 :func:`mmcm_encode_clkout` 编码。
    """
    if not sol.ok:
        raise ValueError("求解失败（ok=False）时不能组装 DRP 配置字")
    sem = semantics or current_semantics()
    del sem  # 相位可控性只影响是否提示，不影响寄存器值的编码
    code = sol.phase_code if sol.phase_controllable else 0
    fb1, fb2 = mmcm_encode_clkout(sol.mult, 0)
    c01, c02 = mmcm_encode_clkout(sol.div, 0)
    c11, c12 = mmcm_encode_clkout(sol.div, sol.phase_actual_milli_deg)
    return DrpWords(
        clkfbout=fb1,
        clkfbout2=fb2,
        clkout0=c01,
        clkout0_2=c02,
        clkout1=c11,
        clkout1_2=c12,
        divclk=mmcm_encode_divclk(sol.divclk),
        phase_code=code,
        phase_field=(code >> 3) & 0x3F,
        phase_mux=code & 0x7,
    )


def decode_drp_words(words: DrpWords) -> dict[str, int]:
    """把寄存器镜像反解成字段（诊断/测试用，与 :func:`drp_words` 互逆）。"""
    div = mmcm_decode_divide(words.clkout0)
    return {
        "mult": mmcm_decode_divide(words.clkfbout),
        "divclk": mmcm_decode_divclk(words.divclk),
        "div0": div,
        "div1": mmcm_decode_divide(words.clkout1),
        "phase_mdeg": mmcm_decode_phase_mdeg(words.clkout1, words.clkout1_2, div),
    }


# --------------------------------------------------------- 冻结频点表


@dataclass(frozen=True)
class FrozenPoint:
    """`docs/ps-protocol.md` 第 6 节冻结验证频点。"""

    freq_hz: int
    vco_hz: int
    mult: int
    divclk: int
    div: int
    phase_step_ps: Fraction

    @property
    def divide_field(self) -> int:
        """DRP 分频字段（低字节 = D）。"""
        return self.div & 0xFF

    @property
    def freq_mhz(self) -> float:
        return self.freq_hz / 1e6

    def describe(self) -> str:
        return (
            f"{self.freq_mhz:g} MHz: VCO={self.vco_hz / 1e6:g}MHz MULT={self.mult} DIVCLK={self.divclk} "
            f"D={self.div} divide_field={self.divide_field} 相位步长={float(self.phase_step_ps):.1f}ps"
        )


def _frozen(freq_mhz: float, vco_mhz: int, mult: int, div: int) -> FrozenPoint:
    freq_hz = int(round(freq_mhz * 1e6))
    return FrozenPoint(
        freq_hz=freq_hz,
        vco_hz=vco_mhz * 1_000_000,
        mult=mult,
        divclk=DIVCLK_DEFAULT,
        div=div,
        phase_step_ps=Fraction(10**12, 8 * vco_mhz * 1_000_000),
    )


#: 冻结验证频点表（`docs/ps-protocol.md` 第 6.4 节）。这张表是**求解规则输出的抽样**：
#: 10 个频点全部由 `solve_sig` 精确解出（`verify_frozen_table` 与
#: `spec/gen_golden.py::build_vectors` 都会断言这一点），覆盖 600/750/800/1000MHz 四个
#: VCO、MULT 12/15/16/20 与 D = 20/25/40/50/80/100/120/125。
#: 频点选取见 `spec/gen_golden.py::FREQ_POINTS_HZ`：信号周期既要整数 ps，也要是 DSP
#: 周期（2.5ns）的整数倍（`8D/MULT` 为整数），此时 `phaseCnt % edgeCnt == 0` 严格成立。
FROZEN_POINTS: tuple[FrozenPoint, ...] = (
    _frozen(5, 600, 12, 120),
    _frozen(6.25, 750, 15, 120),
    _frozen(8, 1000, 20, 125),
    _frozen(10, 1000, 20, 100),
    _frozen(12.5, 1000, 20, 80),
    _frozen(16, 800, 16, 50),
    _frozen(20, 1000, 20, 50),
    _frozen(25, 1000, 20, 40),
    _frozen(40, 1000, 20, 25),
    _frozen(50, 1000, 20, 20),
)

#: 冻结相位点（度）：0~360° 的 45°/2 网格点，覆盖 <180°、=180° 与 >180° 三段。
#:
#: 某频点**能不能**请求其中某个点由 9 位相位码决定（:func:`phase_code_fits`）：
#: 180° 在所有合法 D（≤126）下都合法（`m = 4D <= 504`）；225°/270°/315° 只在
#: `7D <= 511`（D ≤ 73）/`6D <= 511`（D ≤ 85）/`5D <= 511`（D ≤ 102）时合法。
#: 25MHz（D=40）与 20MHz（D=50）能覆盖到 315°，8MHz（D=125）只能到 184.139°。
FROZEN_PHASES_DEG: tuple[float, ...] = (0, 22.5, 45, 67.5, 90, 112.5, 135, 157.5, 180, 202.5, 225, 270, 315)

#: 冻结相位点（1e-3 度）。
FROZEN_PHASES_MILLI_DEG: tuple[int, ...] = tuple(int(round(p * 1000)) for p in FROZEN_PHASES_DEG)


def frozen_point(freq_hz: int) -> FrozenPoint | None:
    """按频率查冻结频点（容差 1Hz）。"""
    for pt in FROZEN_POINTS:
        if abs(pt.freq_hz - int(freq_hz)) <= 1:
            return pt
    return None


def verify_frozen_table(slow_hz: int = F_IN_HZ) -> list[str]:
    """校验冻结频点表：约束合法、频率精确、**求解器输出与表逐位一致**。返回问题列表（空 = 全通过）。

    这是本模块的自校验入口，也被 `host/tests/test_sig.py` 复用。
    """
    problems: list[str] = []
    for pt in FROZEN_POINTS:
        if pt.vco_hz != slow_hz * pt.mult // pt.divclk:
            problems.append(f"{pt.freq_mhz:g}MHz: VCO != f_in*MULT/DIVCLK")
        if not (VCO_MIN_HZ <= pt.vco_hz <= VCO_MAX_HZ):
            problems.append(f"{pt.freq_mhz:g}MHz: VCO 超出范围")
        if not (MULT_MIN <= pt.mult <= MULT_MAX):
            problems.append(f"{pt.freq_mhz:g}MHz: MULT 超出范围")
        if not (DIV_MIN <= pt.div <= DIV_MAX):
            problems.append(f"{pt.freq_mhz:g}MHz: D 超出硬上限 {DIV_MAX}")
        if pt.divide_field > 0xFF:
            problems.append(f"{pt.freq_mhz:g}MHz: divide_field 超出 8 位")
        if pt.vco_hz // pt.div != pt.freq_hz:
            problems.append(f"{pt.freq_mhz:g}MHz: VCO/D 不等于目标频率")
        sol = solve_sig(pt.freq_hz, 0, slow_hz)
        if not sol.ok or sol.error_milli_hz != 0:
            problems.append(f"{pt.freq_mhz:g}MHz: 求解器找不到精确频率（err={sol.error_milli_hz} mHz）")
        elif (sol.vco_hz, sol.mult, sol.div) != (pt.vco_hz, pt.mult, pt.div):
            problems.append(
                f"{pt.freq_mhz:g}MHz: 求解器输出 {sol.vco_hz // 10**6}/{sol.mult}/{sol.div} "
                f"与冻结表 {pt.vco_hz // 10**6}/{pt.mult}/{pt.div} 不一致"
            )
    return problems


__all__ = [
    "F_IN_HZ",
    "DIVCLK_DEFAULT",
    "MULT_MIN",
    "MULT_MAX",
    "VCO_MIN_HZ",
    "VCO_MAX_HZ",
    "DIV_MIN",
    "DIV_MAX",
    "PHASE_FIELD_MAX",
    "FULL_SCALE_MILLI_DEG",
    "DRP_OFF_BASE",
    "DRP_OFF_ID",
    "DRP_OFF_STATUS",
    "DRP_OFF_CLKFBOUT",
    "DRP_OFF_CLKFBOUT2",
    "DRP_OFF_CLKOUT0",
    "DRP_OFF_CLKOUT0_2",
    "DRP_OFF_CLKOUT1",
    "DRP_OFF_CLKOUT1_2",
    "DRP_OFF_DIVCLK",
    "DRP_OFF_POWER",
    "DRP_OFF_LOAD",
    "DRP_ID_VALUE",
    "DRP_STATUS_LOCKED",
    "DRP_STATUS_BUSY",
    "DRP_POWER_ENABLE_PHASE",
    "DRP_LOAD_TRIGGER",
    "DRP_DADDR_ORDER",
    "PHASE_GRID_MDEG_NUM",
    "PHASE_CODE_MAX",
    "PHASE_MAX_MDEG_NUM",
    "mmcm_phase_code",
    "mmcm_phase_code_to_mdeg",
    "phase_max_milli_deg",
    "phase_code_fits",
    "phase_code_top_milli_deg",
    "mmcm_encode_clkout",
    "mmcm_encode_divclk",
    "mmcm_decode_divide",
    "mmcm_decode_divclk",
    "mmcm_decode_phase_mdeg",
    "phase_supported",
    "phase_quantum_milli_deg",
    "phase_grid_milli_deg",
    "mmcm_phase_grid_milli_deg",
    "phase_code_value",
    "DrpSemantics",
    "SigSolution",
    "DrpWords",
    "FrozenPoint",
    "FROZEN_POINTS",
    "FROZEN_PHASES_DEG",
    "FROZEN_PHASES_MILLI_DEG",
    "repo_root",
    "default_semantics_path",
    "parse_semantics_json",
    "load_semantics",
    "reload_semantics",
    "reset_semantics_cache",
    "current_semantics",
    "phase_word_value",
    "round_half_up_int",
    "solve_sig",
    "drp_words",
    "decode_drp_words",
    "frozen_point",
    "verify_frozen_table",
]
