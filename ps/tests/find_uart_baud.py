#!/usr/bin/env python3
"""找 PS 侧裸机程序的真实波特率（上板首次联调用）。

为什么需要它：PS 程序里的 UART 波特率分频由 **UART 参考时钟**（SLCR `UART_CLK_CTRL`）
决定，而该时钟由 FSBL/`ps7_init` 按 BD 的 PS7 配置给出——工程未绑板时它与 PL 的
FCLK_CLK0 不是同一个时钟，拍脑袋填一个值（例如按 50MHz 算）会让整条串口链路变成乱码。
本工具按候选波特率逐个试探，找出能让协议帧正确解码的那一个，并反推参考时钟。

判据（两条都用，任一命中即算找到）：
  1. 程序上电/重启后会周期性打印 ASCII 横幅 `DPM-PS`（`DPM_BOOT_BANNER=1` 时，
     收到第一条有效帧后停止）；
  2. 发 `PING`/`IDENTIFY` 请求，能收到 CRC 正确、命令码匹配的响应帧。

用法：
    python3 ps/tests/find_uart_baud.py /dev/ttyUSB0
    python3 ps/tests/find_uart_baud.py /dev/ttyUSB0 --assumed-ref-hz 50000000
    python3 ps/tests/find_uart_baud.py /dev/ttyUSB0 --baud 115200   # 只验一个波特率

`--assumed-ref-hz` 填**当前固件里** `DPM_UART_REF_HZ` 的值：找到真实波特率 B 后，
参考时钟 = assumed_ref_hz × B / 目标波特率（固件按该参考算出的分频比不变）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "host"))

from dpm_host.protocol import Cmd, decode_frame, encode_frame  # noqa: E402

try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover
    print("需要 pyserial：pip install pyserial", file=sys.stderr)
    raise SystemExit(2)

#: 默认候选波特率：覆盖常见值 + 常见参考时钟（25/50/54/62.5/83.33/86.4/100MHz）算出的结果。
DEFAULT_BAUDS = [
    9600, 19200, 38400, 57600, 76800, 115200, 128000, 153600, 230400, 460800, 921600,
    # 参考时钟 86.4MHz（AX7010 实测）、分频比 432/500/750/1000/1728 等
    86400, 100000, 115200, 125000, 172800, 200000, 43200, 50000, 1000000, 2000000,
]


def probe(port: str, baud: int, seconds: float) -> tuple[bool, str]:
    """在给定波特率上试探：返回 (是否命中, 说明)。"""
    try:
        ser = serial.Serial(port, baud, timeout=0.1)
    except Exception as exc:  # pragma: no cover - 硬件相关
        return False, f"打不开：{exc}"
    try:
        time.sleep(0.05)
        ser.reset_input_buffer()
        # 先发一个 PING（程序收帧后会停发横幅），再听一段时间
        for _ in range(3):
            ser.write(encode_frame(Cmd.PING))
            time.sleep(0.02)
        deadline = time.time() + seconds
        buf = bytearray()
        while time.time() < deadline:
            buf += ser.read(4096)
        if b"DPM-PS" in buf:
            return True, "命中横幅 DPM-PS"
        # 扫描响应帧
        for i in range(len(buf)):
            if buf[i] != 0xA5:
                continue
            for end in range(i + 4, min(i + 80, len(buf)) + 1):
                try:
                    frame = decode_frame(bytes(buf[i:end]))
                except Exception:
                    continue
                if int(frame.cmd) == int(Cmd.PING.response):
                    return True, "命中 PING 响应帧"
                break
        return False, f"收到 {len(buf)} 字节、无有效帧"
    finally:
        ser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="找 PS 侧程序的真实波特率")
    ap.add_argument("port", help="串口设备，例如 /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=None, help="只测这一个波特率")
    ap.add_argument("--seconds", type=float, default=1.2, help="每个候选波特率听多久（默认 1.2s）")
    ap.add_argument("--assumed-ref-hz", type=int, default=50000000,
                    help="固件里 DPM_UART_REF_HZ 的当前值（默认 50000000），用于反推真实参考时钟")
    ap.add_argument("--target-baud", type=int, default=115200, help="固件期望的波特率（默认 115200）")
    args = ap.parse_args()

    cands = [args.baud] if args.baud else sorted(set(DEFAULT_BAUDS))
    hits: list[int] = []
    for b in cands:
        ok, why = probe(args.port, b, args.seconds)
        print(f"{b:>8}: {'HIT ' if ok else '    '}{why}", flush=True)
        if ok:
            hits.append(b)
    if not hits:
        print("未找到可用波特率：确认程序在跑、MIO 引脚与串口接线正确", file=sys.stderr)
        return 1

    b = hits[len(hits) // 2]
    ref = args.assumed_ref_hz * b / args.target_baud
    print(f"\n可用波特率：{hits}")
    print(f"建议：DPM_UART_REF_HZ ≈ {int(round(ref))}（= {args.assumed_ref_hz} x {b} / {args.target_baud}）")
    print("填回 ps/src/dpm_main.c 后重新编译、重新下载；再用 --baud 115200 复验。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
