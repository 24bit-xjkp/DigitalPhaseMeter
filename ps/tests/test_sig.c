/** @file test_sig.c
 *  @brief 宿主机单测：片内信号源求解（7 冻结频点）、DRP 配置字与约束。
 *
 * 判据来自 `spec/golden/vectors.json`（经 `ps/tests/gen_golden_table.py` 转成 C 表）与
 * `docs/ps-protocol.md` §6：VCO ∈ [600,1200]MHz、MULT ∈ 12..24、D ≤ 126、无小数分频。
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

        dpm_sig_solve(k_points[i].freq_hz, 90000u, G_F_SLOW_HZ, &sig, &st);
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
        /* 90° 落在 1/8 VCO 周期网格上：相位码 = 8 × (90° 对应的 VCO 周期数) */
        CHECK(sig.phase_milli_deg == 90000u, "f=%u 90° 应精确可表示（实际 %u）",
              k_points[i].freq_hz, sig.phase_milli_deg);
    }
}

static void test_solve_all_golden(void)
{
    printf("== 金标准 28 条：(freq, phase) → VCO/D/相位码/实际相位\n");
    for (int i = 0; i < G_VECTOR_COUNT; i++) {
        const g_vector_t *v = &g_vectors[i];
        dpm_sig_t sig;
        dpm_sig_solve_t st;
        uint32_t code;

        dpm_sig_solve(v->freq_hz, v->phase_requested_mdeg, G_F_SLOW_HZ, &sig, &st);
        CHECK(st.ok == 1 && st.error_milli_hz == 0u, "f=%u p=%u 无精确解", v->freq_hz,
              v->phase_requested_mdeg);
        CHECK(sig.vco_hz == v->vco_hz && sig.div == v->div, "f=%u p=%u VCO/D 不一致", v->freq_hz,
              v->phase_requested_mdeg);
        CHECK(sig.phase_field == v->phase_field, "f=%u p=%u DELAY_TIME 字段 %u 期望 %u", v->freq_hz,
              v->phase_requested_mdeg, sig.phase_field, v->phase_field);
        code = dpm_mmcm_phase_code(v->phase_requested_mdeg, v->div);
        CHECK((code & 0x7u) == v->phase_mux, "f=%u p=%u PHASE_MUX %u 期望 %u", v->freq_hz,
              v->phase_requested_mdeg, code & 0x7u, v->phase_mux);
        /* 冻结的 4 个相位点在 7 个频点上都在 MMCM 网格上：可实现相位 = 请求相位 */
        CHECK(sig.phase_milli_deg == (uint32_t)v->phase_requested_mdeg,
              "f=%u p=%u 可实现相位 %u 应等于请求值", v->freq_hz, v->phase_requested_mdeg,
              sig.phase_milli_deg);
    }
}

static void test_snap_and_errors(void)
{
    dpm_sig_t sig;
    dpm_sig_solve_t st;

    printf("== 相位网格（1/8 VCO 周期 = 45°/D）与不可达频率\n");
    /* 22.5° 在 25MHz（D=40）下是 20 步（2.5 个 VCO 周期）：精确可表示，
     * 不再被吸附到 45° 的整数倍（相位吸附目标是 45°/D 网格）。 */
    dpm_sig_solve(25000000u, 22500u, G_F_SLOW_HZ, &sig, &st);
    CHECK(sig.phase_milli_deg == 22500u, "22.5° 应精确可表示（实际 %u）", sig.phase_milli_deg);
    CHECK(sig.phase_field == 2u, "22.5° 的 DELAY_TIME 应为 2（实际 %u）", sig.phase_field);
    CHECK(dpm_mmcm_phase_code(22500u, 40u) == 20u, "22.5°/D=40 的相位码应为 20");
    CHECK(dpm_sig_snap_phase_milli_deg(22500u, 40u) == 22500u, "吸附函数 22.5°@D=40 → 22.5°");
    /* D=40 的网格是 1.125°：89° 离 88.875° 更近（不是吸附到 90°），89.5° 才落到 90° */
    CHECK(dpm_sig_snap_phase_milli_deg(89000u, 40u) == 88875u,
          "吸附函数 89°@D=40 → %u（期望 88875）",
          dpm_sig_snap_phase_milli_deg(89000u, 40u));
    CHECK(dpm_sig_snap_phase_milli_deg(89500u, 40u) == 90000u,
          "吸附函数 89.5°@D=40 → %u（期望 90000）",
          dpm_sig_snap_phase_milli_deg(89500u, 40u));
    CHECK(dpm_sig_snap_phase_milli_deg(0u, 40u) == 0u, "吸附函数 0 → 0");
    /* D=125（8MHz）网格是 0.36°：22.5° 落在 62/63 步中间，半点向上 → 63 步 = 22.680° */
    CHECK(dpm_sig_snap_phase_milli_deg(22500u, 125u) == 22680u,
          "吸附函数 22.5°@D=125 → %u（期望 22680）",
          dpm_sig_snap_phase_milli_deg(22500u, 125u));
    /* 179.999°@D=40 会落到网格点 180°（正好一个整周期，与 0° 同义）；180° 本身现在是
     * 合法请求（9 位相位码的上界是按 D 算的，见 test_phase_bounds），这里只确认吸附
     * 本身不改网格语义。 */
    CHECK(dpm_sig_snap_phase_milli_deg(179999u, 40u) == 180000u,
          "吸附函数 179.999°@D=40 → %u（期望 180000）",
          dpm_sig_snap_phase_milli_deg(179999u, 40u));
    CHECK(dpm_sig_snap_phase_milli_deg(179000u, 40u) == 178875u,
          "吸附函数 179°@D=40 → %u（期望 178875）",
          dpm_sig_snap_phase_milli_deg(179000u, 40u));

    /* 3MHz 低于可达下限（600/127 ≈ 4.72MHz）：有解但误差很大 */
    dpm_sig_solve(3000000u, 0u, G_F_SLOW_HZ, &sig, &st);
    CHECK(st.ok == 1 && st.error_milli_hz > DPM_SIG_FREQ_TOL_MILLI_HZ(3000000u),
          "3MHz 误差应超出容差（%u mHz）", st.error_milli_hz);

    /* 2GHz 高于 VCO 上限：最近解 1200MHz，误差同样超容差 */
    dpm_sig_solve(2000000000u, 0u, G_F_SLOW_HZ, &sig, &st);
    CHECK(st.ok == 1 && st.error_milli_hz > DPM_SIG_FREQ_TOL_MILLI_HZ(2000000000u),
          "2GHz 误差应超出容差（%u mHz）", st.error_milli_hz);
    CHECK(sig.vco_hz == 1200000000u && sig.div == 1u, "2GHz 最近解应为 1200MHz/1");
}

/** 相位上界：9 位相位码 `m <= 511`（上界与 D 有关），>180° 的请求在多数频点合法。 */
static void test_phase_bounds(void)
{
    static const uint32_t divs[] = {20u, 25u, 32u, 35u, 40u, 50u, 63u, 70u, 80u, 100u, 120u, 125u};
    dpm_sig_t sig;
    dpm_sig_solve_t st;
    uint32_t i;

    printf("== 相位上界（9 位相位码 <= %u，按 D 逐点）\n", DPM_MMCM_PHASE_CODE_MAX);

    /* 1) 上界公式：max = min(23017499 / D, 359999) */
    CHECK(dpm_mmcm_phase_max_mdeg(125u) == 184139u, "D=125 上界 %u（期望 184139）",
          dpm_mmcm_phase_max_mdeg(125u));
    CHECK(dpm_mmcm_phase_max_mdeg(120u) == 191812u, "D=120 上界 %u（期望 191812）",
          dpm_mmcm_phase_max_mdeg(120u));
    CHECK(dpm_mmcm_phase_max_mdeg(100u) == 230174u, "D=100 上界 %u（期望 230174）",
          dpm_mmcm_phase_max_mdeg(100u));
    CHECK(dpm_mmcm_phase_max_mdeg(63u) == 359999u, "D=63 上界应被满量程截断到 359999");
    CHECK(dpm_mmcm_phase_max_mdeg(40u) == 359999u, "D=40 上界应被满量程截断到 359999");
    CHECK(dpm_mmcm_phase_max_mdeg(0u) == 0u, "D=0 上界为 0");
    CHECK(dpm_mmcm_phase_code_fits(90000u, 0u) == 0u, "D=0 一律判为不可请求");

    /* 2) 逐 D 一致性：上界点恰好取到 511 步、上界+1 被拒、180° 恒合法（4D <= 504） */
    for (i = 0u; i < sizeof(divs) / sizeof(divs[0]); i++) {
        uint32_t d = divs[i];
        uint32_t hi = dpm_mmcm_phase_max_mdeg(d);

        CHECK(dpm_mmcm_phase_code_fits(hi, d) == 1u, "D=%u 的上界 %u 应仍可请求", d, hi);
        if (hi == DPM_MMCM_PHASE_FULL_SCALE_MDEG - 1u) {
            /* D 较小时（D <= 63）先撞上「满量程 360°」：相位码还远没到 511（D=40 → 320） */
            CHECK(dpm_mmcm_phase_code(hi, d) < DPM_MMCM_PHASE_CODE_MAX,
                  "D=%u 的满量程相位码 %u 应小于上界 %u", d, dpm_mmcm_phase_code(hi, d),
                  DPM_MMCM_PHASE_CODE_MAX);
        } else {
            /* D >= 64 时由 9 位相位码封顶：上界点的相位码恰好是 511，上界+1 被拒 */
            CHECK(dpm_mmcm_phase_code(hi, d) == DPM_MMCM_PHASE_CODE_MAX,
                  "D=%u 上界 %u 的相位码应为 %u（实际 %u）", d, hi, DPM_MMCM_PHASE_CODE_MAX,
                  dpm_mmcm_phase_code(hi, d));
            CHECK(dpm_mmcm_phase_code_fits(hi + 1u, d) == 0u, "D=%u 上界+1（%u）应被拒绝", d,
                  hi + 1u);
        }
        CHECK(dpm_mmcm_phase_code_fits(DPM_MMCM_PHASE_FULL_SCALE_MDEG, d) == 0u,
              "D=%u 的满量程 360° 必须被拒绝", d);
        CHECK(dpm_mmcm_phase_code_fits(180000u, d) == 1u, "D=%u 的 180° 应合法", d);
        CHECK(dpm_mmcm_phase_code(180000u, d) == 4u * d, "D=%u 的 180° 相位码应为 4D", d);
    }

    /* 3) 相位码截断 = 回绕（这就是必须拒绝的理由）：D=125 写 270° → 码 750 → 字段截成 238 步 */
    {
        uint16_t a1, a2;

        DPM_MMCM_ENCODE_CLKOUT(125u, 270000u, a1, a2);
        CHECK(dpm_mmcm_phase_code(270000u, 125u) == 750u, "8MHz/270° 的相位码应为 750（>511）");
        CHECK(dpm_mmcm_phase_code_fits(270000u, 125u) == 0u, "8MHz/270° 必须被拒绝");
        CHECK(dpm_mmcm_decode_phase_mdeg(a1, a2, 125u) == 85680u,
              "截断后实际相位回绕成 %u 毫度（期望 85680 = 85.68°）",
              dpm_mmcm_decode_phase_mdeg(a1, a2, 125u));
    }

    /* 4) >180° 请求的求解结果 */
    dpm_sig_solve(8000000u, 183960u, G_F_SLOW_HZ, &sig, &st);
    CHECK(st.ok == 1u && sig.div == 125u, "8MHz 应解出 D=125（实际 D=%u）", sig.div);
    CHECK(sig.phase_milli_deg == 183960u, "8MHz/183.96° 应精确落在网格上（实际 %u）",
          sig.phase_milli_deg);
    CHECK(sig.phase_field == 63u, "8MHz/183.96° 的 DELAY_TIME 应为 63（实际 %u）", sig.phase_field);
    CHECK(dpm_mmcm_phase_code(183960u, 125u) == 511u, "8MHz/183.96° 的相位码应为 511");
    /* 25MHz（D=40）的 270°：相位码 240，远在上界之内 */
    dpm_sig_solve(25000000u, 270000u, G_F_SLOW_HZ, &sig, &st);
    CHECK(sig.div == 40u && sig.phase_milli_deg == 270000u && sig.phase_field == 30u,
          "25MHz/270° 应精确可表示（D=%u 相位=%u DELAY_TIME=%u）", sig.div, sig.phase_milli_deg,
          sig.phase_field);
    CHECK(dpm_mmcm_phase_code_fits(270000u, 40u) == 1u, "25MHz/270° 应合法");
    /* 10MHz（D=100）的 229.95°：相位码恰好是上界 511 */
    dpm_sig_solve(10000000u, 229950u, G_F_SLOW_HZ, &sig, &st);
    CHECK(sig.div == 100u && dpm_mmcm_phase_code(229950u, 100u) == 511u,
          "10MHz/229.95° 的相位码应为 511（D=%u 相位=%u）", sig.div, sig.phase_milli_deg);
}

static void test_drp_words(void)
{
    dpm_sig_t sig;
    dpm_sig_solve_t st;
    uint32_t fb1, fb2, c01, c02, c11, c12;

    printf("== MMCM 寄存器编码（方案 A）\n");
    dpm_sig_solve(25000000u, 90000u, G_F_SLOW_HZ, &sig, &st);
    dpm_sig_drp_words(&sig, &fb1, &fb2, &c01, &c02, &c11, &c12);

    /* 权威值：clk_wiz 的 Write DRP 模式在同一工作点（50MHz 参考、M=20、D=40、
     * 相位 0/90°）生成的值，已由 host/dpm_host/sig.py 与 ps/include/dpm_regs.h 的
     * 编码实现逐点对拍。
     *   CLKFBOUT ClkReg1 = 0x128A、ClkReg2 = 0x0000
     *   CLKOUT0  ClkReg1 = 0x1514、ClkReg2 = 0x0000
     *   CLKOUT1  ClkReg1 = 0x1514、ClkReg2 = 0x000A  ← 90° = 10 个 VCO 周期（DELAY_TIME=10）
     */
    CHECK(fb1 == 0x128Au, "CLKFBOUT ClkReg1 = 0x%04X 期望 0x128A", fb1);
    CHECK(fb2 == 0x0000u, "CLKFBOUT ClkReg2 = 0x%04X 期望 0x0000", fb2);
    CHECK(c01 == 0x1514u, "CLKOUT0 ClkReg1 = 0x%04X 期望 0x1514", c01);
    CHECK(c02 == 0x0000u, "CLKOUT0 ClkReg2 = 0x%04X 期望 0x0000", c02);
    CHECK(c11 == 0x1514u, "CLKOUT1 ClkReg1 = 0x%04X 期望 0x1514", c11);
    CHECK(c12 == 0x000Au, "CLKOUT1 ClkReg2 = 0x%04X 期望 0x000A（DELAY_TIME=10）", c12);
    CHECK(dpm_mmcm_encode_divclk(1u) == 0x1041u, "DIVCLK(1) = 0x%04X 期望 0x1041",
          dpm_mmcm_encode_divclk(1u));

    /* 22.5°@D=40 = 20 步 = DELAY_TIME 2 + PHASE_MUX 4：PHASE_MUX 落在 ClkReg1 的 [15:13]，
     * 这是「相位细分」的关键位域（旧编码把 PHASE_MUX/DELAY_TIME 弄反，相位被量化到
     * 1 个 VCO 周期 = 9°，白白浪费 8 倍分辨率）。 */
    {
        dpm_sig_t s2;
        dpm_sig_solve_t st2;
        uint32_t a1, a2, b1, b2, d1, d2;
        dpm_sig_solve(25000000u, 22500u, G_F_SLOW_HZ, &s2, &st2);
        dpm_sig_drp_words(&s2, &a1, &a2, &b1, &b2, &d1, &d2);
        CHECK(d2 == 0x0002u, "22.5° 的 CLKOUT1 ClkReg2 应为 0x0002（实际 0x%04X）", d2);
        CHECK(d1 == (0x1000u | (4u << 13) | (20u << 6) | 20u),
              "22.5° 的 CLKOUT1 ClkReg1 应为 0x%04X（实际 0x%04X）",
              (0x1000u | (4u << 13) | (20u << 6) | 20u), d1);
        /* 编解码互逆：寄存器 → 毫度应还原出可实现相位 */
        CHECK(dpm_mmcm_decode_phase_mdeg(d1, d2, 40u) == 22500u,
              "解码 22.5° 寄存器对 → %u 期望 22500", dpm_mmcm_decode_phase_mdeg(d1, d2, 40u));
    }

    /* 相位单调性：更大的相位应给出不小于的相位码（同一分频下） */
    {
        dpm_sig_t s2;
        dpm_sig_solve_t st2;
        uint32_t a1, a2, b1, b2, d1, d2;
        dpm_sig_solve(25000000u, 45000u, G_F_SLOW_HZ, &s2, &st2);
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

    /* 相位步长（1/8 VCO 周期）：1GHz → 125ps、600MHz → 208ps */
    CHECK(dpm_sig_phase_step_ps(1000000000u) == 125u, "1000MHz 步长 %u ps",
          dpm_sig_phase_step_ps(1000000000u));
    CHECK(dpm_sig_phase_step_ps(600000000u) == 208u, "600MHz 步长 %u ps",
          dpm_sig_phase_step_ps(600000000u));
    CHECK(dpm_sig_phase_step_ps(0u) == 0u, "0Hz 步长应为 0");
    CHECK(G_PHASE_GRID_MDEG_NUM == 45000u, "相位网格分子 45000（45°/D 的分子）");
}

int main(void)
{
    printf("== test_sig（片内源求解与 DRP 字）\n");
    test_solve_points();
    test_solve_all_golden();
    test_snap_and_errors();
    test_phase_bounds();
    test_drp_words();
    printf("== %d 项检查，%d 项失败\n", g_checks, g_fail);
    return g_fail == 0 ? 0 : 1;
}
