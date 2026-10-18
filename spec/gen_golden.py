#!/usr/bin/env python3
"""生成验证用的金标准向量（spec/golden/vectors.json）。

本脚本是**独立参考实现**：只依赖标准库，直接按 `docs/ps-protocol.md` 第 5、6 节的
公式与 `.agents/skills/dpm-measurement-model` 的语义推导期望值，不 import `host/`
或 `ps/` 的任何代码，因此可用于交叉校验两侧实现。

用法：
    python3 spec/gen_golden.py            # 写入 spec/golden/vectors.json
    python3 spec/gen_golden.py --print    # 同时打印摘要

模型（与 `docs/verify-plan.md` 的「期望值模型」一节一致）：

1. 闸门窗口：`Controller` 数到 targetGate 后还要等被测信号的下一个上升沿才停，
   因此 `gate_cnt = targetGate + ceil(T_sig / T_slow)`（归一化到低速周期）。
2. 参考沿数：窗口内 `sig1` 的上升沿数 `edge_cnt = round(gate_cnt * T_slow / T_sig)`
   （窗口边界不整除时多算/少算一个沿，容限 ±1）。
3. 相位计数：真实相位差 `phase_ns` **先被 1 个 DSP 周期量化**（测量通路的采样网格），
   再与参考沿数相乘：`phase_cnt = round(phase_ns / T_dsp) * edge_cnt`。
   这一步把「相位量化下界」显式建模出来，而不是当成误差抹掉。
4. 输出：按协议第 5 节的整数定点公式给出 `freq_milli_hz` / `phase_milli_deg`。

判据（写进报告时必须区分）：
   - 频率：`|f_meas - f_nominal| <= freq_tol`，容限取 `(2/edge_cnt + 2/gate_cnt)` 量级，
     来源是窗口起止时刻相对被测信号的随机相位（窗口比 targetGate 多出不到一个信号周期，
     少算/多算 1 个沿）——不是设计缺陷；
   - 相位：`|phase_meas - phase_expected| <= phase_tol`，其中 `phase_expected` 是
     **经过 DSP 量化后的期望值**，容限 = 半个 DSP 量化步长 + 1 毫度；
   - `phase_cnt / edge_cnt` 必须等于整数个 DSP 周期（量化下界，不是缺陷）；
   - 请求相位若不是 DSP 量化步长的整数倍（例如 20MHz 下的 45°），
     期望值本身就偏离请求值，报告里要按 `phase_quant_error_mdeg` 说明。
"""

from __future__ import annotations

import argparse
import json
import math
import os
from fractions import Fraction

# ---------------------------------------------------------------- 冻结参数

F_SLOW_HZ = 50_000_000  # 低速（AXI/闸门）时钟
F_DSP_HZ = 450_000_000  # 高速（DSP/测量）时钟
TARGET_GATE = 2000  # 默认闸门目标（低速周期数）→ 40µs

# 冻结的验证频点：目标频率 → (VCO, MULT, DIVCLK=1, D)
# 该表必须与 `solve_sig()` 的规则输出一致（build_vectors 会断言）。
# 约束：600 ≤ VCO ≤ 1200 MHz、分频字段 = D ≤ 126
#
# 450MHz 轮次的选点规则：**DSP 时钟 450MHz = 9 × 50MHz，只有能整除 450MHz 的整数 MHz 频点
# 才让 `f_dsp / f_sig` 为整数**，此时每个信号周期的相位计数窗口都是同一个整数，
# `phaseCnt = round(相位/T_dsp) × edgeCnt` 严格成立（`phaseCnt % edgeCnt == 0`）。
# 于是取 5~50MHz 内 450MHz 的整数 MHz 约数：5/6/9/10/15/18/25/30/45/50MHz；它们也都
# 正好是冻结求解规则的精确解。10 个频点覆盖 VCO 600/750/900/1000/1050MHz、
# MULT 12/15/18/20/21 与 D = 20/35/40/50/70/100/120/125。
#
# 注意 450MHz 下多数信号周期不再是整数 ps（例如 6MHz 的周期是 166666.67ps），金标准
# 因此改用 `fractions.Fraction` 全程有理数精确运算，不再要求「信号周期是整数 ps」。
FREQ_POINTS_HZ = {
    5_000_000: (600_000_000, 12, 120),
    6_000_000: (750_000_000, 15, 125),
    9_000_000: (900_000_000, 18, 100),
    10_000_000: (1_000_000_000, 20, 100),
    15_000_000: (1_050_000_000, 21, 70),
    18_000_000: (900_000_000, 18, 50),
    25_000_000: (1_000_000_000, 20, 40),
    30_000_000: (1_050_000_000, 21, 35),
    45_000_000: (900_000_000, 18, 20),
    50_000_000: (1_000_000_000, 20, 20),
}

# BD 的初始工作点：VCO = 50MHz × 20 = 1000MHz、D = 40（25MHz）
BD_VCO_HZ = 1_000_000_000
BD_DIV = 40
# BD 初始工作点所在的分频范围（CLKOUT_DIVIDE 用 `DIVIDE_F` 表达的常见区间）
BD_DIV_MAX = 126
# 分频比硬上限：超过它不再考虑（见 solve_sig 的说明）
DIV_HARD_MAX = 126


def solve_sig(target_freq_hz: int, f_in_hz: int = F_SLOW_HZ) -> tuple[int, int, int]:
    """求解 MMCM 工作点（与 C 侧 `dpm_sig_solve` 同一规则）。

    规则（冻结，**按顺序逐级比较**）：
      1. `DIVCLK = 1`、`MULT ∈ 12..24` 整数、`VCO = f_in × MULT ∈ [600, 1200] MHz`、
         `D ∈ 1..126`（分频编码成 HIGH_TIME + LOW_TIME 各 6 位，上限 63+63=126）、不使用小数分频；
      2. 先最小化频率误差 `|VCO/D - f_target|`；
      3. 同误差（含多个精确解）时取 `cost = |VCO - 1000MHz| / 1MHz + |D - 40|` 最小者，
         即尽量贴近 BD 的初始工作点 `(VCO=1000MHz, D=40)`。

    该规则的输出就是冻结频点表（`FREQ_POINTS_HZ` 与之一致，由 `build_vectors()` 断言）：
    5MHz 在 1000MHz 下需要 D=200（超硬上限 126），取 `600/12/120`；6MHz 的候选精确解里
    `750/15/125` 的 cost 最小（250+85）；9/18/45MHz 落在 900MHz 的 `MULT=18`；
    15/30MHz 落在 1050MHz 的 `MULT=21`；10/25/50MHz 落在 1000MHz 的 `MULT=20`。

    返回 `(vco_hz, mult, div)`；无解抛异常。
    """
    best = None
    for mult in range(12, 25):
        vco = f_in_hz * mult
        if not (600_000_000 <= vco <= 1_200_000_000):
            continue
        for d in range(1, 256):
            # 频率误差用整数比较：|VCO/D - f| = |VCO - f*D| / D，
            # 比较 |VCO - f*D| * d_best 与 |VCO_best - f*D_best| * D，避免浮点误差
            # 硬约束：不用分频比大于 BD 初始工作点分频范围 + 余量的组合
            # （高 VCO 配大分频既无必要，也让相位字段覆盖的输出周期数变少）
            if d > DIV_HARD_MAX:
                continue
            err_num = abs(vco - target_freq_hz * d)
            cost = abs(vco - BD_VCO_HZ) // 1_000_000 + abs(d - BD_DIV)
            if best is None:
                best = (err_num, d, cost, vco, mult, d)
                continue
            b_err, b_d = best[0], best[1]
            lhs, rhs = err_num * b_d, b_err * d
            if lhs < rhs:
                better = True
            elif lhs > rhs:
                better = False
            else:
                # cost 越小越接近 BD 初始工作点；同 cost 时取 VCO 更接近 1000MHz 者
                better = (cost, abs(vco - BD_VCO_HZ), d) < (best[2], abs(best[3] - BD_VCO_HZ), best[5])
            if better:
                best = (err_num, d, cost, vco, mult, d)
    if best is None:
        raise ValueError(f"目标频率 {target_freq_hz} 无解")
    return best[3], best[4], best[5]


# 相位扫描点：45° 网格的基础点（含 180° 边界）+ 需要 PHASE_MUX 细调的 22.5°
# + >180° 的 45° 倍数点。**不是所有频点都能测所有点**：上界由 9 位相位码决定，
# 见 `phase_points_for()`。
PHASE_BASE_MDEG = (0, 45_000, 90_000, 135_000, 180_000)
PHASE_HALF_MDEG = 22_500  # 仅 D 为偶数时精确落在 45°/D 网格上
PHASE_OVER180_MDEG = (225_000, 270_000, 315_000)  # 相位码 5D/6D/7D ≤ 511 时才可用
PHASE_CODE_MAX = 0x1FF  # 9 位相位码（DELAY_TIME 6 位 + PHASE_MUX 3 位）
PHASE_FULL_SCALE_MDEG = 360_000
#: `m <= 511` ⟺ `phase_mdeg × D <= 23017499`（推导见 `ps/include/dpm_regs.h`）。
PHASE_MAX_MDEG_NUM = 23_017_499


def mmcm_phase_code(phase_mdeg: int, div: int) -> int:
    """把请求相位编码成 MMCM 的 9 位相位码 `m = round(phase_mdeg × div / 45000)`。

    `m` 的单位是 1/8 VCO 周期：`m` 的低 6 位进 DELAY_TIME（整数 VCO 周期）、
    高 3 位进 PHASE_MUX（1/8 VCO 周期），与厂商 `mmcm_pll_phase` /
    `mmcm_pll_count_calc` 的拆分一致（见 `ps/include/dpm_regs.h`）。
    由此可实现的相位网格 = `45000/div` 毫度 = `45/div` 度。
    """
    num = phase_mdeg * div * 8 + 180_000
    return num // 360_000


def mmcm_achievable_mdeg(phase_mdeg: int, div: int) -> int:
    """请求相位经 MMCM 网格量化后**真正能输出**的相位（毫度）。"""
    code = mmcm_phase_code(phase_mdeg, div)
    return (code * 45_000 + div // 2) // div


def phase_max_mdeg(div: int) -> int:
    """给定分频 D 时可**请求**的最大相位（毫度，含）：9 位相位码与满量程谁先撞上。"""
    return min(PHASE_MAX_MDEG_NUM // div, PHASE_FULL_SCALE_MDEG - 1)


def phase_code_top_mdeg(div: int) -> int:
    """相位码上界点（`m = 511`）对应的可请求相位（毫度），已保证落在网格上。

    `m = 511` 是硬件能表示的最大延迟（DELAY_TIME=63、PHASE_MUX=7）。这个点用于
    保证**每个频点都有 >180° 的用例**：D ≥ 103 时 225° 已经越界（`5D > 511`）。
    """
    p = (PHASE_CODE_MAX * 45_000 + div // 2) // div
    assert mmcm_phase_code(p, div) == PHASE_CODE_MAX, f"D={div} 的上界点 {p} 编码不是 511"
    assert mmcm_achievable_mdeg(p, div) == p, f"D={div} 的上界点 {p} 不在网格上"
    return p


def phase_points_for(div: int) -> list[int]:
    """某个频点（分频 D）要测的相位点（毫度，升序）。

    规则（与 `host/dpm_host/sig.py::phase_code_fits`、`ps/include/dpm_regs.h::
    dpm_mmcm_phase_code_fits` 同一判据）：

    1. 必含 `0/45/90/135/180`——45° 的整数倍恒落在 `45°/D` 网格上，且 180° 的相位码
       是 `4D ≤ 504`，所有合法 D（≤126）都能表示（旧固件一律拒 ≥180°，本次放宽）；
    2. D 为偶数时含 22.5°（= `D/2` 个网格步）；
    3. 含 >180° 的 45° 倍数（225/270/315）中相位码 ≤ 511 的那些：
       225° → `5D ≤ 511` ⟺ D ≤ 102；270° → D ≤ 85；315° → D ≤ 73；
    4. 若该频点没有任何 >180° 的 45° 倍数可用（D ≥ 103），补上**相位码上界点**
       （`m = 511`，硬件能表示的最大相位），保证每个频点都有 >180° 的用例。
    """
    if div <= 0:
        raise ValueError(f"D 必须为正数，实际 {div}")
    points = list(PHASE_BASE_MDEG)
    if div % 2 == 0:
        points.append(PHASE_HALF_MDEG)
    over = [p for p in PHASE_OVER180_MDEG if mmcm_phase_code(p, div) <= PHASE_CODE_MAX]
    points.extend(over)
    if not over:
        points.append(phase_code_top_mdeg(div))
    points = sorted(set(points))
    for p in points:
        assert p < PHASE_FULL_SCALE_MDEG, f"D={div} 的相位点 {p} 超过满量程"
        assert mmcm_phase_code(p, div) <= PHASE_CODE_MAX, f"D={div} 的相位点 {p} 越界"
        assert mmcm_achievable_mdeg(p, div) == p, f"D={div} 的相位点 {p} 不在 45°/D 网格上"
    return points


def fixed_point(gate_cnt: int, edge_cnt: int, phase_cnt: int, f_slow_hz: int = F_SLOW_HZ, f_dsp_hz: int = F_DSP_HZ) -> tuple[int, int]:
    """按协议第 5 节算 (freq_hz, phase_milli_deg)，整除截断。

    - 频率：`(edge*f_slow)//gate` 得 Hz（不再乘 1000，避免溢出 2^64）；
    - 相位：按整除分解后乘，并用 kHz 量级的时钟频率（比值不变），
      避免 `360000*phase*f_slow` 溢出 2^64。
    """
    if gate_cnt == 0 or edge_cnt == 0:
        return 0, 0
    freq = (edge_cnt * f_slow_hz) // gate_cnt  # 单位：Hz
    # 相位：整除分解后乘，并用 kHz 量级的时钟频率（比值不变，避免 360000*phase*f_slow 溢出）
    c, d = divmod(phase_cnt, gate_cnt)
    v = 360_000 * c + (360_000 * d) // gate_cnt
    phase = (v * (f_slow_hz // 1000)) // (f_dsp_hz // 1000)
    return freq, phase


# 频率换算：字段单位是 Hz（见 docs/ps-protocol.md 第 5 节），
# 用 `(edge*f_slow)//gate` 得到 kHz 再降到 Hz，中间量 ≤ edge*f_slow ≤ 2.1e17，不过 2^64。


def gate_count(f_sig_hz: float) -> int:
    """闸门握手开销：数到 targetGate 后还要等被测信号下一个上升沿。"""
    if f_sig_hz <= 0:
        return TARGET_GATE
    return TARGET_GATE + math.ceil(F_SLOW_HZ / f_sig_hz)


def round_half_even(num: int | Fraction, den: int | Fraction) -> int:
    """有理数 RNE（round-half-to-even），用于把相位量化到 DSP 采样网格。

    参数可以是 int 或 `Fraction`：450MHz 的 DSP 周期不是整数 ps（20/9 ns），因此这里改用
    有理数精确运算，避免浮点在恰好半格（例如 25MHz/90° = 4.5 个 DSP 周期、
    5MHz/90° = 22.5 个 DSP 周期）上因二进制表示误差给出不可判定结果。
    """
    ratio = Fraction(num) / Fraction(den)
    q = ratio.numerator // ratio.denominator
    r = ratio.numerator - q * ratio.denominator  # 0 <= r < ratio.denominator
    twice = 2 * r
    if twice > ratio.denominator or (twice == ratio.denominator and (q & 1)):
        q += 1
    return q


def round_half_up(value: Fraction) -> int:
    """非负有理数四舍五入（half-up），用于把精确的 ps 相位换算成毫度。"""
    q, r = divmod(value.numerator, value.denominator)
    if 2 * r >= value.denominator:
        q += 1
    return q


def build_vector(freq_hz: int, vco_hz: int, mult: int, div: int, phase_mdeg: int) -> dict:
    f_actual = vco_hz / div
    assert abs(f_actual - freq_hz) < 1.0, f"频点 {freq_hz} 与实际 {f_actual} 不符"
    assert div <= 255, f"D={div} 超出分频字段范围"
    assert 600_000_000 <= vco_hz <= 1_200_000_000, f"VCO={vco_hz} 越界"
    # 相位上界 = 9 位相位码（按 D 逐点），>180° 的请求在相位码范围内同样可测
    assert phase_mdeg < PHASE_FULL_SCALE_MDEG, "相位点必须 < 360°（满量程与 0° 同义）"
    assert mmcm_phase_code(phase_mdeg, div) <= PHASE_CODE_MAX, (
        f"{freq_hz}Hz/{phase_mdeg}mdeg 的相位码 {mmcm_phase_code(phase_mdeg, div)} 超出 9 位" f"（上界 {phase_max_mdeg(div)} mdeg）"
    )

    t_dsp_ps = Fraction(10**12, F_DSP_HZ)  # DSP 周期，ps（450MHz → 20000/9 ps）
    t_sig_ps = Fraction(div * 10**12, vco_hz)  # 信号周期，ps（可能不是整数）
    # 信号周期必须是 DSP 周期的整数倍（等价于 f_dsp / f_sig 为整数）：此时每个信号周期的
    # 相位计数窗口都是同一个整数，`phaseCnt = round(相位/T_dsp) × edgeCnt` 才是**严格**等式
    # （`phaseCnt % edgeCnt == 0`）。非整数时窗口在 k 与 k+1 之间交替，金标准只是近似值。
    assert (t_sig_ps / t_dsp_ps).denominator == 1, (
        f"{freq_hz}Hz 的周期 {float(t_sig_ps):.3f}ps 不是 DSP 周期 {float(t_dsp_ps):.3f}ps 的整数倍"
        f"（f_dsp/f_sig = {F_DSP_HZ / freq_hz:g}，冻结频点必须整除 450MHz）"
    )

    # 相位先被 MMCM 的 1/8 VCO 周期网格量化（方案 A：PS 直接编码 DELAY_TIME/PHASE_MUX），
    # 再由测量通路量化到 DSP 采样网格。`phase_points_for()` 给出的每个点（含 >180° 的
    # 上界点）都精确落在该频点的 45°/D 网格上，因此 `mmcm_mdeg == phase_mdeg`；
    # `assert` 把这个前提固化下来，改频点/相位点时不会静默漂移。
    code = mmcm_phase_code(phase_mdeg, div)
    mmcm_mdeg = mmcm_achievable_mdeg(phase_mdeg, div)
    assert mmcm_mdeg == phase_mdeg, f"{freq_hz}Hz/{phase_mdeg}mdeg 不在 MMCM 网格（45/{div} 度）上：实际 {mmcm_mdeg}"

    gate_cnt = gate_count(f_actual)
    # 参考沿数：窗口长度 / 参考周期 = gate_cnt * T_slow / T_sig = gate_cnt * f_sig / f_slow
    edge_cnt = (gate_cnt * freq_hz) // F_SLOW_HZ

    # 真实相位差（ps，有理数精确值）→ 量化到 DSP 采样网格（整数个 DSP 周期）
    phase_ps = Fraction(mmcm_mdeg, 360_000) * t_sig_ps
    dsp_cycles = round_half_even(phase_ps, t_dsp_ps) if edge_cnt else 0
    phase_ps_actual = dsp_cycles * t_dsp_ps
    phase_cnt = dsp_cycles * edge_cnt

    # 定点输出（整数除法，与 C 侧语义一致；用等价的安全形式避免 64 位溢出，
    # 见 docs/ps-protocol.md 第 5 节）
    freq_milli_hz, phase_milli_deg = fixed_point(gate_cnt, edge_cnt, phase_cnt)

    phase_step_mdeg = round_half_up(Fraction(360_000 * vco_hz, div * F_DSP_HZ))  # 1 个 DSP 周期对应度数
    # 量化偏差：量化后实际相位与请求相位之差（度数，四舍五入到毫度）
    phase_actual_mdeg = round_half_up(phase_ps_actual * 360_000 / t_sig_ps)
    phase_quant_err_mdeg = abs(phase_actual_mdeg - phase_mdeg)

    return {
        "freq_hz": freq_hz,
        "freq_actual_hz": f_actual,
        "vco_hz": vco_hz,
        "mult": mult,
        "divclk": 1,
        "div": div,
        "phase_requested_mdeg": phase_mdeg,
        # MMCM 相位码：低 6 位 = DELAY_TIME（SET_SIG 回包的 phaseField）、高 3 位 = PHASE_MUX
        "phase_code": code,
        "phase_field": (code >> 3) & 0x3F,
        "phase_mux": code & 0x7,
        "phase_mmcm_mdeg": mmcm_mdeg,
        "phase_grid_mdeg_num": 45_000,
        "gate_cnt": gate_cnt,
        "edge_cnt": edge_cnt,
        "phase_cnt": phase_cnt,
        "phase_dsp_cycles": dsp_cycles,
        "phase_expected_mdeg": phase_milli_deg,
        "phase_quant_error_mdeg": phase_quant_err_mdeg,
        "freq_milli_hz": freq_milli_hz,
        "phase_milli_deg": phase_milli_deg,
        "phase_step_mdeg": phase_step_mdeg,
        "phase_tol_mdeg": phase_step_mdeg // 2 + 1,
        "freq_tol_milli_hz": max(1000, (2 * F_SLOW_HZ * 1000) // max(1, edge_cnt) + (2 * F_SLOW_HZ * 1000) // max(1, gate_cnt)),
    }


def build_vectors() -> dict:
    vectors = []
    freq_points = []
    for freq_hz, (vco_hz, mult, div) in FREQ_POINTS_HZ.items():
        solved = solve_sig(freq_hz)
        assert solved == (vco_hz, mult, div), (
            f"频点 {freq_hz} 的解 {solved} 与冻结表 {(vco_hz, mult, div)} 不一致：" "表必须等于求解规则的输出"
        )
        phases = phase_points_for(div)
        freq_points.append(
            {
                "freq_hz": freq_hz,
                "vco_hz": vco_hz,
                "mult": mult,
                "div": div,
                "phases_mdeg": phases,
                "phase_max_mdeg": phase_max_mdeg(div),
                # 相位码上界点（m = 511）对应的可请求相位；D 小时会超过满量程（被请求范围截断）
                "phase_code_top_mdeg": min(phase_code_top_mdeg(div), PHASE_FULL_SCALE_MDEG - 1),
            }
        )
        for phase_mdeg in phases:
            vectors.append(build_vector(freq_hz, vco_hz, mult, div, phase_mdeg))
    union = sorted({p for fp in freq_points for p in fp["phases_mdeg"]})
    return {
        "source": "spec/gen_golden.py（独立参考实现，按 docs/ps-protocol.md 第 5/6 节）",
        "model": {
            "gate_cnt": "target_gate + ceil(T_sig/T_slow)",
            "edge_cnt": "round(gate_cnt*T_slow/T_sig)",
            "phase_cnt": "round(phase_ns/T_dsp) * edge_cnt（相位先被 DSP 采样网格量化）",
        },
        "clocks": {"f_slow_hz": F_SLOW_HZ, "f_dsp_hz": F_DSP_HZ},
        "target_gate": TARGET_GATE,
        "freq_points_hz": sorted(FREQ_POINTS_HZ.keys()),
        # 每个频点的相位点集**不同**（上界 = 9 位相位码，按 D 逐点），
        # phase_points_mdeg 是所有频点相位点的并集（报告/文档用）
        "freq_points": freq_points,
        "phase_points_mdeg": union,
        # 相位网格：MMCM 的 1/8 VCO 周期 = 45000/div 毫度（方案 A，见 ps/include/dpm_regs.h）
        "phase_grid_mdeg_num": 45_000,
        "phase_code_bits": 9,
        "phase_code_max": PHASE_CODE_MAX,
        "phase_max_mdeg_num": PHASE_MAX_MDEG_NUM,
        "vectors": vectors,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成金标准向量")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "vectors.json"))
    ap.add_argument("--print", action="store_true", dest="do_print")
    args = ap.parse_args()

    data = build_vectors()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    n_over = sum(1 for v in data["vectors"] if v["phase_requested_mdeg"] >= 180_000)
    print(
        f"写入 {args.out}：{len(data['vectors'])} 条向量"
        f"（{len(data['freq_points_hz'])} 个频点，相位点每频点不等，并集 "
        f"{len(data['phase_points_mdeg'])} 个；其中 ≥180° 的 {n_over} 条）"
    )
    if args.do_print:
        print(
            f"{'f_sig':>10} {'phase_req':>10} {'gate':>6} {'edge':>6} {'phaseCnt':>9} "
            f"{'f_meas/kHz':>11} {'phase/deg':>10} {'step/deg':>9}"
        )
        for v in data["vectors"]:
            print(
                f"{v['freq_actual_hz']/1e6:>10.4f} {v['phase_requested_mdeg']/1000:>10.1f} "
                f"{v['gate_cnt']:>6} {v['edge_cnt']:>6} {v['phase_cnt']:>9} "
                f"{v['freq_milli_hz']/1000:>11.1f} {v['phase_milli_deg']/1000:>10.3f} "
                f"{v['phase_step_mdeg']/1000:>9.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
