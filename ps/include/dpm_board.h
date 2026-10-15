/** @file dpm_board.h
 *  @brief 板级驱动接口：UART、GIC/中断、真实 MMIO 的 IO vtable。
 *
 * 这一层只服务 PS 侧裸机程序（`ps/src/dpm_*.c`），宿主机验证台不使用它。
 */

#ifndef DPM_BOARD_H
#define DPM_BOARD_H

#include <stdint.h>

#include "dpm_io.h"

/* ------------------------------------------------------- 上板调试 trace */
/* 上板 bring-up 专用（`-DDPM_BRINGUP_TRACE=1`）：把"启动阶段号"写到 PL 的
 * GATE_TARGET 寄存器（0x43C0_0008，PS 可读写）。串口没通时，可以用 JTAG 经
 * APU 目标的物理通路直接读出阶段号，从而判断程序卡在哪一步。
 * 注意：它会占用闸门目标寄存器，测完必须关掉这个宏。 */
#if defined(DPM_BRINGUP_TRACE) && (DPM_BRINGUP_TRACE == 1)
#define DPM_TRACE(stage) \
    (*(volatile uint32_t *)(uintptr_t)0x43C00008u = (0x5E000000u | (uint32_t)(stage)))
#else
#define DPM_TRACE(stage) ((void)0)
#endif

/* ------------------------------------------------------------------ CPU */

/** CPU 变体号（MIDR[23:20]，Cortex-A9：r1p0=1、r2p0=2…）。
 *  `startup.c` 在复位后读一次；r2p0 起才支持 VBAR（本板实测不支持，见 startup.c）。 */
extern uint32_t dpm_cpu_variant;

/* ------------------------------------------------------------------ UART */

/** Zynq-7000 UART 基地址（TRM 第 19 章：UART0 = 0xE000_0000、UART1 = 0xE000_1000）。 */
#define DPM_UART0_BASE 0xE0000000u
#define DPM_UART1_BASE 0xE0001000u

/** UART 寄存器偏移（Zynq-7000 TRM 第 19 章 / Cadence UART；
 *  已逐个对照 Xilinx 官方驱动头 `xuartps_hw.h` 核对过）。 */
#define DPM_UART_CTRL 0x00u       /**< CR：b0 RXRST、b1 TXRST、b2 RX_EN、b4 TX_EN（0x17 = 使能 + 复位收发） */
#define DPM_UART_MODE 0x04u       /**< MR：b5 PARITY_NONE、b2:1 CHARLEN（0 = 8 位）、b0 CLKSEL */
#define DPM_UART_IER 0x08u        /**< 写 1 使能中断 */
#define DPM_UART_IDR 0x0Cu        /**< 写 1 禁中断 */
#define DPM_UART_IMR 0x10u        /**< 只读：中断屏蔽状态 */
#define DPM_UART_ISR 0x14u        /**< W1C：中断状态 */
#define DPM_UART_BAUDGEN 0x18u    /**< 波特率发生器 BDIV（baud = f_ref / (BAUDDIV × (BDIV + 1))) */
#define DPM_UART_RXWM 0x20u       /**< RX FIFO 触发水平（[5:0]，复位值 0x20） */
#define DPM_UART_MODEMCR 0x24u    /**< 调制解调器控制；**b5 FCM = 1 会打开硬件流控**，无 CTS 时会卡死发送 */
#define DPM_UART_MODEMSR 0x28u    /**< 调制解调器状态 */
#define DPM_UART_CHANNEL_STS 0x2Cu/**< SR：b1 RXEMPTY、b4 TXFULL、b3 TXEMPTY、b11 TACTIVE */
#define DPM_UART_RX_FIFO 0x30u    /**< 读：出队 */
#define DPM_UART_TX_FIFO 0x30u    /**< 写：入队 */
#define DPM_UART_BAUDDIV 0x34u    /**< 波特率分频 CD（[7:0]） */

/* 中断状态/使能位（对应 xuartps_hw.h 的 XUARTPS_IXR_*） */
#define DPM_UART_IXR_RXOVR 0x0001u   /**< RX FIFO 到达触发水平 */
#define DPM_UART_IXR_TOUT 0x0100u    /**< RX 超时（不足触发水平也有数据） */

#define DPM_UART_STS_RX_EMPTY (1u << 1) /**< SR.b1：RX FIFO 空 */
#define DPM_UART_STS_TX_FULL (1u << 4)  /**< SR.b4：TX FIFO 满（注意不是 b3，b3 是 TXEMPTY） */
#define DPM_UART_STS_TX_EMPTY (1u << 3) /**< SR.b3：TX FIFO 空 */

/** 初始化 UART：`CTRL = 0x17`（txen|rxen|tres|rres）、`MODE = 0x20`（8N1）、
 *  按 `f_ref_hz` 与 `baud` 计算 BAUDGEN/BAUDDIV。
 *
 *  注意 `f_ref_hz` 是 **UART 参考时钟**（SLCR `UART_CLK_CTRL` @0xF800_0154 给该 UART 的
 *  时钟），不是 PL 的慢时钟 FCLK_CLK0：两者是独立时钟源。上板实测值见 `dpm_main.c`
 *  的 `DPM_UART_REF_HZ`。 */
void dpm_uart_init(uint32_t base, uint32_t f_ref_hz, uint32_t baud);

/** 是否有待读字节（轮询路径）。 */
int dpm_uart_rx_ready(void);

/** 读一个字节；无数据时返回 -1。 */
int dpm_uart_getc(void);

/** 发送一个字节（忙等 TX FIFO 不满）。 */
void dpm_uart_putc(uint8_t b);

/** 发送一段字节。 */
void dpm_uart_write(const uint8_t *buf, uint32_t len);

/** 打开/关闭 UART RX 中断（中断路径，需要 GIC 已使能）。 */
void dpm_uart_irq_enable(int on);

/** 中断路径的取字节：从软件环形缓冲取；空返回 -1。 */
int dpm_uart_irq_getc(void);

/** UART 中断服务（由 `dpm_irq.c` 的 IRQ 分发调用）。 */
void dpm_uart_isr(void);

/* ------------------------------------------------------------- GIC 与中断 */

/** PS 中断号（Zynq-7000 的 SPI 编号）。 */
#define DPM_IRQ_ID_UART0 59u /**< XPS_UART0_INT_ID */
#define DPM_IRQ_ID_UART1 82u /**< XPS_UART1_INT_ID */

/** PL 的 IRQ_F2P[0] 经 GIC 的 SPI 编号（BD 里 pl_ps_irq 的连接决定，可配置）。 */
#ifndef DPM_IRQ_ID
#define DPM_IRQ_ID 61u
#endif

/** 初始化向量表/GIC（关中断状态下调用）。 */
void dpm_irq_init(void);

/** 使能某个 SPI。 */
void dpm_gic_enable_irq(uint32_t id);

/** 关闭某个 SPI。 */
void dpm_gic_disable_irq(uint32_t id);

/** 全局开中断（CPSR 的 I 位清零）。 */
void dpm_irq_global_enable(void);

/** 全局关中断。 */
void dpm_irq_global_disable(void);

/** 测量完成中断标志：ISR 置 1，主循环取走并清零。 */
int dpm_irq_take_meas_flag(void);

/** 中断累计次数（诊断用）。 */
uint32_t dpm_irq_count(void);

/** 异常向量分发（由 `dpm_irq_handler` 汇编入口调用）。 */
void dpm_irq_dispatch(void);

/* ------------------------------------------------- 真实 MMIO 的 IO 实现 */

/** 真实 AXI4-Lite / DRP 读写 + Cortex-A9 全局定时器时间源。 */
extern const dpm_io_t dpm_hw_io;

/** 单调毫秒计数（全局定时器；未使能时返回 0，命令核心会退化为轮询计数）。 */
uint32_t dpm_time_now_ms(void);

/** 初始化时间源（启动全局定时器；幂等）。 */
void dpm_time_init(void);

#endif /* DPM_BOARD_H */
