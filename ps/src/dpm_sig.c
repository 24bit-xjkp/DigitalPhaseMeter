/** @file dpm_sig.c
 *  @brief 片内信号源（MMCM 两路输出）的工作点求解、DRP 配置字与写入时序。
 *
 * 分三块：
 *   1. 纯计算：`dpm_sig_solve`（频率/相位 → VCO、MULT、D、相位字段）与
 *      `dpm_sig_drp_words`（→ CLKFBOUT / CLKOUT0 / CLKOUT1 三个 DRP 字），可移植、无 IO；
 *   2. 单位换算：`dpm_sig_phase_unit_deg_milli()` 等，供宿主机仿真后端复用同一套单位；
 *   3. 写入时序：`dpm_sig_apply()` 按 `docs/ps-protocol.md` §6 的 5 步写 DRP 并等锁定。
 *
 * 三个约束来源：`docs/ps-protocol.md` §6、`ps/include/dpm_calc.h`（求解规则冻结）、
 * `ps/include/dpm_regs.h`（DRP 字编码：低字节 = D 本体、相位字段在 bit[15:8]）。
 */

#include "dpm_cmd.h"
#include "dpm_calc.h"
#include "dpm_io.h"
#include "dpm_regs.h"

/* ================================ 可配置项（未定稿项集中在这里） ============ */

/** 相位字段一个步长对应的毫度（**未定稿项**，见协议 §6 的候选 A/B）。
 *
 *   - 候选 B（默认，45000）：字段 = 45° 的整数倍，1 步 = 1/8 VCO 周期；
 *   - 候选 A（1000）：字段 = 相位度数（1 LSB = 1 ps 延迟）。
 *
 * 交付前若 `spec/drp_semantics.json` 给出实测结论，只改这一处；
 * 宿主机仿真后端通过 `dpm_sig_phase_unit_deg_milli()` 取同一值，因此两边始终一致。
 */
#ifndef DPM_SIG_PHASE_UNIT_DEG_MILLI
#define DPM_SIG_PHASE_UNIT_DEG_MILLI 45000u
#endif

/** VCO 允许范围（-1 速度等级）。 */
#define DPM_SIG_VCO_MIN_HZ 600000000u
#define DPM_SIG_VCO_MAX_HZ 1200000000u
/** CLKFBOUT 倍频范围（整数）。 */
#define DPM_SIG_MULT_MIN 12u
#define DPM_SIG_MULT_MAX 24u
/* 分频上限、BD 初始工作点由 `dpm_calc.h` 冻结：DPM_SIG_DIV_MAX / DPM_SIG_BD_VCO_HZ /
 * DPM_SIG_BD_DIV（不要在本地另写一套，否则与 Python 参考实现不一致）。 */

/** 等待 MMCM 重新锁定的轮询上限（无时间源时按次数计）。 */
#define DPM_SIG_LOCK_POLL_MAX 200000u

/* ================================ 单位换算 ================================ */

uint32_t dpm_sig_phase_unit_deg_milli(void)
{
    return (uint32_t)DPM_SIG_PHASE_UNIT_DEG_MILLI;
}

uint32_t dpm_sig_snap_phase_milli_deg(uint32_t phase_milli_deg)
{
    uint32_t unit = dpm_sig_phase_unit_deg_milli();
    uint32_t field;

    if (unit == 0u) {
        return 0u;
    }
    /* 就近吸附（半点向上），再折算回毫度 */
    field = (phase_milli_deg + unit / 2u) / unit;
    return field * unit;
}

uint32_t dpm_sig_phase_step_ps(uint32_t vco_hz)
{
#if (DPM_SIG_PHASE_UNIT_DEG_MILLI == 1000u)
    /* 候选 A：字段 = 度数，1 LSB = 1ps 延迟 */
    (void)vco_hz;
    return 1u;
#else
    /* 候选 B：1 步 = 1/8 VCO 周期 = 1.25e11 / f_vco（ps） */
    if (vco_hz == 0u) {
        return 0u;
    }
    return (uint32_t)(125000000000ull / (uint64_t)vco_hz);
#endif
}

/* ================================ 求解 ==================================== */

/** cost = |VCO - 1000MHz| / 1MHz + |D - 40|（冻结规则：越接近 BD 初始工作点越好）。 */
static uint32_t sig_cost(uint32_t vco_hz, uint32_t div)
{
    uint32_t dv = (vco_hz > DPM_SIG_BD_VCO_HZ) ? (vco_hz - DPM_SIG_BD_VCO_HZ)
                                               : (DPM_SIG_BD_VCO_HZ - vco_hz);
    uint32_t dd = (div > (uint32_t)DPM_SIG_BD_DIV) ? (div - (uint32_t)DPM_SIG_BD_DIV)
                                                   : ((uint32_t)DPM_SIG_BD_DIV - div);
    return dv / 1000000u + dd;
}

/** 判据：候选是否严格优于当前最优（与 `spec/gen_golden.py::solve_sig` 逐条一致）。
 *
 * 1. 频率误差 `|VCO/D - f| = |VCO - f·D| / D` 用**交叉相乘**比较，避免整除截断；
 * 2. 误差相同时按 `(cost, |VCO - 1000MHz|, D)` 字典序取小者。
 */
static int sig_candidate_better(uint64_t err_num, uint32_t div, uint32_t vco, uint64_t best_err_num,
                               uint32_t best_div, uint32_t best_vco, uint32_t best_cost,
                               uint32_t cost)
{
    uint64_t lhs = err_num * (uint64_t)best_div;
    uint64_t rhs = best_err_num * (uint64_t)div;

    if (lhs < rhs) {
        return 1;
    }
    if (lhs > rhs) {
        return 0;
    }
    if (cost != best_cost) {
        return cost < best_cost;
    }
    {
        uint32_t dv = (vco > DPM_SIG_BD_VCO_HZ) ? (vco - DPM_SIG_BD_VCO_HZ)
                                                : (DPM_SIG_BD_VCO_HZ - vco);
        uint32_t bdv = (best_vco > DPM_SIG_BD_VCO_HZ) ? (best_vco - DPM_SIG_BD_VCO_HZ)
                                                      : (DPM_SIG_BD_VCO_HZ - best_vco);
        if (dv != bdv) {
            return dv < bdv;
        }
    }
    return div < best_div;
}

void dpm_sig_solve(uint32_t target_freq_hz, uint32_t phase_milli_deg, uint32_t slow_hz,
                   uint32_t phase_unit_deg_milli, dpm_sig_t *out, dpm_sig_solve_t *st)
{
    uint32_t mult;
    uint32_t div;
    uint32_t best_vco = 0u;
    uint32_t best_div = 0u;
    uint32_t best_cost = 0u;
    uint64_t best_err_num = 0u;
    uint8_t found = 0u;

    if (st != 0) {
        st->ok = 0;
        st->error_milli_hz = 0u;
    }
    if (out == 0 || st == 0 || target_freq_hz == 0u || slow_hz == 0u ||
        phase_unit_deg_milli == 0u) {
        return;
    }

    for (mult = DPM_SIG_MULT_MIN; mult <= DPM_SIG_MULT_MAX; mult++) {
        uint32_t vco = slow_hz * mult; /* DIVCLK = 1 */
        if (vco < DPM_SIG_VCO_MIN_HZ || vco > DPM_SIG_VCO_MAX_HZ) {
            continue;
        }
        for (div = 1u; div <= (uint32_t)DPM_SIG_DIV_MAX; div++) {
            /* 误差分子（单位 Hz·D）：|VCO - f·D|；比较见 sig_candidate_better */
            uint64_t rhs = (uint64_t)target_freq_hz * (uint64_t)div;
            uint64_t err_num = ((uint64_t)vco > rhs) ? ((uint64_t)vco - rhs) : (rhs - (uint64_t)vco);
            uint32_t cost = sig_cost(vco, div);

            if (found == 0u) {
                found = 1u;
            } else if (sig_candidate_better(err_num, div, vco, best_err_num, best_div, best_vco,
                                            best_cost, cost) == 0) {
                continue;
            }
            best_vco = vco;
            best_div = div;
            best_err_num = err_num;
            best_cost = cost;
        }
    }

    if (found == 0u) {
        return;
    }

    out->vco_hz = best_vco;
    out->mult = (uint16_t)(best_vco / slow_hz);
    out->divclk = 1u;
    out->div = (uint16_t)best_div;
    /* 实际输出频率 = VCO / D（就近取整到 Hz） */
    out->freq_hz = (uint32_t)(((uint64_t)best_vco + (uint64_t)best_div / 2u) / (uint64_t)best_div);
    out->phase_milli_deg = (uint32_t)(((uint64_t)phase_milli_deg + (uint64_t)phase_unit_deg_milli / 2u) /
                                      (uint64_t)phase_unit_deg_milli) *
                           phase_unit_deg_milli;
    out->phase_field = (uint16_t)(out->phase_milli_deg / phase_unit_deg_milli);
    out->phase_step_ps = dpm_sig_phase_step_ps(best_vco);

    st->ok = 1;
    /* 误差（1e-3 Hz）= err_num / D * 1000 */
    st->error_milli_hz = (uint32_t)((best_err_num * 1000u) / (uint64_t)best_div);
}

void dpm_sig_drp_words(const dpm_sig_t *sig, uint32_t *clkfbout, uint32_t *clkfbout2,
                       uint32_t *clkout0, uint32_t *clkout0_2, uint32_t *clkout1,
                       uint32_t *clkout1_2)
{
    uint16_t fb1, fb2, c01, c02, c11, c12;

    if (sig == 0) {
        return;
    }
    /* 方案 A：**软件自己把频率/相位编码成 MMCM 的 16 位寄存器值**，
     * PL 侧的 dpm_src_drp 只负责按顺序写进 MMCM 的 DRP 口（硬件不含任何算术）。
     *
     * 编码规则（位域取自官方 XAPP888 v1.8 Table 1/2，并用 clk_wiz 的 Write DRP
     * 生成值逐点校验；参考实现见 build/analysis/golden/mmcm_encode.py）：
     *   ClkReg1 = {PHASE_MUX[15:13], 1'b1, HIGH_TIME[11:6], LOW_TIME[5:0]}
     *   ClkReg2 = {EDGE[7], NO_COUNT[6], DELAY_TIME[5:0]}
     * 相位差只加在 clk_out2（CLKOUT1）上，clk_out1 恒 0。
     */
    DPM_MMCM_ENCODE_CLKOUT(sig->mult, 0u, fb1, fb2);
    DPM_MMCM_ENCODE_CLKOUT(sig->div, 0u, c01, c02);
    DPM_MMCM_ENCODE_CLKOUT(sig->div, sig->phase_milli_deg, c11, c12);

    if (clkfbout != 0) {
        *clkfbout = (uint32_t)fb1;
    }
    if (clkfbout2 != 0) {
        *clkfbout2 = (uint32_t)fb2;
    }
    if (clkout0 != 0) {
        *clkout0 = (uint32_t)c01;
    }
    if (clkout0_2 != 0) {
        *clkout0_2 = (uint32_t)c02;
    }
    if (clkout1 != 0) {
        *clkout1 = (uint32_t)c11;
    }
    if (clkout1_2 != 0) {
        *clkout1_2 = (uint32_t)c12;
    }
}

uint32_t dpm_sig_phase_word_mdeg(const dpm_sig_t *sig)
{
    (void)sig;
    /* 方案 A 下相位不再是一个独立的「毫度字」：它已经被编码进 CLKOUT1 的
     * ClkReg2（DELAY_TIME 字段），见 `dpm_sig_drp_words`。
     * 保留本函数只为兼容旧接口，返回值不再有硬件含义。 */
    return 0u;
}

/* ============================= 写入时序 =================================== */

int dpm_sig_apply(const dpm_cmd_ctx_t *c, const dpm_sig_t *sig)
{
    uint32_t fb1, fb2, c01, c02, c11, c12;
    uint32_t i;

    if (c == 0 || sig == 0 || c->io == 0) {
        return 2;
    }
    /* 重配置期间两路输出停止：必须在 idle 时写 DRP（协议 §6 第 5 步） */
    if ((dpm_cmd_status(c) & DPM_STATUS_BUSY) != 0u) {
        return 3;
    }

    dpm_sig_drp_words(sig, &fb1, &fb2, &c01, &c02, &c11, &c12);
    /* 顺序与 PL 侧状态机的发送顺序一致（CLKFBOUT → CLKOUT0 → CLKOUT1 → DIVCLK → POWER）。
     * 顺序本身不影响功能（PL 是等 LOAD 才整批写进 MMCM 的），但保持一致便于对照。 */
    dpm_drp_wr(c->io, c->io_ctx, DPM_DRP_OFF_CLKFBOUT, fb1);
    dpm_drp_wr(c->io, c->io_ctx, DPM_DRP_OFF_CLKFBOUT2, fb2);
    dpm_drp_wr(c->io, c->io_ctx, DPM_DRP_OFF_CLKOUT0, c01);
    dpm_drp_wr(c->io, c->io_ctx, DPM_DRP_OFF_CLKOUT0_2, c02);
    dpm_drp_wr(c->io, c->io_ctx, DPM_DRP_OFF_CLKOUT1, c11);
    dpm_drp_wr(c->io, c->io_ctx, DPM_DRP_OFF_CLKOUT1_2, c12);
    dpm_drp_wr(c->io, c->io_ctx, DPM_DRP_OFF_DIVCLK,
               (uint32_t)dpm_mmcm_encode_divclk(sig->divclk));
    dpm_drp_wr(c->io, c->io_ctx, DPM_DRP_OFF_POWER, DPM_DRP_POWER_ENABLE_PHASE);
    /* Load：写 1 触发一次完整重配置 */
    dpm_drp_wr(c->io, c->io_ctx, DPM_DRP_OFF_LOAD, 1u);

    /* 等 DRP 状态字（偏移 0x004 的 bit0）与 DPM 的 SIG_STATUS 都报 locked。
     * 注意：这里只轮询 locked，不轮询 sourceReady —— 后者的语义还与
     * SIG_CTRL.enable/srcSelect 有关，由 SET_SOURCE/START 负责。 */
    for (i = 0u; i < DPM_SIG_LOCK_POLL_MAX; i++) {
        if ((dpm_cmd_drp_status(c) & DPM_DRP_STATUS_LOCKED) != 0u &&
            (dpm_cmd_sig_status(c) & DPM_SIGST_LOCKED) != 0u) {
            return 0;
        }
    }
    return 2;
}
