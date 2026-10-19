/** @file fpga_model.h
 *  @brief 宿主机（x86-64）上的 PL 寄存器级 + 测量级仿真后端。
 *
 * 目的：在没有硬件的情况下验证 **同一份** `dpm_cmd.c`（命令处理核心）与协议实现。
 * 模型暴露的读写接口与真实 MMIO 完全一致（绝对地址 + 32 位整字），
 * 因此 `dpm_cmd.c` 不需要任何条件编译。
 *
 * 建模范围与假设（写进 `ps/README.md`，避免把模型当成硬件）：
 *   1. 寄存器语义逐个对齐 `src/main/scala/DigitalPhaseMeter/ControlRegs.scala`：
 *      start 脉冲（busy 期间忽略）、busy/resultValid/lost/irqPending、
 *      结果被覆盖即 lostCount++、IRQ_STATUS 的 W1C、CTRL.clear 在 busy 期间挂起、
 *      SIG_CTRL 写入后配置快照经若干慢周期才送达测量域（期间 DPM 的写通道挂起）。
 *   2. 测量模型（与 `spec/gen_golden.py` 逐位一致，全程整数 RNE）：
 *        gate_cnt  = target_gate + ceil(T_sig / T_slow)
 *        edge_cnt  = floor(gate_cnt * f_sig / f_slow)
 *        dsp_cycles = RNE(phase_mdeg * D * f_dsp / (360000 * VCO))
 *        phase_cnt = dsp_cycles * edge_cnt
 *      其中内部源的 (VCO, D, phase_field) 取自 DRP 配置字。
 *   3. 时间推进：**每次 AXI 事务（读或写）推进 `DPM_MODEL_CYCLES_PER_TXN` 个慢周期**，
 *      这是宿主机上的简化（真实硬件是自由运行的时钟）。busy 的持续时间按
 *      `target_gate` 个慢周期推进，因此上位机轮询 STATUS 就能看到测量完成。
 *   4. 源不可用（片内源未使能/未锁定，或外部引脚无信号）时两路输入恒低，
 *      测量永远停在「等参考沿」、busy 恒为 1、**不会**产生结果也不会自增 RESULT_SEQ
 *      —— 与真实 PL 一致（外部引脚默认没接信号，`ext_absent = 1`）。
 *      `host_main --ext-present` 可模拟"外部接了 10MHz 信号"，用于正向用例。
 */

#ifndef DPM_FPGA_MODEL_H
#define DPM_FPGA_MODEL_H

#include <stdint.h>

#include "dpm_io.h"

/** 每个 AXI 事务推进的慢周期数（宿主机简化；见文件头说明）。 */
#define DPM_MODEL_CYCLES_PER_TXN 64u

/** 主机验证台的仿真加速：**主循环每迭代一次**额外推进这么多慢周期。
 *
 * 为什么需要它：模型时间只在 AXI 事务时推进，而主循环的节拍由 `poll()` 超时决定
 * （2ms）。宿主机负载高时一次迭代可能远长于 2ms，虚拟时间就会比墙钟慢几十倍，
 * 依赖"1ms 流上报间隔"的用例会等不到通知。显式推进把**虚拟时间与墙钟解耦**：
 * 每迭代 4096 个慢周期 ≈ 82µs 虚拟时间，1ms 虚拟时间只需约 12 次迭代。
 * 置 0 可退回"纯事务推进"（`--speed 0`）。 */
#define DPM_MODEL_CYCLES_PER_ITER 4096u

/** 片内源配置快照跨到测量域所需的慢周期数。 */
#define DPM_MODEL_CONFIG_LATENCY_CYCLES 4u

/** MMCM 重新锁定所需的慢周期数。 */
#define DPM_MODEL_LOCK_LATENCY_CYCLES 64u

/** 仿真的 PL 状态。调用方只需要持有它并把指针作为 `dpm_io_t` 的 ctx。 */
typedef struct {
    /* ---- 寄存器堆（按字节偏移寻址，低 6 位） ---- */
    uint32_t gate_target; /**< GATE_TARGET（28 位有效） */
    uint32_t irq_enable;  /**< CTRL.b1 镜像（读回用） */
    uint32_t sig_ctrl;    /**< SIG_CTRL：b0 enable、b1 select */

    /* ---- 标志与结果 ---- */
    uint8_t busy;             /**< STATUS.b0 */
    uint8_t result_valid;     /**< STATUS.b1 */
    uint32_t lost_count;      /**< LOST_COUNT */
    uint32_t seq;             /**< 下一个记录的序号 */
    uint8_t irq_pending;      /**< STATUS.b3 */
    uint8_t clear_pending;    /**< CTRL.clear 在 busy 期间挂起 */
    uint32_t res_gate;        /**< RESULT_GATE */
    uint32_t res_edge;        /**< RESULT_EDGE */
    uint32_t res_phase;       /**< RESULT_PHASE */
    uint32_t res_seq;         /**< RESULT_SEQ */

    /* ---- 进行中的测量 ---- */
    uint32_t remaining_cycles; /**< 距离完成还差的慢周期数 */
    uint8_t meas_blocked;      /**< 1 = 源无沿，测量永不完成（一直 busy） */
    uint8_t active_source;     /**< 0 = 外部、1 = 片内（启动时锁存） */
    uint32_t pending_gate;     /**< 本次测量锁存的 gate_cnt */
    uint32_t pending_edge;     /**< 本次测量锁存的 edge_cnt */
    uint32_t pending_phase;    /**< 本次测量锁存的 phase_cnt */

    /* ---- MMCM DRP ---- */
    uint32_t drp[32];       /**< DRP 配置字（0x200 + 4n） */
    uint8_t drp_locked;     /**< DRP 状态字 bit0 */
    uint8_t lock_stuck_low; /**< 测试钩子：1 = 永不失锁也不再锁定（覆盖 ERR_BUSY 路径） */
    uint32_t lock_left;     /**< 重新锁定剩余慢周期 */

    /* ---- 配置快照握手 ---- */
    uint32_t config_pending; /**< 剩余慢周期；>0 时 DPM 的写通道挂起 */
    uint8_t wr_held;         /**< 被挂起的写（AXI 写通道 ready=0） */
    uint32_t wr_held_addr;
    uint32_t wr_held_data;

    /* ---- 输入源 ---- */
    uint32_t slow_hz; /**< 低速时钟频率（默认 50MHz） */
    uint32_t dsp_hz;  /**< 高速时钟频率（默认 450MHz） */
    uint32_t ext_freq_hz;    /**< 外部源频率（默认 10MHz，仅 --ext-present 时使用） */
    uint32_t ext_phase_mdeg; /**< 外部源相位差（默认 0） */
    uint8_t ext_absent;      /**< 1 = 外部引脚无信号（默认 1：两路恒低，测量停在等参考沿） */

    /* ---- 统计 ---- */
    uint64_t cycles;   /**< 累计仿真慢周期 */
    uint32_t txn_count;/**< AXI 事务计数 */
    uint32_t start_count; /**< 实际启动的测量次数 */
    uint32_t blocked_count; /**< 因源不可用而卡住的启动次数 */
} dpm_model_t;

/** 初始化模型：DRP 复位值取 BD 的初值（VCO=1000MHz、D=40、相位字段 90），
 *  因此上电默认工作点就是协议 §7 的「25MHz / 90°」。 */
void dpm_model_init(dpm_model_t *m, uint32_t slow_hz, uint32_t dsp_hz);

/** 取模型对应的 IO 实现（静态 vtable，ctx 传模型指针）。 */
const dpm_io_t *dpm_model_io(void);

/** 设置外部源（`absent` = 1 时两路恒低，测量卡在 busy）。 */
void dpm_model_set_external(dpm_model_t *m, uint32_t freq_hz, uint32_t phase_mdeg, int absent);

/** 显式推进时间（等效于做了若干次 AXI 事务），测试用。 */
void dpm_model_advance(dpm_model_t *m, uint32_t cycles);

/** 当前仿真时间（毫秒）。 */
uint32_t dpm_model_now_ms(const dpm_model_t *m);

/** 从 DRP 字解析出的内部源工作点（供测试核对）。 */
void dpm_model_internal_point(const dpm_model_t *m, uint32_t *vco_hz, uint32_t *div,
                              uint32_t *phase_field);

/** 测试钩子：强制 MMCM 永远失锁（用于覆盖「未锁定就选片内源 → ERR_BUSY」路径）。 */
void dpm_model_set_lock_stuck(dpm_model_t *m, int stuck);

/** 当前有效的被测信号（f_sig、相位毫度、是否有沿）。 */
void dpm_model_active_signal(const dpm_model_t *m, uint32_t *freq_hz, uint32_t *phase_mdeg,
                             int *present);

#endif /* DPM_FPGA_MODEL_H */
