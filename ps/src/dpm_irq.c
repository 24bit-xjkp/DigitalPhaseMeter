/** @file dpm_irq.c
 *  @brief ARMv7-A 异常向量、GIC 初始化与中断服务（测量完成 + UART）。
 *
 * 结构：
 *   - `vectors.S` 部分（本文件内的 `__asm__` 全局汇编）提供 8 个异常入口；
 *   - `dpm_irq_init()`：设置 VBAR、使能 GIC 分发器/CPU 接口、注册 PL 测量中断；
 *   - `dpm_irq_dispatch()`：读 GICC_IAR → 分发到 UART ISR 或测量 ISR → 写 GICC_EOIR。
 *
 * **上板状态（AX7010，实测）**：中断路径原先有两处硬伤，已修：
 *   1. IRQ 入口用 `srsdb sp!, #0x12` 把现场存进 IRQ 栈，末尾却用 `rfeia sp!`
 *      从 SVC 栈取 PC/CPSR —— 保存与恢复不是同一处，第一次中断就跳到垃圾地址
 *      （现象：程序不响应、PC 落在中止处理程序里）。改为 `#0x13` 后配对。
 *   2. GIC 只 enable 不设 `ITARGETSR`：SPI 复位后目标 CPU 为 0，中断根本不会送达；
 *      而且原先 enable 的是 UART0 的 ID（本工程用的是 UART1）。
 * 默认编译开关 `DPM_USE_IRQ=1` 走中断路径，`-DDPM_USE_IRQ=0` 退回纯轮询；
 * 主循环始终轮询 STATUS，因此中断只是降低延迟。
 */

#include <stdint.h>

#include "dpm_board.h"
#include "dpm_cmd.h"
#include "dpm_regs.h"

/* --------------------------------------------------------------- GIC 寄存器 */

#define DPM_GIC_DIST_BASE 0xF8F01000u /**< 分发器 */
#define DPM_GIC_CPU_BASE 0xF8F00100u  /**< CPU 接口 */

#define GICD_CTLR 0x000u
#define GICD_ISENABLER 0x100u
#define GICD_ICENABLER 0x180u
#define GICD_IPRIORITYR 0x400u
#define GICD_ITARGETSR 0x800u
#define GICD_ICFGR 0xC00u

#define GICC_CTLR 0x0000u
#define GICC_PMR 0x0004u
#define GICC_IAR 0x000Cu
#define GICC_EOIR 0x0010u

static void gicd_wr(uint32_t off, uint32_t val)
{
    *(volatile uint32_t *)(uintptr_t)(DPM_GIC_DIST_BASE + off) = val;
}

static uint32_t gicc_rd(uint32_t off)
{
    return *(volatile uint32_t *)(uintptr_t)(DPM_GIC_CPU_BASE + off);
}

static void gicc_wr(uint32_t off, uint32_t val)
{
    *(volatile uint32_t *)(uintptr_t)(DPM_GIC_CPU_BASE + off) = val;
}

/** 按字节访问的 GIC 寄存器（优先级/目标 CPU 是 8 位粒度）。 */
static void gicd_wr_byte(uint32_t off, uint32_t id, uint8_t val)
{
    volatile uint8_t *p = (volatile uint8_t *)(uintptr_t)(DPM_GIC_DIST_BASE + off + id);
    *p = val;
}

/** 设置某个中断的优先级（8 位粒度寄存器）。 */
static void dpm_gic_set_priority(uint32_t id, uint8_t prio)
{
    gicd_wr_byte(GICD_IPRIORITYR, id, prio);
}

/** 设置某个 SPI 的目标 CPU（bit0 对应 CPU0）。SPI 的 id ≥ 32 才有该寄存器。 */
static void dpm_gic_set_target(uint32_t id, uint8_t targets)
{
    if (id >= 32u) {
        gicd_wr_byte(GICD_ITARGETSR, id, targets);
    }
}

/* --------------------------------------------------------------- CPU 接口 */

void dpm_irq_global_enable(void)
{
    __asm__ volatile("cpsie i" ::: "memory");
}

void dpm_irq_global_disable(void)
{
    __asm__ volatile("cpsid i" ::: "memory");
}

/* ------------------------------------------------------------- 中断使能 */

void dpm_gic_enable_irq(uint32_t id)
{
    if (id < 32u) {
        return; /* SGI/PPI 不在这里用 */
    }
    gicd_wr_byte(GICD_IPRIORITYR, id, 0xA0u); /* 优先级 */
    gicd_wr_byte(GICD_ITARGETSR, id, 0x01u);  /* 目标 CPU0 */
    /* SPI 默认电平触发、高有效；PL 的 IRQ_F2P 是高电平 */
    gicd_wr(GICD_ISENABLER + 4u * (id / 32u), 1u << (id % 32u));
}

void dpm_gic_disable_irq(uint32_t id)
{
    if (id < 32u) {
        return;
    }
    gicd_wr(GICD_ICENABLER + 4u * (id / 32u), 1u << (id % 32u));
}

/* --------------------------------------------------------------- 中断服务 */

static volatile uint32_t s_meas_flag;
static volatile uint32_t s_irq_count;

int dpm_irq_take_meas_flag(void)
{
    int v = 0;
    dpm_irq_global_disable();
    if (s_meas_flag != 0u) {
        s_meas_flag = 0u;
        v = 1;
    }
    dpm_irq_global_enable();
    return v;
}

uint32_t dpm_irq_count(void)
{
    return s_irq_count;
}

/** PL 测量完成中断：只置标志 + 清 PL 的挂起位，实际读取留给主循环。 */
static void dpm_meas_isr(void)
{
    s_meas_flag = 1u;
    /* 清 PL 的 IRQ 挂起位；resultValid 由主循环读走结果后再清（写 b1）。 */
    *(volatile uint32_t *)(uintptr_t)(DPM_BASE + DPM_REG_IRQ_STATUS) = DPM_IRQ_PENDING;
}

void dpm_irq_dispatch(void)
{
    uint32_t iar = gicc_rd(GICC_IAR);
    uint32_t id = iar & 0x3FFu;

    s_irq_count++;
    if (id == DPM_IRQ_ID) {
        dpm_meas_isr();
    } else if (id == DPM_IRQ_ID_UART0 || id == DPM_IRQ_ID_UART1) {
        dpm_uart_isr();
    } else if (id >= 1020u) {
        return; /* spurious：不需要 EOI */
    }
    gicc_wr(GICC_EOIR, iar);
}

void dpm_irq_init(void)
{
    /* 先把所有 SPI 关掉再逐个开，避免 PL 未就绪时反复进中断 */
    gicd_wr(GICD_ICENABLER + 0u, 0xFFFFFFFFu);
    gicd_wr(GICD_ICENABLER + 4u, 0xFFFFFFFFu);
    gicd_wr(GICD_ICENABLER + 8u, 0xFFFFFFFFu);

    gicc_wr(GICC_PMR, 0xF0u); /* 允许所有优先级 */
    gicc_wr(GICC_CTLR, 0x01u);/* 使能 CPU 接口 */
    gicd_wr(GICD_CTLR, 0x01u);/* 使能分发器 */

#if defined(DPM_USE_IRQ) && (DPM_USE_IRQ == 1)
    /* 分发器复位后 SPI 的 IPRIORITYR/ITARGETSR 全 0：不设 ITARGETSR 就没有目标 CPU，
     * 中断永远不会送达（上板实测：只 enable 不设目标 → 中断路径完全不工作）。 */
    dpm_gic_set_priority(DPM_IRQ_ID, 0xA0u);
    dpm_gic_set_target(DPM_IRQ_ID, 0x01u);
    dpm_gic_enable_irq(DPM_IRQ_ID);      /* PL 测量完成 */

    dpm_gic_set_priority(DPM_IRQ_ID_UART1, 0xA0u);
    dpm_gic_set_target(DPM_IRQ_ID_UART1, 0x01u);
    dpm_gic_enable_irq(DPM_IRQ_ID_UART1);/* UART1（本工程用的串口） */
#endif
}

/* --------------------------------------------------------- 异常入口与诊断 */

void dpm_abort_trap(void);

/** 未定义指令 / 预取中止 / 数据中止的统一入口：记录诊断值后停住。
 *
 *  `DPM_BRINGUP_TRACE=1` 时把「出错返回地址低 16 位 + DFAR 低 16 位」写进 PL 的
 *  GATE_TARGET 寄存器（0x43C0_0008），串口挂了也能用 JTAG（APU 目标物理通路）读出，
 *  据此用 addr2line 定位到具体指令。数据中止时 lr_abt = 出错指令 + 8。 */
void dpm_abort_trap(void)
{
#if defined(DPM_BRINGUP_TRACE) && (DPM_BRINGUP_TRACE == 1)
    uint32_t dfar = 0u;
    uint32_t lr = 0u;

    __asm__ volatile("mrc p15, 0, %0, c6, c0, 0" : "=r"(dfar));
    __asm__ volatile("mov %0, lr" : "=r"(lr));
    (void)dfar;
    *(volatile uint32_t *)(uintptr_t)0x43C00008u = 0xAB000000u | (lr & 0x00FFFFFFu);
#endif
    for (;;) {
        /* 停住：等待调试器读取上面的诊断值 */
    }
}

/* --------------------------------------------------------- 异常向量与入口 */

#if defined(__arm__)
/** 8 个异常入口 + IRQ 现场保护。
 *
 * 每个入口 4 字节；`_start`（定义在 `startup.c`）负责设置 VBAR 指向本表并建立栈。
 * IRQ 入口保存被中断的现场（r0-r3、r12、lr），调用 C 分发函数，再 `subs pc, lr, #4`
 * 返回（ARM 的 IRQ 返回地址是 lr - 4）。
 */
__asm__(".section .vectors, \"ax\"\n"
        ".align 5\n"
        ".global dpm_vectors\n"
        "dpm_vectors:\n"
        "    b   _start\n"              /* 0x00 复位 */
        "    b   dpm_undef_handler\n"   /* 0x04 未定义指令 */
        "    b   dpm_svc_handler\n"     /* 0x08 管理调用 */
        "    b   dpm_pabort_handler\n"  /* 0x0C 预取中止 */
        "    b   dpm_dabort_handler\n"  /* 0x10 数据中止 */
        "    b   dpm_reserved_handler\n"/* 0x14 保留 */
        "    b   dpm_irq_handler\n"     /* 0x18 IRQ */
        "    b   dpm_fiq_handler\n"     /* 0x1C FIQ */
        ".size dpm_vectors, .-dpm_vectors\n"
        ".text\n"
        ".align 2\n"
        ".global dpm_irq_handler\n"
        ".type dpm_irq_handler, %function\n"
        "dpm_irq_handler:\n"
        "    sub     lr, lr, #4\n"
        "    srsdb   sp!, #0x13\n"       /* 存到 SVC 栈：与末尾的 rfeia sp! 配对 */
        "    cpsid   i, #0x13\n"         /* 切到 SVC 模式并关中断（用 SVC 栈） */
        "    push    {r0-r3, r12, lr}\n"
        "    and     r1, sp, #4\n"       /* 8 字节对齐修正 */
        "    sub     sp, sp, r1\n"
        "    push    {r1, lr}\n"
        "    bl      dpm_irq_dispatch\n"
        "    pop     {r1, lr}\n"
        "    add     sp, sp, r1\n"
        "    pop     {r0-r3, r12, lr}\n"
        "    rfeia   sp!\n"              /* 返回被中断的上下文 */
        ".size dpm_irq_handler, .-dpm_irq_handler\n"
        ".global dpm_undef_handler\n"
        "dpm_undef_handler:\n    b   dpm_abort_trap\n"
        ".global dpm_svc_handler\n"
        "dpm_svc_handler:\n      b .\n"
        ".global dpm_pabort_handler\n"
        "dpm_pabort_handler:\n   b   dpm_abort_trap\n"
        ".global dpm_dabort_handler\n"
        "dpm_dabort_handler:\n   b   dpm_abort_trap\n"
        ".global dpm_reserved_handler\n"
        "dpm_reserved_handler:\n b .\n"
        ".global dpm_fiq_handler\n"
        "dpm_fiq_handler:\n      b .\n");
#endif /* __arm__ */
