"""定点换算的测试：与 `spec/golden/vectors.json` 逐位一致，并自带自算向量兜底。

运行：
    仓库根目录：`uv run pytest host/tests/test_mathx.py -q`
    host 目录： `uv run pytest tests/test_mathx.py -q`
"""

from __future__ import annotations

import json
import os
import random
from fractions import Fraction

import pytest
from dpm_host import mathx as M

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(HOST_DIR)
GOLDEN_PATH = os.path.join(REPO_ROOT, "spec", "golden", "vectors.json")
SLOW = 50_000_000
DSP = 450_000_000


def load_golden() -> tuple[list[dict], str]:
    """读 `spec/golden/vectors.json`；文件不存在时用**本测试自算**的向量（并标注来源）。"""
    if os.path.exists(GOLDEN_PATH):
        with open(GOLDEN_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        vectors = data.get("vectors") or []
        if vectors:
            return vectors, GOLDEN_PATH
    return _self_computed_vectors(), "自算向量（spec/golden/vectors.json 不存在）"


def _self_computed_vectors() -> list[dict]:
    """独立参考实现（用 Fraction 精确算，不与 mathx 共用代码），覆盖 8 组以上。"""
    vectors = []
    freqs = (
        5_000_000,
        6_000_000,
        9_000_000,
        10_000_000,
        15_000_000,
        18_000_000,
        25_000_000,
        30_000_000,
        45_000_000,
        50_000_000,
    )
    for freq in freqs:
        gate = 2000 + -(-SLOW // freq)  # ceil(T_sig/T_slow)
        edge = (gate * freq) // SLOW
        for phase_mdeg in (0, 45_000, 90_000, 135_000):
            dsp_cycles = int((Fraction(phase_mdeg, 360_000) * Fraction(SLOW, freq) * DSP).__round__()) if edge else 0
            phase_cnt = dsp_cycles * edge
            if gate and edge:
                freq_hz = (edge * SLOW) // gate
                phase = int(Fraction(360_000) * Fraction(phase_cnt * SLOW, gate * DSP))
            else:
                freq_hz, phase = 0, 0
            vectors.append(
                {
                    "freq_hz": freq,
                    "gate_cnt": gate,
                    "edge_cnt": edge,
                    "phase_cnt": phase_cnt,
                    "phase_requested_mdeg": phase_mdeg,
                    "freq_milli_hz": freq_hz,
                    "phase_milli_deg": phase,
                }
            )
    return vectors


#: 金标准向量在**收集阶段**读一次：`parametrize` 需要它在（本仓库的 spec 文件提供 28 组）。
VECTORS, VECTOR_SOURCE = load_golden()
_VECTOR_IDS = [f"{vec['freq_hz']}Hz-{vec['phase_requested_mdeg']}mdeg" for vec in VECTORS]


@pytest.fixture(scope="session", autouse=True)
def _report_vector_source(request):
    """保留原来 `setUpClass` 的那条信息输出：金标准向量来源与组数（直接写终端，不受 capture 影响）。"""
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"[test_mathx] 金标准向量来源：{VECTOR_SOURCE}（{len(VECTORS)} 组）")
    return VECTOR_SOURCE


# ------------------------------------------------------------------ 金标准向量逐位比对


def test_vector_count_at_least_eight() -> None:
    """向量组数下界：少于 8 组说明金标准文件被削弱了。"""
    assert len(VECTORS) >= 8


@pytest.mark.parametrize("vec", VECTORS, ids=_VECTOR_IDS)
def test_golden_bit_exact(vec: dict) -> None:
    """`freqMilliHz`（= Hz）与 `phaseMilliDeg` 都必须逐位一致。"""
    freq = M.freq_field(vec["edge_cnt"], vec["gate_cnt"], SLOW)
    phase = M.phase_milli_deg(vec["phase_cnt"], vec["gate_cnt"], vec["edge_cnt"], SLOW, DSP)
    assert freq == vec["freq_milli_hz"]
    assert phase == vec["phase_milli_deg"]


@pytest.mark.parametrize("vec", VECTORS, ids=_VECTOR_IDS)
def test_meas_calc_matches_golden(vec: dict) -> None:
    """`meas_calc` 一次算出的频率/相位同样逐位一致。"""
    calc = M.meas_calc(vec["gate_cnt"], vec["edge_cnt"], vec["phase_cnt"], SLOW, DSP)
    assert calc.freq_hz == vec["freq_milli_hz"]
    assert calc.phase_milli_deg == vec["phase_milli_deg"]


@pytest.mark.parametrize("vec", VECTORS, ids=_VECTOR_IDS)
def test_phase_cycles_are_integral(vec: dict) -> None:
    """`phaseCnt / edgeCnt` 必须是整数个 DSP 周期（相位量化下界，不是缺陷）。"""
    assert vec["phase_cnt"] % vec["edge_cnt"] == 0
    cycles = M.phase_cycles(vec["phase_cnt"], vec["edge_cnt"])
    assert cycles.denominator == 1


# ------------------------------------------------------------------ 安全形式与朴素公式的等价性、边界与辅助量


def test_safe_form_equals_naive_formula() -> None:
    """随机抽样验证「整除分解」形式 == 朴素公式（避免 64 位溢出用的等价改写）。"""
    rng = random.Random(20261015)
    for _ in range(500):
        gate = rng.randint(1, 2**28 - 1)
        edge = rng.randint(1, 2**32 - 1)
        phase = rng.randint(0, 2**32 - 1)
        naive_phase = (360_000 * phase * SLOW) // (gate * DSP)
        assert M.phase_milli_deg(phase, gate, edge, SLOW, DSP) == naive_phase
        naive_freq = (edge * SLOW) // gate
        assert M.freq_hz(edge, gate, SLOW) == naive_freq


def test_zero_gate_or_edge() -> None:
    """零闸门/零边沿：除零路径一律返回 0，不抛异常。"""
    assert M.freq_hz(0, 2002, SLOW) == 0
    assert M.freq_hz(1001, 0, SLOW) == 0
    assert M.phase_milli_deg(4004, 0, 1001, SLOW, DSP) == 0
    assert M.phase_milli_deg(4004, 2002, 0, SLOW, DSP) == 0
    calc = M.meas_calc(0, 0, 0)
    assert (calc.freq_hz, calc.phase_milli_deg) == (0, 0)


def test_custom_clocks() -> None:
    """自定义时钟（f_slow/f_dsp 都是 1kHz 的整数倍）也要能被整除分解形式支持。"""
    freq = M.freq_hz(edge_cnt=200, gate_cnt=2000, slow_hz=100_000_000)
    assert freq == 10_000_000
    phase = M.phase_milli_deg(phase_cnt=1000, gate_cnt=2000, edge_cnt=200, slow_hz=100_000_000, dsp_hz=200_000_000)
    assert phase == (360_000 * 1000 * 100_000_000) // (2000 * 200_000_000)
    with pytest.raises(ValueError):
        M.phase_milli_deg(1, 1, 1, 50_000_001, DSP)  # 不能被 1000 整除


def test_freq_milli_hz_exact() -> None:
    # 8MHz 的窗口边界不整除：整数 Hz 与精确毫赫兹不同
    assert M.freq_hz(321, 2007, SLOW) == 7_997_010
    assert M.freq_milli_hz_exact(321, 2007, SLOW) == 7_997_010_463
    assert M.freq_milli_hz_exact(0, 2000, SLOW) == 0


def test_quantization_step() -> None:
    assert M.phase_quant_milli_deg(25_000_000, DSP) == 20_000  # 20.0°
    assert M.phase_quant_milli_deg(10_000_000, DSP) == 8_000  # 8.0°
    assert M.phase_quant_milli_deg(6_000_000, DSP) == 4_800  # 4.8°
    assert M.phase_quant_deg(25_000_000, DSP) == 20
    assert M.phase_quant_milli_deg(0, DSP) == 0


def test_round_half_even() -> None:
    assert M.round_half_even(5, 2) == 2  # 2.5 → 2
    assert M.round_half_even(7, 2) == 4  # 3.5 → 4
    assert M.round_half_even(25, 2) == 12  # 12.5 → 12
    assert M.round_half_even(23, 2) == 12  # 11.5 → 12
    assert M.round_half_even_frac(Fraction(25, 2)) == 12
    with pytest.raises(ValueError):
        M.round_half_even_frac(Fraction(-1, 2))


def test_normalize_and_errors() -> None:
    assert M.normalize_phase_milli_deg(370_000) == 10_000
    assert M.normalize_phase_milli_deg(-10_000) == 350_000
    assert M.phase_err_deg(90_500, 90.0) == pytest.approx(0.5, abs=1e-7)
    assert M.phase_err_deg(0, 359.5) == pytest.approx(0.5, abs=1e-7)
    assert M.freq_err_ppm(10_000_010, 10_000_000) == 1.0
    assert M.freq_err_ppm(0, 0) == 0.0
    assert M.freq_tolerance_ratio(2000) == pytest.approx(0.001, abs=1e-7)
    assert M.freq_tolerance_ratio(0) == 1.0


def test_signal_period() -> None:
    # 25MHz → 40ns = 400 × 1e-10 s
    assert M.signal_period_100ps(25_000_000) == 400
    assert M.signal_period_100ps(0) == 0


def test_formatters() -> None:
    assert M.format_freq_hz(25_000_000) == "25.000000 MHz"
    assert M.format_freq_hz(1_500) == "1.500 kHz"
    assert M.format_milli_deg(90_000) == "90.000°"
    assert M.format_freq_milli_hz(25_000_000_000) == "25.000000 MHz"


def test_clamp_gate_target() -> None:
    assert M.clamp_gate_target(-5) == 0
    assert M.clamp_gate_target(M.GATE_TARGET_MAX + 10) == M.GATE_TARGET_MAX
