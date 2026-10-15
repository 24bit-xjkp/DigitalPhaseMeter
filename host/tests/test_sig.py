"""片内信号源（MMCM 求解 + DRP 字 + 相位字语义）的单元测试。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpm_host import sig as S  # noqa: E402

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(HOST_DIR)
SEMANTICS_PATH = os.path.join(REPO_ROOT, "spec", "drp_semantics.json")

#: 冻结表（`docs/ps-protocol.md` 第 6.3 节 / `spec/golden/vectors.json`）
FROZEN_TRIPLES = {
    5_000_000: (600_000_000, 12, 120),
    6_250_000: (750_000_000, 15, 120),
    8_000_000: (1_000_000_000, 20, 125),
    10_000_000: (1_000_000_000, 20, 100),
    12_500_000: (1_000_000_000, 20, 80),
    20_000_000: (1_000_000_000, 20, 50),
    25_000_000: (1_000_000_000, 20, 40),
}


class SolverTests(unittest.TestCase):
    """冻结求解规则（三处实现必须一致）。"""

    def test_frozen_triples_bit_exact(self) -> None:
        for freq, (vco, mult, div) in FROZEN_TRIPLES.items():
            with self.subTest(freq=freq):
                sol = S.solve_sig(freq, 90_000)
                self.assertTrue(sol.ok)
                self.assertEqual((sol.vco_hz, sol.mult, sol.div), (vco, mult, div))
                self.assertEqual(sol.divclk, 1)
                self.assertEqual(sol.error_milli_hz, 0)
                self.assertTrue(sol.exact)

    def test_frozen_table_self_check(self) -> None:
        self.assertEqual(S.verify_frozen_table(), [])

    def test_frozen_point_helper(self) -> None:
        point = S.frozen_point(25_000_000)
        assert point is not None
        self.assertEqual((point.vco_hz, point.mult, point.div), (1_000_000_000, 20, 40))
        self.assertEqual(point.divide_field, 40)
        self.assertEqual(float(point.phase_step_ps), 125.0)
        self.assertIsNone(S.frozen_point(7_000_000))

    def test_constraints_are_satisfied(self) -> None:
        for freq in (5_000_000, 7_000_000, 30_000_000, 123_456_789):
            sol = S.solve_sig(freq, 0)
            self.assertTrue(sol.ok)
            self.assertGreaterEqual(sol.vco_hz, S.VCO_MIN_HZ)
            self.assertLessEqual(sol.vco_hz, S.VCO_MAX_HZ)
            self.assertIn(sol.mult, range(S.MULT_MIN, S.MULT_MAX + 1))
            self.assertIn(sol.div, range(S.DIV_MIN, S.DIV_MAX + 1))
            self.assertEqual(sol.vco_hz, 50_000_000 * sol.mult)
            self.assertEqual(sol.divide_field, sol.div)

    def test_unsolvable_and_out_of_range(self) -> None:
        self.assertFalse(S.solve_sig(0, 0).ok)
        self.assertFalse(S.solve_sig(-1, 0).ok)
        low = S.solve_sig(1_000_000, 0)
        self.assertTrue(low.ok)
        self.assertEqual(low.div, S.DIV_MAX)  # 最低可实现频率：600MHz / 127
        self.assertGreater(low.error_milli_hz, 0)
        self.assertFalse(low.exact)
        self.assertTrue(any("不能精确实现" in note for note in low.notes))

    def test_exact_solution_error_is_zero(self) -> None:
        for freq in FROZEN_TRIPLES:
            self.assertEqual(S.solve_sig(freq, 0).error_milli_hz, 0)


class DrpWordTests(unittest.TestCase):
    """DRP 写入流程（docs/ps-protocol.md 第 6.4 节）。"""

    def test_words_follow_doc(self) -> None:
        sol = S.solve_sig(25_000_000, 90_000)
        words = S.drp_words(sol)
        self.assertEqual(words.clkfbout, (20 << 8) | 1)
        self.assertEqual(words.clkout0, 40)
        self.assertEqual(words.clkout1, 40)
        self.assertEqual(words.phase0_word, 0)
        self.assertEqual(words.phase1_word, 90_000)
        self.assertEqual(words.load_off, S.DRP_LOAD_OFF)
        self.assertEqual(words.load_value, 0x07)
        self.assertEqual(words.status_off, 0x004)

    def test_write_order_and_offsets(self) -> None:
        words = S.drp_words(S.solve_sig(10_000_000, 22_500))
        pairs = dict(words.as_pairs())
        self.assertEqual(pairs[0x200], (20 << 8) | 1)
        self.assertEqual(pairs[0x208], 100)
        self.assertEqual(pairs[0x214], 100)
        self.assertEqual(pairs[0x20C], 0)
        self.assertEqual(pairs[0x218], 22_500)
        self.assertEqual(pairs[0x25C], 0x07)
        self.assertEqual([off for off, _v in words.as_pairs()], [0x200, 0x208, 0x214, 0x20C, 0x218, 0x25C])

    def test_field_encodings(self) -> None:
        self.assertEqual(S.divide_field(127), 127)
        self.assertEqual(S.divide_field_get(0x28), 40)
        self.assertEqual(S.phase_field_bits(2), 0x200)
        self.assertEqual(S.phase_field_get(0x5A28), 0x5A)
        self.assertEqual(S.drp_word_off(0), 0x200)
        self.assertEqual(S.drp_word_off(31), 0x27C)
        self.assertEqual(S.DRP_STATUS_OFF, 0x004)
        self.assertEqual(S.DRP_STATUS_LEGACY_OFF, 0x27C)

    def test_decode_words(self) -> None:
        words = S.drp_words(S.solve_sig(6_250_000, 0))
        fields = S.decode_drp_words(words.clkfbout, words.clkout0, words.clkout1)
        self.assertEqual(fields["mult"], 15)
        self.assertEqual(fields["divclk"], 1)
        self.assertEqual(fields["div0"], 120)
        self.assertEqual(fields["div1"], 120)

    def test_drp_words_requires_solution(self) -> None:
        with self.assertRaises(ValueError):
            S.drp_words(S.solve_sig(0, 0))


class SemanticsTests(unittest.TestCase):
    """`spec/drp_semantics.json` 的解析优先级、容错与 reload。"""

    def tearDown(self) -> None:
        S.reset_semantics_cache()

    def test_fallback_is_candidate_b(self) -> None:
        sem = S.load_semantics("/nonexistent/path/drp_semantics.json", force=True)
        self.assertTrue(sem.is_fallback)
        self.assertEqual(sem.mode, "deg_unit")
        self.assertEqual(sem.phase_unit_deg_milli, 45_000)
        self.assertTrue(sem.phase_controllable)

    def test_key_priority_deg_unit_first(self) -> None:
        sem = S.parse_semantics_json(
            {"phase_unit_deg_milli": 45_000, "phase_unit_ps_per_lsb": 125.0, "phase_field_is_degrees": True},
            "x.json",
        )
        self.assertEqual(sem.mode, "deg_unit")
        self.assertEqual(sem.keys, ("phase_unit_deg_milli",))

    def test_ps_per_lsb_second(self) -> None:
        sem = S.parse_semantics_json({"phase_unit_ps_per_lsb": 208.3, "phase_field_is_degrees": True}, "x.json")
        self.assertEqual(sem.mode, "ps_per_lsb")
        self.assertEqual(float(sem.phase_unit_ps_per_lsb), 208.3)

    def test_degrees_third(self) -> None:
        sem = S.parse_semantics_json({"phase_field_is_degrees": True}, "x.json")
        self.assertEqual(sem.mode, "degrees")
        self.assertEqual(sem.phase_unit_deg_milli, 1000)

    def test_zero_units_do_not_crash(self) -> None:
        """实测文件里 `phase_unit_deg_milli: 0`：不能当成「单位是 0」报错。"""
        sem = S.parse_semantics_json(
            {"phase_controllable": False, "phase_unit_deg_milli": 0, "phase_unit_ps_per_lsb": 0}, "x.json"
        )
        self.assertEqual(sem.mode, "none")
        self.assertFalse(sem.phase_controllable)
        self.assertEqual(sem.phase_static_milli_deg, 90_000)

    def test_phase_word_index(self) -> None:
        sem = S.parse_semantics_json(
            {"phase_controllable": True, "phase_unit_deg_milli": 1000, "phase_word_index": {"wired": True, "clkout1": 6}},
            "x.json",
        )
        self.assertTrue(sem.phase_word_wired)
        self.assertEqual(sem.phase_word_off, 0x218)

    def test_reload_reads_file_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "drp_semantics.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"phase_controllable": False, "phase_static_deg_milli": 45_000}, fh)
            sem = S.load_semantics(path, force=True)
            self.assertEqual(sem.mode, "none")
            self.assertEqual(sem.phase_static_milli_deg, 45_000)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"phase_controllable": True, "phase_unit_deg_milli": 45_000}, fh)
            again = S.reload_semantics(path)
            self.assertTrue(again.phase_controllable)
            self.assertEqual(again.mode, "deg_unit")

    def test_bad_json_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            with self.assertRaises(ValueError):
                S.load_semantics(path, force=True)

    @unittest.skipUnless(os.path.exists(SEMANTICS_PATH), "spec/drp_semantics.json 不存在")
    def test_real_spec_file(self) -> None:
        """仓库里的实测结论：bitstream 打开 PHASE_DUTY_CONFIG 后相位可在线改
        （上板实测见 docs/onboard-ax7010.md 第 3 节）。"""
        sem = S.load_semantics(SEMANTICS_PATH, force=True)
        self.assertTrue(sem.phase_controllable)
        self.assertEqual(sem.mode, "deg_unit")
        supported, _reason = S.phase_supported(90_000, sem)
        self.assertTrue(supported)
        supported, _reason = S.phase_supported(45_000, sem)
        self.assertTrue(supported)


class PhaseGridTests(unittest.TestCase):
    """相位网格 45/D 与量化。"""

    CONTROLLABLE = S.DrpSemantics(
        mode="deg_unit", phase_unit_deg_milli=45_000, path=None, source="test:可控"
    )

    def test_grid_is_45_over_d(self) -> None:
        self.assertEqual(S.mmcm_phase_grid_milli_deg(40), Fraction(1125))
        self.assertEqual(S.mmcm_phase_grid_milli_deg(80), Fraction(1125, 2))
        self.assertEqual(S.mmcm_phase_grid_milli_deg(125), Fraction(360))
        self.assertEqual(S.phase_quantum_milli_deg(80, self.CONTROLLABLE), 563)  # 562.5 四舍五入
        self.assertIsNone(S.phase_quantum_milli_deg(40, S.parse_semantics_json({"phase_controllable": False}, "x")))

    def test_frozen_phases_are_exactly_representable(self) -> None:
        """冻结相位点在**偶 D** 下都能精确表示（`45/D` 网格；早期「只能 45° 整数倍」作废）。

        D 为奇数时网格 `45/D` 与 22.5° 的比值是 x.5 步，只能取最近格点（docs 6.2 的
        `unresolved` 也记录了 D=125 时 22.5° = 62.5 步）。
        """
        for div in (40, 50, 80, 100, 120):
            for phase_mdeg in S.FROZEN_PHASES_MILLI_DEG:
                with self.subTest(div=div, phase=phase_mdeg):
                    _word, actual, _quantum, _notes = S.phase_word_value(phase_mdeg, div, 0, self.CONTROLLABLE)
                    self.assertEqual(actual, phase_mdeg)
        # D=125（8MHz 频点）：只有 45° 的整数倍网格点能精确落上
        for phase_mdeg, exact in ((0, True), (45_000, True), (90_000, True), (135_000, True), (22_500, False)):
            _word, actual, _quantum, notes = S.phase_word_value(phase_mdeg, 125, 0, self.CONTROLLABLE)
            self.assertEqual(actual == phase_mdeg, exact, msg=f"D=125 phase={phase_mdeg}")
            if not exact:
                self.assertTrue(notes)

    def test_quantization_rounds_to_grid(self) -> None:
        # D=40：网格 1.125° → 100° 落到 100.125°
        _word, actual, quantum, notes = S.phase_word_value(100_000, 40, 0, self.CONTROLLABLE)
        self.assertEqual(actual, 100_125)
        self.assertEqual(quantum, 1125)
        self.assertTrue(notes)

    def test_not_controllable_keeps_static_phase(self) -> None:
        sem = S.parse_semantics_json({"phase_controllable": False}, "x.json")
        word, actual, quantum, notes = S.phase_word_value(22_500, 80, 0, sem)
        self.assertEqual(word, 22_500)  # 照写（docs 6.2：写无害）
        self.assertEqual(actual, 90_000)  # 实际仍是静态 90°
        self.assertIsNone(quantum)
        self.assertTrue(any("不改变输出" in note for note in notes))

    def test_phase_step_ps(self) -> None:
        sol = S.solve_sig(25_000_000, 0)
        self.assertEqual(sol.phase_step_ps, Fraction(10**12, 8 * 1_000_000_000))
        self.assertEqual(float(sol.phase_step_ps), 125.0)
        self.assertEqual(S.solve_sig(5_000_000, 0).phase_step_ps, Fraction(10**12, 8 * 600_000_000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
