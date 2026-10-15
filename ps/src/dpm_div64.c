/** @file dpm_div64.c
 *  @brief freestanding 环境下的 64 位除/取模（不依赖 libgcc / newlib）。
 *
 * 背景（本机实测）：`arm-none-eabi-gcc` 只装了 M-profile 的 multilib，链接
 * newlib/libgcc 会报 `conflicting architecture profiles A/M`，所以 PS 侧程序必须
 * `-nostdlib`。C 代码里的 64 位除法（`dpm_calc.c` / `dpm_sig.c`）会由编译器
 * 转成对运行时例程的调用：
 *   - `__udivdi3` / `__umoddi3`：GCC 通用命名（`-mcpu=cortex-a9` 下不一定使用）；
 *   - `__aeabi_uldivmod`：ARM EABI 命名（AAPCS：r0:r1 = 被除数、r2:r3 = 除数，
 *     返回 r0:r1 = 商、r2:r3 = 余数）。
 * 这里把三者都实现出来，保证 `nm -u` 里没有未定义符号。
 *
 * 算法：移位减法（32 次额外迭代的按位长除法），只用 32 位运算，Cortex-A9 上足够快
 * （每次除法约 64 次循环；本项目的调用频率是"每条命令几次"，不是热路径）。
 */

#include <stdint.h>

/* 对外原型（-Wmissing-prototypes 要求）：PS 侧供 dpm_calc/dpm_sig 的 64 位除法使用，
 * `__udivdi3`/`__umoddi3` 是编译器运行时例程的固定名字。 */
uint64_t dpm_udivmod64(uint64_t num, uint64_t den, uint64_t *rem);
uint64_t dpm_udiv64(uint64_t num, uint64_t den);
uint64_t dpm_umod64(uint64_t num, uint64_t den);
uint64_t __udivdi3(uint64_t a, uint64_t b);
uint64_t __umoddi3(uint64_t a, uint64_t b);

/* ARM EABI 的 32 位除法例程（HARD 除法指令在 -marm 下不一定生成，这里显式提供） */
uint32_t __aeabi_uidiv(uint32_t num, uint32_t den);

uint32_t dpm_udivmod32(uint32_t num, uint32_t den, uint32_t *rem);

/* 下面两个是 RTABI 的 divmod 例程（返回值放在寄存器里，不是 C 结构体返回），
 * 本文件末尾用汇编写；这里只声明"存在"，不参与 C 调用的类型检查。 */
struct dpm_u32div { uint32_t quot; uint32_t rem; };
struct dpm_u32div __aeabi_uidivmod(uint32_t num, uint32_t den);

/** 64 位无符号除法：返回商，`rem` 非空时写入余数；除数为 0 时返回 0（避免死循环）。 */
uint64_t dpm_udivmod64(uint64_t num, uint64_t den, uint64_t *rem)
{
    uint64_t quot = 0u;
    uint64_t r = 0u;
    int i;

    if (den == 0u) {
        if (rem != 0) {
            *rem = 0u;
        }
        return 0u;
    }
    if (num < den) {
        if (rem != 0) {
            *rem = num;
        }
        return 0u;
    }
    for (i = 63; i >= 0; i--) {
        r = (r << 1) | ((num >> (uint32_t)i) & 1u);
        if (r >= den) {
            r -= den;
            quot |= (1ull << (uint32_t)i);
        }
    }
    if (rem != 0) {
        *rem = r;
    }
    return quot;
}

/** 64 位无符号除法（商）。 */
uint64_t dpm_udiv64(uint64_t num, uint64_t den)
{
    return dpm_udivmod64(num, den, 0);
}

/** 64 位无符号取模。 */
uint64_t dpm_umod64(uint64_t num, uint64_t den)
{
    uint64_t r = 0u;
    (void)dpm_udivmod64(num, den, &r);
    return r;
}

/* ------------------------------------------------------- GCC 运行时例程 */

uint64_t __udivdi3(uint64_t a, uint64_t b)
{
    return dpm_udivmod64(a, b, 0);
}

uint64_t __umoddi3(uint64_t a, uint64_t b)
{
    uint64_t r = 0u;
    (void)dpm_udivmod64(a, b, &r);
    return r;
}

/* --------------------------------------------- ARM EABI 的 32 位除法例程 */

/* 32 位无符号移位减法除法（不调用 `/`，避免递归回 `__aeabi_uidiv`）。
 * 非 static：末尾的 RTABI 汇编例程要 `bl` 它，而 static 函数可能被内联掉。 */
uint32_t dpm_udivmod32(uint32_t num, uint32_t den, uint32_t *rem)
{
    uint32_t quot = 0u;
    uint32_t r = 0u;
    int i;

    if (den == 0u) {
        if (rem != 0) {
            *rem = 0u;
        }
        return 0u;
    }
    for (i = 31; i >= 0; i--) {
        r = (r << 1) | ((num >> (uint32_t)i) & 1u);
        if (r >= den) {
            r -= den;
            quot |= (1u << (uint32_t)i);
        }
    }
    if (rem != 0) {
        *rem = r;
    }
    return quot;
}

uint32_t __aeabi_uidiv(uint32_t num, uint32_t den)
{
    return dpm_udivmod32(num, den, 0);
}

/* ------------------------------------------- RTABI 的 divmod 例程（必须用汇编）
 *
 *  **踩过的坑（上板实测）**：`__aeabi_uidivmod` / `__aeabi_uldivmod` 的返回约定
 *  是 **RTABI 特例**——商/余数直接放在寄存器里（32 位：商 r0、余数 r1；
 *  64 位：商 r0:r1、余数 r2:r3）。它**不是** C 的结构体返回：AAPCS32 下大于 4 字节的
 *  结构体返回走"隐藏指针"（被调用方把结果写进调用方给的内存，指针放在 r0），
 *  于是 GCC 生成的除法调用会把"被除数低字"当成那个指针，第一次 `str` 就写到垃圾
 *  地址触发数据访问异常（实测 PC 停在 `str r0, [fp, #12]`，程序卡在 dabort）。
 *  所以这两个例程只能用汇编写，并且自己保证 AAPCS 的 8 字节栈对齐。
 */
#if defined(__arm__)
__asm__(
    ".text\n"
    ".align 2\n"
    ".global __aeabi_uidivmod\n"
    ".type __aeabi_uidivmod, %function\n"
    "__aeabi_uidivmod:\n"            /* r0 = 被除数, r1 = 除数 */
    "    push    {r4, lr}\n"         /* 8 字节：保持 8 字节栈对齐 */
    "    sub     sp, sp, #8\n"
    "    add     r4, sp, #0\n"
    "    str     r4, [sp]\n"         /* 第 3 个参数：&rem（栈传递） */
    "    bl      dpm_udivmod32\n"        /* 商 -> r0，余数写入 [sp] */
    "    ldr     r1, [sp]\n"         /* RTABI：余数在 r1 */
    "    add     sp, sp, #8\n"
    "    pop     {r4, pc}\n"
    ".size __aeabi_uidivmod, .-__aeabi_uidivmod\n"

    ".align 2\n"
    ".global __aeabi_uldivmod\n"
    ".type __aeabi_uldivmod, %function\n"
    "__aeabi_uldivmod:\n"            /* r0:r1 = 被除数, r2:r3 = 除数 */
    "    push    {r4, lr}\n"
    "    sub     sp, sp, #16\n"      /* [sp]=&rem 参数, [sp+8..15]=rem 结果 */
    "    add     r4, sp, #8\n"
    "    str     r4, [sp]\n"
    "    bl      dpm_udivmod64\n"    /* 商 -> r0:r1 */
    "    ldr     r2, [sp, #8]\n"     /* RTABI：余数在 r2:r3 */
    "    ldr     r3, [sp, #12]\n"
    "    add     sp, sp, #16\n"
    "    pop     {r4, pc}\n"
    ".size __aeabi_uldivmod, .-__aeabi_uldivmod\n");
#endif /* __arm__ */
