/** @file dpm_uart.c
 *  @brief Zynq-7000 UART 驱动：初始化 + 轮询收发 + 可选 RX 中断环形缓冲。
 *
 * 寄存器与位域见 Zynq-7000 TRM 第 19 章；本文件只依赖 `dpm_board.h` 里的宏。
 * 两条路径都实现：
 *   - **轮询（默认，上板首选）**：`dpm_uart_rx_ready/getc` + `dpm_uart_putc`；
 *   - **中断**：`dpm_uart_irq_enable(1)` 后 ISR 把 FIFO 搬进软件环形缓冲，
 *     主循环用 `dpm_uart_irq_getc` 取。中断路径的配置与上板验证见 `docs/bringup.md`。
 *
 * 波特率（Zynq 的公式 `baud = f_ref / (BAUDDIV × (BAUDGEN + 1))`，不引入浮点）：
 *   先取 8 倍过采样的整数近似 `baudgen = round(f_ref/(baud×8)) - 1`，
 *   再用 `bauddiv = round(f_ref/((baudgen+1)×baud))` 收尾；
 *   两者合起来给出 `f_ref/baud` 的整数近似，误差 ≤ 0.5%（UART 容忍约 ±2.5%）。
 *   `f_ref` 是 UART 参考时钟（SLCR `UART_CLK_CTRL` @0xF800_0154），与 PL 的
 *   FCLK_CLK0（慢时钟域）是不同的时钟，不能混用。
 */

#include "dpm_board.h"

static uint32_t s_base = DPM_UART1_BASE;
static uint32_t s_baudgen = 0u;
static uint32_t s_bauddiv = 0u;

/* --------------------------------------------------------- 无 libc 的小工具 */

static uint32_t uart_rd(uint32_t off)
{
    return *(volatile uint32_t *)(uintptr_t)(s_base + off);
}

static void uart_wr(uint32_t off, uint32_t val)
{
    *(volatile uint32_t *)(uintptr_t)(s_base + off) = val;
}

/** 四舍五入的整数除法（不引入浮点）。 */
static uint32_t div_round(uint32_t num, uint32_t den)
{
    if (den == 0u) {
        return 0u;
    }
    return (num + den / 2u) / den;
}

/* ------------------------------------------------------------------- 初始化 */

void dpm_uart_init(uint32_t base, uint32_t f_ref_hz, uint32_t baud)
{
    uint32_t baudgen;
    uint32_t bauddiv;

    s_base = base;
    if (baud == 0u) {
        baud = 115200u;
    }
    /* 波特率发生器：8 倍过采样 */
    baudgen = div_round(f_ref_hz, baud * 8u);
    if (baudgen == 0u) {
        baudgen = 1u;
    }
    baudgen -= 1u;
    bauddiv = div_round(f_ref_hz, (baudgen + 1u) * baud);
    if (bauddiv == 0u) {
        bauddiv = 1u;
    }
    s_baudgen = baudgen;
    s_bauddiv = bauddiv;

#if defined(DPM_BRINGUP_TRACE) && (DPM_BRINGUP_TRACE == 1)
    /* 临时上板调试（测完删除）：把算出的 BDIV/CD 与参考时钟低 12 位写到 PL 的
     * GATE_TARGET 寄存器（0x43C0_0008，PS 可读写），这样不依赖串口也能用 JTAG
     * 经 APU 物理通路读出程序内部的计算结果。 */
    *(volatile uint32_t *)(uintptr_t)0x43C00008u =
        (0x5A000000u) | ((bauddiv & 0xFFu) << 16) | (baudgen & 0xFFFFu);
#endif

    uart_wr(DPM_UART_CTRL, 0u);          /* 先关收发 */
    uart_wr(DPM_UART_IDR, 0x3FFFu);      /* 关全部中断（IXR 有效位 [12:0]） */
    uart_wr(DPM_UART_MODEMCR, 0u);       /* 明确关掉硬件流控（FCM=b5）：无 CTS 时会卡死发送 */
    uart_wr(DPM_UART_RXWM, 1u);          /* RX 触发水平 = 1 字节，中断路径低延迟 */
    uart_wr(DPM_UART_BAUDGEN, baudgen);
    uart_wr(DPM_UART_BAUDDIV, bauddiv);
    uart_wr(DPM_UART_MODE, 0x20u);       /* 8 数据位（CHARLEN=0）、1 停止位、无校验 */
    uart_wr(DPM_UART_CTRL, 0x17u);       /* b4 TX_EN | b2 RX_EN | b1 TXRST | b0 RXRST */
}

/* ----------------------------------------------------------------- 轮询收发 */

int dpm_uart_rx_ready(void)
{
    return (uart_rd(DPM_UART_CHANNEL_STS) & DPM_UART_STS_RX_EMPTY) == 0u ? 1 : 0;
}

int dpm_uart_getc(void)
{
    if (dpm_uart_rx_ready() == 0) {
        return -1;
    }
    return (int)(uart_rd(DPM_UART_RX_FIFO) & 0xFFu);
}

void dpm_uart_putc(uint8_t b)
{
    while ((uart_rd(DPM_UART_CHANNEL_STS) & DPM_UART_STS_TX_FULL) != 0u) {
        /* 忙等 TX FIFO 有空位 */
    }
    uart_wr(DPM_UART_TX_FIFO, (uint32_t)b);
}

void dpm_uart_write(const uint8_t *buf, uint32_t len)
{
    uint32_t i;
    for (i = 0u; i < len; i++) {
        dpm_uart_putc(buf[i]);
    }
}

/* --------------------------------------------------------- 中断路径（可选） */

/** 软件环形缓冲：ISR 生产、主循环消费。 */
#define DPM_UART_RING_SIZE 256u
#define DPM_UART_RING_MASK (DPM_UART_RING_SIZE - 1u)

static volatile uint8_t s_ring[DPM_UART_RING_SIZE];
static volatile uint32_t s_ring_head; /* 写指针（ISR） */
static volatile uint32_t s_ring_tail; /* 读指针（主循环） */

void dpm_uart_irq_enable(int on)
{
    if (on != 0) {
        s_ring_head = 0u;
        s_ring_tail = 0u;
        /* RX FIFO 触发水平已在 init 里设成 1 字节，因此 RXOVR 就是"来数据"；
         * 再加 RX 超时（TOUT）兜底：不足触发水平时也会进中断。 */
        uart_wr(DPM_UART_IER, DPM_UART_IXR_RXOVR | DPM_UART_IXR_TOUT);
    } else {
        uart_wr(DPM_UART_IDR, DPM_UART_IXR_RXOVR | DPM_UART_IXR_TOUT);
    }
}

int dpm_uart_irq_getc(void)
{
    int v = -1;
    if (s_ring_tail != s_ring_head) {
        v = (int)s_ring[s_ring_tail & DPM_UART_RING_MASK];
        s_ring_tail++;
    }
    return v;
}

void dpm_uart_isr(void)
{
    uint32_t isr = uart_rd(DPM_UART_ISR);
    /* 把 RX FIFO 搬空（FIFO 深度 64；用 CHANNEL_STS 判空，不依赖中断位） */
    while (dpm_uart_rx_ready() != 0) {
        uint8_t b = (uint8_t)(uart_rd(DPM_UART_RX_FIFO) & 0xFFu);
        uint32_t next = (s_ring_head + 1u) & DPM_UART_RING_MASK;
        if (next != (s_ring_tail & DPM_UART_RING_MASK)) {
            s_ring[s_ring_head & DPM_UART_RING_MASK] = b;
            s_ring_head++;
        } else {
            break; /* 缓冲满：丢字节（协议层 CRC 会丢弃该帧） */
        }
    }
    /* W1C：写回读到的位清中断 */
    uart_wr(DPM_UART_ISR, isr);
}
