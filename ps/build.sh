#!/usr/bin/env bash
# ps/build.sh —— PS 侧 freestanding 裸机程序的交叉编译 + 宿主机验证台的构建
#
#   用法：
#     bash ps/build.sh              # 交叉编译 ps/build/dpm_ps.elf + 宿主机 host_main
#     bash ps/build.sh --ps-only    # 只做交叉编译
#     bash ps/build.sh --host-only  # 只做宿主机构建
#
# 约束（实测）：本机 arm-none-eabi-gcc 只有 M-profile 的 multilib，链接 newlib/libgcc
# 会报 `conflicting architecture profiles A/M`，因此：
#   - 必须 -ffreestanding -nostdlib -nostartfiles，自带 startup.c 与 dpm_div64.c；
#   - 不 include 任何 libc 头（只用 <stdint.h>/<stddef.h>）；
#   - 期望零警告：-Wall -Wextra -Werror。
# 宿主机侧（x86-64）用 clang，允许 libc；同一份 dpm_proto.c / dpm_calc.c / dpm_sig.c /
# dpm_cmd.c 两边都编译。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PS="$ROOT/ps"
BUILD="$PS/build"
CROSS="${DPM_CROSS_PREFIX:-/home/luo/Toolchains/x86_64-pc-linux-gnu-host-arm-none-eabi-target-gcc17/bin/arm-none-eabi-}"
CC_ARM="${CROSS}gcc"
SIZE_ARM="${CROSS}size"
OBJCOPY_ARM="${CROSS}objcopy"
CLANG="${DPM_HOST_CC:-clang}"

CPU_FLAGS=(-mcpu=cortex-a9 -marm -mfloat-abi=soft -ffreestanding -nostdlib -nostartfiles
           -ffunction-sections -fdata-sections -fno-builtin -fno-common -fno-strict-aliasing)
WARN_FLAGS=(-Wall -Wextra -Werror -Wshadow -Wundef -Wpointer-arith -Wcast-align
            -Wstrict-prototypes -Wmissing-prototypes)
DEFS_ARM=(-DDPM_USE_IRQ=1)

# 编译期注入 git 版本（32 位短哈希；没有 git 或不在仓库里时用 0）
GIT_REV=0
if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --short=8 HEAD >/dev/null 2>&1; then
    GIT_REV=$((16#$(git -C "$ROOT" rev-parse --short=8 HEAD)))
fi
DEFS_ARM+=("-DDPM_GIT_REV=${GIT_REV}u")

PS_SRCS=(dpm_main.c dpm_proto.c dpm_calc.c dpm_sig.c dpm_cmd.c dpm_div64.c
         dpm_regs_io.c dpm_uart.c dpm_irq.c startup.c)
HOST_SHARED=(dpm_proto.c dpm_calc.c dpm_sig.c dpm_cmd.c)

MODE="${1:-all}"
mkdir -p "$BUILD"

build_ps() {
    echo "== 交叉编译（arm-none-eabi-gcc, Cortex-A9, freestanding）"
    local objs=()
    for src in "${PS_SRCS[@]}"; do
        local obj="$BUILD/${src%.c}.o"
        echo "   CC  $src"
        "$CC_ARM" "${CPU_FLAGS[@]}" "${WARN_FLAGS[@]}" "${DEFS_ARM[@]}" \
            -I"$PS/include" -O2 -g -std=c99 -c "$PS/src/$src" -o "$obj"
        objs+=("$obj")
    done
    echo "   LD  dpm_ps.elf"
    "$CC_ARM" "${CPU_FLAGS[@]}" -T "$PS/src/linker.ld" -Wl,--gc-sections \
        -Wl,-Map="$BUILD/dpm_ps.map" -Wl,--build-id=none \
        "${objs[@]}" -o "$BUILD/dpm_ps.elf"
    "$OBJCOPY_ARM" -O binary "$BUILD/dpm_ps.elf" "$BUILD/dpm_ps.bin"

    echo "== file 校验"
    file "$BUILD/dpm_ps.elf"
    echo "== arm-none-eabi-size"
    "$SIZE_ARM" "$BUILD/dpm_ps.elf"
    echo "== 未定义符号检查（必须为空，否则说明还依赖 libgcc/newlib）"
    local undef
    undef="$("$CROSS"nm -u "$BUILD/dpm_ps.elf" || true)"
    if [ -n "$undef" ]; then
        echo "$undef"
        echo "!! 存在未定义符号" >&2
        exit 1
    fi
    echo "   （无未定义符号）"
}

build_host() {
    echo "== 宿主机构建（clang, x86-64）"
    local incs=(-I"$PS/include" -I"$PS/sim")
    local srcs=()
    for src in "${HOST_SHARED[@]}"; do srcs+=("$PS/src/$src"); done
    srcs+=("$PS/sim/fpga_model.c" "$PS/sim/host_main.c")
    echo "   CC  host_main"
    "$CLANG" -std=c99 -Wall -Wextra -Werror -O1 -g -DDPM_HOST_SELFTEST=1 \
        -DDPM_GIT_REV="$GIT_REV" "${incs[@]}" "${srcs[@]}" -o "$BUILD/host_main"
    echo "   构建完成：$BUILD/host_main"
}

case "$MODE" in
    --ps-only) build_ps ;;
    --host-only) build_host ;;
    all) build_ps; build_host ;;
    *) echo "未知参数：$MODE（可用 --ps-only / --host-only）" >&2; exit 2 ;;
esac

echo "== 完成"
