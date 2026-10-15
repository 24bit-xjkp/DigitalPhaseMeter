"""测量换算：频率与相位的定点整数运算（与 C 侧 `ps/include/dpm_calc.h` 同一契约）。

公式与取整口径以 `docs/ps-protocol.md` 第 5 节为准（**整除截断**）：

.. code-block:: text

    T_win    = gateCnt / f_slow
    f_sig    = edgeCnt * f_slow / gateCnt                → freqHz（单位 Hz）
    phase_deg= 360 * phaseCnt * f_slow / (gateCnt * f_dsp) → phaseMilliDeg（单位毫度）

`gateCnt == 0` 或 `edgeCnt == 0` 时两个输出都是 0（不产生除零）。

**协议字段单位（冻结）**：`GET_MEAS`/`STREAM_DATA` 的 `freqMilliHz` 字段承载的是
**整数 Hz**（名字是历史遗留：真正的毫赫兹在 25MHz 时是 2.5e10，会溢出 u32），
25MHz/90° 时字段值 = `25000000`、`phaseMilliDeg` = `90000`。

**必须用等价的安全形式**（避免 64 位溢出，`docs/ps-protocol.md` 第 5 节）：

.. code-block:: text

    freqHz = (edgeCnt * f_slow) // gateCnt            # 中间量 ≤ 2.1e17
    c, r   = divmod(phaseCnt, gateCnt)
    v      = 360000 * c + (360000 * r) // gateCnt
    phaseMilliDeg = (v * (f_slow // 1000)) // (f_dsp // 1000)

本模块的 :func:`freq_hz` 与 :func:`phase_milli_deg` 就是这两条，与
`spec/gen_golden.py::fixed_point` 逐位一致（`host/tests/test_mathx.py` 用
`spec/golden/vectors.json` 逐位校验）。

> 记录：`spec/golden/vectors.json` 与 `spec/sim_server.py` 之前多除了一次 1000
> （5MHz 存成 5000），与文档第 5 节的字段单位表（Hz）不符；已上报 Lead 等待重新生成，
> 见 `host/README.md` 的「协议问题记录」。本模块按**文档字段单位表**实现 Hz。

除整数换算外，本模块还提供判据用辅助量：

- :func:`phase_cycles`：`phaseCnt / edgeCnt` 的精确有理数（主判据，与闸门时间无关）；
- :func:`phase_quant_milli_deg`：量化步长 `360 * f_sig / f_dsp`（1 个 DSP 周期对应的度数）；
- :func:`phase_err_deg` / :func:`freq_err_ppm`：与标称值的误差（显示用浮点）。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

#: 默认低速（AXI/闸门）时钟频率（Hz）。
SLOW_HZ_DEFAULT = 50_000_000
#: 默认高速（DSP/测量）时钟频率（Hz）。
DSP_HZ_DEFAULT = 400_000_000
#: 相位差满量程（1e-3 度）。
FULL_SCALE_MILLI_DEG = 360_000
#: `GET_MEAS.freqMilliHz` 字段承载的单位除数：字段值 = 频率 [Hz] / 该除数。
#: 文档第 5 节的字段单位表冻结为「整数 Hz」→ 除数 = 1。
#: （`spec/golden/vectors.json` 与 `spec/sim_server.py` 目前用了 1000，是待修的口径差异。）
FREQ_FIELD_DIVISOR = 1
#: 闸门计数器的位宽（`Controller(gateCntWidth = 28)`）。
GATE_CNT_WIDTH = 28
#: `GATE_TARGET` 的最大值（低 28 位有效）。
GATE_TARGET_MAX = (1 << GATE_CNT_WIDTH) - 1
#: 相位量化下界：1 个 DSP 周期（用于判据容限说明）。
PHASE_TOLERANCE_CYCLES = 1


def freq_hz(edge_cnt: int, gate_cnt: int, slow_hz: int = SLOW_HZ_DEFAULT) -> int:
    """被测信号频率，单位 **Hz**（整除截断；`gateCnt == 0` 或 `edgeCnt == 0` → 0）。

    这就是 `GET_MEAS.freqMilliHz` 字段要承载的值（见模块文档的字段单位说明），
    中间量 `edge_cnt * slow_hz ≤ 2.1e17`，不会溢出 64 位。
    """
    if gate_cnt <= 0 or edge_cnt <= 0:
        return 0
    return (int(edge_cnt) * int(slow_hz)) // int(gate_cnt)


def freq_field(edge_cnt: int, gate_cnt: int, slow_hz: int = SLOW_HZ_DEFAULT) -> int:
    """协议字段 `freqMilliHz` 的值（= :func:`freq_hz` / :data:`FREQ_FIELD_DIVISOR`）。"""
    return freq_hz(edge_cnt, gate_cnt, slow_hz) // FREQ_FIELD_DIVISOR


def freq_milli_hz_exact(edge_cnt: int, gate_cnt: int, slow_hz: int = SLOW_HZ_DEFAULT) -> int:
    """频率的毫赫兹精确值（仅用于高分辨率显示/报告，**不是**协议字段）。

    用 `freq_hz * 1000 + 余数` 的形式算，避免 `edge_cnt * slow_hz * 1000` 触到 2^64。
    """
    if gate_cnt <= 0 or edge_cnt <= 0:
        return 0
    whole = freq_hz(edge_cnt, gate_cnt, slow_hz)
    rem = (int(edge_cnt) * int(slow_hz)) % int(gate_cnt)
    return whole * 1000 + (rem * 1000) // int(gate_cnt)


def phase_milli_deg(
    phase_cnt: int,
    gate_cnt: int,
    edge_cnt: int,
    slow_hz: int = SLOW_HZ_DEFAULT,
    dsp_hz: int = DSP_HZ_DEFAULT,
) -> int:
    """相位差，单位毫度（整除截断；`gateCnt == 0` 或 `edgeCnt == 0` → 0）。

    与 `docs/ps-protocol.md` 第 5 节的安全形式逐位一致（也是
    `spec/gen_golden.py::fixed_point` 的实现）：

    .. code-block:: text

        c, r = divmod(phaseCnt, gateCnt)
        v = 360000 * c + (360000 * r) // gateCnt
        phaseMilliDeg = (v * (f_slow // 1000)) // (f_dsp // 1000)

    公式里 `edgeCnt` 被约掉，但它是否为 0 决定「源有没有边沿」，因此仍作为输入给出。
    要求 `f_slow` 与 `f_dsp` 都能被 1000 整除（默认 50MHz / 400MHz 满足）。
    """
    if gate_cnt <= 0 or edge_cnt <= 0:
        return 0
    if int(slow_hz) % 1000 or int(dsp_hz) % 1000:
        raise ValueError(f"f_slow/f_dsp 必须能被 1000 整除（当前 {slow_hz}/{dsp_hz}）")
    slow_k = int(slow_hz) // 1000
    dsp_k = int(dsp_hz) // 1000
    if slow_k <= 0 or dsp_k <= 0:
        raise ValueError(f"f_slow/f_dsp 必须为正数（当前 {slow_hz}/{dsp_hz}）")
    c, r = divmod(int(phase_cnt), int(gate_cnt))
    v = FULL_SCALE_MILLI_DEG * c + (FULL_SCALE_MILLI_DEG * r) // int(gate_cnt)
    return (v * slow_k) // dsp_k


def round_half_even(num: int, den: int) -> int:
    """整数 RNE（round-half-to-even，半数取偶），全程整数运算。

    用于「相位先被 DSP 采样网格量化」这一类**恰好半格**的场合：例如 8MHz/90° = 12.5 个
    DSP 周期、20MHz/45° = 2.5 个，用浮点会因二进制表示误差给出不可判定的结果。
    与 `spec/gen_golden.py` 的 `round_half_even` 逐位一致。
    """
    num, den = int(num), int(den)
    if den <= 0:
        raise ValueError(f"den 必须为正数，实际 {den}")
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and (q & 1)):
        q += 1
    return q


def round_half_even_frac(value: Fraction) -> int:
    """对非负 :class:`Fraction` 做 RNE（内部转成整数除法）。"""
    fr = Fraction(value)
    if fr < 0:
        raise ValueError(f"只支持非负值，实际 {value}")
    return round_half_even(fr.numerator, fr.denominator)


def phase_quant_milli_deg(freq_hz_value: int, dsp_hz: int = DSP_HZ_DEFAULT) -> int:
    """相位量化步长（1 个 DSP 周期）对应的度数，单位 1e-3 度（整数 RNE）。

    `360 * f_sig / f_dsp`：25MHz / 400MHz → 22.5°；10MHz → 9°。`freq_hz_value` 单位是 Hz。
    """
    if freq_hz_value <= 0 or dsp_hz <= 0:
        return 0
    return round_half_even(360_000 * int(freq_hz_value), int(dsp_hz))


def phase_quant_deg(freq_hz_value: int, dsp_hz: int = DSP_HZ_DEFAULT) -> int:
    """相位量化步长的**整度**近似（`ps/include/dpm_calc.h` 的 `phase_q_deg` 字段口径，整数 RNE）。"""
    q = phase_quant_milli_deg(freq_hz_value, dsp_hz)
    return round_half_even(q, 1000)


def phase_cycles(phase_cnt: int, edge_cnt: int) -> Fraction:
    """`phaseCnt / edgeCnt`（DSP 周期数，精确有理数）。`edgeCnt == 0` → 0。"""
    if edge_cnt <= 0:
        return Fraction(0)
    return Fraction(int(phase_cnt), int(edge_cnt))


def phase_cycles_float(phase_cnt: int, edge_cnt: int) -> float:
    """`phaseCnt / edgeCnt` 的浮点值（仅显示/绘图用，判据请用 :func:`phase_cycles`）。"""
    if edge_cnt <= 0:
        return 0.0
    return int(phase_cnt) / int(edge_cnt)


def signal_period_100ps(freq_hz_value: int) -> int:
    """由频率（Hz）推算信号周期，单位 0.1ns（1e-10 s）；频率为 0 时返回 0。"""
    if freq_hz_value <= 0:
        return 0
    return 10**10 // int(freq_hz_value)


def normalize_phase_milli_deg(milli_deg: int) -> int:
    """把相位差归一化到 `[0, 360000)` 毫度。"""
    return int(milli_deg) % FULL_SCALE_MILLI_DEG


def phase_err_deg(measured_milli_deg: int, nominal_deg: float) -> float:
    """测量相位与标称相位的误差（度），把差值折算到 `(-180, 180]`。

    `err = measured - nominal`：正负表示测量值偏大/偏小。
    """
    err = measured_milli_deg / 1000.0 - float(nominal_deg)
    err = (err + 180.0) % 360.0 - 180.0
    # `%` 的结果落在 [-180, 180)，把恰好 -180 归到 +180，与 (-180, 180] 一致
    if err <= -180.0:
        err += 360.0
    return err


def freq_err_ppm(measured_hz: float, nominal_hz: float) -> float:
    """频率相对误差（ppm）。标称频率为 0 时返回 0。"""
    if nominal_hz == 0:
        return 0.0
    return (float(measured_hz) - float(nominal_hz)) / float(nominal_hz) * 1e6


def freq_tolerance_ratio(edge_cnt: int) -> float:
    """频率相对误差量级 `2 / edgeCnt`（见 `.agents/skills/dpm-measurement-model`）。"""
    if edge_cnt <= 0:
        return 1.0
    return 2.0 / float(edge_cnt)


def phase_tolerance_milli_deg(freq_hz_value: int, dsp_hz: int = DSP_HZ_DEFAULT) -> int:
    """相位的量化下界（1 个 DSP 周期对应的度数，单位 1e-3 度，向上取整）。"""
    q = phase_quant_milli_deg(freq_hz_value, dsp_hz)
    return q if q % 1000 == 0 else q + 1000


@dataclass(frozen=True)
class Meas:
    """`dpm_meas_t` 的 Python 对应（定点整数）。

    `freq_hz` 就是 `GET_MEAS.freqMilliHz` 字段要承载的值（整数 Hz，见模块文档）；
    `freq_milli_hz_exact` 是高分辨率辅助量，**不是**协议字段。
    """

    #: 被测信号频率，单位 **Hz**（协议字段口径）。
    freq_hz: int
    #: 相位差，单位 1e-3 度，范围 [0, 360000]。
    phase_milli_deg: int
    #: 相位量化步长（1 个 DSP 周期对应的度数，四舍五入）。
    phase_q_deg: int
    #: `phaseCnt / edgeCnt` 的整数部分（DSP 周期数）。
    phase_cycles: int
    #: `phaseCnt % edgeCnt`（判断量化残差用）。
    phase_cycles_rem: int
    #: `phase_cycles_rem` 的分母（`edgeCnt`）。
    phase_cycles_den: int = 1
    #: 频率的毫赫兹精确值（仅显示/报告用）。
    freq_milli_hz_exact: int = 0

    @property
    def freq_mhz(self) -> float:
        """频率的 MHz 浮点值（仅显示）。"""
        return self.freq_hz / 1e6

    @property
    def phase_deg(self) -> float:
        return self.phase_milli_deg / 1000.0

    @property
    def phase_cycles_exact(self) -> Fraction:
        """`phaseCnt / edgeCnt` 的精确值（由整数部分与余数还原）。"""
        if self.phase_cycles == 0 and self.phase_cycles_rem == 0:
            return Fraction(0)
        return Fraction(self.phase_cycles) + Fraction(self.phase_cycles_rem, max(1, self.phase_cycles_den))


def meas_calc(
    gate_cnt: int,
    edge_cnt: int,
    phase_cnt: int,
    slow_hz: int = SLOW_HZ_DEFAULT,
    dsp_hz: int = DSP_HZ_DEFAULT,
) -> Meas:
    """一次测量的完整换算（与 `dpm_meas_calc` 同契约，单位按协议字段表：频率 Hz、相位毫度）。"""
    f_hz = freq_hz(edge_cnt, gate_cnt, slow_hz)
    pmdeg = phase_milli_deg(phase_cnt, gate_cnt, edge_cnt, slow_hz, dsp_hz)
    den = int(edge_cnt) if edge_cnt > 0 else 1
    return Meas(
        freq_hz=f_hz,
        phase_milli_deg=pmdeg,
        phase_q_deg=phase_quant_deg(f_hz, dsp_hz),
        phase_cycles=int(phase_cnt) // den if edge_cnt > 0 else 0,
        phase_cycles_rem=int(phase_cnt) % den if edge_cnt > 0 else 0,
        phase_cycles_den=den,
        freq_milli_hz_exact=freq_milli_hz_exact(edge_cnt, gate_cnt, slow_hz),
    )


# ------------------------------------------------------------ 显示格式化


def format_freq_hz(value_hz: float) -> str:
    """把频率（Hz）格式化成便于阅读的字符串（自动选单位）。"""
    v = float(value_hz)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e9:
        return f"{sign}{a / 1e9:.6f} GHz"
    if a >= 1e6:
        return f"{sign}{a / 1e6:.6f} MHz"
    if a >= 1e3:
        return f"{sign}{a / 1e3:.3f} kHz"
    return f"{sign}{a:.3f} Hz"


def format_freq_milli_hz(value_milli_hz: int) -> str:
    """把 1e-3 Hz 整数格式化成便于阅读的频率字符串（自动选单位）。"""
    v = int(value_milli_hz)
    sign = "-" if v < 0 else ""
    millis = abs(v)
    hz = millis / 1000.0
    if hz >= 1e9:
        return f"{sign}{hz / 1e9:.6f} GHz"
    if hz >= 1e6:
        return f"{sign}{hz / 1e6:.6f} MHz"
    if hz >= 1e3:
        return f"{sign}{hz / 1e3:.6f} kHz"
    if hz >= 1.0:
        return f"{sign}{hz:.3f} Hz"
    return f"{sign}{millis} mHz"


def format_milli_deg(value_milli_deg: int) -> str:
    """把 1e-3 度整数格式化成 ``90.000°``。"""
    return f"{int(value_milli_deg) / 1000.0:.3f}°"


def format_hz(value_hz: float) -> str:
    """把 Hz 浮点格式化成带单位字符串（CLI/GUI 输入回显用）。"""
    v = float(value_hz)
    if v >= 1e9:
        return f"{v / 1e9:.6f} GHz"
    if v >= 1e6:
        return f"{v / 1e6:.6f} MHz"
    if v >= 1e3:
        return f"{v / 1e3:.6f} kHz"
    return f"{v:.3f} Hz"


def clamp_gate_target(gate_target: int) -> int:
    """把闸门目标限制在 28 位有效范围内（`GATE_TARGET` 只有低 28 位有效）。"""
    return max(0, min(int(gate_target), GATE_TARGET_MAX))


__all__ = [
    "round_half_even",
    "round_half_even_frac",
    "FREQ_FIELD_DIVISOR",
    "SLOW_HZ_DEFAULT",
    "DSP_HZ_DEFAULT",
    "FULL_SCALE_MILLI_DEG",
    "GATE_CNT_WIDTH",
    "GATE_TARGET_MAX",
    "PHASE_TOLERANCE_CYCLES",
    "Meas",
    "freq_hz",
    "freq_field",
    "freq_milli_hz_exact",
    "phase_milli_deg",
    "phase_quant_milli_deg",
    "phase_quant_deg",
    "phase_cycles",
    "phase_cycles_float",
    "signal_period_100ps",
    "normalize_phase_milli_deg",
    "phase_err_deg",
    "freq_err_ppm",
    "freq_tolerance_ratio",
    "phase_tolerance_milli_deg",
    "meas_calc",
    "format_freq_hz",
    "format_freq_milli_hz",
    "format_milli_deg",
    "format_hz",
    "clamp_gate_target",
]
