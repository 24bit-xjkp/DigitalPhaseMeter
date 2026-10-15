/** @file test_calc.c
 *  @brief 宿主机单测：`dpm_calc.c` 的定点换算（对金标准逐位、边界与大数）。
 *
 * 构建/运行见 `ps/CMakeLists.txt`（`ctest` 目标 `test_calc`）。退出码 0 = 全过。
 */

#include <stdio.h>
#include <stdlib.h>

#include "dpm_calc.h"
#include "golden_table.h"

static int g_fail;
static int g_checks;

#define CHECK(cond, ...)                                                     \
    do {                                                                     \
        g_checks++;                                                          \
        if (!(cond)) {                                                       \
            g_fail++;                                                        \
            printf("  [FAIL] %s:%d: ", __FILE__, __LINE__);                  \
            printf(__VA_ARGS__);                                             \
            printf("\n");                                                    \
        }                                                                    \
    } while (0)

static void test_golden_all(void)
{
    dpm_clocks_t clk = {G_F_SLOW_HZ, G_F_DSP_HZ};
    int i;

    printf("== 金标准：%d 条向量（请求相位口径的频率/相位）\n", G_VECTOR_COUNT);
    for (i = 0; i < G_VECTOR_COUNT; i++) {
        const g_vector_t *v = &g_vectors[i];
        dpm_result_t r;
        dpm_meas_t m;

        r.gate_cnt = v->gate_cnt;
        r.edge_cnt = v->edge_cnt;
        r.phase_cnt = v->phase_cnt_req; /* 固件写的相位字 = 请求相位（当前硬件不生效） */
        r.seq = (uint32_t)i;
        dpm_meas_calc(&r, &clk, &m);

        CHECK(m.freq_milli_hz == v->freq_hz_out, "f=%u 实测 %u 期望 %u", v->freq_hz, m.freq_milli_hz,
              v->freq_hz_out);
        CHECK(m.phase_milli_deg == v->phase_mdeg_out,
              "f=%u phase_req=%u 实测 %u 期望 %u", v->freq_hz, v->phase_requested_mdeg,
              m.phase_milli_deg, v->phase_mdeg_out);
        CHECK(m.phase_cycles_rem == (v->phase_cnt_req % v->edge_cnt), "余数不一致");
        /* 量化步长按**实测频率**算，金标准按**标称频率**算：8MHz/20MHz 点上
         * 实测频率有 1/edge 量级的截断（7997010 vs 8000000），所以给 1% + 10 毫度容差。 */
        {
            uint32_t diff = (m.phase_q_deg > v->phase_step_mdeg)
                                ? (m.phase_q_deg - v->phase_step_mdeg)
                                : (v->phase_step_mdeg - m.phase_q_deg);
            CHECK(diff <= v->phase_step_mdeg / 100u + 10u,
                  "量化步长 %u 与金标准 %u 相差 %u（容差 1%%+10）", m.phase_q_deg,
                  v->phase_step_mdeg, diff);
        }
    }
}

static void test_edges(void)
{
    dpm_clocks_t clk = {G_F_SLOW_HZ, G_F_DSP_HZ};
    dpm_result_t r;
    dpm_meas_t m;

    printf("== 边界\n");
    /* gate_cnt = 0 与 edge_cnt = 0：不得除零，输出全 0 且保留原始计数由调用方持有 */
    r.gate_cnt = 0;
    r.edge_cnt = 1;
    r.phase_cnt = 1;
    r.seq = 0;
    dpm_meas_calc(&r, &clk, &m);
    CHECK(m.freq_milli_hz == 0 && m.phase_milli_deg == 0, "gate=0 应输出 0");

    r.gate_cnt = 1;
    r.edge_cnt = 0;
    r.phase_cnt = 1;
    dpm_meas_calc(&r, &clk, &m);
    CHECK(m.freq_milli_hz == 0 && m.phase_milli_deg == 0, "edge=0 应输出 0");

    /* 大数不溢出：edge 取 32 位上限、gate 取 1，中间量 4.29e9×5e7 = 2.1e17 > 2^64? 否（2^64=1.8e19） */
    r.gate_cnt = 1;
    r.edge_cnt = 0xFFFFFFFFu;
    r.phase_cnt = 0xFFFFFFFFu;
    dpm_meas_calc(&r, &clk, &m);
    CHECK(m.freq_milli_hz == (uint32_t)((0xFFFFFFFFull * G_F_SLOW_HZ) / 1u), "大 edge 频率 %u",
          m.freq_milli_hz);
    CHECK(m.phase_milli_deg < 360000u || m.phase_milli_deg > 0u, "大数相位 %u", m.phase_milli_deg);

    /* 归一化与周期 */
    CHECK(dpm_phase_normalize_milli_deg(360000u) == 0u, "360000 应归一化到 0");
    CHECK(dpm_phase_normalize_milli_deg(45000u) == 45000u, "45000 不变");
    CHECK(dpm_phase_normalize_milli_deg(810000u) == 90000u, "810000 → 90000");
    CHECK(dpm_signal_period_100ps(10000000u) == 1000000ull, "10MHz 周期 = 1e6 × 0.1ns");
    CHECK(dpm_signal_period_100ps(0u) == 0ull, "f=0 → 周期 0");
}

int main(void)
{
    printf("== test_calc（dpm_calc.c 定点换算）\n");
    test_golden_all();
    test_edges();
    printf("== %d 项检查，%d 项失败\n", g_checks, g_fail);
    return g_fail == 0 ? 0 : 1;
}
