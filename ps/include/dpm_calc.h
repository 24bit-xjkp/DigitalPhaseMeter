/** @file dpm_calc.h
 *  @brief 测量结果与片内信号源的纯计算部分（无 IO、无全局状态、可移植）。
 *
 * 约定：
 *   - 本文件是 PS 侧 C 实现与 PC 侧 Python 上位机的**公共算法契约**，
 *     两侧必须给出相同的定点结果（见 `host/dpm_host/mathx.py` 与 `host/tests/`）。
 *   - 全部使用整数运算，不依赖 libc / libm / libgcc（PS 侧为 freestanding 环境，
 *     64 位除法由 `dpm_div64.c` 提供）。
 *   - 时钟频率在运行时给出：默认 `f_slow = 50MHz`、`f_dsp = 400MHz`。
 */

#ifndef DPM_CALC_H
#define DPM_CALC_H

#include <stdint.h>

/** 一次测量的原始观测量（与 PL 的 4 个结果寄存器一一对应）。 */
typedef struct {
    uint32_t gate_cnt;  /**< 实际闸门计数（低速时钟周期数），PL：RESULT_GATE */
    uint32_t edge_cnt;  /**< 窗口内参考信号上升沿数，PL：RESULT_EDGE */
    uint32_t phase_cnt; /**< 相位计数（高速时钟周期累加值），PL：RESULT_PHASE */
    uint32_t seq;       /**< 记录序号，PL：RESULT_SEQ */
} dpm_result_t;

/** 换算所需的时钟频率。 */
typedef struct {
    uint32_t slow_hz; /**< 低速（AXI/闸门）时钟频率，默认 50_000_000 */
    uint32_t dsp_hz;  /**< 高速（DSP/测量）时钟频率，默认 400_000_000 */
} dpm_clocks_t;

/** 换算结果（定点整数，避免浮点与格式化依赖）。 */
typedef struct {
    uint32_t freq_milli_hz;   /**< 被测信号频率。**单位是 Hz**（名字是历史遗留：
                               * 真正的毫赫兹在 25MHz 时是 2.5e10，会溢出 u32），
                               * 与线上字段 `freqMilliHz` 一致 */
    uint32_t phase_milli_deg; /**< 相位差，单位 1e-3 度，范围 [0, 360000] */
    uint32_t phase_q_deg;     /**< 相位量化步长（1 个 DSP 周期对应的度数，四舍五入） */
    uint32_t phase_cycles;    /**< phase_cnt / edge_cnt 的整数部分（DSP 周期数） */
    uint32_t phase_cycles_rem;/**< phase_cnt % edge_cnt（用于判断量化残差） */
} dpm_meas_t;

/** 计算频率与相位差。
 *
 * 公式（与 `docs/ps-protocol.md` 第 5 节一致）：
 *   T_win           = gate_cnt / f_slow
 *   f_sig           = edge_cnt * f_slow / gate_cnt
 *   phase_milli_deg = 360000 * phase_cnt * f_slow / (gate_cnt * f_dsp)
 *
 * **实现必须用下面的等价形式**（逐位相同，但 64 位中间量不会溢出）：
 *   freq_hz = (edge_cnt * f_slow) / gate_cnt        // 整除，单位 Hz
 *   （edge_cnt 是 32 位、f_slow = 50MHz 时 edge_cnt*f_slow ≤ 2.1e17 < 2^64，不会溢出）
 *   c = phase_cnt / gate_cnt; d = phase_cnt % gate_cnt
 *   v = 360000 * c + (360000 * d) / gate_cnt
 *   phase_milli_deg = v * (f_slow / 1000) / (f_dsp / 1000)
 * 要求 f_slow 与 f_dsp 都能被 1000 整除（默认 50MHz/400MHz 满足）。
 *
 * `gate_cnt == 0` 或 `edge_cnt == 0` 时两个输出均为 0（不产生除零）。
 */
void dpm_meas_calc(const dpm_result_t *r, const dpm_clocks_t *clk, dpm_meas_t *out);

/** 把相位差按「0~360°」归一化，返回 1e-3 度。 */
uint32_t dpm_phase_normalize_milli_deg(uint32_t milli_deg);

/** 由结果推算被测信号周期，单位 0.1ns（即 1e-10 s）。`f_sig == 0` 时返回 0。 */
uint64_t dpm_signal_period_100ps(uint32_t freq_milli_hz);

/* ------------------------------------------------------- 片内信号源换算 */

/** 片内信号源（MMCM 两路输出）的工作点。 */
typedef struct {
    uint32_t freq_hz;      /**< 目标/实际输出频率（两路同频） */
    uint32_t phase_milli_deg; /**< **可实现**的相位差（已吸附到 45°/D 网格），1e-3 度 */
    uint32_t vco_hz;       /**< VCO 频率 */
    uint16_t mult;         /**< CLKFBOUT 倍频（整数） */
    uint16_t divclk;       /**< DIVCLK 分频（本设计恒为 1） */
    uint16_t div;          /**< CLKOUT 分频 D */
    uint16_t phase_field;  /**< MMCM 的 DELAY_TIME 字段（整数 VCO 周期，0..63） */
    uint32_t phase_step_ps;/**< 相位 1 步（1/8 VCO 周期）对应的时间（ps） */
} dpm_sig_t;

/** 频率求解状态：是否找到、误差（1e-3 Hz）。 */
typedef struct {
    int ok;                /**< 1 = 找到满足约束的解 */
    uint32_t error_milli_hz;/**< |f_actual - f_target|，1e-3 Hz */
} dpm_sig_solve_t;

/** 为给定目标频率与相位差求解 MMCM 工作点（VCO、MULT、D 与相位字段）。
 *
 * 约束（`docs/ps-protocol.md` 第 6 节）：
 *   - `600MHz <= VCO <= 1200MHz`，`VCO = slow_hz * mult / divclk`；
 *   - `divclk = 1`，`mult = 12..24` 整数；
 *   - `1 <= div <= 126`：分频 = `HIGH_TIME + LOW_TIME`（各 6 位），
 *     8 位可表示到 255，但硬上限取 127（`DPM_SIG_DIV_MAX`），超出不再考虑；
 *   - 不使用小数分频。
 *
 * 选择规则（**冻结，C / Python / `spec/gen_golden.py` 三处必须一致**）：
 *   1. 先最小化频率误差 `|VCO/div - f_target|`（用整数交叉相乘比较，避免浮点误差）；
 *   2. 误差相同（含多个精确解）时，取 `cost = |VCO - 1000MHz| / 1MHz + |div - 40|` 最小者，
 *      即尽量贴近 BD 的初始工作点 `(VCO = 1000MHz, D = 40)`。
 *
 * 该规则的输出就是冻结频点表：8/10/12.5/20/25MHz → `MULT=20`、`div=125/100/80/50/40`；
 * 5MHz → `600/12/120`；6.25MHz → `750/15/120`。**不要硬编码频点表**，按规则算。
 *
 * 相位：`out->phase_milli_deg` 是**可实现**的相位（吸附到 MMCM 的 1/8 VCO 周期网格，
 * 即 `45°/div`；见 `dpm_regs.h` 的 `dpm_mmcm_phase_code`），不是请求值本身。
 */
void dpm_sig_solve(uint32_t target_freq_hz, uint32_t phase_milli_deg, uint32_t slow_hz,
                   dpm_sig_t *out, dpm_sig_solve_t *st);

/** 分频比硬上限（见 `dpm_sig_solve` 的约束）。
 *
 * 分频在 MMCM 寄存器里编码成 `HIGH_TIME` + `LOW_TIME`（各 6 位），可表示的最大值是
 * `63 + 63 = 126`：`D = 127` 的 `LOW_TIME = 64` 会被 6 位字段截断成 63，写下去实际是
 * D=63（输出频率差一倍）。因此硬上限取 126，而不是分频字段能放的 255。 */
#define DPM_SIG_DIV_MAX 126u
/** BD 初始工作点：VCO = 50MHz × 20 = 1000MHz、D = 40（对应 25MHz）。 */
#define DPM_SIG_BD_VCO_HZ 1000000000u
#define DPM_SIG_BD_DIV 40u

/** 由工作点算出要写进 MMCM 寄存器镜像的 6 个 16 位寄存器值（方案 A）。
 *
 * 每个 CLKOUTx / CLKFBOUT 需要两个寄存器（ClkReg1/ClkReg2），因此是三对：
 *   - `clkfbout`  / `clkfbout2`  → MMCM 0x14 / 0x15（CLKFBOUT）
 *   - `clkout0`   / `clkout0_2`  → MMCM 0x08 / 0x09（参考输出）
 *   - `clkout1`   / `clkout1_2`  → MMCM 0x0A / 0x0B（移相输出，相位差在这里）
 *
 * 编码规则见 `dpm_regs.h` 的 `DPM_MMCM_ENCODE_CLKOUT`（位域取自 XAPP888 v1.8，
 * 相位拆分另按厂商 `mmcm_pll_drp_func_7s_mmcm.vh` 核对）。PL 侧只搬值、不做算术。
 * 任一输出指针可以为 NULL，表示不需要该值。
 */
void dpm_sig_drp_words(const dpm_sig_t *sig, uint32_t *clkfbout, uint32_t *clkfbout2,
                       uint32_t *clkout0, uint32_t *clkout0_2, uint32_t *clkout1,
                       uint32_t *clkout1_2);

#endif /* DPM_CALC_H */
