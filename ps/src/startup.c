/** @file startup.c
 *  @brief freestanding 启动代码：栈、BSS 清零、异常向量，然后进 `main`。
 *
 * 不依赖 newlib/crt0；栈放在 `.stack` 段（linker.ld），IRQ/SVC 各自一份，
 * 避免中断切入时压在被中断的栈上。
 */

#include <stdint.h>

#include "dpm_board.h"

extern int main(void);

/** 启动入口（链接脚本的 ENTRY，也是向量表 0x00 的目标）。 */
void _start(void);

/* 链接脚本提供的段边界 */
extern uint32_t _sdata[];
extern uint32_t _edata[];
extern uint32_t _sidata[];
extern uint32_t _sbss[];
extern uint32_t _ebss[];
extern uint32_t _stack_top[];
extern uint32_t _irq_stack_top[];
extern uint32_t _svc_stack_top[];

/** 异常向量基址（`dpm_irq.c` 里的 `dpm_vectors`，位于 `.vectors` 段 = 地址 0）。 */
extern void dpm_vectors(void);

/** CPU 变体号（MIDR[23:20]：r0p0=0、r1p0=1、r2p0=2…），启动时读一次供打印。 */
uint32_t dpm_cpu_variant = 0xFFFFFFFFu;

/** 读 MIDR（CP15 c0, c0, 0）。 */
static uint32_t read_midr(void)
{
#if defined(__arm__)
    uint32_t v;
    __asm__ volatile("mrc p15, 0, %0, c0, c0, 0" : "=r"(v));
    return v;
#else
    return 0u;
#endif
}

/** 设置异常向量表位置（低地址 0x00000000）。
 *
 *  上板实测（AX7010 / xc7z010）：**不要写 VBAR**——执行
 *  `mcr p15, 0, r0, c12, c0, 0` 会触发未定义指令异常，程序停在
 *  `dpm_undef_handler`，现象是"串口一个字节都没有、CPU 其实在跑"。
 *  原因：Zynq-7000 的 A9 没有 Security Extensions（无 TrustZone），
 *  CP15 c12,c0,0 未实现，与 ARM 的"r2p0 起有 VBAR"无关；
 *  Xilinx 的 Zynq-7000 standalone BSP 同样从不使用 VBAR。
 *
 *  正确做法是把向量表放在 0x00000000（链接脚本已把 `.vectors` 放在 0），
 *  并清 SCTLR.V 保证基址就是 0（复位后 V 本来也是 0）。 */
static void setup_vectors(void)
{
#if defined(__arm__)
    uint32_t sctlr;

    __asm__ volatile("mrc p15, 0, %0, c1, c0, 0" : "=r"(sctlr));
    sctlr &= ~(1u << 13); /* V = 0：低地址向量表 */
    __asm__ volatile("mcr p15, 0, %0, c1, c0, 0" ::"r"(sctlr) : "memory");
#endif
}

/** 切换栈指针到指定地址（用于 IRQ/SVC 模式）。 */
static void set_sp(uint32_t addr, uint32_t mode_bits)
{
#if defined(__arm__)
    __asm__ volatile("msr cpsr_c, %0\n\t"
                     "mov sp, %1\n\t"
                     "msr cpsr_c, %2" ::"r"(mode_bits),
                     "r"(addr),
                     "r"(0x13u) /* 回到 SVC 模式 */
                     : "memory");
#else
    (void)addr;
    (void)mode_bits;
#endif
}

/** 启动入口（向量表 0x00 跳到这里；JTAG 下载后从 `_start` 开始执行）。 */
void _start(void)
{
    uint32_t *p;

    DPM_TRACE(1);
    dpm_cpu_variant = (read_midr() >> 20) & 0xFu;
    DPM_TRACE(2);
    setup_vectors();

    /* 为 IRQ/ABT/UND 模式准备栈；SVC 用主栈 */
#if defined(__arm__)
    set_sp((uint32_t)_irq_stack_top, 0x12u); /* IRQ 模式 */
    set_sp((uint32_t)_svc_stack_top, 0x13u); /* SVC 模式（回到这里继续） */
#endif
    (void)_stack_top;

    /* .data 从镜像的加载地址搬到运行地址（整块加载时两者相同，等价于无操作） */
    {
        const uint32_t *src = _sidata;
        for (p = _sdata; p < _edata; p++) {
            *p = *src++;
        }
    }
    /* .bss 清零 */
    for (p = _sbss; p < _ebss; p++) {
        *p = 0u;
    }

    DPM_TRACE(3);
    (void)main();

    for (;;) {
        /* main 不应返回；返回则停在这里便于调试 */
    }
}
