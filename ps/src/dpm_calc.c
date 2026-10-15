/** @file dpm_calc.c
 *  @brief `dpm_calc.h` 的定点换算实现（测量结果部分）。
 *
 * 可移植：只用 <stdint.h>，64 位中间量用 `uint64_t`；PS 侧 freestanding 环境的
 * 64 位除法由 `dpm_div64.c` 提供（这里只用到 64 位除 32/64 位）。
 * 公式与 `docs/ps-protocol.md` 第 5 节、`spec/gen_golden.py` 完全一致。
 */

#include "dpm_calc.h"

/** 毫度/圈。 */
#define DPM_MDEG_PER_TURN 360000u

void dpm_meas_calc(const dpm_result_t *r, const dpm_clocks_t *clk, dpm_meas_t *out)
{
    if (out == 0) {
        return;
    }
    out->freq_milli_hz = 0u;
    out->phase_milli_deg = 0u;
    out->phase_q_deg = 0u;
    out->phase_cycles = 0u;
    out->phase_cycles_rem = 0u;

    if (r == 0 || clk == 0) {
        return;
    }
    if (r->gate_cnt == 0u || r->edge_cnt == 0u) {
        return; /* 源没有边沿：不做除法，返回全 0（协议 §5） */
    }

    /* freq_hz = (edge_cnt * f_slow) / gate_cnt（整除；edge*f_slow ≤ 2.1e17 < 2^64 不溢出）。
     * 注意协议里的字段名 `freqMilliHz` 是历史遗留，实际单位是 **Hz**：
     * 真正的毫赫兹在 25MHz 时是 2.5e10，超出 u32。 */
    out->freq_milli_hz =
        (uint32_t)(((uint64_t)r->edge_cnt * (uint64_t)clk->slow_hz) / (uint64_t)r->gate_cnt);

    /* phase_milli_deg = 360000 * phase_cnt * f_slow / (gate_cnt * f_dsp)
     * 用不溢出的等价形式（与 `dpm_calc.h` 的冻结说明一致）：
     *   c = phase_cnt / gate_cnt, d = phase_cnt % gate_cnt
     *   v = 360000*c + (360000*d)/gate_cnt
     *   phase_milli_deg = v * (f_slow/1000) / (f_dsp/1000)      // 前置条件：两者都能被 1000 整除
     */
    {
        uint32_t c = r->phase_cnt / r->gate_cnt;
        uint32_t d = r->phase_cnt % r->gate_cnt;
        uint64_t v = (uint64_t)DPM_MDEG_PER_TURN * (uint64_t)c +
                     ((uint64_t)DPM_MDEG_PER_TURN * (uint64_t)d) / (uint64_t)r->gate_cnt;
        uint32_t slow_khz = clk->slow_hz / 1000u;
        uint32_t dsp_khz = clk->dsp_hz / 1000u;
        if (dsp_khz == 0u) {
            return;
        }
        out->phase_milli_deg = (uint32_t)((v * (uint64_t)slow_khz) / (uint64_t)dsp_khz);
    }

    /* phase_cnt / edge_cnt：整数个 DSP 周期与余数（量化残差诊断用） */
    out->phase_cycles = r->phase_cnt / r->edge_cnt;
    out->phase_cycles_rem = r->phase_cnt % r->edge_cnt;

    /* 1 个 DSP 周期对应的度数（毫度，四舍五入）= (T_dsp / T_sig) × 360000
     *   = 360000 × f_sig / f_dsp
     * 注意 `freq_milli_hz` 字段装的已经是 Hz（协议历史命名），不要再乘 1000。 */
    if (clk->dsp_hz != 0u) {
        uint64_t den2 = (uint64_t)clk->dsp_hz;
        uint64_t num2 = (uint64_t)DPM_MDEG_PER_TURN * (uint64_t)out->freq_milli_hz;
        out->phase_q_deg = (uint32_t)((num2 + den2 / 2u) / den2);
    }
}

uint32_t dpm_phase_normalize_milli_deg(uint32_t milli_deg)
{
    /* uint32 取模对 >= 360000 的输入也成立；360000 恰好归一化到 0 */
    return milli_deg % DPM_MDEG_PER_TURN;
}

uint64_t dpm_signal_period_100ps(uint32_t freq_milli_hz)
{
    /* T = 1/f，单位 0.1ns = 1e-10 s → T_100ps = 1e13 / f_mHz */
    if (freq_milli_hz == 0u) {
        return 0u;
    }
    return 10000000000000ull / (uint64_t)freq_milli_hz;
}
