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

/** DRP 寄存器文件基偏移（PG065：AXI4-Lite 动态重配置寄存器从 0x200 起，按字对齐）。 */
#define DPM_DRP_OFF_BASE 0x200u
/** 第 n 个配置字（n = 0..31）的字节偏移：0x200 + 4n。 */
#define DPM_DRP_WORD(n) (DPM_DRP_OFF_BASE + 4u * (uint32_t)(n))

/** 配置字索引（与 `clk_wiz_drp.vhd` 的 ram_clk_config 索引一致）。 */
#define DPM_DRP_IDX_CLKFBOUT 0u      /**< {MULT[15:8], DIVCLK[7:0]} */
#define DPM_DRP_IDX_CLKOUT0 2u       /**< clk_out1 分频（低字节 = D） */
#define DPM_DRP_IDX_CLKOUT0_PHASE 3u /**< clk_out1 相位，单位**毫度** */
#define DPM_DRP_IDX_CLKOUT0_DUTY 4u  /**< clk_out1 占空比（×1e5，50000 = 50%） */
#define DPM_DRP_IDX_CLKOUT1 5u       /**< clk_out2 分频（低字节 = D） */
#define DPM_DRP_IDX_CLKOUT1_PHASE 6u /**< clk_out2 相位，单位**毫度** */
#define DPM_DRP_IDX_CLKOUT1_DUTY 7u  /**< clk_out2 占空比 */

/** 配置字内容（实测：`*_clk_wiz_drp.vhd:391-415` 的复位赋值 + xsim 读回，
 *  每个输出占 3 个字；`spec/drp_semantics.json` 是机器可读版本）。
 *
 * | 字索引 | 字节偏移 | 复位值     | 含义                                    |
 * | ------ | -------- | ---------- | --------------------------------------- |
 * | 0      | 0x200    | 0x00001401 | CLKFBOUT：MULT = 0x14 = 20、DIVCLK = 1  |
 * | 2      | 0x208    | 0x00000028 | clk_out1 分频 = 0x28 = **40 = D 本体**  |
 * | 3      | 0x20C    | 0x00000000 | clk_out1 相位（**毫度**）               |
 * | 4      | 0x210    | 0x0000C350 | clk_out1 占空比（50000 → 50%）          |
 * | 5      | 0x214    | 0x00000028 | clk_out2 分频 = 40 = D                  |
 * | 6      | 0x218    | 0x00015F90 | clk_out2 相位 = 90000 毫度 = 90.000°    |
 * | 7      | 0x21C    | 0x0000C350 | clk_out2 占空比 = 50%                   |
 *
 * **两条纠正**（早期文档写错过，以实测为准）：
 *   1. 分频字段是**低字节的 D 本体**，不是 `2*D`；
 *   2. 相位**不是**分频字里的 bit[15:8]，而是**独立的 32 位毫度字**（0x20C / 0x218）。
 */

/** 分频字段：低字节直接存放分频比 D。 */
#define DPM_DRP_DIVIDE_FIELD(div) ((uint32_t)(div) & 0xFFu)
/** 从配置字取回分频比（低字节）。 */
#define DPM_DRP_DIVIDE_GET(w) ((w) & 0xFFu)
/** 相位字：整数**毫度**（例如 90000 = 90.000°、22500 = 22.5°）。 */
#define DPM_DRP_PHASE_MDEG(mdeg) ((uint32_t)(mdeg))
/** 从相位字取回毫度。 */
#define DPM_DRP_PHASE_MDEG_GET(w) ((w) & 0xFFFFFFFFu)

/** 写「Load」寄存器触发重新锁定。实测语义：
 *  - bit0 = **触发位**（写 1 才真正加载）；
 *  - bit1 = **SADDR**：0 = 用静态 S1 配置（BD 里的初值）、1 = 用寄存器文件（S2）配置；
 *  - 复位值 `0x00000003`（= 触发 + S1 路径），xsim 实测 `0x03` 与 `0x07` 结果一致。
 */
#define DPM_DRP_LOAD_OFF 0x25Cu
#define DPM_DRP_LOAD_TRIGGER (1u << 0) /**< 触发重新加载 */
#define DPM_DRP_LOAD_SADDR (1u << 1)   /**< 1 = 使用寄存器文件（动态）配置 */
/** 触发一次加载（寄存器文件路径）。 */
#define DPM_DRP_LOAD_BOTH 0x07u

/** 状态寄存器在**偏移 0x004**（不是「字 31 / 0x27C」，后者恒为 0）。 */
#define DPM_DRP_STATUS_OFF 0x004u
#define DPM_DRP_STATUS_LOCKED (1u << 0) /**< MMCM 已锁定 */
#define DPM_DRP_STATUS_SRDY (1u << 1)   /**< 重新配置完成（只在锁定那一拍为 1） */

/** **当前 bitstream 的限制**：寄存器文件里的相位字没有接到 MMCM 重建逻辑，
 *  写它不会改变输出相位（xsim 实测 43 组，`clk_out2` 恒为静态配置的 90°）。
 *  频率（分频字）可以改。要恢复在线相位能力需要给 `clk_wiz_src` 加
 *  `CONFIG.PHASE_DUTY_CONFIG {true}` 后重建 IP/BD，见 `docs/verify-drp.md`。
 */
#define DPM_DRP_PHASE_WRITABLE 0

#endif /* DPM_REGS_H */
