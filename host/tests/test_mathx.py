"""定点换算的单元测试：与 `spec/golden/vectors.json` 逐位一致，并自带自算向量兜底。

运行：`cd host && python3 -m unittest tests.test_mathx -v`
"""

from __future__ import annotations

import json
import os
import random
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpm_host import mathx as M  # noqa: E402

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(HOST_DIR)
GOLDEN_PATH = os.path.join(REPO_ROOT, "spec", "golden", "vectors.json")
SLOW = 50_000_000
DSP = 400_000_000


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
    freqs = (5_000_000, 6_250_000, 8_000_000, 10_000_000, 12_500_000, 20_000_000, 25_000_000)
    for freq in freqs:
        gate = 2000 + -(-SLOW // freq)  # ceil(T_sig/T_slow)
        edge = (gate * freq) // SLOW
        for phase_mdeg in (0, 45_000, 90_000, 135_000):
            dsp_cycles = int(
                (Fraction(phase_mdeg, 360_000) * Fraction(SLOW, freq) * DSP).__round__()
            ) if edge else 0
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


class GoldenVectorTests(unittest.TestCase):
    """金标准向量逐位比对（≥8 组；本仓库的 spec 文件提供 28 组）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.vectors, cls.source = load_golden()
        print(f"\n[test_mathx] 金标准向量来源：{cls.source}（{len(cls.vectors)} 组）")

    def test_vector_count_at_least_eight(self) -> None:
        self.assertGreaterEqual(len(self.vectors), 8)

    def test_golden_bit_exact(self) -> None:
        """`freqMilliHz`（= Hz）与 `phaseMilliDeg` 都必须逐位一致。"""
        for vec in self.vectors:
            with self.subTest(freq=vec["freq_hz"], phase=vec["phase_requested_mdeg"]):
                freq = M.freq_field(vec["edge_cnt"], vec["gate_cnt"], SLOW)
                phase = M.phase_milli_deg(vec["phase_cnt"], vec["gate_cnt"], vec["edge_cnt"], SLOW, DSP)
                self.assertEqual(freq, vec["freq_milli_hz"])
                self.assertEqual(phase, vec["phase_milli_deg"])

    def test_meas_calc_matches_golden(self) -> None:
        for vec in self.vectors:
            calc = M.meas_calc(vec["gate_cnt"], vec["edge_cnt"], vec["phase_cnt"], SLOW, DSP)
            self.assertEqual(calc.freq_hz, vec["freq_milli_hz"])
            self.assertEqual(calc.phase_milli_deg, vec["phase_milli_deg"])

    def test_phase_cycles_are_integral(self) -> None:
        """`phaseCnt / edgeCnt` 必须是整数个 DSP 周期（相位量化下界，不是缺陷）。"""
        for vec in self.vectors:
            self.assertEqual(vec["phase_cnt"] % vec["edge_cnt"], 0)
            cycles = M.phase_cycles(vec["phase_cnt"], vec["edge_cnt"])
            self.assertEqual(cycles.denominator, 1)


class FixedPointTests(unittest.TestCase):
    """安全形式与朴素公式的等价性、边界与辅助量。"""

    def test_safe_form_equals_naive_formula(self) -> None:
        """随机抽样验证「整除分解」形式 == 朴素公式（避免 64 位溢出用的等价改写）。"""
        rng = random.Random(20261015)
        for _ in range(500):
            gate = rng.randint(1, 2**28 - 1)
            edge = rng.randint(1, 2**32 - 1)
            phase = rng.randint(0, 2**32 - 1)
            naive_phase = (360_000 * phase * SLOW) // (gate * DSP)
            self.assertEqual(M.phase_milli_deg(phase, gate, edge, SLOW, DSP), naive_phase)
            naive_freq = (edge * SLOW) // gate
            self.assertEqual(M.freq_hz(edge, gate, SLOW), naive_freq)

    def test_zero_gate_or_edge(self) -> None:
        self.assertEqual(M.freq_hz(0, 2002, SLOW), 0)
        self.assertEqual(M.freq_hz(1001, 0, SLOW), 0)
        self.assertEqual(M.phase_milli_deg(4004, 0, 1001, SLOW, DSP), 0)
        self.assertEqual(M.phase_milli_deg(4004, 2002, 0, SLOW, DSP), 0)
        calc = M.meas_calc(0, 0, 0)
        self.assertEqual((calc.freq_hz, calc.phase_milli_deg), (0, 0))

    def test_custom_clocks(self) -> None:
        """自定义时钟（f_slow/f_dsp 都是 1kHz 的整数倍）也要能被整除分解形式支持。"""
        freq = M.freq_hz(edge_cnt=200, gate_cnt=2000, slow_hz=100_000_000)
        self.assertEqual(freq, 10_000_000)
        phase = M.phase_milli_deg(phase_cnt=1000, gate_cnt=2000, edge_cnt=200, slow_hz=100_000_000, dsp_hz=200_000_000)
        self.assertEqual(phase, (360_000 * 1000 * 100_000_000) // (2000 * 200_000_000))
        with self.assertRaises(ValueError):
            M.phase_milli_deg(1, 1, 1, 50_000_001, DSP)  # 不能被 1000 整除

    def test_freq_milli_hz_exact(self) -> None:
        # 8MHz 的窗口边界不整除：整数 Hz 与精确毫赫兹不同
        self.assertEqual(M.freq_hz(321, 2007, SLOW), 7_997_010)
        self.assertEqual(M.freq_milli_hz_exact(321, 2007, SLOW), 7_997_010_463)
        self.assertEqual(M.freq_milli_hz_exact(0, 2000, SLOW), 0)

    def test_quantization_step(self) -> None:
        self.assertEqual(M.phase_quant_milli_deg(25_000_000, DSP), 22_500)
        self.assertEqual(M.phase_quant_milli_deg(10_000_000, DSP), 9_000)
        self.assertEqual(M.phase_quant_deg(25_000_000, DSP), 22)  # 22.5 → RNE → 22
        self.assertEqual(M.phase_quant_milli_deg(0, DSP), 0)

    def test_round_half_even(self) -> None:
        self.assertEqual(M.round_half_even(5, 2), 2)  # 2.5 → 2
        self.assertEqual(M.round_half_even(7, 2), 4)  # 3.5 → 4
        self.assertEqual(M.round_half_even(25, 2), 12)  # 12.5 → 12
        self.assertEqual(M.round_half_even(23, 2), 12)  # 11.5 → 12
        self.assertEqual(M.round_half_even_frac(Fraction(25, 2)), 12)
        with self.assertRaises(ValueError):
            M.round_half_even_frac(Fraction(-1, 2))

    def test_normalize_and_errors(self) -> None:
        self.assertEqual(M.normalize_phase_milli_deg(370_000), 10_000)
        self.assertEqual(M.normalize_phase_milli_deg(-10_000), 350_000)
        self.assertAlmostEqual(M.phase_err_deg(90_500, 90.0), 0.5)
        self.assertAlmostEqual(M.phase_err_deg(0, 359.5), 0.5)
        self.assertEqual(M.freq_err_ppm(10_000_010, 10_000_000), 1.0)
        self.assertEqual(M.freq_err_ppm(0, 0), 0.0)
        self.assertAlmostEqual(M.freq_tolerance_ratio(2000), 0.001)
        self.assertEqual(M.freq_tolerance_ratio(0), 1.0)

    def test_signal_period(self) -> None:
        # 25MHz → 40ns = 400 × 1e-10 s
        self.assertEqual(M.signal_period_100ps(25_000_000), 400)
        self.assertEqual(M.signal_period_100ps(0), 0)

    def test_formatters(self) -> None:
        self.assertEqual(M.format_freq_hz(25_000_000), "25.000000 MHz")
        self.assertEqual(M.format_freq_hz(1_500), "1.500 kHz")
        self.assertEqual(M.format_milli_deg(90_000), "90.000°")
        self.assertEqual(M.format_freq_milli_hz(25_000_000_000), "25.000000 MHz")

    def test_clamp_gate_target(self) -> None:
        self.assertEqual(M.clamp_gate_target(-5), 0)
        self.assertEqual(M.clamp_gate_target(M.GATE_TARGET_MAX + 10), M.GATE_TARGET_MAX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
