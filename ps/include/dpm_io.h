/** @file dpm_io.h
 *  @brief 寄存器 IO 抽象（vtable）：真实 MMIO 与宿主机仿真后端共用同一接口。
 *
 * 命令处理核心 `dpm_cmd.c` 只通过本接口访问 PL，因此同一份代码可以：
 *   - 在 PS 上跑真实硬件（`dpm_regs_io.c` 用 volatile 指针实现，地址空间一致）；
 *   - 在 x86-64 上跑寄存器级仿真后端（`sim/fpga_model.c`，按地址译码）。
 *
 * 地址一律使用**绝对地址**（`DPM_BASE + offset` 或 `DPM_DRP_BASE + offset`），
 * 与真实 MMIO 的看门狗/译码行为保持一致：写错地址就是写到别的从口。
 */

#ifndef DPM_IO_H
#define DPM_IO_H

#include <stdint.h>

#include "dpm_regs.h"

/** PL 寄存器与 MMCM DRP 口的读写接口。
 *
 * `now_ms` / `delay_ms` 允许为 0（没有时间源的场合由调用方退化为轮询计数）。
 */
typedef struct dpm_io {
    /** 读 32 位；地址不响应时返回实现自定义值（真实硬件由总线返回 0）。 */
    uint32_t (*rd32)(void *ctx, uint32_t addr);
    /** 写 32 位整字（strb = 0xF）。 */
    void (*wr32)(void *ctx, uint32_t addr, uint32_t val);
    /** 单调递增的毫秒计数；为 0 表示无时间源。 */
    uint32_t (*now_ms)(void *ctx);
    /** 忙等若干毫秒；为 0 表示不可用（调用方退化为轮询）。 */
    void (*delay_ms)(void *ctx, uint32_t ms);
} dpm_io_t;

/** 读一个 PL 寄存器（`off` 取 `DPM_REG_*`）。 */
static inline uint32_t dpm_reg_rd(const dpm_io_t *io, void *ctx, uint32_t off)
{
    return io->rd32(ctx, (uint32_t)DPM_BASE + off);
}

/** 写一个 PL 寄存器（`off` 取 `DPM_REG_*`）。 */
static inline void dpm_reg_wr(const dpm_io_t *io, void *ctx, uint32_t off, uint32_t val)
{
    io->wr32(ctx, (uint32_t)DPM_BASE + off, val);
}

/** 读 MMCM DRP 寄存器镜像（`off` 取 `DPM_DRP_OFF_*`）。 */
static inline uint32_t dpm_drp_rd(const dpm_io_t *io, void *ctx, uint32_t off)
{
    return io->rd32(ctx, (uint32_t)DPM_DRP_BASE + off);
}

/** 写 MMCM DRP 配置字。 */
static inline void dpm_drp_wr(const dpm_io_t *io, void *ctx, uint32_t off, uint32_t val)
{
    io->wr32(ctx, (uint32_t)DPM_DRP_BASE + off, val);
}

#endif /* DPM_IO_H */
