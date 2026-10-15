/** @file test_sig.c
 *  @brief 宿主机单测：片内信号源求解（7 冻结频点）、DRP 配置字与约束。
 *
 * 判据来自 `spec/golden/vectors.json`（经 `ps/tests/gen_golden_table.py` 转成 C 表）与
 * `docs/ps-protocol.md` §6：VCO ∈ [600,1200]MHz、MULT ∈ 12..24、D ≤ 127、无小数分频。
 *
 * 构建/运行见 `ps/CMakeLists.txt`（`ctest` 目标 `test_sig`）。退出码 0 = 全过。
 */

#include <stdio.h>

#include "dpm_cmd.h"
#include "dpm_regs.h"
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

/** 7 个冻结频点与期望工作点（与 golden 表一致）。 */
static const struct {
    uint32_t freq_hz;
    uint32_t vco_hz;
    uint16_t mult;
    uint16_t div;
} k_points[] = {
    {5000000u, 600000000u, 12u, 120u},
    {6250000u, 750000000u, 15u, 120u},
    {8000000u, 1000000000u, 20u, 125u},
    {10000000u, 1000000000u, 20u, 100u},
    {12500000u, 1000000000u, 20u, 80u},
    {20000000u, 1000000000u, 20u, 50u},
    {25000000u, 1000000000u, 20u, 40u},
};

static void test_solve_points(void)
{
    printf("== 7 个冻结频点：精确解与约束\n");
    for (unsigned i = 0; i < sizeof(k_points) / sizeof(k_points[0]); i++) {
        dpm_sig_t sig;
        dpm_sig_solve_t st;

        dpm_sig_solve(k_points[i].freq_hz, 90000u, G_F_SLOW_HZ, G_PHASE_UNIT_MDEG, &sig, &st);
        CHECK(st.ok == 1, "f=%u 无解", k_points[i].freq_hz);
        CHECK(st.error_milli_hz == 0u, "f=%u 误差 %u mHz 应为 0", k_points[i].freq_hz,
              st.error_milli_hz);
        CHECK(sig.freq_hz == k_points[i].freq_hz, "f=%u 实际 %u", k_points[i].freq_hz, sig.freq_hz);
        CHECK(sig.vco_hz == k_points[i].vco_hz, "f=%u VCO=%u 期望 %u", k_points[i].freq_hz,
              sig.vco_hz, k_points[i].vco_hz);
        CHECK(sig.mult == k_points[i].mult && sig.div == k_points[i].div,
              "f=%u MULT/D=%u/%u 期望 %u/%u", k_points[i].freq_hz, sig.mult, sig.div,
              k_points[i].mult, k_points[i].div);
        /* 约束 */
        CHECK(sig.vco_hz >= 600000000u && sig.vco_hz <= 1200000000u, "VCO 越界 %u", sig.vco_hz);
        CHECK(sig.mult >= 12u && sig.mult <= 24u, "MULT 越界 %u", sig.mult);
        CHECK(sig.div >= 1u && sig.div <= DPM_SIG_DIV_MAX, "D 越界 %u", sig.div);
        CHECK(sig.divclk == 1u, "DIVCLK 应为 1");
    }
}

static void test_solve_all_golden(void)
{
    printf("== 金标准 28 条：(freq, phase) → VCO/D/字段/实际相位\n");
    for (int i = 0; i < G_VECTOR_COUNT; i++) {
        const g_vector_t *v = &g_vectors[i];
        dpm_sig_t sig;
        dpm_sig_solve_t st;

        dpm_sig_solve(v->freq_hz, v->phase_requested_mdeg, G_F_SLOW_HZ, G_PHASE_UNIT_MDEG, &sig,
                      &st);
        CHECK(st.ok == 1 && st.error_milli_hz == 0u, "f=%u p=%u 无精确解", v->freq_hz,
              v->phase_requested_mdeg);
        CHECK(sig.vco_hz == v->vco_hz && sig.div == v->div, "f=%u p=%u VCO/D 不一致", v->freq_hz,
              v->phase_requested_mdeg);
        CHECK(sig.phase_field == v->phase_field, "f=%u p=%u 字段 %u 期望 %u", v->freq_hz,
              v->phase_requested_mdeg, sig.phase_field, v->phase_field);
        CHECK(sig.phase_milli_deg == (uint32_t)v->phase_requested_mdeg || v->phase_requested_mdeg == 0,
              "实际相位应等于请求（45° 网格点）");
    }
}

static void test_snap_and_errors(void)
{
    dpm_sig_t sig;
    dpm_sig_solve_t st;

    printf("== 相位吸附与不可达频率\n");
    /* 22.5° 不在 45° 网格上：半点向上 → 45°（字段 1） */
    dpm_sig_solve(25000000u, 22500u, G_F_SLOW_HZ, G_PHASE_UNIT_MDEG, &sig, &st);
    CHECK(sig.phase_field == 1u && sig.phase_milli_deg == 45000u, "22.5° 吸附：字段 %u 相位 %u",
          sig.phase_field, sig.phase_milli_deg);
    CHECK(dpm_sig_snap_phase_milli_deg(22500u) == 45000u, "吸附函数 22.5° → 45°");
    CHECK(dpm_sig_snap_phase_milli_deg(89000u) == 90000u, "吸附函数 89° → 90°");
    CHECK(dpm_sig_snap_phase_milli_deg(0u) == 0u, "吸附函数 0 → 0");

    /* 3MHz 低于可达下限（600/127 ≈ 4.72MHz）：有解但误差很大 */
    dpm_sig_solve(3000000u, 0u, G_F_SLOW_HZ, G_PHASE_UNIT_MDEG, &sig, &st);
    CHECK(st.ok == 1 && st.error_milli_hz > DPM_SIG_FREQ_TOL_MILLI_HZ(3000000u),
          "3MHz 误差应超出容差（%u mHz）", st.error_milli_hz);

    /* 2GHz 高于 VCO 上限：最近解 1200MHz，误差同样超容差 */
    dpm_sig_solve(2000000000u, 0u, G_F_SLOW_HZ, G_PHASE_UNIT_MDEG, &sig, &st);
    CHECK(st.ok == 1 && st.error_milli_hz > DPM_SIG_FREQ_TOL_MILLI_HZ(2000000000u),
          "2GHz 误差应超出容差（%u mHz）", st.error_milli_hz);
    CHECK(sig.vco_hz == 1200000000u && sig.div == 1u, "2GHz 最近解应为 1200MHz/1");
}

static void test_drp_words(void)
{
    dpm_sig_t sig;
    dpm_sig_solve_t st;
    uint32_t fb1, fb2, c01, c02, c11, c12;

    printf("== MMCM 寄存器编码（方案 A）\n");
    dpm_sig_solve(25000000u, 90000u, G_F_SLOW_HZ, G_PHASE_UNIT_MDEG, &sig, &st);
    dpm_sig_drp_words(&sig, &fb1, &fb2, &c01, &c02, &c11, &c12);

    /* 权威值：clk_wiz 的 Write DRP 模式在同一工作点（50MHz 参考、M=20、D=40、
     * 相位 0/90°）生成的值，已在 build/analysis/golden 逐点校验过。
     *   CLKFBOUT ClkReg1 = 0x128A、ClkReg2 = 0x0000
     *   CLKOUT0  ClkReg1 = 0x1514、ClkReg2 = 0x0000
     *   CLKOUT1  ClkReg1 = 0x1514、ClkReg2 = 0x000A  ← 90° 编码在 DELAY_TIME=10
     */
    CHECK(fb1 == 0x128Au, "CLKFBOUT ClkReg1 = 0x%04X 期望 0x128A", fb1);
    CHECK(fb2 == 0x0000u, "CLKFBOUT ClkReg2 = 0x%04X 期望 0x0000", fb2);
    CHECK(c01 == 0x1514u, "CLKOUT0 ClkReg1 = 0x%04X 期望 0x1514", c01);
    CHECK(c02 == 0x0000u, "CLKOUT0 ClkReg2 = 0x%04X 期望 0x0000", c02);
    CHECK(c11 == 0x1514u, "CLKOUT1 ClkReg1 = 0x%04X 期望 0x1514", c11);
    CHECK(c12 == 0x000Au, "CLKOUT1 ClkReg2 = 0x%04X 期望 0x000A（DELAY_TIME=10）", c12);
    CHECK(dpm_mmcm_encode_divclk(1u) == 0x1041u, "DIVCLK(1) = 0x%04X 期望 0x1041",
          dpm_mmcm_encode_divclk(1u));

    /* 相位单调性：更大的相位应给出不小于的 DELAY_TIME（同一分频下） */
    {
        dpm_sig_t s2;
        dpm_sig_solve_t st2;
        uint32_t a1, a2, b1, b2, d1, d2;
        dpm_sig_solve(25000000u, 45000u, G_F_SLOW_HZ, G_PHASE_UNIT_MDEG, &s2, &st2);
        dpm_sig_drp_words(&s2, &a1, &a2, &b1, &b2, &d1, &d2);
        CHECK((d2 & 0x3Fu) <= (c12 & 0x3Fu), "45° 的 DELAY_TIME(%u) 应 <= 90° 的(%u)",
              d2 & 0x3Fu, c12 & 0x3Fu);
    }

    /* 寄存器表与状态偏移 */
    CHECK(DPM_DRP_OFF_LOAD == 0x28u, "LOAD 偏移 0x28（实际 0x%02X）", DPM_DRP_OFF_LOAD);
    CHECK(DPM_DRP_STATUS_OFF == 0x004u, "状态寄存器偏移 0x004");
    CHECK(DPM_DRP_OFF_CLKFBOUT == 0x08u && DPM_DRP_OFF_CLKOUT1_2 == 0x1Cu,
          "寄存器偏移表（CLKFBOUT=0x08、CLKOUT1_2=0x1C）");
    CHECK(DPM_DRP_PHASE_WRITABLE == 1, "方案 A 下相位可写（软件直接编码 MMCM 位域）");

    /* 相位步长与相位单位宏保持原语义 */
    CHECK(dpm_sig_phase_step_ps(1000000000u) == 125u, "1000MHz 步长 %u ps",
          dpm_sig_phase_step_ps(1000000000u));
    CHECK(dpm_sig_phase_unit_deg_milli() == 45000u, "相位单位宏");
}

int main(void)
{
    printf("== test_sig（片内源求解与 DRP 字）\n");
    test_solve_points();
    test_solve_all_golden();
    test_snap_and_errors();
    test_drp_words();
    printf("== %d 项检查，%d 项失败\n", g_checks, g_fail);
    return g_fail == 0 ? 0 : 1;
}
