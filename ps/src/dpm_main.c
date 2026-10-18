/** @file dpm_main.c
 *  @brief PS 侧主程序：初始化板级外设 → 注入命令核心 → 主循环收发与调度。
 *
 * 主循环做四件事（顺序固定）：
 *   1. 若有中断路径且测量完成标志置起 → 抢先把结果取进缓存；
 *   2. 把 UART 收到的字节喂给收帧状态机，凑齐一帧就交给 `dpm_cmd_handle`，
 *      响应直接写回 UART（**不经过缓冲，避免响应被日志挤后**）；
 *   3. `dpm_cmd_tick()`：连续模式续跑、流上报、MMCL 失锁/恢复事件；
 *   4. 回到 1。
 *
 * 时钟参数默认 `f_slow = 50MHz`（AXI/闸门域）、`f_dsp = 450MHz`（测量域），
 * 可用 `-DDPM_SLOW_HZ=...` / `-DDPM_DSP_HZ=...` 覆盖（必须与 PL 的 MMCM 配置一致）。
 */

#include "dpm_board.h"
#include "dpm_cmd.h"
#include "dpm_proto.h"

#ifndef DPM_SLOW_HZ
#define DPM_SLOW_HZ 50000000u
#endif
#ifndef DPM_DSP_HZ
#define DPM_DSP_HZ 450000000u
#endif
#ifndef DPM_UART_BAUD
#define DPM_UART_BAUD 115200u
#endif
/* UART 参考时钟：SLCR `UART_CLK_CTRL`（0xF800_0154）送进 UART 的时钟，由 FSBL /
 * `ps7_init` 按 BD 的 PS7 配置给出，**与 PL 的慢时钟 FCLK_CLK0 无关**。
 * 它决定波特率分频，填错会导致收发全乱码。
 *
 * 默认值 86400000 是 **AX7010 上板实测**的：把分频固定成 CD=6、BAUDGEN=124
 * （K = 6×125 = 750）后用 JTAG 直接往 TX FIFO 灌 "DPM-PS"，上位机在
 * 112000~120000 波特率之间能清晰解出，即 115200×750 = 86.4MHz。
 * 换板/换 FSBL 后用 ps/tests/find_uart_baud.py 重测即可。 */
#ifndef DPM_UART_REF_HZ
#define DPM_UART_REF_HZ 86400000u
#endif
#ifndef DPM_UART_BASE_SEL
#define DPM_UART_BASE_SEL DPM_UART1_BASE
#endif
/* 上电横幅：纯 ASCII 一行，用于上板确认程序在跑、串口通路通。
 * 上位机收帧状态机会跳过非帧字节，横幅不影响协议。 */
#ifndef DPM_BOOT_BANNER
#define DPM_BOOT_BANNER 1
#endif

static uint8_t s_tx_frame[DPM_FRAME_MAX];

/** 打印 uint32 十进制（不引入 libc）。 */
static void puts_u32(uint32_t v)
{
    char buf[11];
    int n = 0;

    if (v == 0u) {
        dpm_uart_putc('0');
        return;
    }
    while (v != 0u && n < (int)sizeof(buf)) {
        buf[n++] = (char)('0' + (char)(v % 10u));
        v /= 10u;
    }
    while (n > 0) {
        dpm_uart_putc((uint8_t)buf[--n]);
    }
}

/** 上电横幅：一行 ASCII，含 UART 实例、波特率、参考时钟与协议版本。 */
static void boot_banner(void)
{
    static const char line[] = "\r\nDPM-PS ready: uart=UART1 baud=";
    DPM_TRACE(8);
    uint32_t i;

    for (i = 0u; i < sizeof(line) - 1u; i++) {
        dpm_uart_putc((uint8_t)line[i]);
    }
    puts_u32(DPM_UART_BAUD);
    {
        static const char tail[] = " uart_ref_hz=";
        for (i = 0u; i < sizeof(tail) - 1u; i++) {
            dpm_uart_putc((uint8_t)tail[i]);
        }
    }
    puts_u32(DPM_UART_REF_HZ);
    {
        static const char end[] = " cpu_variant=";
        for (i = 0u; i < sizeof(end) - 1u; i++) {
            dpm_uart_putc((uint8_t)end[i]);
        }
    }
    puts_u32(dpm_cpu_variant);
    DPM_TRACE(9);
    {
        static const char end[] = " proto=1.1\r\n";
        for (i = 0u; i < sizeof(end) - 1u; i++) {
            dpm_uart_putc((uint8_t)end[i]);
        }
    }
}

/** 通知帧回调：直接写 UART。 */
static int uart_emit(void *user, const uint8_t *frame, uint32_t len)
{
    (void)user;
    dpm_uart_write(frame, len);
    return 0;
}

/** 取一个待处理字节：中断环形缓冲优先，其次轮询 FIFO；无数据返回 -1。 */
static int next_byte(void)
{
#if defined(DPM_USE_IRQ) && (DPM_USE_IRQ == 1)
    int b = dpm_uart_irq_getc();
    if (b >= 0) {
        return b;
    }
#endif
    return dpm_uart_getc();
}

int main(void)
{
    dpm_cmd_ctx_t ctx;
    dpm_rx_t rx;
    dpm_clocks_t clk;
#if DPM_BOOT_BANNER
    /* 收到过任何一条有效帧之前，周期性重发横幅：上板首次联调时上位机可以
     * 按波特率逐个试探（见 ps/tests/find_uart_baud.py），不需要重启程序。
     * 一旦收到有效帧就永久停发，不影响协议时序。 */
    uint32_t banner_ms = 0u;
    int banner_stop = 0;
#endif

    DPM_TRACE(5);
    dpm_time_init();
    DPM_TRACE(6);
    dpm_uart_init(DPM_UART_BASE_SEL, DPM_UART_REF_HZ, DPM_UART_BAUD);
    DPM_TRACE(7);
#if defined(DPM_BRINGUP_TRACE) && (DPM_BRINGUP_TRACE == 1)
    {
        /* 阶段 0x5F：程序自己读回 UART 的 BAUDGEN/BAUDDIV，判断写入是否落到硬件 */
        volatile uint32_t *u = (volatile uint32_t *)(uintptr_t)DPM_UART_BASE_SEL;
        *(volatile uint32_t *)(uintptr_t)0x43C00008u = 0x5F000000u
            | ((u[0x34u / 4u] & 0xFFu) << 16) | (u[0x18u / 4u] & 0xFFFFu);
    }
#endif

#if DPM_BOOT_BANNER
    boot_banner();
#endif

#if defined(DPM_USE_IRQ) && (DPM_USE_IRQ == 1)
    DPM_TRACE(10);
    dpm_irq_init();
    dpm_uart_irq_enable(1);
    dpm_irq_global_enable();
#endif

    clk.slow_hz = DPM_SLOW_HZ;
    clk.dsp_hz = DPM_DSP_HZ;
    dpm_cmd_init(&ctx, &dpm_hw_io, 0, &clk);
    ctx.emit = uart_emit;
    ctx.emit_user = 0;
    dpm_rx_init(&rx);

    DPM_TRACE(11);
    for (;;) {
#if defined(DPM_USE_IRQ) && (DPM_USE_IRQ == 1)
        if (dpm_irq_take_meas_flag() != 0) {
            (void)dpm_cmd_fetch(&ctx); /* 结果读走 + 清 IRQ_STATUS 由核心负责 */
        }
#endif
        {
            int b;
            while ((b = next_byte()) >= 0) {
                uint8_t cmd = 0u;
                const uint8_t *payload = 0;
                uint8_t plen = 0u;
                if (dpm_rx_push(&rx, (uint8_t)b, &cmd, &payload, &plen) != 0) {
                    uint32_t n = dpm_cmd_handle(&ctx, cmd, payload, plen, s_tx_frame,
                                                (uint32_t)sizeof(s_tx_frame));
                    if (n != 0u) {
                        dpm_uart_write(s_tx_frame, n);
                    }
#if DPM_BOOT_BANNER
                    banner_stop = 1;
#endif
                }
            }
        }
#if DPM_BOOT_BANNER
        if (banner_stop == 0) {
            uint32_t now = dpm_time_now_ms();
            if (now - banner_ms >= 1000u) {
                banner_ms = now;
                boot_banner();
            }
        }
#endif
        dpm_cmd_tick(&ctx, dpm_time_now_ms());
    }
}
