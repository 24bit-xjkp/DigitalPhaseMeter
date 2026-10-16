#!/usr/bin/env python3
"""规格一致性自检：防止协议文档、C 头文件、Python 实现、金标准向量之间悄悄漂移。

用法：
    python3 spec/selftest_spec.py            # 退出码 0 = 一致
    python3 spec/selftest_spec.py -v

检查项：
1. `spec/golden/vectors.json` 与 `spec/gen_golden.py` 重新生成的结果一致（防止手改向量）；
2. 金标准向量的自洽性：`phase_cnt % edge_cnt == 0`、`freq`/`phase` 定点公式可复算、
   `div` 与 `vco_hz` 满足约束、`div` 等于分频字段可表示范围；
3. C 头文件 `ps/include/dpm_regs.h` 的关键宏值与文档一致（分频字段 = D 本体、相位字段在 bit[15:8]、
   DRP 偏移、寄存器偏移）；
4. C 头文件 `ps/include/dpm_proto.h` 的命令码与 `docs/ps-protocol.md` 第 3 节的命令表一致；
5. 协议文档里的冻结频点表与 `spec/gen_golden.py` 的频点表一致。
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
FAILURES: list[str] = []
CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        if VERBOSE:
            print(f"  ok   {msg}")
    else:
        FAILURES.append(msg)
        print(f"  FAIL {msg}")


def load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    gen = load_module(os.path.join(HERE, "gen_golden.py"), "gen_golden")
    golden_path = os.path.join(HERE, "golden", "vectors.json")
    golden = json.loads(read(golden_path))

    print("[1] 金标准向量可复现")
    fresh = gen.build_vectors()
    check(golden["vectors"] == fresh["vectors"], "vectors.json 与 gen_golden.py 重新生成的结果一致")
    check(golden["freq_points_hz"] == fresh["freq_points_hz"], "频点集合一致")
    check(golden["phase_points_mdeg"] == fresh["phase_points_mdeg"], "相位点集合并集一致")
    check(golden["freq_points"] == fresh["freq_points"], "逐频点相位点表一致（含各频点相位上界）")
    check(
        golden.get("phase_code_max") == 0x1FF and golden.get("phase_max_mdeg_num") == 23_017_499,
        "金标准写明 9 位相位码上限与相位上界分子（m ≤ 511）",
    )

    print("[2] 金标准向量自洽")
    for v in golden["vectors"]:
        tag = f"{v['freq_actual_hz']/1e6:.4f}MHz/{v['phase_requested_mdeg']/1000:.1f}deg"
        if v["edge_cnt"]:
            check(v["phase_cnt"] % v["edge_cnt"] == 0, f"{tag}: phase_cnt 是 edge_cnt 的整数倍")
        check(v["div"] == v["vco_hz"] // v["freq_hz"], f"{tag}: div = VCO/f")
        check(1 <= v["div"] <= 255, f"{tag}: div 在分频字段范围内")
        check(600_000_000 <= v["vco_hz"] <= 1_200_000_000, f"{tag}: VCO 在 600~1200MHz")
        check(v["mult"] * gen.F_SLOW_HZ == v["vco_hz"], f"{tag}: VCO = f_in * MULT")
        f_calc = (v["edge_cnt"] * gen.F_SLOW_HZ) // v["gate_cnt"]
        p_calc = (360_000 * v["phase_cnt"] * gen.F_SLOW_HZ) // (v["gate_cnt"] * gen.F_DSP_HZ)
        check(v["freq_milli_hz"] == f_calc, f"{tag}: freq 定点公式可复算")
        check(v["phase_milli_deg"] == p_calc, f"{tag}: phase 定点公式可复算")
        check(v["phase_requested_mdeg"] < 360_000, f"{tag}: 相位 < 360°（满量程）")
        # 相位上界 = 9 位相位码（按 D 逐点）：>180° 的请求在相位码范围内同样合法
        check(
            gen.mmcm_phase_code(v["phase_requested_mdeg"], v["div"]) <= gen.PHASE_CODE_MAX,
            f"{tag}: 相位码 ≤ {gen.PHASE_CODE_MAX}（m = round(相位×D/45000)）",
        )
        check(v["phase_requested_mdeg"] <= gen.phase_max_mdeg(v["div"]), f"{tag}: 相位 ≤ 该频点上界 {gen.phase_max_mdeg(v['div'])} mdeg")

    print("[2b] 溢出安全形式 == 朴素公式（随机抽样，逐位一致）")
    rng = random.Random(20251015)
    for _ in range(400):
        gate = rng.randint(200, 300000)
        edge = rng.randint(1, 2**20)
        cycles = rng.randint(0, max(1, edge // 8))
        phase = cycles * edge
        naive_f = (edge * gen.F_SLOW_HZ) // gate
        naive_p = (360_000 * phase * gen.F_SLOW_HZ) // (gate * gen.F_DSP_HZ)
        safe_f, safe_p = gen.fixed_point(gate, edge, phase)
        check(safe_f == naive_f and safe_p == naive_p, f"随机样本 gate={gate} edge={edge} phase={phase}：安全形式与朴素公式一致")

    print("[3] C 头文件：寄存器与 DRP 编码")
    regs = read(os.path.join(ROOT, "ps", "include", "dpm_regs.h"))
    for off, name in [
        ("0x00u", "DPM_REG_CTRL"),
        ("0x04u", "DPM_REG_STATUS"),
        ("0x08u", "DPM_REG_GATE_TARGET"),
        ("0x0Cu", "DPM_REG_RESULT_GATE"),
        ("0x10u", "DPM_REG_RESULT_EDGE"),
        ("0x14u", "DPM_REG_RESULT_PHASE"),
        ("0x18u", "DPM_REG_RESULT_SEQ"),
        ("0x1Cu", "DPM_REG_LOST_COUNT"),
        ("0x20u", "DPM_REG_IRQ_STATUS"),
        ("0x24u", "DPM_REG_VERSION"),
        ("0x28u", "DPM_REG_CAPS"),
        ("0x2Cu", "DPM_REG_SIG_CTRL"),
        ("0x30u", "DPM_REG_SIG_STATUS"),
    ]:
        check(re.search(rf"#define {name} {re.escape(off)}", regs) is not None, f"{name} 偏移为 {off}")
    # 方案 A：PL 侧只有 MMCM 的 16 位寄存器镜像 + LOAD（0x43C1_0000 段）
    for off, name in [
        ("0x00u", "DPM_DRP_OFF_ID"),
        ("0x04u", "DPM_DRP_OFF_STATUS"),
        ("0x08u", "DPM_DRP_OFF_CLKFBOUT"),
        ("0x0Cu", "DPM_DRP_OFF_CLKFBOUT2"),
        ("0x10u", "DPM_DRP_OFF_CLKOUT0"),
        ("0x14u", "DPM_DRP_OFF_CLKOUT0_2"),
        ("0x18u", "DPM_DRP_OFF_CLKOUT1"),
        ("0x1Cu", "DPM_DRP_OFF_CLKOUT1_2"),
        ("0x20u", "DPM_DRP_OFF_DIVCLK"),
        ("0x24u", "DPM_DRP_OFF_POWER"),
        ("0x28u", "DPM_DRP_OFF_LOAD"),
    ]:
        check(re.search(rf"#define {name} {re.escape(off)}", regs) is not None, f"{name} 偏移为 {off}（方案 A 的寄存器镜像）")
    check("#define DPM_DRP_PHASE_WRITABLE 1" in regs, "相位可在线改（PS 直接编码 MMCM 的 PHASE_MUX/DELAY_TIME）")
    check("#define DPM_DRP_STATUS_OFF 0x04u" in regs, "DRP 状态寄存器在偏移 0x04（bit0 = locked）")
    check("DPM_MMCM_PHASE_STEP_MDEG_NUM 45000u" in regs, "相位网格分子 45000（= 45°/D 的分子）")
    # 相位拆分必须与厂商 mmcm_pll_phase 一致：小数进 PHASE_MUX、整数进 DELAY_TIME
    check(re.search(r"#define DPM_MMCM_ENCODE_CLKOUT", regs) is not None, "存在 MMCM 寄存器编码宏")
    check(
        "((m_ & 0x7u) << 13)" in regs and "((m_ >> 3) & 0x3Fu)" in regs,
        "PHASE_MUX = 相位码低 3 位的分数、DELAY_TIME = 相位码 >> 3（与 mmcm_pll_phase 一致）",
    )
    check("#define DPM_VERSION_VALUE 0x44504D02u" in regs, "VERSION 期望值 0x44504D02")

    print("[3b] 分频上限三处一致（D ≤ 126：HIGH_TIME + LOW_TIME 各 6 位）")
    doc = read(os.path.join(ROOT, "docs", "ps-protocol.md"))
    calc = read(os.path.join(ROOT, "ps", "include", "dpm_calc.h"))
    host_sig = read(os.path.join(ROOT, "host", "dpm_host", "sig.py"))
    check(re.search(r"#define DPM_SIG_DIV_MAX 126u", calc) is not None, "C 侧 DPM_SIG_DIV_MAX = 126")
    check(re.search(r"^DIV_MAX = 126$", host_sig, re.M) is not None, "上位机 DIV_MAX = 126")
    check(gen.DIV_HARD_MAX == 126, f"金标准生成器硬上限 126（实际 {gen.DIV_HARD_MAX}）")
    check("D ∈ 1..126" in doc or "D ≤ 126" in doc, "协议文档写明分频上限 126（127 的 LOW_TIME 装不进 6 位）")

    print("[4] C 头文件命令码 vs 协议文档")
    proto = read(os.path.join(ROOT, "ps", "include", "dpm_proto.h"))
    doc = read(os.path.join(ROOT, "docs", "ps-protocol.md"))
    cmds = {
        "PING": 0x01,
        "IDENTIFY": 0x02,
        "GET_STATUS": 0x03,
        "START": 0x04,
        "GET_RESULT": 0x05,
        "GET_MEAS": 0x06,
        "SET_GATE": 0x07,
        "SET_MODE": 0x08,
        "SET_SOURCE": 0x09,
        "SET_SIG": 0x0A,
        "GET_SIG": 0x0B,
        "CLEAR": 0x0C,
        "STREAM": 0x0D,
        "RESET_COUNTERS": 0x0E,
    }
    for name, code in cmds.items():
        check(re.search(rf"#define DPM_CMD_{name} 0x{code:02X}u", proto) is not None, f"dpm_proto.h 定义 DPM_CMD_{name} = 0x{code:02X}")
        check(
            re.search(rf"\|\s*0x{code:02X}\s*\|\s*`?{name}", doc) is not None
            or re.search(rf"0x{code:02X}\s*\|\s*`{name}`", doc) is not None,
            f"协议文档表格含 0x{code:02X} {name}",
        )
    for code, name in ((0xFE, "ERR_PARAM"), (0xFF, "ERR_BUSY")):
        check(f"0x{code:02X}" in doc and name in doc, f"协议文档含错误码 0x{code:02X} {name}")

    print("[5] 协议文档冻结频点 vs gen_golden.py")
    for freq_hz, (vco, mult, div) in gen.FREQ_POINTS_HZ.items():
        mhz = freq_hz / 1e6
        mhz_txt = f"{mhz:g}"
        vco_mhz = f"{vco / 1e6:g}"
        row = re.search(rf"\|\s*{re.escape(mhz_txt)}\s*MHz\s*\|\s*{re.escape(vco_mhz)}\s*\|\s*" rf"{mult}\s*\|\s*1\s*\|\s*{div}\s*\|", doc)
        check(row is not None, f"协议文档频点表含 {mhz_txt}MHz → VCO={vco_mhz} MULT={mult} D={div}")
    phase_list = ", ".join(f"{p / 1000:g}" for p in golden["phase_points_mdeg"])
    check(phase_list in doc, f"协议文档的相位点并集与金标准一致（{phase_list}）")
    # 逐频点相位点表：>180° 的测试点受 9 位相位码限制，各频点能测的范围不同
    for fp in golden["freq_points"]:
        per_freq = ", ".join(f"{p / 1000:g}" for p in fp["phases_mdeg"])
        check(per_freq in doc, f"协议文档相位点表含 {fp['freq_hz'] / 1e6:g}MHz 的相位点（{per_freq}）")
    check("23017499" in doc or "m ≤ 511" in doc or "m <= 511" in doc, "协议文档写明相位上界 = 9 位相位码（m ≤ 511 / 23017499）")
    check("0x43C1_0000" in doc and "MMCM 寄存器镜像" in doc, "协议文档写明方案 A 的寄存器镜像段（0x43C1_0000）")
    check("PHASE_MUX" in doc and "DELAY_TIME" in doc and "45°/D" in doc, "协议文档写明相位编码（PHASE_MUX/DELAY_TIME，网格 45°/D）")
    check("必须先拉高 MMCM 的 RST" in doc, "协议文档写明重配置必须先拉高 MMCM 的 RST（写 DRP 的前提）")

    print(f"\n共 {CHECKS} 项检查，失败 {len(FAILURES)} 项")
    if FAILURES:
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
