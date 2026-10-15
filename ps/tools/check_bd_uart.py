#!/usr/bin/env python3
"""检查 BD 里 PS7 的 UART 配置：当前 bitstream 是否真的能用串口。

用法：
    python3 ps/tools/check_bd_uart.py <dpm_system_processing_system7_0_0.xci>
    # 或指向 XSA（会只读取其中的 .hwh，不写盘）：
    python3 ps/tools/check_bd_uart.py build/vivado/dpm_system.xsa

输出（人类 + 机器可读尾行 `PS_UART: ...`）：
    - UART0/UART1 是否使能、外设号、波特率
    - MIO 分配情况（哪个 MIO 给了 UART，tx/rx 方向）
    - 对应到 PS 侧软件里应该用的 UART 基地址（UART0 = 0xE0001000，UART1 = 0xE0000000）
退出码：0 = 至少一个 UART 可用；1 = 没有可用的 PS 串口（用 `vivado/create_bd.tcl -uart uart1`
重建 BD，见 docs/bringup.md 第 0 节）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import zipfile

UART_BASE = {"UART0": 0xE0001000, "UART1": 0xE0000000}
MIO_COUNT = 54


def read_text(path: str) -> str:
    if path.endswith(".xsa") or path.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith(".hwh")]
            if not names:
                raise SystemExit(f"{path} 里没有 .hwh")
            return zf.read(names[0]).decode("utf-8", errors="replace")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def find_props(text: str) -> dict:
    """从 XCI（JSON）或 HWH（XML）里抽出感兴趣的属性。"""
    props: dict[str, str] = {}
    for key in ("PCW_EN_UART0", "PCW_EN_UART1", "PCW_UART0_PERIPHERAL_ENABLE",
                "PCW_UART1_PERIPHERAL_ENABLE", "PCW_UART0_UART0_IO", "PCW_UART1_UART1_IO",
                "PCW_UART0_BAUD_RATE", "PCW_UART1_BAUD_RATE",
                "PCW_MIO_TREE_PERIPHERALS", "PCW_MIO_TREE_SIGNALS"):
        # JSON: "KEY": [ { "value": "...", ... } ]
        m = re.search(rf'"{key}"\s*:\s*\[\s*\{{\s*"value"\s*:\s*"([^"]*)"', text)
        if m:
            props[key] = m.group(1)
            continue
        # XML/HWH: <PARAMETER NAME="KEY" VALUE="..."/>
        m = re.search(rf'NAME="{key}"\s+VALUE="([^"]*)"', text)
        if m:
            props[key] = m.group(1)
    return props


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    if not os.path.exists(path):
        raise SystemExit(f"文件不存在：{path}")

    props = find_props(read_text(path))
    if not props:
        raise SystemExit(f"{path} 里没有找到 PS7 配置属性（是不是读错了文件？）")

    print(f"来源：{path}")
    enabled = []
    for uart in ("UART0", "UART1"):
        en = props.get(f"PCW_EN_{uart}", "?")
        periph = props.get(f"PCW_{uart}_PERIPHERAL_ENABLE", "?")
        io = props.get(f"PCW_{uart}_{uart}_IO", "?")
        baud = props.get(f"PCW_{uart}_BAUD_RATE", "?")
        on = en == "1" or periph == "1"
        print(f"  {uart}: EN={en} PERIPHERAL_ENABLE={periph} IO={io} BAUD={baud}"
              f" → {'使能' if on else '未使能'}")
        if on:
            enabled.append((uart, io, baud))

    mio_periph = (props.get("PCW_MIO_TREE_PERIPHERALS") or "").split("#")
    mio_sig = (props.get("PCW_MIO_TREE_SIGNALS") or "").split("#")
    if len(mio_periph) == MIO_COUNT:
        used = [(i, mio_periph[i], mio_sig[i] if i < len(mio_sig) else "?")
                for i in range(MIO_COUNT) if mio_periph[i] != "unassigned"]
        if used:
            print(f"  MIO 已分配 {len(used)} 个：")
            for i, per, sig in used:
                print(f"    MIO{i:<3} {per:<12} {sig}")
        else:
            print("  MIO：全部 unassigned（PS 侧没有任何引脚可用）")
    else:
        print(f"  MIO：树长度 {len(mio_periph)} != {MIO_COUNT}，无法解析")

    if not enabled:
        print("PS_UART: none（BD 里没配串口：用 vivado/create_bd.tcl -uart uart1 重建，"
              "见 docs/bringup.md 第 0 节）")
        return 1
    for uart, io, baud in enabled:
        if baud not in ("115200", "?"):
            print(f"  注意：{uart} 波特率 {baud} != 115200，PS 侧与上位机要同步改")
        print(f"PS_UART: {uart} base=0x{UART_BASE[uart]:08X} io={io} baud={baud}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
