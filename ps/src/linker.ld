/* linker.ld —— DPM PS 侧 freestanding 程序的链接脚本
 *
 * 默认把程序放在 OCM（0x00000000 起，192KB）：
 *   - 复位向量表就在 .vectors（地址 0），与 Cortex-A9 的默认向量基址一致；
 *   - 无需 DDR 初始化即可用 JTAG `dow` + `con` 运行。
 *
 * 要放到 DDR（例如 0x00100000）时，只改 MEMORY 的 ORIGIN/LENGTH；
 * 启动代码里的 VBAR 会跟随链接地址，不需要改 C 代码。
 */

ENTRY(_start)

MEMORY
{
    OCM (rwx) : ORIGIN = 0x00000000, LENGTH = 192K
}

SECTIONS
{
    /* 异常向量表必须在最前（地址 0） */
    .vectors : ALIGN(32)
    {
        KEEP(*(.vectors))
    } > OCM

    .text : ALIGN(4)
    {
        *(.text*)
        *(.gnu.linkonce.t*)
    } > OCM

    . = ALIGN(4);
    _etext = .;                 /* 代码段结束（.rodata 之前） */

    .rodata : ALIGN(4)
    {
        *(.rodata*)
        *(.gnu.linkonce.r*)
    } > OCM

    .data : ALIGN(8)
    {
        _sdata = .;
        *(.data*)
        *(.gnu.linkonce.d*)
        _edata = .;
    } > OCM

    /* .data 在**镜像里**的加载地址（启动代码的拷贝源）。
     * 注意不要用 _etext：它在 .rodata 之前，会把只读数据当初始值拷进 .data。
     * 本工程镜像是整块直接加载到 OCM（JTAG 下载 / FSBL 搬运），因此
     * LOADADDR(.data) == ADDR(.data)，拷贝是等地址的无操作；
     * 将来若改成 .data 的 LMA 放到 Flash（AT> 语法），这里会自动跟着变对。 */
    _sidata = LOADADDR(.data);

    .bss : ALIGN(8)
    {
        _sbss = .;
        *(.bss*)
        *(COMMON)
        _ebss = .;
    } > OCM

    /* 栈：主栈在启动时使用，IRQ 单独一份（避免中断压在被中断的栈上） */
    .stack : ALIGN(8)
    {
        . += 0x2000;            /* 8KB 主栈（SVC） */
        _stack_top = .;
        . += 0x1000;            /* 4KB IRQ 栈 */
        _irq_stack_top = .;
        . += 0x1000;            /* 4KB SVC 栈（启动代码切换用） */
        _svc_stack_top = .;
    } > OCM

    /DISCARD/ :
    {
        *(.comment)
        *(.note*)
        *(.eh_frame*)
    }
}
