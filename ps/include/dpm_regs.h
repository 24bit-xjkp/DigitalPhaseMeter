/** @file dpm_regs.h
 *  @brief PL 侧 AXI4-Lite 寄存器映射（PS 视角）与 MMCM 动态重配置口定义。
 *
 * 本文件是 PS 侧软件对 PL 的唯一 ABI 描述：偏移量与位域必须与
 * `src/main/scala/DigitalPhaseMeter/ControlRegs.scala` 完全一致（同一次提交内保持一致）。
 * 权威来源是 Verilog/Chisel 侧，本文件只用宏把它表达出来，不改语义。
 *
 * 约定：
 *   - 所有寄存器按字节偏移给出，32 位访问（PS 侧不支持窄访问，字节选通由 PL 的 strb 处理）。
 *   - 基地址由 block design 的地址分配决定，见 `docs/ps-protocol.md` 的地址表。
 */

#ifndef DPM_REGS_H
#define DPM_REGS_H

#include <stdint.h>

/* ---------------------------------------------------------------- 地址映射 */

/* DPM 测量 IP 的 AXI4-Lite 地址段（BD：SEG_dpm_top_0_Reg） */
#ifndef DPM_BASE
#define DPM_BASE 0x43C00000u
#endif

/* 片内信号源 MMCM（clk_wiz_src）的动态重配置口（BD：SEG_clk_wiz_src_Reg）。
 *
 * 这不是 MMCM 原语的 DRP 地址：AXI 地址的高位由 axi_interconnect 译码，落到
 * clk_wiz 的 s_axi_lite 时低 12 位才是 PG065 的 DRP 偏移，因此这里用 0x200 起的绝对偏移。
 */
#ifndef DPM_DRP_BASE
#define DPM_DRP_BASE 0x43C10000u
#endif

/* ------------------------------------------------------------ 寄存器偏移量 */

#define DPM_REG_CTRL 0x00u        /**< 混合：b0 start（只写）、b1 irqEnable、b2 clear（只写） */
#define DPM_REG_STATUS 0x04u      /**< 只读 */
#define DPM_REG_GATE_TARGET 0x08u /**< 读写：目标闸门计数（28 位有效），字节选通写入 */
#define DPM_REG_RESULT_GATE 0x0Cu /**< 只读：最新结果的 gateCnt */
#define DPM_REG_RESULT_EDGE 0x10u /**< 只读：最新结果的 edgeCnt */
#define DPM_REG_RESULT_PHASE 0x14u/**< 只读：最新结果的 phaseCnt */
#define DPM_REG_RESULT_SEQ 0x18u  /**< 只读：最新结果的记录序号 */
#define DPM_REG_LOST_COUNT 0x1Cu  /**< 只读：结果被覆盖丢失的次数 */
#define DPM_REG_IRQ_STATUS 0x20u  /**< W1C：b0 irqPending、b1 resultValid、b2 lostCount */
#define DPM_REG_VERSION 0x24u     /**< 只读：0x44504D02 */
#define DPM_REG_CAPS 0x28u        /**< 只读：能力位 */
#define DPM_REG_SIG_CTRL 0x2Cu    /**< 读写：b0 enable、b1 srcSelect（按 b0 字节写入） */
#define DPM_REG_SIG_STATUS 0x30u  /**< 只读 */

/* --------------------------------------------------------------- 位域定义 */

/** CTRL（0x00）：b0/b2 是写 1 触发位，读回时恒为 0；b1 是普通读写位。 */
#define DPM_CTRL_START (1u << 0)     /**< 写 1 启动一次测量（busy 期间被忽略） */
#define DPM_CTRL_IRQ_ENABLE (1u << 1)/**< 中断使能（每次写 CTRL 都按此位整体更新） */
#define DPM_CTRL_CLEAR (1u << 2)     /**< 写 1 复位结果寄存器与全部标志（busy 期间挂起） */

/** STATUS（0x04）。 */
#define DPM_STATUS_BUSY (1u << 0)        /**< 测量进行中 */
#define DPM_STATUS_RESULT_VALID (1u << 1)/**< 有未被读走的结果 */
#define DPM_STATUS_LOST (1u << 2)        /**< 有结果被覆盖丢失（lostCount != 0） */
#define DPM_STATUS_IRQ_PENDING (1u << 3) /**< 中断挂起位 */

/** IRQ_STATUS（0x20）：三路写 1 清除。 */
#define DPM_IRQ_PENDING (1u << 0)      /**< 清中断挂起位 */
#define DPM_IRQ_RESULT_READ (1u << 1)  /**< 结果已读走（清 resultValid） */
#define DPM_IRQ_LOST_CLEAR (1u << 2)   /**< 清丢失计数 */

/** SIG_CTRL（0x2C）。 */
#define DPM_SIG_ENABLE (1u << 0)   /**< 片内信号源使能；0 时两路输出恒低 */
#define DPM_SIG_SELECT (1u << 1)   /**< 0 = 外部引脚（sig1/sig2），1 = 片内信号源 */

/** SIG_STATUS（0x30）。 */
#define DPM_SIGST_ENABLE (1u << 0)     /**< 片内源使能位回读 */
#define DPM_SIGST_SELECT (1u << 1)     /**< 输入选择位回读 */
#define DPM_SIGST_LOCKED (1u << 2)     /**< 片内源 MMCM 已锁定 */
#define DPM_SIGST_READY (1u << 3)      /**< 当前选择在配置上可用（选外部恒 1） */

/** VERSION / CAPS 的期望值（自检用）。 */
#define DPM_VERSION_VALUE 0x44504D02u
#define DPM_CAPS_RECORD_WORDS(c) ((c) & 0xFFu)        /**< 每条结果字数，应为 4 */
#define DPM_CAPS_RESULT_DEPTH(c) (((c) >> 8) & 0xFFu) /**< 结果深度，应为 1 */
#define DPM_CAPS_REGMAP (1u << 16)                    /**< 数据通道为寄存器（GPIO 式） */
#define DPM_CAPS_SIG_SRC (1u << 17)                   /**< 带片内信号源与输入选择 */

/** GATE_TARGET 只有低 28 位有效（gateCntWidth = 28，低速时钟 50MHz → 最长约 5.37s）。 */
#define DPM_GATE_TARGET_MAX 0x0FFFFFFFu

/** 结果寄存器只有 32 位，软件侧用 64 位中间量做乘法，见 dpm_calc.h。 */
#define DPM_EDGE_MAX 0xFFFFFFFFu
#define DPM_PHASE_MAX 0xFFFFFFFFu

/* --------------------------------------------------- 片内源 MMCM 的 DRP 口 */

/** DRP 寄存器文件基偏移。
 *
 * 方案 A（`vivado/package_src_ip.tcl` 打包的 `dpm_src_drp`）把寄存器表重排为
 * **直接镜像 MMCM 的 16 位寄存器值**：软件自己完成「频率/相位 → MMCM 计数器位域」
 * 的编码，PL 侧只负责把值按顺序写进 MMCM 的 DRP 口（硬件不含任何算术）。
 *
 * 这样做的原因：原来用 clk_wiz 的 `USE_DYN_RECONFIG + PHASE_DUTY_CONFIG`，该配置
 * 会把这套定点换算综合成硬件，实测在 xc7z010clg400-1 上占 13,287 个物理 LUT site
 * 与 38 个 DSP48E1（整设计 96% 的逻辑 LUT）；而 PG065 明确建议用「Write DRP」
 * 之类不做换算的方式省 DSP。
 *
 * 寄存器映射（字节偏移，每个都是 32 位访问、只有低 16 位有效）：
 *
 * | 偏移 | 名称        | 属性 | 说明                                     |
 * | ---- | ----------- | ---- | ---------------------------------------- |
 * | 0x00 | ID          | 只读 | 固定 0x44504D03（"DPM3"）                  |
 * | 0x04 | STATUS      | 只读 | b0 = MMCM locked、b1/b2 = 重配置进行中     |
 * | 0x08 | CLKFBOUT    | 读写 | MMCM 寄存器 0x14 的值                     |
 * | 0x0C | CLKFBOUT2   | 读写 | MMCM 寄存器 0x15 的值                     |
 * | 0x10 | CLKOUT0     | 读写 | MMCM 寄存器 0x08 的值（参考输出）           |
 * | 0x14 | CLKOUT0_2   | 读写 | MMCM 寄存器 0x09 的值                     |
 * | 0x18 | CLKOUT1     | 读写 | MMCM 寄存器 0x0A 的值（移相输出）           |
 * | 0x1C | CLKOUT1_2   | 读写 | MMCM 寄存器 0x0B 的值                     |
 * | 0x20 | DIVCLK      | 读写 | MMCM 寄存器 0x16 的值                     |
 * | 0x24 | POWER       | 读写 | MMCM 寄存器 0x28 的值（复位值 0xFFFF）      |
 * | 0x28 | LOAD        | W1S  | 写 1 触发一次完整重配置                     |
 */
#define DPM_DRP_OFF_BASE 0x08u

/** 关键寄存器偏移（字节）。 */
#define DPM_DRP_OFF_ID 0x00u        /**< 只读，应为 0x44504D03 */
#define DPM_DRP_OFF_STATUS 0x04u    /**< b0 = locked、b1/b2 = busy */
#define DPM_DRP_OFF_CLKFBOUT 0x08u  /**< MMCM 0x14 */
#define DPM_DRP_OFF_CLKFBOUT2 0x0Cu /**< MMCM 0x15 */
#define DPM_DRP_OFF_CLKOUT0 0x10u   /**< MMCM 0x08（参考输出） */
#define DPM_DRP_OFF_CLKOUT0_2 0x14u /**< MMCM 0x09 */
#define DPM_DRP_OFF_CLKOUT1 0x18u   /**< MMCM 0x0A（移相输出） */
#define DPM_DRP_OFF_CLKOUT1_2 0x1Cu /**< MMCM 0x0B */
#define DPM_DRP_OFF_DIVCLK 0x20u    /**< MMCM 0x16 */
#define DPM_DRP_OFF_POWER 0x24u     /**< MMCM 0x28 */
#define DPM_DRP_OFF_LOAD 0x28u      /**< W1S：写 1 触发重配置 */

#define DPM_DRP_ID_VALUE 0x44504D03u /**< ID 寄存器的固定值 */
/** 状态寄存器偏移（与 ID 同处本地寄存器区，不是 MMCM 镜像的一部分）。 */
#define DPM_DRP_STATUS_OFF 0x04u
#define DPM_DRP_STATUS_LOCKED (1u << 0) /**< MMCM 已锁定 */
#define DPM_DRP_STATUS_BUSY (1u << 1)   /**< 重配置进行中 */
/** LOAD 寄存器的别名（写 1 触发一次完整重配置）。 */
#define DPM_DRP_LOAD_OFF DPM_DRP_OFF_LOAD

/** POWER 寄存器：使能相位 mux 与 delay time（复位值）。 */
#define DPM_DRP_POWER_ENABLE_PHASE 0xFFFFu

/* ---- MMCM 寄存器编码（与 build/analysis/golden/mmcm_encode.py 逐步一致）----
 *
 * 位域依据官方 XAPP888 v1.8 的 Table 1/2，并用 clk_wiz 的 Write DRP 模式生成的
 * 权威寄存器值逐点校验过（多分频 × 多相位共 14 个点全部一致）：
 *
 *   ClkReg1 = {PHASE_MUX[15:13], 1'b1, HIGH_TIME[11:6], LOW_TIME[5:0]}
 *   ClkReg2 = {9'h000, EDGE[7], NO_COUNT[6], DELAY_TIME[5:0]}
 *
 * 其中：
 *   HIGH_TIME = divide / 2，LOW_TIME = divide - HIGH_TIME（50% 占空比）
 *   EDGE      = divide & 1（奇数分频才置位）
 *   NO_COUNT  = 1 当且仅当 divide == 1
 *   DELAY_TIME = round(phase_mdeg * divide / 360000) & 0x3F
 *   PHASE_MUX  = (round(phase_mdeg * divide / 360000) >> 6) & 0x7
 *
 * 注：手册把 ClkReg1 的 bit12 标为 RSVD，但 clk_wiz 的生成器在该位**恒置 1**
 * （div=20/40/80/100 实测全为 1）；编码以实测生成值为准，勿按手册改。
 */

/** 由分频比与相位（毫度）算出一个 CLKOUTx / CLKFBOUT 寄存器对。
 *
 * @param divide     输出分频比 D（1..128）
 * @param phase_mdeg 相位，单位毫度（0..360000）
 * @param clkreg1    输出：写入 MMCM 基地址（0x08/0x0A/0x14）的 16 位值
 * @param clkreg2    输出：写入基地址 +1（0x09/0x0B/0x15）的 16 位值
 */
#define DPM_MMCM_ENCODE_CLKOUT(divide, phase_mdeg, clkreg1, clkreg2)                       \
    do {                                                                                   \
        uint32_t d_ = (uint32_t)(divide);                                                  \
        uint32_t ht_ = d_ / 2u;                                                            \
        uint32_t lt_ = d_ - ht_;                                                           \
        uint32_t vco_ = ((uint32_t)(phase_mdeg) * d_ + 180000u) / 360000u;                 \
        uint32_t ed_ = (d_ == 1u) ? 0u : (d_ & 1u);                                        \
        uint32_t nc_ = (d_ == 1u) ? 1u : 0u;                                               \
        (clkreg1) = (uint16_t)(0x1000u | (((vco_ >> 6) & 0x7u) << 13) |                     \
                               ((ht_ & 0x3Fu) << 6) | (lt_ & 0x3Fu));                      \
        (clkreg2) = (uint16_t)((ed_ << 7) | (nc_ << 6) | (vco_ & 0x3Fu));                  \
    } while (0)

/** 从 CLKOUTx / CLKFBOUT 的 ClkReg1 反推分频比。
 *
 * 布局：`{PHASE_MUX[15:13], MARK[12], HIGH_TIME[11:6], LOW_TIME[5:0]}`。
 * MARK（bit12）在生成值里**恒为 1**，而 `divide == 1` 的计数器字是 `0x0041`
 * （bit12 为 0），因此：bit12 == 0 即 divide == 1，否则 divide = HIGH_TIME + LOW_TIME。
 */
static inline uint32_t dpm_mmcm_decode_divide(uint32_t clkreg1)
{
    if (((clkreg1 >> 12) & 1u) == 0u) {
        return 1u; /* divide == 1：HIGH/LOW_TIME 是占位的 1 */
    }
    return ((clkreg1 >> 6) & 0x3Fu) + (clkreg1 & 0x3Fu);
}

/** 由 DELAY_TIME 与输出分频反推相位（毫度）。
 *
 * DELAY_TIME 是 **VCO 周期**的整数倍，而 1 个输出周期 = `divide` 个 VCO 周期，
 * 因此 `phase_mdeg = delay_time * 360000 / divide`。
 * 与 `DPM_MMCM_ENCODE_CLKOUT` 的 `delay = round(phase * divide / 360000)` 互逆。
 */
static inline uint32_t dpm_mmcm_decode_phase_mdeg(uint32_t delay_time, uint32_t divide)
{
    if (divide == 0u) {
        return 0u;
    }
    return (uint32_t)(((uint64_t)(delay_time & 0x3Fu) * 360000u) / (uint64_t)divide) % 360000u;
}

/** 从 DIVCLK 寄存器（MMCM 地址 0x16）反推 DIVCLK_DIVIDE。
 *
 * 布局与 ClkReg1 不同：`{2'b00, EDGE[13], NO_COUNT[12], HIGH_TIME[11:6], LOW_TIME[5:0]}`，
 * 这里 bit12 是 NO_COUNT（divide == 1 时为 1）。
 */
static inline uint32_t dpm_mmcm_decode_divclk(uint32_t divreg)
{
    if (((divreg >> 12) & 1u) != 0u) {
        return 1u;
    }
    return ((divreg >> 6) & 0x3Fu) + (divreg & 0x3Fu);
}

/** 由分频比算 DIVCLK 寄存器（MMCM 地址 0x16）。 */
static inline uint16_t dpm_mmcm_encode_divclk(uint32_t divclk_divide)
{
    uint32_t d = divclk_divide;
    uint32_t ht = (d == 1u) ? 1u : (d / 2u);
    uint32_t lt = (d == 1u) ? 1u : (d - d / 2u);
    uint32_t ed = (d == 1u) ? 0u : (d & 1u);
    uint32_t nc = (d == 1u) ? 1u : 0u;
    /* 布局：{2'b00, EDGE[13], NO_COUNT[12], HIGH_TIME[11:6], LOW_TIME[5:0]}
     * 权威值 DIVCLK(divide=1) = 0x1041 = no_count(bit12) | high_time=1(bit6) | low_time=1 */
    return (uint16_t)((ed << 13) | (nc << 12) | ((ht & 0x3Fu) << 6) | (lt & 0x3Fu));
}

/** **相位现在可写**：方案 A 由软件直接编码 MMCM 的 PHASE_MUX/DELAY_TIME，
 *  不再经过 clk_wiz 那套只在 PHASE_DUTY_CONFIG=true 时才接通的语义寄存器。
 *  上板语义待重新实测（见 spec/drp_semantics.json）。 */
#define DPM_DRP_PHASE_WRITABLE 1

#endif /* DPM_REGS_H */
