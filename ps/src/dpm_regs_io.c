/** @file dpm_regs_io.c
 *  @brief 真实 MMIO 的 `dpm_io_t` 实现 + Cortex-A9 全局定时器时间源。
 *
 * 与宿主机仿真后端（`ps/sim/fpga_model.c`）实现同一个 vtable，因此 `dpm_cmd.c`
 * 在两侧是同一份代码。地址一律绝对地址，写法与 Xilinx 的 `Xil_Out32` 一致。
 *
 * 注意：本文件不配置 PS 时钟与 MIO —— 那属于 FSBL（`ps7_init`）的职责；
 * 通过 JTAG 直接 `dow` 运行时，请先用 FSBL 或 `xsct` 初始化 DDR/MIO/时钟。
 */

#include "dpm_board.h"
#include "dpm_regs.h"

/* ------------------------------------------------------------- MMIO 读写 */

static uint32_t hw_rd32(void *ctx, uint32_t addr)
{
    (void)ctx;
    return *(volatile uint32_t *)(uintptr_t)addr;
}

static void hw_wr32(void *ctx, uint32_t addr, uint32_t val)
{
    (void)ctx;
    *(volatile uint32_t *)(uintptr_t)addr = val;
}

/* ------------------------------------------------------- 全局定时器（时间源） */

/** Cortex-A9 全局定时器（每 CPU 一份，两个 CPU 共享同一计数）。 */
#define DPM_GTIMER_BASE 0xF8F00200u
#define DPM_GTIMER_COUNTER_LOW 0x00u
#define DPM_GTIMER_COUNTER_HIGH 0x04u
#define DPM_GTIMER_CONTROL 0x08u

/** 全局定时器计数频率：默认 = CPU_3x2x = 666MHz/2。与 FSBL 的时钟配置有关，
 *  可用 `-DDPM_GTIMER_HZ=...` 覆盖；数值不准只影响超时判定的松紧，不影响协议。 */
#ifndef DPM_GTIMER_HZ
#define DPM_GTIMER_HZ 333333333u
#endif

static uint64_t gtimer_ticks(void)
{
    uint32_t hi1;
    uint32_t hi2;
    uint32_t lo;

    do {
        hi1 = hw_rd32(0, DPM_GTIMER_BASE + DPM_GTIMER_COUNTER_HIGH);
        lo = hw_rd32(0, DPM_GTIMER_BASE + DPM_GTIMER_COUNTER_LOW);
        hi2 = hw_rd32(0, DPM_GTIMER_BASE + DPM_GTIMER_COUNTER_HIGH);
    } while (hi1 != hi2); /* 读低 32 位期间可能进位 */
    return ((uint64_t)hi2 << 32) | (uint64_t)lo;
}

uint32_t dpm_time_now_ms(void)
{
    /* 计数器没开时读出常量：返回 0（命令核心退化为纯轮询计数） */
    return (uint32_t)((gtimer_ticks() * 1000u) / (uint64_t)DPM_GTIMER_HZ);
}

/** 启动全局定时器（幂等）。FSBL 通常已经开过，这里兜底。 */
static void gtimer_start(void)
{
    uint32_t ctrl = hw_rd32(0, DPM_GTIMER_BASE + DPM_GTIMER_CONTROL);
    if ((ctrl & 1u) == 0u) {
        hw_wr32(0, DPM_GTIMER_BASE + DPM_GTIMER_CONTROL, ctrl | 1u);
    }
}

static void hw_delay_ms(void *ctx, uint32_t ms)
{
    uint64_t t0;
    uint64_t need;

    (void)ctx;
    if (ms == 0u) {
        return;
    }
    t0 = gtimer_ticks();
    need = ((uint64_t)ms * (uint64_t)DPM_GTIMER_HZ) / 1000u;
    while ((gtimer_ticks() - t0) < need) {
        /* 忙等；调用方（命令核心）只在等待测量/DRP 时用很短的时间片 */
    }
}

static uint32_t hw_now_ms(void *ctx)
{
    (void)ctx;
    return dpm_time_now_ms();
}

const dpm_io_t dpm_hw_io = {
    hw_rd32,
    hw_wr32,
    hw_now_ms,
    hw_delay_ms,
};

/** 初始化时间源（`dpm_main.c` 调用）。 */
void dpm_time_init(void)
{
    gtimer_start();
}
