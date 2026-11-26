#!/usr/bin/env python3
"""检查 DEF 里的 IO pin 是否落在配置的边上。

用法：python3 asic/openroad/check_pins.py build/asic/openroad/01_floorplan.def

期望的边来自环境变量 DPM_IO_SIDES（与 OpenROAD 的 config.tcl 读同一个变量），
默认 "left right"。检查项：每个配置的边都有 pin；未配置的边上没有 pin；没有
pin 落在 die 内部；PINS 声明的数量与实际解析到的一致。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter

PLACED = re.compile(r"\+?\s*PLACED\s+\(\s*(-?[0-9.]+)\s+(-?[0-9.]+)\s*\)")
ALL_SIDES = ("top", "right", "bottom", "left")


def configured_sides() -> list[str]:
    """返回 DPM_IO_SIDES 配置的边；未设置时默认左右两边。"""

    raw = os.environ.get("DPM_IO_SIDES", "left right")
    sides = [token for token in raw.replace(",", " ").split() if token]
    if not sides:
        print("[check_pins] 错误：DPM_IO_SIDES 为空", file=sys.stderr)
        raise SystemExit(2)
    invalid = [side for side in sides if side not in ALL_SIDES]
    if invalid:
        print(
            f"[check_pins] 错误：DPM_IO_SIDES 含非法边 {invalid}，" f"只接受 {'/'.join(ALL_SIDES)}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return sides


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 DEF 里的 IO pin 是否落在配置的边上",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("def_file", help="floorplan 生成的 DEF")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1.0,
        help="判定 pin 位于边界上的容差（微米）",
    )
    args = parser.parse_args()
    sides = configured_sides()

    text = open(args.def_file, encoding="utf-8", errors="replace").read()

    units = re.search(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", text)
    scale = int(units.group(1)) if units else 1000

    die = re.search(
        r"DIEAREA\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)",
        text,
    )
    if not die:
        print(f"[check_pins] 找不到 DIEAREA：{args.def_file}", file=sys.stderr)
        return 1
    lx, ly, ux, uy = (int(value) / scale for value in die.groups())

    pins_block = re.search(r"^PINS\s+(\d+)\s*;(.*?)^END\s+PINS", text, re.S | re.M)
    if not pins_block:
        print(f"[check_pins] 找不到 PINS 段：{args.def_file}", file=sys.stderr)
        return 1
    declared = int(pins_block.group(1))

    coordinates = [(float(match.group(1)) / scale, float(match.group(2)) / scale) for match in PLACED.finditer(pins_block.group(2))]

    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = {"top": [], "right": [], "bottom": [], "left": []}
    for x, y in coordinates:
        if abs(x - lx) <= args.tolerance:
            side = "left"
        elif abs(x - ux) <= args.tolerance:
            side = "right"
        elif abs(y - ly) <= args.tolerance:
            side = "bottom"
        elif abs(y - uy) <= args.tolerance:
            side = "top"
        else:
            side = "interior"
        counts[side] += 1
        if side in examples:
            examples[side].append(f"({x:.2f},{y:.2f})")

    print(f"[check_pins] die {ux - lx:.3f} x {uy - ly:.3f} um，" f"声明的 pin 数 {declared}，实际找到 {len(coordinates)}")
    for side in (*ALL_SIDES, "interior"):
        if counts[side]:
            print(f"  {side:8s}: {counts[side]:3d} 个")

    failed = False
    for side in sides:
        if counts[side] == 0:
            print(f"[check_pins] 失败：{side} 边没有 pin（DPM_IO_SIDES 要求有）", file=sys.stderr)
            failed = True
    for side in ALL_SIDES:
        if side not in sides and counts[side]:
            print(
                f"[check_pins] 失败：{side} 边有 {counts[side]} 个 pin，" f"但 DPM_IO_SIDES 只配置了 {'/'.join(sides)}",
                file=sys.stderr,
            )
            failed = True
    if counts["interior"]:
        print(
            f"[check_pins] 失败：{counts['interior']} 个 pin 不在 die 边界上",
            file=sys.stderr,
        )
        failed = True
    if declared != len(coordinates):
        print(
            f"[check_pins] 失败：PINS 声明 {declared} 个，解析到 {len(coordinates)} 个",
            file=sys.stderr,
        )
        failed = True
    if failed:
        return 1
    print(f"[check_pins] 通过：IO 已分布在 {' / '.join(sides)} 边")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
