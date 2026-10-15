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

# ---------------------------------------------------------------- 冻结参数

F_SLOW_HZ = 50_000_000      # 低速（AXI/闸门）时钟
F_DSP_HZ = 400_000_000      # 高速（DSP/测量）时钟
TARGET_GATE = 2000          # 默认闸门目标（低速周期数）→ 40µs

# 冻结的验证频点：目标频率 → (VCO, MULT, DIVCLK=1, D)
# 该表必须与 `solve_sig()` 的规则输出一致（文件末尾的 assert 会校验）。
# 约束：600 ≤ VCO ≤ 1200 MHz、分频字段 = D ≤ 255
FREQ_POINTS_HZ = {
    5_000_000: (600_000_000, 12, 120),
    6_250_000: (750_000_000, 15, 120),
    8_000_000: (1_000_000_000, 20, 125),
    10_000_000: (1_000_000_000, 20, 100),
    12_500_000: (1_000_000_000, 20, 80),
    20_000_000: (1_000_000_000, 20, 50),
    25_000_000: (1_000_000_000, 20, 40),
}

# BD 的初始工作点：VCO = 50MHz × 20 = 1000MHz、D = 40（25MHz）
BD_VCO_HZ = 1_000_000_000
BD_DIV = 40
# BD 初始工作点所在的分频范围（CLKOUT_DIVIDE 用 `DIVIDE_F` 表达的常见区间）
BD_DIV_MAX = 127
# 分频比硬上限：超过它不再考虑（见 solve_sig 的说明）
DIV_HARD_MAX = 127


def solve_sig(target_freq_hz: int, f_in_hz: int = F_SLOW_HZ) -> tuple[int, int, int]:
    """求解 MMCM 工作点（与 C 侧 `dpm_sig_solve` 同一规则）。

    规则（冻结，**按顺序逐级比较**）：
      1. `DIVCLK = 1`、`MULT ∈ 12..24` 整数、`VCO = f_in × MULT ∈ [600, 1200] MHz`、
         `D ∈ 1..127`（分频字段 8 位可表示到 255，但硬上限取 127：见下）、不使用小数分频；
      2. 先最小化频率误差 `|VCO/D - f_target|`；
      3. 同误差（含多个精确解）时取 `cost = |VCO - 1000MHz| / 1MHz + |D - 40|` 最小者，
         即尽量贴近 BD 的初始工作点 `(VCO=1000MHz, D=40)`。

    该规则的输出就是冻结频点表（`FREQ_POINTS_HZ` 与之一致，由 `build_vectors()` 断言）：
    8/10/12.5/20/25MHz 落在 `MULT=20, D=125/100/80/50/40`；5MHz 在 1000MHz 下需要 D=200
    （超硬上限 127），取 `600/12/120`；6.25MHz 的候选精确解里 `750/15/120` 的 cost 最小
    （cost = 250+80 = 330 < `600/12/96` 的 456）。

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
                better = (cost, abs(vco - BD_VCO_HZ), d) < \
                         (best[2], abs(best[3] - BD_VCO_HZ), best[5])
            if better:
                best = (err_num, d, cost, vco, mult, d)
    if best is None:
        raise ValueError(f"目标频率 {target_freq_hz} 无解")
    return best[3], best[4], best[5]

# 冻结的相位点（度）：MMCM 相位字段的自然网格 = 1/8 VCO 周期 = 45°（VCO 越高步长越小，
# 但字段本身是整数，写 1 个单位即 45°）。全部 < 180°，满足相位映射前提。
PHASE_POINTS_MDEG = [0, 45_000, 90_000, 135_000]


def fixed_point(gate_cnt: int, edge_cnt: int, phase_cnt: int,
                f_slow_hz: int = F_SLOW_HZ, f_dsp_hz: int = F_DSP_HZ) -> tuple[int, int]:
    """按协议第 5 节算 (freq_hz, phase_milli_deg)，整除截断。

    - 频率：`(edge*f_slow)//gate` 得 Hz（不再乘 1000，避免溢出 2^64）；
    - 相位：按整除分解后乘，并用 kHz 量级的时钟频率（比值不变），
      避免 `360000*phase*f_slow` 溢出 2^64。
    """
    if gate_cnt == 0 or edge_cnt == 0:
        return 0, 0
    freq = (edge_cnt * f_slow_hz) // gate_cnt             # 单位：Hz
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


def round_half_even(num: int, den: int) -> int:
    """整数 RNE（round-half-to-even），用于把相位量化到 DSP 采样网格。

    全程整数运算，避免浮点在恰好半格（例如 8MHz/90° = 12.5 个 DSP 周期、
    20MHz/45° = 2.5 个 DSP 周期）上因二进制表示误差给出不可判定结果。
    """
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and (q & 1)):
        q += 1
    return q


def build_vector(freq_hz: int, vco_hz: int, mult: int, div: int, phase_mdeg: int) -> dict:
    f_actual = vco_hz / div
    assert abs(f_actual - freq_hz) < 1.0, f"频点 {freq_hz} 与实际 {f_actual} 不符"
    assert div <= 255, f"D={div} 超出分频字段范围"
    assert 600_000_000 <= vco_hz <= 1_200_000_000, f"VCO={vco_hz} 越界"
    assert phase_mdeg < 180_000, "相位点必须 < 180°（半个信号周期）"

    t_dsp_ps = 10**12 // F_DSP_HZ          # DSP 周期，ps（400MHz → 2500ps，整除）
    t_sig_ps_num = div * 10**12            # 信号周期 ps 的分子（vco_hz 整除情形）
    assert t_sig_ps_num % vco_hz == 0, "信号周期必须是整数 ps（频点表保证）"
    t_sig_ps = t_sig_ps_num // vco_hz

    gate_cnt = gate_count(f_actual)
    # 参考沿数：窗口长度 / 参考周期 = gate_cnt * T_slow / T_sig = gate_cnt * f_sig / f_slow
    edge_cnt = (gate_cnt * freq_hz) // F_SLOW_HZ

    # 真实相位差（ps）→ 量化到 DSP 采样网格（整数个 DSP 周期）
    phase_ps_num = phase_mdeg * t_sig_ps       # 分子（单位 1e-3 度 · ps）
    dsp_cycles = round_half_even(phase_ps_num, 360_000 * t_dsp_ps) if edge_cnt else 0
    phase_ps_actual = dsp_cycles * t_dsp_ps
    phase_cnt = dsp_cycles * edge_cnt

    # 定点输出（整数除法，与 C 侧语义一致；用等价的安全形式避免 64 位溢出，
    # 见 docs/ps-protocol.md 第 5 节）
    freq_milli_hz, phase_milli_deg = fixed_point(gate_cnt, edge_cnt, phase_cnt)

    phase_step_mdeg = round(360_000.0 * f_actual / F_DSP_HZ)  # 1 个 DSP 周期对应度数
    # 量化偏差：量化后实际相位与请求相位之差（度数，四舍五入到毫度）
    phase_actual_mdeg = (phase_ps_actual * 360_000 + t_sig_ps // 2) // t_sig_ps
    phase_quant_err_mdeg = abs(phase_actual_mdeg - phase_mdeg)

    return {
        "freq_hz": freq_hz,
        "freq_actual_hz": f_actual,
        "vco_hz": vco_hz,
        "mult": mult,
        "divclk": 1,
        "div": div,
        "phase_requested_mdeg": phase_mdeg,
        "phase_field_45deg": phase_mdeg // 45_000,
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
        "freq_tol_milli_hz": max(1000, (2 * F_SLOW_HZ * 1000) // max(1, edge_cnt)
                                 + (2 * F_SLOW_HZ * 1000) // max(1, gate_cnt)),
    }


def build_vectors() -> dict:
    vectors = []
    for freq_hz, (vco_hz, mult, div) in FREQ_POINTS_HZ.items():
        solved = solve_sig(freq_hz)
        assert solved == (vco_hz, mult, div), (
            f"频点 {freq_hz} 的解 {solved} 与冻结表 {(vco_hz, mult, div)} 不一致："
            "表必须等于求解规则的输出")
        for phase_mdeg in PHASE_POINTS_MDEG:
            vectors.append(build_vector(freq_hz, vco_hz, mult, div, phase_mdeg))
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
        "phase_points_mdeg": PHASE_POINTS_MDEG,
        "phase_unit_deg_mdeg": 45_000,
        "vectors": vectors,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成金标准向量")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "golden", "vectors.json"))
    ap.add_argument("--print", action="store_true", dest="do_print")
    args = ap.parse_args()

    data = build_vectors()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"写入 {args.out}：{len(data['vectors'])} 条向量"
          f"（{len(data['freq_points_hz'])} 个频点 × {len(data['phase_points_mdeg'])} 个相位点）")
    if args.do_print:
        print(f"{'f_sig':>10} {'phase_req':>10} {'gate':>6} {'edge':>6} {'phaseCnt':>9} "
              f"{'f_meas/kHz':>11} {'phase/deg':>10} {'step/deg':>9}")
        for v in data["vectors"]:
            print(f"{v['freq_actual_hz']/1e6:>10.4f} {v['phase_requested_mdeg']/1000:>10.1f} "
                  f"{v['gate_cnt']:>6} {v['edge_cnt']:>6} {v['phase_cnt']:>9} "
                  f"{v['freq_milli_hz']/1000:>11.1f} {v['phase_milli_deg']/1000:>10.3f} "
                  f"{v['phase_step_mdeg']/1000:>9.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
