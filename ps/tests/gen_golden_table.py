#!/usr/bin/env python3
"""从 `spec/golden/vectors.json` 生成 C 侧金标准表（`ps/tests/golden_table.h`）。

C 代码不解析 JSON：把 `vectors.json` 里的全部向量转成静态数组，由 `ps/tests/test_calc.c` 与
`ps/tests/test_sig.c` 使用。改金标准后重新跑本脚本即可。

用法：
    python3 ps/tests/gen_golden_table.py
"""

from __future__ import annotations

import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "spec", "golden", "vectors.json")
DST = os.path.join(REPO, "ps", "tests", "golden_table.h")

HEADER = """/* 本文件由 ps/tests/gen_golden_table.py 从 spec/golden/vectors.json 生成，请勿手改。 */
#ifndef DPM_TESTS_GOLDEN_TABLE_H
#define DPM_TESTS_GOLDEN_TABLE_H

#include <stdint.h>

/* 冻结时钟与闸门目标 */
#define G_F_SLOW_HZ %(slow)d
#define G_F_DSP_HZ %(dsp)d
#define G_TARGET_GATE %(gate)d
#define G_PHASE_GRID_MDEG_NUM %(grid)d

/** 一条金标准向量（字段含义见 spec/gen_golden.py）。 */
typedef struct {
    uint32_t freq_hz;              /* 目标频率 */
    uint32_t vco_hz;               /* 求解得到的 VCO */
    uint16_t mult;                 /* CLKFBOUT 倍频 */
    uint16_t div;                  /* CLKOUT 分频 D */
    uint32_t phase_requested_mdeg; /* 请求相位（毫度） */
    uint16_t phase_field;          /* MMCM DELAY_TIME 字段（= 相位码 >> 3） */
    uint16_t phase_mux;            /* MMCM PHASE_MUX 字段（= 相位码 & 7） */
    uint32_t gate_cnt;             /* 期望实际闸门计数 */
    uint32_t edge_cnt;             /* 期望参考沿数 */
    uint32_t phase_cnt;            /* 期望相位计数（固定 90 度时的实测口径） */
    uint32_t phase_cnt_req;        /* 期望相位计数（请求相位时的口径） */
    uint32_t freq_hz_out;          /* 定点输出：频率（单位 Hz，字段名历史遗留） */
    uint32_t phase_mdeg_out;       /* 定点输出：请求相位下的相位（毫度） */
    uint32_t phase_step_mdeg;      /* 1 个 DSP 周期对应的度数 */
} g_vector_t;

#define G_VECTOR_COUNT %(count)d

static const g_vector_t g_vectors[G_VECTOR_COUNT] = {
%(rows)s};

#endif /* DPM_TESTS_GOLDEN_TABLE_H */
"""


def main() -> int:
    with open(SRC, encoding="utf-8") as fh:
        data = json.load(fh)
    # 90 度行（当前 bitstream 相位固定 90 度，测量口径用它）
    by = {(v["freq_hz"], v["phase_requested_mdeg"]): v for v in data["vectors"]}
    rows = []
    for v in data["vectors"]:
        v90 = by[(v["freq_hz"], 90000)]
        rows.append(
            "    {%d, %d, %d, %d, %d, %d, %d, %d, %d, %d, %d, %d, %d, %d},\n"
            % (
                v["freq_hz"],
                v["vco_hz"],
                v["mult"],
                v["div"],
                v["phase_requested_mdeg"],
                v["phase_field"],
                v["phase_mux"],
                v90["gate_cnt"],
                v90["edge_cnt"],
                v90["phase_cnt"],
                v["phase_cnt"],
                v["freq_milli_hz"],
                v["phase_milli_deg"],
                v["phase_step_mdeg"],
            )
        )
    with open(DST, "w", encoding="utf-8") as fh:
        fh.write(
            HEADER
            % {
                "slow": data["clocks"]["f_slow_hz"],
                "dsp": data["clocks"]["f_dsp_hz"],
                "gate": data["target_gate"],
                "grid": data["phase_grid_mdeg_num"],
                "count": len(data["vectors"]),
                "rows": "".join(rows),
            }
        )
    print(f"写入 {DST}：{len(data['vectors'])} 条向量")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
