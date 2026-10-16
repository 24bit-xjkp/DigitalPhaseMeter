"""片内信号源（MMCM 求解 + DRP 寄存器镜像 + 相位码）的单元测试。

被测实现是「方案 A」：PL 侧只有 8 个 16 位 MMCM 寄存器**镜像**（偏移 0x08..0x24）+
LOAD（0x28），频率与相位都在 `dpm_host.sig` 里编码成 MMCM 的原始位域（见
`docs/ps-protocol.md` 第 6 节）。旧 `clk_wiz_src` 方案（毫度相位字、SADDR 静态/动态切换、
相位单位优先级）的 API 已删除，因此这些用例只覆盖方案 A 的契约。

运行：
    仓库根目录：`uv run pytest host/tests/test_sig.py -q`
    host 目录： `uv run pytest tests/test_sig.py -q`
"""

from __future__ import annotations

import json
import os
import tempfile
from fractions import Fraction

import pytest
from dpm_host import sig as S

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(HOST_DIR)
SEMANTICS_PATH = os.path.join(REPO_ROOT, "spec", "drp_semantics.json")
GOLDEN_PATH = os.path.join(REPO_ROOT, "spec", "golden", "vectors.json")

#: 冻结表（`docs/ps-protocol.md` 第 6.4 节 / `spec/golden/vectors.json`）
FROZEN_TRIPLES = {
    5_000_000: (600_000_000, 12, 120),
    6_250_000: (750_000_000, 15, 120),
    8_000_000: (1_000_000_000, 20, 125),
    10_000_000: (1_000_000_000, 20, 100),
    12_500_000: (1_000_000_000, 20, 80),
    16_000_000: (800_000_000, 16, 50),
    20_000_000: (1_000_000_000, 20, 50),
    25_000_000: (1_000_000_000, 20, 40),
    40_000_000: (1_000_000_000, 20, 25),
    50_000_000: (1_000_000_000, 20, 20),
}
_FROZEN_IDS = [f"{freq_hz / 1e6:g}MHz" for freq_hz in FROZEN_TRIPLES]

#: 相位可控（方案 A，当前 bitstream）：相位用例默认用它，不依赖磁盘上的实测结论文件。
CONTROLLABLE = S.DrpSemantics(phase_controllable=True, source="test:可控")
#: 相位不可控（旧 bitstream：PHASE_DUTY_CONFIG=false）：输出恒为静态 90°。
STATIC = S.DrpSemantics(phase_controllable=False, phase_static_milli_deg=90_000, source="test:不可控")


@pytest.fixture(autouse=True)
def _isolate_semantics_cache():
    """每个用例前后都清空语义缓存（原来是 `SemanticsTests.tearDown` 的职责）。"""
    S.reset_semantics_cache()
    yield
    S.reset_semantics_cache()


# ------------------------------------------------------------------ 冻结求解规则


@pytest.mark.parametrize("freq_hz, expected", list(FROZEN_TRIPLES.items()), ids=_FROZEN_IDS)
def test_frozen_triple_bit_exact(freq_hz: int, expected: tuple[int, int, int]) -> None:
    """冻结求解规则（三处实现必须一致）：(VCO, MULT, D) 逐位相等且频率精确。

    对应旧 `test_frozen_triples_bit_exact` + 它每个频点的 `subTest`。
    """
    sol = S.solve_sig(freq_hz, 90_000)
    assert sol.ok
    assert (sol.vco_hz, sol.mult, sol.div) == expected
    assert sol.divclk == 1
    assert sol.error_milli_hz == 0
    assert sol.exact


def test_frozen_table_self_check() -> None:
    assert S.verify_frozen_table() == []


def test_frozen_point_helper() -> None:
    point = S.frozen_point(25_000_000)
    assert point is not None
    assert (point.vco_hz, point.mult, point.div) == (1_000_000_000, 20, 40)
    assert point.divide_field == 40
    assert float(point.phase_step_ps) == 125.0
    assert S.frozen_point(7_000_000) is None


@pytest.mark.parametrize("freq_hz", [5_000_000, 7_000_000, 30_000_000, 123_456_789], ids=lambda f: f"{f / 1e6:g}MHz")
def test_constraints_are_satisfied(freq_hz: int) -> None:
    sol = S.solve_sig(freq_hz, 0)
    assert sol.ok
    assert S.VCO_MIN_HZ <= sol.vco_hz <= S.VCO_MAX_HZ
    assert sol.mult in range(S.MULT_MIN, S.MULT_MAX + 1)
    assert sol.div in range(S.DIV_MIN, S.DIV_MAX + 1)
    assert sol.vco_hz == 50_000_000 * sol.mult
    assert sol.divide_field == sol.div


def test_unsolvable_and_out_of_range() -> None:
    assert not S.solve_sig(0, 0).ok
    assert not S.solve_sig(-1, 0).ok
    low = S.solve_sig(1_000_000, 0)
    assert low.ok
    # 最低可实现频率：600MHz / 127。注意 D=127 目前**编码不回去**（见 test_d127_divide_round_trip 的 xfail）
    assert low.div == S.DIV_MAX
    assert low.error_milli_hz > 0
    assert not low.exact
    assert any("不能精确实现" in note for note in low.notes)


@pytest.mark.parametrize("freq_hz", list(FROZEN_TRIPLES), ids=_FROZEN_IDS)
def test_exact_solution_error_is_zero(freq_hz: int) -> None:
    assert S.solve_sig(freq_hz, 0).error_milli_hz == 0


# ------------------------------------------------- 与金标准向量的一致性（方案 A 相位字段）


def _load_golden_vectors() -> list[dict]:
    """读 `spec/golden/vectors.json`；文件缺失时返回空表（对应用例自动跳过）。"""
    if not os.path.exists(GOLDEN_PATH):
        return []
    with open(GOLDEN_PATH, encoding="utf-8") as fh:
        return json.load(fh).get("vectors") or []


GOLDEN_VECTORS = _load_golden_vectors()
_GOLDEN_IDS = [f"{vec['freq_hz']}Hz-{vec['phase_requested_mdeg']}mdeg" for vec in GOLDEN_VECTORS]


@pytest.mark.skipif(not GOLDEN_VECTORS, reason="spec/golden/vectors.json 不存在")
@pytest.mark.parametrize("vec", GOLDEN_VECTORS, ids=_GOLDEN_IDS)
def test_solver_matches_golden_phase_fields(vec: dict) -> None:
    """方案 A 的工作点/相位码/DELAY_TIME/PHASE_MUX 与金标准向量逐位一致。"""
    sol = S.solve_sig(vec["freq_hz"], vec["phase_requested_mdeg"])
    words = S.drp_words(sol)
    assert (sol.vco_hz, sol.mult, sol.div) == (vec["vco_hz"], vec["mult"], vec["div"])
    assert sol.phase_code == vec["phase_code"]
    assert sol.phase_field == vec["phase_field"]
    assert sol.phase_actual_milli_deg == vec["phase_mmcm_mdeg"]
    assert (words.clkout1 >> 13) & 0x7 == vec["phase_mux"]
    assert words.clkout1_2 & 0x3F == vec["phase_field"]
    assert vec["phase_grid_mdeg_num"] == S.PHASE_GRID_MDEG_NUM


# ------------------------------------------------------------- DRP 寄存器镜像（方案 A）


def test_words_follow_doc() -> None:
    """25MHz/90°（BD 初值工作点 VCO=1000MHz、MULT=20、D=40）的 8 个镜像 + LOAD。

    对应旧 `test_words_follow_doc`，但断言的字段从旧方案的「分频字/毫度相位字」换成了
    方案 A 的 MMCM 原始位域（`docs/ps-protocol.md` 6.1/6.2）。
    """
    words = S.drp_words(S.solve_sig(25_000_000, 90_000))
    # CLKFBOUT：MULT=20 → HIGH_TIME/LOW_TIME = 10/10（ClkReg1 bit12 恒 1）
    assert (words.clkfbout, words.clkfbout2) == (0x128A, 0x0000)
    # CLKOUT0 / CLKOUT1：D=40 → HIGH_TIME/LOW_TIME = 20/20
    assert (words.clkout0, words.clkout0_2) == (0x1514, 0x0000)
    # CLKOUT1 的相位：相位码 80 = DELAY_TIME 10（ClkReg2[5:0]）+ PHASE_MUX 0（ClkReg1[15:13]）
    assert (words.clkout1, words.clkout1_2) == (0x1514, 0x000A)
    assert words.divclk == 0x1041
    assert words.power == S.DRP_POWER_ENABLE_PHASE == 0xFFFF
    assert words.load == S.DRP_LOAD_TRIGGER == 0x01
    assert (words.phase_code, words.phase_field, words.phase_mux) == (80, 10, 0)
    assert S.DRP_OFF_STATUS == 0x04
    assert S.DRP_OFF_LOAD == 0x28


def test_phase_mux_lands_in_clkreg1_high_bits() -> None:
    """22.5°@D=40 → 相位码 20 = DELAY_TIME 2 + PHASE_MUX 4（CLKOUT1 = 0x9514/0x0002）。"""
    sol = S.solve_sig(25_000_000, 22_500)
    assert (sol.phase_code, sol.phase_field, sol.phase_actual_milli_deg) == (20, 2, 22_500)
    words = S.drp_words(sol)
    assert (words.clkout1, words.clkout1_2) == (0x9514, 0x0002)
    assert (words.clkout1 >> 13) & 0x7 == 4  # PHASE_MUX
    assert words.clkout1_2 & 0x3F == 2  # DELAY_TIME


def test_d125_phase_words() -> None:
    """8MHz（D=125）：90° → 相位码 250 = DELAY_TIME 31 + PHASE_MUX 2 = 0x5FBF/0x009F。"""
    sol = S.solve_sig(8_000_000, 90_000)
    assert (sol.phase_code, sol.phase_field, sol.phase_actual_milli_deg) == (250, 31, 90_000)
    words = S.drp_words(sol)
    assert (words.clkout1, words.clkout1_2) == (0x5FBF, 0x009F)


def test_write_order_and_offsets() -> None:
    """写入顺序与偏移：8 个镜像（0x08..0x24）+ LOAD（0x28），共 9 笔。"""
    words = S.drp_words(S.solve_sig(10_000_000, 22_500))
    pairs = words.as_pairs()
    assert [off for off, _value in pairs] == [0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x20, 0x24, 0x28]
    values = dict(pairs)
    assert values[0x08] == 0x128A  # CLKFBOUT：MULT=20
    assert values[0x0C] == 0x0000  # CLKFBOUT2
    assert values[0x10] == 0x1CB2  # CLKOUT0：D=100
    assert values[0x14] == 0x0000  # CLKOUT0_2：小数字，不用
    assert values[0x18] == 0x5CB2  # CLKOUT1：D=100 + PHASE_MUX 2
    assert values[0x1C] == 0x0006  # CLKOUT1_2：DELAY_TIME 6
    assert values[0x20] == 0x1041  # DIVCLK_DIVIDE = 1
    assert values[0x24] == 0xFFFF  # POWER：使能相位 mux / delay
    assert values[0x28] == 0x01  # LOAD：写 1 触发


def test_register_offset_table() -> None:
    """寄存器镜像偏移表（旧 `drp_word_off`/`DRP_STATUS_LEGACY_OFF` 的替代：偏移现在是固定的）。"""
    assert (S.DRP_OFF_ID, S.DRP_OFF_STATUS) == (0x00, 0x04)
    assert (S.DRP_OFF_CLKFBOUT, S.DRP_OFF_CLKFBOUT2) == (0x08, 0x0C)
    assert (S.DRP_OFF_CLKOUT0, S.DRP_OFF_CLKOUT0_2) == (0x10, 0x14)
    assert (S.DRP_OFF_CLKOUT1, S.DRP_OFF_CLKOUT1_2) == (0x18, 0x1C)
    assert (S.DRP_OFF_DIVCLK, S.DRP_OFF_POWER, S.DRP_OFF_LOAD) == (0x20, 0x24, 0x28)
    assert S.DRP_ID_VALUE == 0x44504D03
    assert (S.DRP_STATUS_LOCKED, S.DRP_STATUS_BUSY) == (1, 2)
    assert S.DRP_DADDR_ORDER == (0x14, 0x15, 0x08, 0x09, 0x0A, 0x0B, 0x16, 0x28)


def test_divide_field_round_trip() -> None:
    """ClkReg1 的分频编解码互逆（旧 `divide_field` / `divide_field_get` 的等价覆盖）。

    只遍历**能精确编码**的 D（1..126）：HIGH_TIME/LOW_TIME 各 6 位，D=127 时 LOW_TIME=64
    放不进字段（已知缺陷，见 :func:`test_d127_divide_round_trip`）。
    """
    for div in range(S.DIV_MIN, 127):
        clkreg1, clkreg2 = S.mmcm_encode_clkout(div, 0)
        assert S.mmcm_decode_divide(clkreg1) == div
        high, low = (clkreg1 >> 6) & 0x3F, clkreg1 & 0x3F
        assert high + low == div  # HIGH_TIME + LOW_TIME = D
        assert abs(high - low) <= 1  # 50% 占空比
        assert clkreg1 & S.CLKREG1_MARK  # bit12 恒 1（clk_wiz 生成值，勿按手册当 RSVD）
        assert clkreg2 & 0x3F == 0  # 相位 0 → DELAY_TIME 0


def test_div_max_is_126_because_of_six_bit_fields() -> None:
    """分频硬上限是 **126**，不是 127。

    分频在 MMCM 里编码成 `HIGH_TIME + LOW_TIME`（**各 6 位**），能表示的最大值是
    `63 + 63 = 126`。`D = 127` 的 `LOW_TIME = 64` 会被 6 位字段截断成 63，写下去实际是
    D=63（输出频率差一倍）——所以求解规则必须把 D 限制在 126，而不是「分频字段能放 255」。

    这条以前是 `xfail(strict=True)` 钉住的已知缺陷；现在三处（C `DPM_SIG_DIV_MAX`、
    Python `DIV_MAX`、`spec/gen_golden.py`）都收到 126，因此改成显式断言：
    上界不可表示、126 可原样往返。
    """
    assert S.DIV_MAX == 126
    assert S.mmcm_decode_divide(S.mmcm_encode_clkout(S.DIV_MAX, 0)[0]) == S.DIV_MAX
    # 上界之外的值不保证能编码回去（LOW_TIME 溢出 6 位）——这正是要把它挡在规则之外的原因
    assert S.mmcm_decode_divide(S.mmcm_encode_clkout(127, 0)[0]) == 63


def test_d1_edge_bit_is_cleared_like_vendor() -> None:
    """D=1：NO_COUNT = 1，且 **EDGE 按厂商做法清 0**（HIGH/LOW_TIME 是占位值）。

    厂商 `mmcm_pll_divider` 的 D=1 分支给出 `EDGE=0`；C 侧 `DPM_MMCM_ENCODE_CLKOUT` 也是
    `(d == 1) ? 0 : (d & 1)`。Python 以前写 `d & 1`（=1），三处不一致，已按厂商/C 侧对齐。
    """
    assert S.mmcm_encode_clkout(1, 0) == (0x1001, 0x0040)
    assert S.mmcm_decode_divide(0x1001) == 1


def test_decode_divide_branches() -> None:
    """bit12 == 0 → divide == 1（`divide == 1` 的计数器字是 0x0041），否则 HIGH+LOW。"""
    assert S.mmcm_decode_divide(0x28) == 1
    assert S.mmcm_decode_divide(S.CLKREG1_MARK | 0x28) == 40  # 旧 `divide_field_get(0x28) == 40`
    assert S.mmcm_decode_divide(S.mmcm_encode_clkout(126, 0)[0]) == 126  # 6 位字段能表示的 D 上界


@pytest.mark.parametrize("div, edge, no_count", [(40, 0, 0), (100, 0, 0), (125, 1, 0), (126, 0, 0)])
def test_clkreg2_edge_and_no_count(div: int, edge: int, no_count: int) -> None:
    """ClkReg2 的 EDGE（奇数分频才置位）与 NO_COUNT（仅 D==1）位。"""
    _clkreg1, clkreg2 = S.mmcm_encode_clkout(div, 0)
    assert (clkreg2 >> 7) & 1 == edge
    assert (clkreg2 >> 6) & 1 == no_count


def test_divclk_encoding() -> None:
    """DIVCLK_DIVIDE = 1 → 权威值 0x1041（bit12 是 NO_COUNT，不是 CLKOUTx 的 MARK）。"""
    assert S.mmcm_encode_divclk(1) == 0x1041
    assert S.mmcm_decode_divclk(0x1041) == 1
    for div in (2, 3, 4, 7, 126):
        assert S.mmcm_decode_divclk(S.mmcm_encode_divclk(div)) == div


@pytest.mark.parametrize(
    "div, phase_mdeg, code",
    [
        (40, 0, 0),
        (40, 22_500, 20),
        (40, 90_000, 80),
        (100, 22_500, 50),
        (125, 22_500, 63),
        (125, 90_000, 250),
    ],
)
def test_phase_code_splits_into_mux_and_delay(div: int, phase_mdeg: int, code: int) -> None:
    """相位码 m = DELAY_TIME（ClkReg2[5:0]，整数 VCO 周期）+ PHASE_MUX（ClkReg1[15:13]，1/8 周期）。

    旧 `phase_field_bits` / `phase_field_get` 的等价覆盖（旧方案那套字段编码已删除）。
    """
    assert S.mmcm_phase_code(phase_mdeg, div) == code
    clkreg1, clkreg2 = S.mmcm_encode_clkout(div, phase_mdeg)
    assert (clkreg1 >> 13) & 0x7 == code & 0x7  # PHASE_MUX = m[2:0]
    assert clkreg2 & 0x3F == (code >> 3) & 0x3F  # DELAY_TIME = m[8:3]
    assert S.mmcm_decode_divide(clkreg1) == div
    assert S.mmcm_decode_phase_mdeg(clkreg1, clkreg2, div) == S.mmcm_phase_code_to_mdeg(code, div)


def test_protocol_phase_range_is_driven_by_phase_code() -> None:
    """相位上界不再是固定的 <180°，而是 9 位相位码 `m <= 511`（按 D 逐点）。

    180° 在 C/Python/spec 三处都合法（`m = 4D <= 504`）；越界请求（编码会截断回绕）
    由 `phase_code_fits` 判假。上界公式 `min(23017499/D, 359999)` 与
    `ps/include/dpm_regs.h::DPM_MMCM_PHASE_MAX_MDEG_NUM` 是同一个数。
    """
    assert S.PHASE_MAX_MDEG_NUM == 23_017_499
    assert S.phase_max_milli_deg(125) == 184_139
    assert S.phase_max_milli_deg(120) == 191_812
    assert S.phase_max_milli_deg(100) == 230_174
    assert S.phase_max_milli_deg(40) == 359_999  # 满量程先封顶
    for div in range(1, S.DIV_MAX + 1):
        hi = S.phase_max_milli_deg(div)
        assert S.phase_code_fits(hi, div)
        if hi < S.FULL_SCALE_MILLI_DEG - 1:
            assert not S.phase_code_fits(hi + 1, div)
            assert S.mmcm_phase_code(hi, div) == S.PHASE_CODE_MAX
        # 180° 在所有合法 D 上都可请求（这是本次放宽的核心）
        assert S.phase_code_fits(180_000, div)
        assert S.mmcm_phase_code(180_000, div) == 4 * div
    # 满量程与 D=0 的边界
    assert not S.phase_code_fits(360_000, 40)
    with pytest.raises(ValueError):
        S.phase_code_fits(90_000, 0)
    with pytest.raises(ValueError):
        S.phase_max_milli_deg(0)


@pytest.mark.parametrize(
    "freq_hz, phase_mdeg",
    [(25_000_000, 270_000), (25_000_000, 225_000), (25_000_000, 180_000), (20_000_000, 315_000),
     (10_000_000, 225_000), (8_000_000, 180_000), (5_000_000, 191_250)],
    ids=["25M-270", "25M-225", "25M-180", "20M-315", "10M-225", "8M-180", "5M-191.25"],
)
def test_over_180_is_exact_on_the_grid(freq_hz: int, phase_mdeg: int) -> None:
    """>180°（含正好 180°）的请求在相位码范围内是**精确可表示**的网格点，不是吸附值。"""
    sol = S.solve_sig(freq_hz, phase_mdeg)
    assert sol.ok and sol.phase_actual_milli_deg == phase_mdeg
    assert sol.phase_code == S.mmcm_phase_code(phase_mdeg, sol.div)
    assert sol.phase_code <= S.PHASE_CODE_MAX
    assert sol.phase_field == (sol.phase_code >> 3) & 0x3F
    assert S.phase_code_fits(phase_mdeg, sol.div)
    assert not any("越界" in note for note in sol.notes)


def test_270deg_at_25mhz_is_fifteen_vco_cycles() -> None:
    """25MHz/270°（D=40）：相位码 240 = 15 个 VCO 周期 → DELAY_TIME=30、PHASE_MUX=0。"""
    sol = S.solve_sig(25_000_000, 270_000)
    assert (sol.div, sol.phase_code, sol.phase_field) == (40, 240, 30)
    assert S.mmcm_encode_clkout(40, 270_000)[1] & 0x3F == 30


def test_over_180_beyond_phase_code_wraps_and_is_flagged() -> None:
    """超过相位码上界的请求是**回绕**（不是吸附）：8MHz/270° → 码 750 → 字段截断成 238 步。"""
    assert S.phase_max_milli_deg(125) == 184_139
    assert not S.phase_code_fits(270_000, 125)
    code, _actual, _quantum, notes = S.phase_code_value(270_000, 125, 0, CONTROLLABLE)
    assert code == 750 and code > S.PHASE_CODE_MAX
    assert any("越界" in note for note in notes)
    clkreg1, clkreg2 = S.mmcm_encode_clkout(125, 270_000)
    # 字段截断：DELAY_TIME = (750 >> 3) & 0x3F = 29、PHASE_MUX = 750 & 7 = 6 → 238 步
    assert ((clkreg2 & 0x3F) << 3) | ((clkreg1 >> 13) & 0x7) == 238
    assert S.mmcm_decode_phase_mdeg(clkreg1, clkreg2, 125) == 85_680
    # 上界内的 183.96°（= 511 步）仍然精确
    assert S.phase_code_value(183_960, 125, 0, CONTROLLABLE) == (511, 183_960, 360, ())


def test_phase_code_rejects_zero_divide() -> None:
    """D = 0 一律显式报错（不要悄悄算出一个无意义的编码）。"""
    with pytest.raises(ValueError):
        S.mmcm_phase_code(0, 0)
    with pytest.raises(ValueError):
        S.mmcm_phase_code_to_mdeg(0, 0)
    with pytest.raises(ValueError):
        S.mmcm_encode_clkout(0, 0)
    with pytest.raises(ValueError):
        S.mmcm_encode_divclk(0)
    with pytest.raises(ValueError):
        S.mmcm_phase_grid_milli_deg(0)


def test_decode_words() -> None:
    words = S.drp_words(S.solve_sig(6_250_000, 0))
    fields = S.decode_drp_words(words)
    assert fields["mult"] == 15
    assert fields["divclk"] == 1
    assert fields["div0"] == 120
    assert fields["div1"] == 120
    assert fields["phase_mdeg"] == 0


@pytest.mark.parametrize("phase_mdeg", [0, 22_500, 90_000, 135_000], ids=lambda p: f"{p / 1000:g}deg")
def test_decode_words_round_trips_phase(phase_mdeg: int) -> None:
    """镜像 → 字段 → 相位与求解结果互逆（`decode_drp_words` 与 `drp_words` 配对）。"""
    sol = S.solve_sig(25_000_000, phase_mdeg)
    fields = S.decode_drp_words(S.drp_words(sol))
    assert fields["phase_mdeg"] == sol.phase_actual_milli_deg


def test_drp_words_requires_solution() -> None:
    with pytest.raises(ValueError):
        S.drp_words(S.solve_sig(0, 0))


# --------------------------------------------------------------- 相位可控性语义


def test_fallback_semantics_is_plan_a() -> None:
    """实测结论文件缺失 → 回退到方案 A（相位可写，网格 45°/D），不再回退旧候选 B。"""
    sem = S.load_semantics("/nonexistent/path/drp_semantics.json", force=True)
    assert sem.is_fallback
    assert sem.phase_controllable is True
    assert sem.phase_static_milli_deg == 90_000
    assert "方案A" in sem.source
    assert "45°/D" in sem.describe()  # 一行描述可直接给 CLI/日志


#: 旧 `clk_wiz_src` 方案的相位单位键（方案 A 显式忽略，不再有优先级）。
_LEGACY_UNIT_JSONS = [
    {"phase_unit_deg_milli": 45_000, "phase_unit_ps_per_lsb": 125.0, "phase_field_is_degrees": True},
    {"phase_unit_ps_per_lsb": 208.3, "phase_field_is_degrees": True},
    {"phase_field_is_degrees": True},
]


@pytest.mark.parametrize("data", _LEGACY_UNIT_JSONS, ids=["deg_milli", "ps_per_lsb", "degrees"])
def test_legacy_phase_unit_keys_are_ignored(data: dict) -> None:
    """旧单位键（原来按 deg_milli → ps_per_lsb → degrees 排队）不再影响任何结论。"""
    sem = S.parse_semantics_json(dict(data), "x.json")
    assert sem.phase_controllable is True
    assert sem.phase_static_milli_deg == 90_000
    assert sem.keys == ("phase_controllable",)
    assert sem.path == "x.json"


def test_zero_units_do_not_crash() -> None:
    """实测文件里 `phase_unit_deg_milli: 0`：不能当成「单位是 0」报错。"""
    sem = S.parse_semantics_json({"phase_controllable": False, "phase_unit_deg_milli": 0, "phase_unit_ps_per_lsb": 0}, "x.json")
    assert sem.phase_controllable is False
    assert sem.phase_static_milli_deg == 90_000
    assert not S.phase_supported(45_000, sem)[0]


def test_legacy_phase_word_index_is_ignored() -> None:
    """旧 `phase_word_index`（毫度相位字）在方案 A 里不存在：解析不报错，也不产生偏移字段。"""
    sem = S.parse_semantics_json(
        {"phase_controllable": True, "phase_unit_deg_milli": 1000, "phase_word_index": {"wired": True, "clkout1": 6}},
        "x.json",
    )
    assert sem.phase_controllable is True
    assert not hasattr(sem, "phase_word_off")  # 旧偏移字段随方案 A 删除
    assert sem.keys == ("phase_controllable",)


def test_static_semantics_fixes_and_reports_phase() -> None:
    """`phase_controllable=false`（旧 bitstream）：只有请求值等于静态相位才算可实现。"""
    sem = S.parse_semantics_json({"phase_controllable": False, "phase_static_deg_milli": 45_000}, "x.json")
    assert sem.phase_controllable is False
    assert sem.phase_static_milli_deg == 45_000
    supported, reason = S.phase_supported(45_000, sem)
    assert supported is True
    assert "45.000" in reason
    supported, reason = S.phase_supported(90_000, sem)
    assert supported is False
    assert "45.000" in reason and "90.000" in reason
    assert "不改变输出" in sem.describe()


def test_reload_reads_file_again() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "drp_semantics.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"phase_controllable": False, "phase_static_deg_milli": 45_000}, fh)
        sem = S.load_semantics(path, force=True)
        assert sem.phase_controllable is False
        assert sem.phase_static_milli_deg == 45_000
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"phase_controllable": True, "phase_unit_deg_milli": 45_000}, fh)
        again = S.reload_semantics(path)
        assert again.phase_controllable is True
        assert again.path == path


def test_bad_json_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with pytest.raises(ValueError):
            S.load_semantics(path, force=True)


@pytest.mark.skipif(not os.path.exists(SEMANTICS_PATH), reason="spec/drp_semantics.json 不存在")
def test_semantics_cache_is_reused_until_reset() -> None:
    """带缓存：同一路径第二次读盘命中缓存，`reset_semantics_cache()` 之后重新读盘。"""
    first = S.load_semantics(SEMANTICS_PATH, force=True)
    assert S.load_semantics(SEMANTICS_PATH) is first
    S.reset_semantics_cache()
    assert S.load_semantics(SEMANTICS_PATH) is not first


@pytest.mark.skipif(not os.path.exists(SEMANTICS_PATH), reason="spec/drp_semantics.json 不存在")
def test_real_spec_file() -> None:
    """仓库里的实测结论：bitstream 打开 PHASE_DUTY_CONFIG 后相位可在线改
    （上板实测见 docs/onboard-ax7010.md 第 3 节）。"""
    sem = S.load_semantics(SEMANTICS_PATH, force=True)
    assert sem.phase_controllable is True
    assert sem.phase_static_milli_deg == 90_000
    assert sem.path == SEMANTICS_PATH
    assert sem.keys == ("phase_controllable",)
    assert S.phase_supported(90_000, sem)[0] is True
    assert S.phase_supported(45_000, sem)[0] is True


# ------------------------------------------------------------ 相位网格 45°/D 与量化


def test_grid_is_45_over_d() -> None:
    assert S.mmcm_phase_grid_milli_deg(40) == Fraction(1125)
    assert S.mmcm_phase_grid_milli_deg(80) == Fraction(1125, 2)
    assert S.mmcm_phase_grid_milli_deg(125) == Fraction(360)
    assert S.phase_grid_milli_deg(125, CONTROLLABLE) == Fraction(360)
    assert S.phase_quantum_milli_deg(80, CONTROLLABLE) == 563  # 562.5 四舍五入
    assert S.phase_quantum_milli_deg(40, STATIC) is None
    assert S.phase_grid_milli_deg(40, STATIC) is None


#: 冻结相位点 × 偶 D（22.5° 是 45°/D 的整数倍要求 D 为偶数），再按 9 位相位码上界裁剪：
#: D=120（5MHz）在 202.5° 以上越界、D=100（10MHz）在 270° 越界、D=80（12.5MHz）在 315° 越界。
_FROZEN_GRID_CASES = [
    (div, phase)
    for div in (40, 50, 80, 100, 120)
    for phase in S.FROZEN_PHASES_MILLI_DEG
    if S.phase_code_fits(phase, div)
]
_FROZEN_GRID_IDS = [f"D{div}-{phase / 1000:g}deg" for div, phase in _FROZEN_GRID_CASES]


@pytest.mark.parametrize("div, phase_mdeg", _FROZEN_GRID_CASES, ids=_FROZEN_GRID_IDS)
def test_frozen_phases_are_exactly_representable(div: int, phase_mdeg: int) -> None:
    """冻结相位点在**偶 D** 下都能精确表示（`45/D` 网格；早期「只能 45° 整数倍」作废）。

    D 为奇数时网格 `45/D` 与 22.5° 的比值是 x.5 步，只能取最近格点（docs 6.2 的
    `unresolved` 也记录了 D=125 时 22.5° = 62.5 步）。
    """
    code, actual, quantum, notes = S.phase_code_value(phase_mdeg, div, 0, CONTROLLABLE)
    assert actual == phase_mdeg
    assert 0 <= code <= S.PHASE_CODE_MAX
    assert quantum == S.phase_quantum_milli_deg(div, CONTROLLABLE)
    assert notes == ()  # 精确落格 → 没有量化提示


@pytest.mark.parametrize(
    "phase_mdeg, exact",
    [(0, True), (45_000, True), (90_000, True), (135_000, True), (22_500, False)],
    ids=["0deg", "45deg", "90deg", "135deg", "22.5deg"],
)
def test_d125_only_multiples_of_45_deg_are_exact(phase_mdeg: int, exact: bool) -> None:
    """D=125（8MHz）：网格 0.36°，22.5° = 62.5 步 → 只能取最近格点（63 步 = 22.68°）。"""
    code, actual, quantum, notes = S.phase_code_value(phase_mdeg, 125, 0, CONTROLLABLE)
    assert quantum == 360
    assert (actual == phase_mdeg) is exact
    if exact:
        assert notes == ()
    else:
        assert notes
        assert (code, actual) == (63, 22_680)


def test_quantization_rounds_to_grid() -> None:
    # D=40：网格 1.125° → 100° 落到 100.125°
    code, actual, quantum, notes = S.phase_code_value(100_000, 40, 0, CONTROLLABLE)
    assert (code, actual, quantum) == (89, 100_125, 1125)
    assert notes


def test_not_controllable_keeps_static_phase() -> None:
    code, actual, quantum, notes = S.phase_code_value(22_500, 80, 0, STATIC)
    assert code == 0  # 方案 A 下第一个返回值是相位码；相位不可控时无硬件含义
    assert actual == 90_000  # 实际仍是静态 90°
    assert quantum is None
    assert any("不改变输出" in note for note in notes)
    # 历史别名 `phase_word_value` 与 `phase_code_value` 是同一个实现
    assert S.phase_word_value(22_500, 80, 0, STATIC) == (code, actual, quantum, notes)


def test_phase_step_ps() -> None:
    sol = S.solve_sig(25_000_000, 0)
    assert sol.phase_step_ps == Fraction(10**12, 8 * 1_000_000_000)
    assert float(sol.phase_step_ps) == 125.0
    assert S.solve_sig(5_000_000, 0).phase_step_ps == Fraction(10**12, 8 * 600_000_000)
