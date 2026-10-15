"""片内信号源：MMCM 工作点求解与 DRP 配置字（`docs/ps-protocol.md` 第 6 节）。

本模块与 `ps/include/dpm_calc.h`（`dpm_sig_solve` / `dpm_sig_drp_words`）和
`ps/include/dpm_regs.h`（`DPM_DRP_*`）保持同一契约：

约束（冻结）
    - `f_in = 50MHz`、`DIVCLK = 1`、`MULT = 12..24`（整数）、不使用小数分频；
    - `VCO = f_in * MULT / DIVCLK`，且 `600MHz <= VCO <= 1200MHz`；
    - 输出分频 `D = VCO / f_sig`，`1 <= D <= 127`（字段本身能存 255，规则取 127 为硬上限）；
    - 相位字段 8 位，`clk_out1` 相位 = 0。

DRP 字编码（`ps/include/dpm_regs.h`，2026-02 已按实测证据改过一版）::

    CLKFBOUT = (DIVCLK & 0xFF) | (MULT << 8)
    CLKOUT0  = (D & 0xFF)      | (phase0 << 8)
    CLKOUT1  = (D & 0xFF)      | (phase1 << 8)
    LOAD     = 0x25C，值 0x07（加载 CLKFBOUT + CLKOUT0 + CLKOUT1）
    STATUS   = 0x200 + 4 * 31 = 0x27C，bit0 = locked

相位字段的**单位**尚未定稿（`docs/ps-protocol.md` 第 6 节「待确认项」），必须从
`spec/drp_semantics.json` 读取，绝不在这里硬编码两套语义。键的优先级（Lead 冻结）：

1. `phase_unit_deg_milli`（整数，毫度/步）——**默认候选 B**（`45000` = 45° = 1/8 VCO 周期）；
2. `phase_unit_ps_per_lsb`（数值，ps/LSB）——按 `deg_per_lsb = 360 * f_vco * ps * 1e-12` 换算，
   因此单位随 VCO 变化，在使用时按工作点换算；
3. `phase_field_is_degrees`（布尔，true → `1000` 毫度/步，候选 A）。

文件不存在或没有任何可用键时回退**候选 B**（45000）。`reload_semantics()` 可在实测结论落盘后
重新加载，不需要重启进程。`phase_controllable` 键（缺省 true）用于标记「当前 bitstream 的
clk_out2 相位是否真的能由 DRP 改写」——drp-verify 正在实测，若为 false，CLI/GUI 会把相位
输入标注为不可用而不是假装能改。
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
DIV_MAX = 127
#: 相位字段最大值（8 位）；历史字段，已由毫度相位字取代。
PHASE_FIELD_MAX = 0xFF
#: 相位字的满量程（毫度）：0..359999。
FULL_SCALE_MILLI_DEG = 360_000

# ------------------------------------------------------------- DRP 常量

#: DRP 寄存器文件基偏移（PG065：AXI4-Lite 动态重配置寄存器从 0x200 起）。
DRP_OFF_BASE = 0x200
#: 配置字索引（与 `clk_wiz_drp.vhd` 的 ram_clk_config 索引一致）。
DRP_IDX_CLKFBOUT = 0
DRP_IDX_CLKOUT0 = 2
DRP_IDX_CLKOUT1 = 5
#: clk_out1 / clk_out2 的相位字索引（实测：单位毫度；当前 IP 未接线）。
DRP_IDX_CLKOUT0_PHASE = 3
DRP_IDX_CLKOUT1_PHASE = 6
#: LOAD 字索引（0x25C = 0x200 + 4*23）。
DRP_IDX_LOAD = 23
DRP_IDX_STATUS = 31
#: LOAD 寄存器的字节偏移与取值（加载 CLKFBOUT + CLKOUT0 + CLKOUT1）。
DRP_LOAD_OFF = 0x25C
DRP_LOAD_BOTH = 0x07
#: 实测等价写法：0x07 与 0x03 行为一致（bit2 及以上未使用）。
DRP_LOAD_DYNAMIC = 0x03
#: `locked` 状态寄存器的字节偏移与位域。
#:
#: **实测（spec/drp_semantics.json，teammate drp-verify）**：状态寄存器不在 0x200 起的
#: 64 字寄存器文件里，而在 IPIF 的本地寄存器 **0x004**（bit0 = locked、bit1 = srdy）；
#: 任务书与旧文档写的「字 31 / 0x27C」在真实 IP 里**恒为 0**。
DRP_STATUS_OFF = 0x004
DRP_STATUS_LEGACY_OFF = DRP_OFF_BASE + 4 * DRP_IDX_STATUS  # 0x27C（实测恒 0，保留用于对照）
DRP_STATUS_LOCKED = 1 << 0
DRP_STATUS_SRDY = 1 << 1
#: LOAD（0x25C）的位域：bit0 = 触发一次重配置；bit1 = SADDR（0 = 静态配置 S1_*，1 = 动态寄存器文件）。
DRP_LOAD_TRIGGER_BIT = 1 << 0
DRP_LOAD_SADDR_BIT = 1 << 1
#: 小数分频使能位（整数分频时置 0）。
DRP_FRAC_EN = 1 << 16


def drp_word_off(index: int) -> int:
    """第 n 个 DRP 配置字的字节偏移（0x200 + 4n）。"""
    return DRP_OFF_BASE + 4 * int(index)


def divide_field(div: int) -> int:
    """分频字段编码：低字节直接存分频比 D（`div & 0xFF`）。"""
    return int(div) & 0xFF


def divide_field_get(word: int) -> int:
    """从配置字取回分频比（低字节）。"""
    return int(word) & 0xFF


def phase_field_bits(phase_field: int) -> int:
    """相位字段编码：位 [15:8]。"""
    return (int(phase_field) & 0xFF) << 8


def phase_field_get(word: int) -> int:
    """从配置字取回相位字段。"""
    return (int(word) >> 8) & 0xFF


# ---------------------------------------------------------- 相位字段语义


@dataclass(frozen=True)
class DrpSemantics:
    """相位字段单位的实测结论（来自 `spec/drp_semantics.json` 或回退候选 B）。"""

    #: 单位模式：``"degrees"``（字段=度数）、``"ps_per_lsb"``（字段=ps 延迟）、``"deg_unit"``（字段步长=unit_deg_milli）
    mode: str
    #: `ps_per_lsb` 模式下 1 LSB 的 ps 数。
    phase_unit_ps_per_lsb: Fraction | None = None
    #: `degrees` / `deg_unit` 模式下 1 LSB 的毫度（degrees 模式恒为 1000）。
    phase_unit_deg_milli: int | None = None
    #: 当前 bitstream 的 clk_out2 相位是否真的能由 DRP 改写（实测项，缺省 True）。
    phase_controllable: bool = True
    #: 相位不可控时输出固定相位（BD 静态配置的 90°，单位 1e-3 度）。
    phase_static_milli_deg: int = 90_000
    #: 相位字是否已接到 DRP（`phase_word_index.wired`）；未接线时写相位字没有任何效果。
    phase_word_wired: bool = False
    #: 相位字的字节偏移（接线的 clk_out2 相位字；缺省 0x218 = 字索引 6）。
    phase_word_off: int | None = None
    #: 结论文件路径（回退时为 ``None``）。
    path: str | None = None
    #: 数据来源说明（写进 README / CLI 输出）。
    source: str = "fallback:候选B（字段=45° 步长，1 LSB = 1/8 VCO 周期）"
    #: JSON 里实际用到的键。
    keys: tuple[str, ...] = ()

    @property
    def is_fallback(self) -> bool:
        """是否使用了回退语义（没有实测结论文件）。"""
        return self.path is None

    def describe(self) -> str:
        """一行描述，供 CLI / 日志输出。"""
        if self.mode == "none":
            return (
                f"相位不可控（mode=none）：当前 bitstream 的相位字未接线，clk_out2 相位固定 "
                f"{self.phase_static_milli_deg / 1000:.3f}°；来源：{self.source}"
            )
        if self.mode == "ps_per_lsb":
            unit = f"{float(self.phase_unit_ps_per_lsb):g} ps/LSB"
        elif self.mode == "deg_unit":
            unit = f"{self.phase_unit_deg_milli / 1000.0:g}°/LSB"
        else:
            unit = "1°/LSB（字段=相位度数）"
        wired = "，相位字已接线" if self.phase_word_wired else ""
        return f"相位字段语义：{self.mode}（{unit}{wired}），来源：{self.source}"


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
    """回退语义：候选 B（字段 = 45° 的整数倍，即 1/8 VCO 周期）。"""
    src = "fallback:候选B（字段=45° 步长，1 LSB = 1/8 VCO 周期，phase_unit_deg_milli=45000）"
    if note:
        src += f"；{note}"
    return DrpSemantics(mode="deg_unit", phase_unit_deg_milli=45_000, phase_controllable=True, source=src)


def _as_fraction(value: Any) -> Fraction:
    """把 JSON 数值转成十进制精确的 :class:`Fraction`（避免二进制浮点误差）。"""
    if isinstance(value, bool):
        raise ValueError("布尔值不能当数值用")
    if isinstance(value, int):
        return Fraction(value)
    return Fraction(str(value))


def parse_semantics_json(data: dict[str, Any], path: str | None) -> DrpSemantics:
    """解析结论 JSON（兼容顶层键与 ``phase`` 子对象两种写法）。

    关键点（都与实测结论 `spec/drp_semantics.json` 对齐）：

    - `phase_controllable`（缺省 true）：当前 bitstream 的 clk_out2 相位能不能由 DRP 改写。
      实测为 **false**（相位字未接线，写了没有任何效果）→ `mode = "none"`，此时
      `phase_unit_deg_milli` / `phase_unit_ps_per_lsb` 常为 0，**不能当成「单位是 0」报错**；
    - 相位可控时键优先级：`phase_unit_deg_milli` → `phase_unit_ps_per_lsb` →
      `phase_field_is_degrees` → 回退候选 B（45000）；数值 <= 0 视为「没有这个键」；
    - `phase_word_index.wired` / `phase_word_index.clkout1`：相位字是否接线与偏移；
    - `phase_static_deg_milli`（可缺省，默认 90000 = BD 静态配置的 90°）。
    """
    scope: dict[str, Any] = dict(data)
    sub = data.get("phase")
    if isinstance(sub, dict):
        for key, value in sub.items():
            scope.setdefault(key, value)
    phase_word = data.get("phase_word_index")
    if isinstance(phase_word, dict):
        for key, value in phase_word.items():
            scope.setdefault(f"phase_word_index.{key}", value)
    else:
        # `phase_word_index` 也可能在 `phase` 子对象里
        pw_sub = scope.get("phase_word_index")
        if isinstance(pw_sub, dict):
            for key, value in pw_sub.items():
                scope.setdefault(f"phase_word_index.{key}", value)

    source = str(data.get("source") or data.get("measured_by") or data.get("generated_by")
                 or (path or "spec/drp_semantics.json"))
    controllable = bool(scope.get("phase_controllable", True))
    static_mdeg = int(scope.get("phase_static_deg_milli", scope.get("phase_static_mdeg", 90_000)) or 90_000)
    wired = bool(scope.get("phase_word_index.wired", False))
    word_off = scope.get("phase_word_index.clkout1")
    phase_word_off = (DRP_OFF_BASE + 4 * int(word_off)) if word_off is not None else None

    if not controllable:
        # 相位不可控：单位字段（常为 0）不参与换算，也不要报错
        return DrpSemantics(
            mode="none",
            phase_controllable=False,
            phase_static_milli_deg=static_mdeg,
            phase_word_wired=wired,
            phase_word_off=phase_word_off,
            path=path,
            source=source,
            keys=("phase_controllable",),
        )

    deg_unit = scope.get("phase_unit_deg_milli")
    if deg_unit is not None and int(deg_unit) > 0:
        unit_mdeg = int(deg_unit)
        return DrpSemantics(
            mode="degrees" if unit_mdeg == 1000 else "deg_unit",
            phase_unit_deg_milli=unit_mdeg,
            phase_word_wired=wired,
            phase_word_off=phase_word_off,
            path=path,
            source=source,
            keys=("phase_unit_deg_milli",),
        )

    ps_unit = scope.get("phase_unit_ps_per_lsb")
    if ps_unit is not None and _as_fraction(ps_unit) > 0:
        return DrpSemantics(
            mode="ps_per_lsb",
            phase_unit_ps_per_lsb=_as_fraction(ps_unit),
            phase_word_wired=wired,
            phase_word_off=phase_word_off,
            path=path,
            source=source,
            keys=("phase_unit_ps_per_lsb",),
        )

    if bool(scope.get("phase_field_is_degrees")):
        return DrpSemantics(
            mode="degrees",
            phase_unit_deg_milli=1000,
            phase_word_wired=wired,
            phase_word_off=phase_word_off,
            path=path,
            source=source,
            keys=("phase_field_is_degrees",),
        )

    sem = _fallback_semantics(f"{path} 中没有可用的相位单位键（phase_controllable=true）")
    return DrpSemantics(
        mode=sem.mode,
        phase_unit_deg_milli=sem.phase_unit_deg_milli,
        phase_word_wired=wired,
        phase_word_off=phase_word_off,
        path=path,
        source=source,
        keys=("(fallback)",),
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

    返回 ``(supported, reason)``。当前 bitstream 的相位字未接线（`phase_controllable=false`）时
    只有「请求值 == 静态固定相位」才算可支持；请求别的相位不会报错（`docs/ps-protocol.md`
    第 6.2 节要求 PS 侧照写相位字，只是不要声称它生效），因此本函数只用于提示。
    """
    sem = semantics or current_semantics()
    requested = int(phase_milli_deg)
    if sem.mode != "none" and sem.phase_controllable:
        return True, ""
    if requested == int(sem.phase_static_milli_deg):
        return True, f"相位固定为 {sem.phase_static_milli_deg / 1000:.3f}°（与请求值相同，无需改相位）"
    return (
        False,
        f"当前 bitstream 不支持在线改相位：clk_out2 相位固定 {sem.phase_static_milli_deg / 1000:.3f}°，"
        f"请求 {requested / 1000:.3f}° 不会生效（相位字 0x218 未接入重建逻辑，见 spec/drp_semantics.json）",
    )


def mmcm_phase_grid_milli_deg(div: int) -> Fraction:
    """MMCM 相位网格的物理值（与是否接线无关）：`45° / D`，单位毫度（分数）。"""
    if int(div) <= 0:
        raise ValueError(f"D 必须为正数，实际 {div}")
    return Fraction(45_000, int(div))


def phase_grid_milli_deg(div: int, semantics: DrpSemantics | None = None) -> Fraction | None:
    """相位网格的精确值（1e-3 度，分数）：`45° / D`。

    实测：MMCM 把相位量化到 1/8 VCO 周期（125ps @ VCO=1000MHz），折算到输出即
    `45° / D`（`docs/ps-protocol.md` 第 6.2 节）：D=40 → 1.125°（1125 毫度，整数）、
    D=80 → 0.5625°（562.5 毫度，**非整数**，所以用分数表示避免网格畸变）。
    """
    sem = semantics or current_semantics()
    if sem.mode == "none" or not sem.phase_controllable:
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


def phase_word_value(
    phase_milli_deg: int,
    div: int,
    vco_hz: int = 0,
    semantics: DrpSemantics | None = None,
) -> tuple[int, int, int | None, tuple[str, ...]]:
    """算相位字（毫度）与可实现相位。

    返回 ``(写入 0x218 的值, 实际可实现相位 mdeg, 量化步长 mdeg 或 None, 提示)``：

    - 相位可控：写请求值（硬件按 `45/D` 度网格量化），实际值取量化后的最近格点；
    - 相位不可控（当前 bitstream）：照写请求值（无害，`docs/ps-protocol.md` 6.2），
      但实际相位是静态固定值（BD 初值 90°）。
    """
    sem = semantics or current_semantics()
    requested = max(0, min(int(phase_milli_deg), FULL_SCALE_MILLI_DEG - 1))
    notes: list[str] = []
    grid = phase_grid_milli_deg(div, sem)
    del vco_hz  # 相位字单位是毫度，与 VCO 无关（VCO 只决定量化网格）
    if grid is None:
        actual = int(sem.phase_static_milli_deg)
        notes.append(
            f"相位字未接入（phase_controllable=false）：写 0x218={requested} 不改变输出，"
            f"实际相位恒为 {actual / 1000:.3f}°"
        )
        return requested, actual, None, tuple(notes)
    # 步数 = round(请求 / 网格) = round(请求 * D / 45000)；用整数运算，避免分数误差
    steps = round_half_up_int(requested * int(div), 45_000)
    actual = round_half_up_int(steps * 45_000, int(div))
    if actual != requested:
        notes.append(
            f"相位量化：请求 {requested / 1000:.3f}°，网格 {float(grid) / 1000:.4f}°（45/D，D={div}），"
            f"实际 {actual / 1000:.3f}°"
        )
    return requested, actual, phase_quantum_milli_deg(div, sem), tuple(notes)


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
    #: 相位字段实际表示的相位差（1e-3 度）。
    phase_actual_milli_deg: int
    vco_hz: int
    mult: int
    divclk: int
    div: int
    #: 协议响应里的 `phaseField(u8)`：历史字段（原「分频字 bit[15:8] 的相位字段」已被实测否定），
    #: 本实现固定为 0，相位以**毫度相位字 0x218** 为准。
    phase_field: int = 0
    #: 写进 clk_out2 相位字（0x218）的值，单位毫度。
    phase_word_value: int = 0
    #: 相位量化网格（45/D 度，单位毫度）；相位不可控时为 None。
    phase_quantum_milli_deg: int | None = None
    #: 当前 bitstream 相位是否可控。
    phase_controllable: bool = True
    #: VCO 相位步长对应的时间（ps，= 1/8 VCO 周期）。
    phase_step_ps: Fraction = Fraction(0)
    #: 提示信息（频率/相位量化、回退语义等）。
    notes: tuple[str, ...] = ()
    #: 使用的相位字段语义来源。
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
            + f"，相位字 0x218={self.phase_word_value}（实际 {self.phase_actual_milli_deg / 1000:.3f}°）"
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
            ok=False, freq_hz=target, actual_freq_hz=0, error_milli_hz=0,
            phase_milli_deg=int(phase_milli_deg), phase_actual_milli_deg=0,
            vco_hz=0, mult=0, divclk=DIVCLK_DEFAULT, div=0, phase_field=0,
            phase_step_ps=Fraction(0), notes=("目标频率必须为正数",), semantics_source=sem.source,
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
            ok=False, freq_hz=target, actual_freq_hz=0, error_milli_hz=0,
            phase_milli_deg=int(phase_milli_deg), phase_actual_milli_deg=0,
            vco_hz=0, mult=0, divclk=DIVCLK_DEFAULT, div=0, phase_field=0,
            phase_step_ps=Fraction(0), notes=("约束内没有 (MULT, D) 组合",), semantics_source=sem.source,
        )

    _, vco, mult, div, divclk = best
    err = freq_error_exact(vco, div, target)
    actual_freq = _round_half_up(Fraction(vco, div))
    error_milli_hz = _round_half_up(err * 1000)
    step_ps = Fraction(10**12, 8 * vco)
    phase_word, phase_actual, phase_quantum, notes = phase_word_value(phase_milli_deg, div, vco, sem)

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
        phase_field=0,  # 历史字段，见 SigSolution 注释
        phase_word_value=phase_word,
        phase_quantum_milli_deg=phase_quantum,
        phase_controllable=(sem.mode != "none" and sem.phase_controllable),
        phase_step_ps=step_ps,
        notes=tuple(notes_list),
        semantics_source=sem.source,
    )


# ------------------------------------------------------------ DRP 配置字


@dataclass(frozen=True)
class DrpWords:
    """一次 `SET_SIG` 要写的 DRP 配置字（对齐 `docs/ps-protocol.md` 第 6.4 节）。

    - `clkfbout`（0x200）= `(MULT << 8) | DIVCLK`；低字节 = DIVCLK 本体；
    - `clkout0`（0x208）/ `clkout1`（0x214）：低字节 = 分频比 D 本体（实测 `divide_field_scale = 1`）；
    - `phase0_word`（0x20C）= clk_out1 相位，`phase1_word`（0x218）= clk_out2 相位，**单位毫度**；
    - `load_value`（写 0x25C）：bit0 = 触发重配置、bit1 = SADDR（1 = 动态寄存器文件）；
    - `status_off` = 0x004（bit0 = locked；字 31 / 0x27C 实测恒 0）。
    """

    clkfbout: int
    clkout0: int
    clkout1: int
    phase0_word: int = 0
    phase1_word: int = 0
    #: 相位字是否真的接到重建逻辑（当前 bitstream 为 False）。
    phase_word_wired: bool = False
    load_off: int = DRP_LOAD_OFF
    load_value: int = DRP_LOAD_BOTH
    status_off: int = DRP_STATUS_OFF
    status_locked: int = DRP_STATUS_LOCKED

    def as_pairs(self) -> tuple[tuple[int, int], ...]:
        """(偏移, 值) 序列，按 PS 侧写入顺序（含两个相位字与 LOAD）。"""
        return (
            (drp_word_off(DRP_IDX_CLKFBOUT), self.clkfbout),
            (drp_word_off(DRP_IDX_CLKOUT0), self.clkout0),
            (drp_word_off(DRP_IDX_CLKOUT1), self.clkout1),
            (drp_word_off(DRP_IDX_CLKOUT0_PHASE), self.phase0_word),
            (drp_word_off(DRP_IDX_CLKOUT1_PHASE), self.phase1_word),
            (self.load_off, self.load_value),
        )


def drp_words(sol: SigSolution, semantics: DrpSemantics | None = None) -> DrpWords:
    """按 `docs/ps-protocol.md` 第 6.4 节的写入流程组装 DRP 字。

    顺序：CLKFBOUT(0x200) → clk_out1 分频(0x208) → clk_out2 分频(0x214) →
    clk_out1 相位(0x20C = 0) → clk_out2 相位(0x218 = 毫度) → Load(0x25C = 0x07)。

    相位字是**独立的 32 位毫度字**（不是分频字的 bit[15:8]）；当前 bitstream 下它没接到
    重建逻辑（写无害、不生效），补上 `PHASE_DUTY_CONFIG` 后生效。
    """
    if not sol.ok:
        raise ValueError("求解失败（ok=False）时不能组装 DRP 配置字")
    sem = semantics or current_semantics()
    return DrpWords(
        clkfbout=divide_field(sol.divclk) | ((sol.mult & 0xFF) << 8),
        clkout0=divide_field(sol.div),
        clkout1=divide_field(sol.div),
        phase0_word=0,
        phase1_word=sol.phase_word_value & 0xFFFFFFFF,
        phase_word_wired=bool(sem.phase_word_wired),
    )


def decode_drp_words(clkfbout: int, clkout0: int, clkout1: int) -> dict[str, int]:
    """把分频字反解成字段（诊断/测试用；相位在独立相位字里，不在这里）。"""
    return {
        "mult": (int(clkfbout) >> 8) & 0xFF,
        "divclk": divide_field_get(clkfbout),
        "div0": divide_field_get(clkout0),
        "div1": divide_field_get(clkout1),
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


#: 冻结验证频点表（`docs/ps-protocol.md` 第 6 节）。注意：这张表是**人工挑选**的工作点，
#: 与 `solve_sig` 的冻结规则输出的 (VCO, MULT, D) 在 5MHz / 6.25MHz 上不一致（见 README
#: 「待澄清项」）；两者产生的 f_sig 精确相等，因此不影响 gateCnt/edgeCnt/phaseCnt 的期望值。
FROZEN_POINTS: tuple[FrozenPoint, ...] = (
    _frozen(5, 600, 12, 120),
    _frozen(6.25, 750, 15, 120),
    _frozen(8, 1000, 20, 125),
    _frozen(10, 1000, 20, 100),
    _frozen(12.5, 1000, 20, 80),
    _frozen(20, 1000, 20, 50),
    _frozen(25, 1000, 20, 40),
)

#: 冻结相位点（度），全部 < 180°。
FROZEN_PHASES_DEG: tuple[float, ...] = (0, 22.5, 45, 67.5, 90, 112.5, 135, 157.5)

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
    "DRP_IDX_CLKFBOUT",
    "DRP_IDX_CLKOUT0",
    "DRP_IDX_CLKOUT1",
    "DRP_IDX_CLKOUT0_PHASE",
    "DRP_IDX_CLKOUT1_PHASE",
    "DRP_IDX_LOAD",
    "DRP_IDX_STATUS",
    "DRP_LOAD_OFF",
    "DRP_LOAD_BOTH",
    "DRP_STATUS_OFF",
    "DRP_STATUS_LOCKED",
    "DRP_FRAC_EN",
    "drp_word_off",
    "divide_field",
    "divide_field_get",
    "phase_field_bits",
    "phase_field_get",
    "phase_supported",
    "phase_quantum_milli_deg",
    "phase_grid_milli_deg",
    "mmcm_phase_grid_milli_deg",
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
