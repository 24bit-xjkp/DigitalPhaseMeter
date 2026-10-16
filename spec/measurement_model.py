#!/usr/bin/env python3
"""数字相位计的测量模型——**独立参考实现**（纯标准库，无三方依赖）。

语义来源：
  - `.agents/skills/dpm-measurement-model`（gateCnt / edgeCnt / phaseCnt 的语义与容限）；
  - `docs/ps-protocol.md` 第 5 节（频率与相位的整数定点输出公式、整除截断）；
  - `docs/ps-protocol.md` 第 6 节（片内信号源的频率/相位换算，供上游取点用）。

本文件与 `host/`、`ps/` 两侧的实现**互相独立**：它只用整数与 `math` 推导期望值，
用于生成金标准向量（`spec/golden/vectors_model.json`）并交叉校验两侧实现，
不 import 任何项目内模块。

核心公式（`T_slow` / `T_dsp` / `T_sig` 分别为低速时钟、高速时钟、被测信号的周期）：

    gate_cnt  = target_gate + ceil(T_sig / T_slow)          # 闸门到点后还要等被测信号的下一个沿
    edge_cnt  = round(gate_cnt * T_slow / T_sig)            # 窗口内参考信号的上升沿数
    phase_cnt = round(phase_deg/360 * T_sig / T_dsp * edge_cnt)
                                                            # 每个参考周期累加「参考沿→被测沿」间隔

容限（不是设计余量，而是窗口边界与采样网格的量化下界）：

    gate_cnt  : round(T_sig / T_slow) + 1
    edge_cnt  : ±2
    phase_cnt : ±edge_cnt（每个参考周期最多差 1 个高速时钟周期的量化）
    相位量化步长 = 360 * f_sig / f_dsp  [度]

定点输出（整数、整除截断，两侧必须一致，见 `docs/ps-protocol.md` 第 5 节）：

    freq_milli_hz   = edge_cnt * (f_slow * 1000) // gate_cnt
    phase_milli_deg = (phase_cnt * f_slow * 360000) // (gate_cnt * f_dsp)

用法：

    python3 spec/measurement_model.py                        # 写到 spec/golden/vectors_model.json
    python3 spec/measurement_model.py --out /tmp/v.json --print
    python3 spec/measurement_model.py --selftest             # 只跑内置自检
    python3 -c "import sys; sys.path.insert(0,'spec'); import measurement_model"

被 import 时没有任何副作用。
"""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import asdict, dataclass, field
from typing import Iterable, Sequence

# ------------------------------------------------------------------ 冻结默认值

F_SLOW_HZ = 50_000_000       # 低速（AXI/闸门）时钟：PS 的 FCLK_CLK0
F_DSP_HZ = 400_000_000       # 高速（DSP/测量）时钟：PL 内 MMCM 倍频
TARGET_GATE = 2000           # 默认闸门目标（低速周期数）→ 40µs

# 冻结的验证频点（Hz），与 docs/ps-protocol.md 第 6 节的频点表一致。
# 要求信号周期既是整数 ps、也是 DSP 周期（2.5ns）的整数倍（8D/MULT 为整数），
# 此时 `phaseCnt % edgeCnt == 0` 严格成立（见 spec/gen_golden.py 的说明）。
DEFAULT_FREQS_HZ = (
    5_000_000,
    6_250_000,
    8_000_000,
    10_000_000,
    12_500_000,
    16_000_000,
    20_000_000,
    25_000_000,
    40_000_000,
    50_000_000,
)
# 冻结的相位点（度）：0~315° 的 45° 网格点。
# 相位计测的是「sig1 上升沿 → 下一个 sig2 上升沿」的**无符号**间隔（落在 (0, 360°]），
# 因此 >180° 的请求同样是可测的；只有把它当成有符号偏移（|δ| < T/2）解释时才需要
# < 180° 的前提（见 `predict()` 的说明与 dpm-measurement-model skill）。
DEFAULT_PHASES_DEG = (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0)

# 默认输出：不覆盖别的 teammate 生成的 spec/golden/vectors.json（见 docs/verify-drp.md）
DEFAULT_OUT = os.path.join("spec", "golden", "vectors_model.json")


# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True)
class Prediction:
    """一次测量的理论值与容限（单位见字段名）。"""

    # 输入
    f_slow_hz: float
    f_dsp_hz: float
    f_sig_hz: float
    phase_deg: float
    target_gate: int
    # 派生周期
    t_slow_s: float
    t_dsp_s: float
    t_sig_s: float
    # 三个观测量
    gate_cnt: int
    edge_cnt: int
    phase_cnt: int
    # 容限
    gate_cnt_tol: int
    edge_cnt_tol: int
    phase_cnt_tol: int
    # 量化
    phase_step_deg: float
    phase_step_mdeg: int
    phase_cnt_per_edge: float
    # 定点输出（docs/ps-protocol.md 第 5 节）
    freq_milli_hz: int
    phase_milli_deg: int


@dataclass(frozen=True)
class Vector:
    """金标准向量的一项（键名固定，供 C / Python 两侧读取）。"""

    freq_hz: int
    phase_mdeg: int
    gate_cnt: int
    edge_cnt: int
    phase_cnt: int
    f_meas_milli_hz: int
    phase_meas_milli_deg: int
    phase_cycles: int
    # --- 以下是补充信息，主键之外的下游可选用 ---
    f_sig_hz: float = 0.0
    phase_deg: float = 0.0
    gate_cnt_tol: int = 0
    edge_cnt_tol: int = 0
    phase_cnt_tol: int = 0
    phase_step_mdeg: int = 0
    phase_cnt_per_edge_milli: int = 0
    phase_cycles_float: float = 0.0
    phase_expected_mdeg: int = 0      # 量化到 DSP 网格后的期望相位（毫度）
    phase_quant_error_mdeg: int = 0   # 请求相位与量化期望之差（毫度）

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ 基本工具


def _ceil_div(a: int, b: int) -> int:
    """整数向上取整除法（b > 0）。"""
    return -((-a) // b)


def _round_half_up(x: float) -> int:
    """四舍五入（.5 向上），与两侧定点实现的约定一致。"""
    return int(math.floor(x + 0.5))


def _div_trunc(a: int, b: int) -> int:
    """整除截断（向下取整）；b == 0 时返回 0，便于「源没边沿」的诊断路径。"""
    if b == 0:
        return 0
    return a // b


# ------------------------------------------------------------------ 核心模型


def predict(
    f_slow: float,
    f_dsp: float,
    f_sig: float,
    phase_deg: float,
    target_gate: int,
) -> Prediction:
    """给定 (f_slow, f_dsp, f_sig, phase_deg, target_gate) 推导理论值与容限。

    参数：
      f_slow     低速（AXI/闸门）时钟频率 [Hz]
      f_dsp      高速（DSP/测量）时钟频率 [Hz]
      f_sig      被测信号频率 [Hz]（sig1 与 sig2 同频，这也是设计前提）
      phase_deg  sig2 上升沿相对 sig1 上升沿的相位差 [度]，要求 0 ≤ phase_deg < 360
                 （**无符号**间隔：相位计量的是「sig1 沿 → 下一个 sig2 沿」，
                 >180° 同样可测；只有按有符号偏移解释时才需要 < 180°）
      target_gate 闸门目标（低速周期数）

    返回 `Prediction`。全部为浮点/整数运算，可直接与两侧实现对照。
    """
    if f_slow <= 0 or f_dsp <= 0 or f_sig <= 0:
        raise ValueError("f_slow / f_dsp / f_sig 必须为正")
    if target_gate <= 0:
        raise ValueError("target_gate 必须为正")
    if not (0.0 <= phase_deg < 360.0):
        # 相位计测的是无符号间隔 (0, 360°]：>180° 也能测；360° 与 0° 同义，满量程不接收。
        # （旧版本按有符号偏移解释、要求 < 180°，见 docs/verify-plan.md 的说明。）
        raise ValueError("phase_deg 必须在 [0, 360) 内（无符号相位差的口径）")

    t_slow = 1.0 / f_slow
    t_dsp = 1.0 / f_dsp
    t_sig = 1.0 / f_sig

    ratio_sig_slow = t_sig / t_slow                      # = f_slow / f_sig
    # 1) 闸门：Controller 数到 target_gate 后还要等被测信号的下一个上升沿
    gate_cnt = target_gate + _ceil_div(int(round(t_sig * 1e18)), int(round(t_slow * 1e18)))
    # 2) 参考沿数：窗口长度 ÷ 被测周期
    edge_cnt = _round_half_up(gate_cnt * t_slow / t_sig)
    # 3) 相位计数：每个参考周期累加「参考沿 → 被测沿」的间隔（高速时钟周期数）
    phase_cnt = _round_half_up(phase_deg / 360.0 * (t_sig / t_dsp) * edge_cnt)

    # 容限
    gate_cnt_tol = _round_half_up(ratio_sig_slow) + 1
    edge_cnt_tol = 2
    phase_cnt_tol = edge_cnt

    # 量化步长：一个高速时钟周期对应多少度
    phase_step_deg = 360.0 * f_sig / f_dsp
    phase_step_mdeg = int(round(phase_step_deg * 1000.0))
    phase_cnt_per_edge = phase_cnt / edge_cnt if edge_cnt else 0.0

    # 定点输出（整除截断）
    freq_milli_hz = _div_trunc(edge_cnt * int(round(f_slow * 1000)), gate_cnt)
    phase_milli_deg = _div_trunc(phase_cnt * int(round(f_slow)) * 360000, gate_cnt * int(round(f_dsp)))

    return Prediction(
        f_slow_hz=float(f_slow),
        f_dsp_hz=float(f_dsp),
        f_sig_hz=float(f_sig),
        phase_deg=float(phase_deg),
        target_gate=int(target_gate),
        t_slow_s=t_slow,
        t_dsp_s=t_dsp,
        t_sig_s=t_sig,
        gate_cnt=int(gate_cnt),
        edge_cnt=int(edge_cnt),
        phase_cnt=int(phase_cnt),
        gate_cnt_tol=int(gate_cnt_tol),
        edge_cnt_tol=int(edge_cnt_tol),
        phase_cnt_tol=int(phase_cnt_tol),
        phase_step_deg=phase_step_deg,
        phase_step_mdeg=phase_step_mdeg,
        phase_cnt_per_edge=phase_cnt_per_edge,
        freq_milli_hz=int(freq_milli_hz),
        phase_milli_deg=int(phase_milli_deg),
    )


def generate_vectors(
    freqs: Iterable[float],
    phases: Iterable[float],
    target_gate: int = TARGET_GATE,
    f_slow: float = F_SLOW_HZ,
    f_dsp: float = F_DSP_HZ,
) -> list[dict]:
    """生成金标准向量：`freqs × phases` 的笛卡尔积。

    参数：
      freqs        被测信号频率 [Hz]（可为浮点，`freq_hz` 取四舍五入后的整数）
      phases       相位差 [度]（可为小数，如 22.5；内部换算成整数毫度）
      target_gate  闸门目标（低速周期数）
      f_slow       低速时钟频率 [Hz]
      f_dsp        高速时钟频率 [Hz]

    每项含（键名固定）：
      freq_hz, phase_mdeg, gate_cnt, edge_cnt, phase_cnt,
      f_meas_milli_hz, phase_meas_milli_deg, phase_cycles
    另附容限、量化步长与量化后期望相位等补充键，便于下游写判据。
    """
    vectors: list[dict] = []
    for f_sig in freqs:
        for phase_deg in phases:
            if not (0.0 <= float(phase_deg) < 360.0):
                raise ValueError(f"相位点 {phase_deg} 不在 [0, 360) 内（无符号相位差口径）")
            pred = predict(f_slow, f_dsp, f_sig, float(phase_deg), target_gate)
            phase_mdeg = int(round(float(phase_deg) * 1000.0)) if abs(float(phase_deg)) < 1e6 else int(phase_deg)
            phase_cycles = _div_trunc(pred.phase_cnt, pred.edge_cnt)
            # 把请求相位量化到「整数个 DSP 周期」的网格上：期望实测值
            dsp_cycles_exact = float(phase_deg) / 360.0 * (pred.t_sig_s / pred.t_dsp_s)
            phase_expected_mdeg = int(round(_round_half_up(dsp_cycles_exact) * 360.0 * pred.t_dsp_s / pred.t_sig_s * 1000.0))
            vec = Vector(
                freq_hz=int(round(f_sig)),
                phase_mdeg=phase_mdeg,
                gate_cnt=pred.gate_cnt,
                edge_cnt=pred.edge_cnt,
                phase_cnt=pred.phase_cnt,
                f_meas_milli_hz=pred.freq_milli_hz,
                phase_meas_milli_deg=pred.phase_milli_deg,
                phase_cycles=phase_cycles,
                f_sig_hz=pred.f_sig_hz,
                phase_deg=float(phase_deg),
                gate_cnt_tol=pred.gate_cnt_tol,
                edge_cnt_tol=pred.edge_cnt_tol,
                phase_cnt_tol=pred.phase_cnt_tol,
                phase_step_mdeg=pred.phase_step_mdeg,
                phase_cnt_per_edge_milli=int(round(pred.phase_cnt_per_edge * 1000.0)),
                phase_cycles_float=dsp_cycles_exact,
                phase_expected_mdeg=phase_expected_mdeg,
                phase_quant_error_mdeg=phase_expected_mdeg - phase_mdeg,
            )
            vectors.append(vec.to_dict())
    return vectors


def build_document(
    freqs: Sequence[float] | None = None,
    phases: Sequence[float] | None = None,
    target_gate: int = TARGET_GATE,
    f_slow: float = F_SLOW_HZ,
    f_dsp: float = F_DSP_HZ,
) -> dict:
    """生成完整的金标准 JSON 文档（含模型说明与时钟参数）。"""
    freqs = list(DEFAULT_FREQS_HZ if freqs is None else freqs)
    phases = list(DEFAULT_PHASES_DEG if phases is None else phases)
    return {
        "source": "spec/measurement_model.py（独立参考实现，按 dpm-measurement-model 与 ps-protocol.md 第 5 节）",
        "model": {
            "gate_cnt": "target_gate + ceil(T_sig/T_slow)",
            "edge_cnt": "round(gate_cnt*T_slow/T_sig)",
            "phase_cnt": "round(phase_deg/360 * T_sig/T_dsp * edge_cnt)",
            "gate_cnt_tol": "round(T_sig/T_slow) + 1",
            "edge_cnt_tol": "±2",
            "phase_cnt_tol": "±edge_cnt",
            "phase_step_deg": "360*f_sig/f_dsp",
            "freq_milli_hz": "edge_cnt*(f_slow*1000)//gate_cnt（整除截断）",
            "phase_milli_deg": "(phase_cnt*f_slow*360000)//(gate_cnt*f_dsp)（整除截断）",
            "phase_cycles": "phase_cnt//edge_cnt（每个参考周期对应的整数个 DSP 周期）",
        },
        "clocks": {"f_slow_hz": f_slow, "f_dsp_hz": f_dsp},
        "target_gate": target_gate,
        "freq_points_hz": [int(round(f)) for f in freqs],
        "phase_points_mdeg": [int(round(p * 1000.0)) for p in phases],
        "vector_count": len(freqs) * len(phases),
        "vectors": generate_vectors(freqs, phases, target_gate, f_slow, f_dsp),
    }


# ------------------------------------------------------------------ 自检


def selftest() -> int:
    """内置自检：把本模型与手算值、skill 里的语义对照。"""
    failures = 0

    def check(name: str, actual, expected) -> None:
        nonlocal failures
        if actual != expected:
            failures += 1
            print(f"FAIL {name}: 实际 {actual} 期望 {expected}")
        else:
            print(f"ok   {name} = {actual}")

    # 25MHz、40µs 闸门、90°、50MHz/400MHz
    p = predict(F_SLOW_HZ, F_DSP_HZ, 25_000_000, 90.0, 2000)
    check("gate_cnt(25MHz,gate=2000)", p.gate_cnt, 2002)      # 2000 + ceil(40ns/20ns)
    check("edge_cnt(25MHz)", p.edge_cnt, 1001)                # round(2002*20/40)
    check("phase_cnt(90°@25MHz)", p.phase_cnt, 4004)          # round(0.25*16*1001)
    check("phase_step_mdeg(25MHz)", p.phase_step_mdeg, 22500)  # 360*25/400 = 22.5°
    check("freq_milli_hz(25MHz)", p.freq_milli_hz, 25_000_000_000)
    check("phase_milli_deg(90°@25MHz)", p.phase_milli_deg, 90_000)
    check("phase_cycles(90°@25MHz)", _div_trunc(p.phase_cnt, p.edge_cnt), 4)

    # 10MHz、22.5°：量化步长 9°，22.5/9 = 2.5 个 DSP 周期（半格，落在量化下界内）
    q = predict(F_SLOW_HZ, F_DSP_HZ, 10_000_000, 22.5, 2000)
    check("gate_cnt(10MHz)", q.gate_cnt, 2005)                # 2000 + ceil(100ns/20ns)
    check("edge_cnt(10MHz)", q.edge_cnt, 401)                 # round(2005*20/100)
    check("phase_step_mdeg(10MHz)", q.phase_step_mdeg, 9000)
    check("phase_cnt(22.5°@10MHz)", q.phase_cnt, 1003)        # round(0.0625*40*401) = round(1002.5)

    # 无符号相位差口径：180° 与 270° 都是可测点，只有 360°（与 0° 同义）必须报错
    for deg, want in ((180.0, 180.0), (270.0, 270.0)):
        r = predict(F_SLOW_HZ, F_DSP_HZ, 25_000_000, deg, 2000)
        # 25MHz/400MHz：1 个 DSP 周期 = 22.5°，180° = 8 个周期、270° = 12 个周期
        check(f"phase_cnt({deg:g}°@25MHz)", r.phase_cnt // r.edge_cnt, int(want / 22.5))
        check(f"phase_meas({deg:g}°@25MHz)", r.phase_milli_deg, int(want * 1000))
    try:
        predict(F_SLOW_HZ, F_DSP_HZ, 25_000_000, 360.0, 2000)
        print("FAIL 相位 360° 未被拒绝")
        failures += 1
    except ValueError:
        print("ok   相位 360° 被拒绝（满量程，与 0° 同义）")

    # 向量生成：键齐全
    vecs = generate_vectors([25_000_000], [0.0, 22.5], 2000)
    check("向量条数", len(vecs), 2)
    required = {
        "freq_hz",
        "phase_mdeg",
        "gate_cnt",
        "edge_cnt",
        "phase_cnt",
        "f_meas_milli_hz",
        "phase_meas_milli_deg",
        "phase_cycles",
    }
    check("向量键齐全", required.issubset(vecs[0].keys()), True)
    check("22.5° 的毫度", vecs[1]["phase_mdeg"], 22500)

    print(f"自检{'通过' if failures == 0 else f'失败（{failures} 项）'}")
    return 0 if failures == 0 else 1


# ------------------------------------------------------------------ 命令行


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数字相位计测量模型的独立参考实现")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"输出 JSON 路径（默认 {DEFAULT_OUT}）")
    parser.add_argument("--freqs", type=float, nargs="*", default=None, help="被测频率点 [Hz]")
    parser.add_argument("--phases", type=float, nargs="*", default=None, help="相位点 [度]")
    parser.add_argument("--target-gate", type=int, default=TARGET_GATE, help="闸门目标（低速周期数）")
    parser.add_argument("--f-slow", type=float, default=F_SLOW_HZ, help="低速时钟频率 [Hz]")
    parser.add_argument("--f-dsp", type=float, default=F_DSP_HZ, help="高速时钟频率 [Hz]")
    parser.add_argument("--print", dest="do_print", action="store_true", help="同时打印向量摘要")
    parser.add_argument("--selftest", action="store_true", help="只跑内置自检")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    doc = build_document(args.freqs, args.phases, args.target_gate, args.f_slow, args.f_dsp)
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"已写入 {out_path}：{doc['vector_count']} 条向量"
          f"（{len(doc['freq_points_hz'])} 个频点 × {len(doc['phase_points_mdeg'])} 个相位点）")
    if args.do_print:
        for vec in doc["vectors"]:
            print(
                f"  f={vec['freq_hz'] / 1e6:7.4f} MHz phase={vec['phase_mdeg'] / 1000:8.3f}° "
                f"gate={vec['gate_cnt']} edge={vec['edge_cnt']} phase_cnt={vec['phase_cnt']} "
                f"step={vec['phase_step_mdeg'] / 1000:5.2f}°"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
