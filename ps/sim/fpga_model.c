/** @file fpga_model.c
 *  @brief 宿主机 PL 仿真后端实现（寄存器语义 + 测量模型 + DRP/MMCM 简化模型）。
 *
 * 细节与假设见 `fpga_model.h` 的文件头。测量模型与 `spec/gen_golden.py` 逐位一致：
 * 全程整数运算、相位量化用 RNE（round-half-to-even），不使用浮点。
 */

#include "fpga_model.h"

#include <stddef.h>

#include "dpm_cmd.h"
#include "dpm_regs.h"

/* ------------------------------------------------------------- 内部工具 */

/** PL 的 CTRL 索引是按地址 bit[5:2] 取的，这里保持一致。 */
static uint32_t reg_off(uint32_t addr)
{
    return ((addr - (uint32_t)DPM_BASE)) & 0x3Cu;
}

static int in_dpm(uint32_t addr)
{
    return (addr >= (uint32_t)DPM_BASE) && (addr < (uint32_t)DPM_BASE + 0x1000u);
}

static int in_drp(uint32_t addr)
{
    return (addr >= (uint32_t)DPM_DRP_BASE) && (addr < (uint32_t)DPM_DRP_BASE + 0x1000u);
}

/** 整数 RNE（半数取偶），与 golden 一致。 */
static uint32_t rne_u64(uint64_t num, uint64_t den)
{
    uint64_t q = num / den;
    uint64_t r = num % den;
    uint64_t twice = r * 2u;

    if (twice > den || (twice == den && ((q & 1u) != 0u))) {
        q += 1u;
    }
    return (uint32_t)q;
}

static uint32_t div_ceil_u64(uint64_t num, uint64_t den)
{
    if (den == 0u) {
        return 0u;
    }
    return (uint32_t)((num + den - 1u) / den);
}

/* ------------------------------------------------------------- 状态推进 */

static void do_clear(dpm_model_t *m)
{
    m->res_gate = 0u;
    m->res_edge = 0u;
    m->res_phase = 0u;
    m->res_seq = 0u;
    m->seq = 0u;
    m->lost_count = 0u;
    m->irq_pending = 0u;
    m->result_valid = 0u;
    m->clear_pending = 0u;
}

/** 结果锁存（与 ControlRegs 的 response 分支同序：覆盖旧结果 → lostCount++）。 */
static void latch_result(dpm_model_t *m)
{
    if (m->clear_pending != 0u) {
        do_clear(m); /* CTRL.clear 在 busy 期间挂起，busy 落下这一刻生效 */
    }
    if (m->result_valid != 0u) {
        m->lost_count++; /* 上一次结果没被读走就被覆盖 */
    }
    m->res_gate = m->pending_gate;
    m->res_edge = m->pending_edge;
    m->res_phase = m->pending_phase;
    m->res_seq = m->seq;
    m->seq++;
    m->busy = 0u;
    m->irq_pending = 1u;
    m->result_valid = 1u;
}

static void advance_time(dpm_model_t *m, uint32_t cycles)
{
    m->cycles += (uint64_t)cycles;

    /* 配置快照跨到测量域 */
    if (m->config_pending > 0u) {
        if (m->config_pending <= cycles) {
            m->config_pending = 0u;
        } else {
            m->config_pending -= cycles;
        }
    }
    /* MMCM 重新锁定（lock_stuck_low 为测试钩子：永远不锁定） */
    if (m->lock_stuck_low != 0u) {
        m->drp_locked = 0u;
    } else if (m->drp_locked == 0u && m->lock_left > 0u) {
        if (m->lock_left <= cycles) {
            m->lock_left = 0u;
            m->drp_locked = 1u;
        } else {
            m->lock_left -= cycles;
        }
    }
    /* 测量推进 */
    if (m->busy != 0u && m->meas_blocked == 0u) {
        if (m->remaining_cycles > cycles) {
            m->remaining_cycles -= cycles;
        } else {
            m->remaining_cycles = 0u;
            latch_result(m);
        }
    }
}

/** 一次 AXI 事务：推进模型时间。 */
static void txn(dpm_model_t *m)
{
    m->txn_count++;
    advance_time(m, DPM_MODEL_CYCLES_PER_TXN);
}

/* ------------------------------------------------------------- 测量模型 */

/** 从 MMCM 寄存器镜像解析内部源：返回 1 = 可用（VCO/D 合法）。
 *
 * 方案 A 下寄存器就是 MMCM 的原始 16 位值，按 XAPP888 的位域解码：
 *   ClkReg1 = {PHASE_MUX[15:13], 1'b1, HIGH_TIME[11:6], LOW_TIME[5:0]}
 *   ClkReg2 = {EDGE[7], NO_COUNT[6], DELAY_TIME[5:0]}
 * 分频比 = HIGH_TIME + LOW_TIME；相位 = PHASE_MUX × 1/8 VCO 周期 + DELAY_TIME × 1 VCO 周期。 */
static int internal_point(const dpm_model_t *m, uint32_t *vco_hz, uint32_t *div, uint32_t *phase_mdeg)
{
    uint32_t fb = m->drp[DPM_DRP_OFF_CLKFBOUT / 4u];
    uint32_t c0 = m->drp[DPM_DRP_OFF_CLKOUT0 / 4u];
    uint32_t c11 = m->drp[DPM_DRP_OFF_CLKOUT1 / 4u];
    uint32_t c12 = m->drp[DPM_DRP_OFF_CLKOUT1_2 / 4u];
    uint32_t dv = m->drp[DPM_DRP_OFF_DIVCLK / 4u];
    /* divide==1 时 NO_COUNT 置位、HIGH/LOW_TIME 都是占位的 1，必须用共享解码函数 */
    uint32_t mult = dpm_mmcm_decode_divide(fb);
    uint32_t divclk = dpm_mmcm_decode_divclk(dv);
    uint32_t d = dpm_mmcm_decode_divide(c0);
    uint32_t v;

#ifdef DPM_MODEL_DEBUG
    fprintf(stderr, "DBG ip: fb=0x%04X dv=0x%04X c0=0x%04X c11=0x%04X c12=0x%04X -> mult=%u divclk=%u d=%u\n",
            fb, dv, c0, c11, c12, mult, divclk, d);
#endif
    if (mult == 0u || divclk == 0u || d == 0u) {
        return 0;
    }
    v = (uint32_t)(((uint64_t)m->slow_hz * (uint64_t)mult) / (uint64_t)divclk);
    if (v < 600000000u || v > 1200000000u) {
        return 0;
    }
    *vco_hz = v;
    *div = d;
    if (phase_mdeg != 0) {
        *phase_mdeg = dpm_mmcm_decode_phase_mdeg(c11, c12, d);
    }
    return 1;
}

void dpm_model_internal_point(const dpm_model_t *m, uint32_t *vco_hz, uint32_t *div,
                              uint32_t *phase_mdeg)
{
    uint32_t v = 0u;
    uint32_t d = 0u;
    uint32_t f = 0u;

    (void)internal_point(m, &v, &d, &f);
    if (vco_hz != 0) {
        *vco_hz = v;
    }
    if (div != 0) {
        *div = d;
    }
    if (phase_mdeg != 0) {
        *phase_mdeg = f; /* 由 PHASE_MUX/DELAY_TIME 反推的实现相位（毫度） */
    }
}

void dpm_model_active_signal(const dpm_model_t *m, uint32_t *freq_hz, uint32_t *phase_mdeg,
                             int *present)
{
    uint32_t f = 0u;
    uint32_t p = 0u;
    int ok = 0;

    if ((m->sig_ctrl & DPM_SIG_SELECT) != 0u) {
        uint32_t vco = 0u;
        uint32_t d = 0u;
        uint32_t phw = 0u;
        if ((m->sig_ctrl & DPM_SIG_ENABLE) != 0u && m->drp_locked != 0u &&
            internal_point(m, &vco, &d, &phw) != 0) {
            f = vco / d;
            /* 上板实测（docs/onboard-ax7010.md）：bitstream 打开 CONFIG.PHASE_DUTY_CONFIG
             * 之后，clk_out2 相对 clk_out1 的相位差由 DRP 相位字（毫度）决定，
             * 再按 1 个 DSP 周期（450MHz → 2.222ns）量化到 phaseCnt。这里如实建模：
             * 写多少相位就出多少相位（旧 bitstream 的"恒 90°"行为已不是默认语义）。 */
            p = phw;
            ok = 1;
        }
    } else {
        f = m->ext_freq_hz;
        p = m->ext_phase_mdeg;
        ok = (m->ext_absent == 0u) ? 1 : 0;
    }
    if (freq_hz != 0) {
        *freq_hz = f;
    }
    if (phase_mdeg != 0) {
        *phase_mdeg = p;
    }
    if (present != 0) {
        *present = ok;
    }
}

/** 按当前源计算本次测量的 (gate_cnt, edge_cnt, phase_cnt) 并启动 busy 计时。 */
static void start_measurement(dpm_model_t *m, uint32_t target_gate)
{
    uint32_t f_sig = 0u;
    uint32_t phase_mdeg = 0u;
    int present = 0;
    uint64_t gate;
    uint64_t edge;
    uint64_t dsp_cycles;
    uint64_t num;
    uint64_t den;

    dpm_model_active_signal(m, &f_sig, &phase_mdeg, &present);
    if (present == 0 || f_sig == 0u) {
        /* 两路输入恒低：测量永远等不到参考沿，busy 一直为 1 */
        m->busy = 1u;
        m->meas_blocked = 1u;
        m->remaining_cycles = 0u;
        m->blocked_count++;
        return;
    }

    /* 内部源用有理数 VCO/D 算，避免 f_sig 取整误差影响 gate/edge（golden 同源） */
    if ((m->sig_ctrl & DPM_SIG_SELECT) != 0u) {
        uint32_t vco = 0u;
        uint32_t d = 0u;
        uint32_t field = 0u;
        (void)internal_point(m, &vco, &d, &field);
        gate = (uint64_t)target_gate + div_ceil_u64((uint64_t)m->slow_hz * (uint64_t)d, (uint64_t)vco);
        edge = ((uint64_t)gate * (uint64_t)vco) / ((uint64_t)d * (uint64_t)m->slow_hz);
        num = (uint64_t)phase_mdeg * (uint64_t)d * (uint64_t)m->dsp_hz;
        den = (uint64_t)360000u * (uint64_t)vco;
    } else {
        gate = (uint64_t)target_gate + div_ceil_u64((uint64_t)m->slow_hz, (uint64_t)f_sig);
        edge = ((uint64_t)gate * (uint64_t)f_sig) / (uint64_t)m->slow_hz;
        num = (uint64_t)phase_mdeg * (uint64_t)m->dsp_hz;
        den = (uint64_t)360000u * (uint64_t)f_sig;
    }

    /* 相位先被 1 个 DSP 周期量化（整数 RNE），再乘参考沿数 */
    dsp_cycles = (edge != 0u) ? (uint64_t)rne_u64(num, den) : 0u;

    m->pending_gate = (uint32_t)(gate & 0x0FFFFFFFu); /* gateCntWidth = 28 */
    m->pending_edge = (uint32_t)edge;
    m->pending_phase = (uint32_t)(dsp_cycles * edge); /* 硬件上 32 位回卷 */
    if (m->pending_edge == 0u || m->pending_gate == 0u) {
        m->busy = 1u;
        m->meas_blocked = 1u; /* 窗口里一个沿都没有：握手挂住 */
        m->blocked_count++;
        return;
    }
    m->busy = 1u;
    m->meas_blocked = 0u;
    m->remaining_cycles = m->pending_gate;
    m->start_count++;
}

/* ------------------------------------------------------------- 寄存器读写 */

static void apply_dpm_write(dpm_model_t *m, uint32_t off, uint32_t data)
{
    switch (off) {
    case DPM_REG_CTRL:
        m->irq_enable = (data & DPM_CTRL_IRQ_ENABLE) != 0u ? 1u : 0u;
        if ((data & DPM_CTRL_CLEAR) != 0u) {
            if (m->busy != 0u) {
                m->clear_pending = 1u; /* busy 期间挂起 */
            } else {
                do_clear(m);
            }
        }
        if ((data & DPM_CTRL_START) != 0u && m->busy == 0u) {
            start_measurement(m, m->gate_target & 0x0FFFFFFFu);
        }
        break;
    case DPM_REG_GATE_TARGET:
        m->gate_target = data;
        break;
    case DPM_REG_IRQ_STATUS:
        if ((data & DPM_IRQ_PENDING) != 0u) {
            m->irq_pending = 0u;
        }
        if ((data & DPM_IRQ_RESULT_READ) != 0u) {
            m->result_valid = 0u;
        }
        if ((data & DPM_IRQ_LOST_CLEAR) != 0u) {
            m->lost_count = 0u;
        }
        break;
    case DPM_REG_SIG_CTRL:
        m->sig_ctrl = data & (DPM_SIG_ENABLE | DPM_SIG_SELECT);
        /* 写这些寄存器会组装配置快照，跨到测量域之前 DPM 的写通道 ready = 0 */
        m->config_pending = DPM_MODEL_CONFIG_LATENCY_CYCLES;
        break;
    default:
        break; /* 其它地址只读/保留 */
    }
}

static void apply_drp_write(dpm_model_t *m, uint32_t off, uint32_t data)
{
    if (off < DPM_DRP_OFF_BASE) {
        return; /* 本地寄存器（例如状态 0x004）不通过写配置字改变 */
    }
    if (off == DPM_DRP_LOAD_OFF) {
        uint32_t vco = 0u;
        uint32_t d = 0u;
        uint32_t field = 0u;
        /* LOAD：按新配置字重新锁定；配置非法则保持失锁 */
        m->drp_locked = 0u;
        if (m->lock_stuck_low != 0u) {
            m->lock_left = 0u;
            return;
        }
        if (internal_point(m, &vco, &d, &field) != 0) {
            m->lock_left = DPM_MODEL_LOCK_LATENCY_CYCLES;
        } else {
            m->lock_left = 0u;
        }
        return;
    }
    if ((off / 4u) < 32u) {
        m->drp[off / 4u] = data;
    }
}

static uint32_t model_rd32(void *ctx, uint32_t addr)
{
    dpm_model_t *m = (dpm_model_t *)ctx;
    uint32_t v = 0u;

    txn(m); /* 读事务也推进仿真时间（见 fpga_model.h 的假设 3） */

    if (in_drp(addr)) {
        uint32_t off = addr - (uint32_t)DPM_DRP_BASE;
        if (off == DPM_DRP_OFF_ID) {
            v = DPM_DRP_ID_VALUE;
        } else if (off == DPM_DRP_STATUS_OFF) {
            /* bit0 = locked、bit1 = busy（重配置进行中） */
            v = ((m->drp_locked != 0u) ? DPM_DRP_STATUS_LOCKED : 0u) |
                ((m->lock_left != 0u) ? DPM_DRP_STATUS_BUSY : 0u);
        } else if ((off / 4u) < 32u) {
            /* 寄存器镜像按字索引（off/4），与写路径一致 */
            v = m->drp[off / 4u];
        }
        return v;
    }
    if (in_dpm(addr) == 0) {
        return 0u;
    }
    switch (reg_off(addr)) {
    case DPM_REG_CTRL:
        v = m->irq_enable << 1; /* b0/b2 只写，读回恒 0 */
        break;
    case DPM_REG_STATUS:
        v = (m->busy != 0u ? DPM_STATUS_BUSY : 0u) |
            (m->result_valid != 0u ? DPM_STATUS_RESULT_VALID : 0u) |
            (m->lost_count != 0u ? DPM_STATUS_LOST : 0u) |
            (m->irq_pending != 0u ? DPM_STATUS_IRQ_PENDING : 0u);
        break;
    case DPM_REG_GATE_TARGET:
        v = m->gate_target;
        break;
    case DPM_REG_RESULT_GATE:
        v = m->res_gate;
        break;
    case DPM_REG_RESULT_EDGE:
        v = m->res_edge;
        break;
    case DPM_REG_RESULT_PHASE:
        v = m->res_phase;
        break;
    case DPM_REG_RESULT_SEQ:
        v = m->res_seq;
        break;
    case DPM_REG_LOST_COUNT:
        v = m->lost_count;
        break;
    case DPM_REG_IRQ_STATUS:
        v = (m->irq_pending != 0u ? DPM_IRQ_PENDING : 0u) |
            (m->result_valid != 0u ? DPM_IRQ_RESULT_READ : 0u) |
            (m->lost_count != 0u ? DPM_IRQ_LOST_CLEAR : 0u);
        break;
    case DPM_REG_VERSION:
        v = DPM_VERSION_VALUE;
        break;
    case DPM_REG_CAPS:
        v = 4u | (1u << 8) | DPM_CAPS_REGMAP | DPM_CAPS_SIG_SRC;
        break;
    case DPM_REG_SIG_CTRL:
        v = m->sig_ctrl & (DPM_SIG_ENABLE | DPM_SIG_SELECT);
        break;
    case DPM_REG_SIG_STATUS: {
        uint32_t enable = (m->sig_ctrl & DPM_SIG_ENABLE) != 0u ? DPM_SIGST_ENABLE : 0u;
        uint32_t select = (m->sig_ctrl & DPM_SIG_SELECT) != 0u ? DPM_SIGST_SELECT : 0u;
        uint32_t locked = (m->drp_locked != 0u) ? DPM_SIGST_LOCKED : 0u;
        uint32_t ready = (select == 0u || (enable != 0u && locked != 0u)) ? DPM_SIGST_READY : 0u;
        v = enable | select | locked | ready;
        break;
    }
    default:
        v = 0u;
        break;
    }
    return v;
}

static void model_wr32(void *ctx, uint32_t addr, uint32_t data)
{
    dpm_model_t *m = (dpm_model_t *)ctx;

    if (in_drp(addr)) {
        apply_drp_write(m, addr - (uint32_t)DPM_DRP_BASE, data);
        txn(m);
        return;
    }
    if (in_dpm(addr) == 0) {
        return;
    }
    if (m->config_pending > 0u) {
        /* 写通道 ready = 0：AXI 写被挂起，配置快照送出后由 advance_time 补写 */
        m->wr_held = 1u;
        m->wr_held_addr = reg_off(addr);
        m->wr_held_data = data;
        txn(m);
        if (m->config_pending == 0u && m->wr_held != 0u) {
            m->wr_held = 0u;
            apply_dpm_write(m, m->wr_held_addr, m->wr_held_data);
        }
        return;
    }
    apply_dpm_write(m, reg_off(addr), data);
    txn(m);
}

static uint32_t model_now_ms(void *ctx)
{
    dpm_model_t *m = (dpm_model_t *)ctx;
    if (m->slow_hz == 0u) {
        return 0u;
    }
    return (uint32_t)((m->cycles * 1000u) / (uint64_t)m->slow_hz);
}

static void model_delay_ms(void *ctx, uint32_t ms)
{
    dpm_model_t *m = (dpm_model_t *)ctx;
    /* 简化：1ms = f_slow/1000 个慢周期；便于超时路径在宿主上快速跑完 */
    if (m->slow_hz != 0u) {
        advance_time(m, (uint32_t)(((uint64_t)ms * (uint64_t)m->slow_hz) / 1000u));
    }
}

static const dpm_io_t s_model_io = {
    model_rd32,
    model_wr32,
    model_now_ms,
    model_delay_ms,
};

const dpm_io_t *dpm_model_io(void)
{
    return &s_model_io;
}

/* ------------------------------------------------------------- 生命周期 */

void dpm_model_init(dpm_model_t *m, uint32_t slow_hz, uint32_t dsp_hz)
{
    uint8_t *p = (uint8_t *)m;
    uint32_t i;

    for (i = 0u; i < (uint32_t)sizeof(*m); i++) {
        p[i] = 0u;
    }
    m->slow_hz = (slow_hz != 0u) ? slow_hz : 50000000u;
    m->dsp_hz = (dsp_hz != 0u) ? dsp_hz : 450000000u;
    m->ext_freq_hz = 10000000u; /* --ext-present 时用的外部信号（golden 有 10MHz 点） */
    m->ext_phase_mdeg = 0u;
    m->ext_absent = 1u;         /* 默认外部引脚无信号：测量不会完成（与真实 PL 一致） */

    /* DRP 复位值 = 方案 A 下 MMCM 寄存器的 BD 初值（与 clk_wiz Write DRP 在
     * 同一工作点生成的权威值一致）：
     * VCO = 50MHz×20/1 = 1000MHz、D = 40 → clk_out1/clk_out2 = 25MHz；
     * clk_out2 的 ClkReg2 的 DELAY_TIME = 10 个 VCO 周期 = 90°。 */
    m->drp[DPM_DRP_OFF_CLKFBOUT / 4u] = 0x128Au;  /* CLKFBOUT ClkReg1 */
    m->drp[DPM_DRP_OFF_CLKFBOUT2 / 4u] = 0x0000u; /* CLKFBOUT ClkReg2 */
    m->drp[DPM_DRP_OFF_CLKOUT0 / 4u] = 0x1514u;   /* CLKOUT0 ClkReg1 */
    m->drp[DPM_DRP_OFF_CLKOUT0_2 / 4u] = 0x0000u; /* CLKOUT0 ClkReg2 */
    m->drp[DPM_DRP_OFF_CLKOUT1 / 4u] = 0x1514u;   /* CLKOUT1 ClkReg1 */
    m->drp[DPM_DRP_OFF_CLKOUT1_2 / 4u] = 0x000Au; /* CLKOUT1 ClkReg2（90°） */
    m->drp[DPM_DRP_OFF_DIVCLK / 4u] = 0x1041u;    /* DIVCLK = 1 */
    m->drp[DPM_DRP_OFF_POWER / 4u] = 0xFFFFu;     /* POWER */
    m->drp_locked = 1u;
    m->lock_left = 0u;
    m->cycles = 0u;
}

void dpm_model_set_external(dpm_model_t *m, uint32_t freq_hz, uint32_t phase_mdeg, int absent)
{
    m->ext_freq_hz = freq_hz;
    m->ext_phase_mdeg = phase_mdeg;
    m->ext_absent = (absent != 0) ? 1u : 0u;
}

void dpm_model_set_lock_stuck(dpm_model_t *m, int stuck)
{
    m->lock_stuck_low = (stuck != 0) ? 1u : 0u;
    if (m->lock_stuck_low != 0u) {
        m->drp_locked = 0u;
        m->lock_left = 0u;
    }
}

void dpm_model_advance(dpm_model_t *m, uint32_t cycles)
{
    advance_time(m, cycles);
}

uint32_t dpm_model_now_ms(const dpm_model_t *m)
{
    return model_now_ms((void *)m);
}
