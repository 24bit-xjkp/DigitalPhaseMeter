#!/usr/bin/env python3
"""打印一次完整的 pty 会话记录（命令帧 hex + 解析后的字段），用于交付证据。

流程与 `docs/ps-protocol.md` 的推荐用法一致：
    PING → IDENTIFY → GET_STATUS → SET_SIG → SET_GATE → START → GET_MEAS/GET_RESULT
    → 错误路径（非法参数）→ SET_MODE(1) 连续 + STREAM → CLEAR → RESET_COUNTERS

用法：
    python3 ps/tests/e2e_session_record.py [--host-main ps/build/host_main]
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_server_selftest import (  # noqa: E402
    CMD_CLEAR,
    CMD_GET_MEAS,
    CMD_GET_RESULT,
    CMD_GET_SIG,
    CMD_GET_STATUS,
    CMD_IDENTIFY,
    CMD_PING,
    CMD_RESET_COUNTERS,
    CMD_SET_GATE,
    CMD_SET_MODE,
    CMD_SET_SIG,
    CMD_SET_SOURCE,
    CMD_START,
    CMD_STREAM,
    ERR_PARAM,
    NTF_EVENT,
    NTF_STREAM_DATA,
    REPO,
    Server,
    build_frame,
    rd_u16,
    rd_u32,
    u16,
    u32,
)

NAME = {
    CMD_PING: "PING",
    CMD_IDENTIFY: "IDENTIFY",
    CMD_GET_STATUS: "GET_STATUS",
    CMD_START: "START",
    CMD_GET_RESULT: "GET_RESULT",
    CMD_GET_MEAS: "GET_MEAS",
    CMD_SET_GATE: "SET_GATE",
    CMD_SET_MODE: "SET_MODE",
    CMD_SET_SOURCE: "SET_SOURCE",
    CMD_SET_SIG: "SET_SIG",
    CMD_GET_SIG: "GET_SIG",
    CMD_CLEAR: "CLEAR",
    CMD_STREAM: "STREAM",
    CMD_RESET_COUNTERS: "RESET_COUNTERS",
}


def show(sv: Server, cmd: int, payload: bytes, note: str = "") -> tuple[int, bytes]:
    req = build_frame(cmd, payload)
    f = sv.request(cmd, payload)
    code = f[0] if f else -1
    pl = f[1] if f else b""
    kind = NAME.get(cmd, f"0x{cmd:02X}") if code == (cmd | 0x80) else ("ERR_PARAM" if code == ERR_PARAM else f"0x{code:02X}")
    print(f"TX {req.hex(' ')}   ({NAME.get(cmd, hex(cmd))})")
    print(f"RX {code:02X} {pl.hex(' ')}   → {kind}" + (f"  {note}" if note else ""))
    return code, pl


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host-main", default=os.path.join(REPO, "ps", "build", "host_main"))
    ap.add_argument("--golden", default=os.path.join(REPO, "spec", "golden", "vectors.json"))
    args = ap.parse_args()

    with open(args.golden, encoding="utf-8") as fh:
        golden = json.load(fh)
    row = next(v for v in golden["vectors"] if v["freq_hz"] == 25_000_000 and v["phase_requested_mdeg"] == 90_000)

    sv = Server(args.host_main)
    print("== pty 会话记录（host_main --device /dev/pts/N）==")
    print(f"-- 服务端: {args.host_main}；金标准: {args.golden}\n")

    def show_fields(cmd: int, payload: bytes, fmt) -> bytes:
        """发命令、打印帧，再按需打印解析后的字段（避免 f-string 提前求值）。"""
        code, pl = show(sv, cmd, payload)
        if code == (cmd | 0x80) and fmt is not None:
            print("      " + fmt(pl))
        return pl

    show_fields(CMD_PING, b"", lambda pl: "空响应")
    show_fields(
        CMD_IDENTIFY,
        b"",
        lambda pl: (
            f"ver=0x{rd_u32(pl, 0):08X} gitRev=0x{rd_u32(pl, 4):08X} caps=0x{rd_u32(pl, 8):08X} "
            f"versionReg=0x{rd_u32(pl, 12):08X} capsReg=0x{rd_u32(pl, 16):08X} "
            f"dsp={rd_u32(pl, 20)} slow={rd_u32(pl, 24)} flags=0x{rd_u16(pl, 28):04X}"
        ),
    )
    show_fields(
        CMD_GET_STATUS,
        b"",
        lambda pl: (
            f"status=0x{rd_u32(pl, 0):08X} sigStatus=0x{rd_u32(pl, 4):08X} "
            f"gateTarget={rd_u32(pl, 8)} lostCount={rd_u32(pl, 12)} seq={rd_u32(pl, 16)}"
        ),
    )
    show_fields(
        CMD_SET_SIG,
        u32(25_000_000) + u32(90_000),
        lambda pl: (
            f"freq={rd_u32(pl, 0)} phase={rd_u32(pl, 4)} vco={rd_u32(pl, 8)} div={rd_u16(pl, 12)} "
            f"phaseField={pl[14]}（MMCM DELAY_TIME = 相位码 >> 3）"
        ),
    )
    show_fields(
        CMD_GET_SIG,
        b"",
        lambda pl: (
            f"freq={rd_u32(pl, 0)} phase={rd_u32(pl, 4)} vco={rd_u32(pl, 8)} div={rd_u16(pl, 12)} "
            f"phaseField={pl[14]}（与 SET_SIG 一致）"
        ),
    )
    show_fields(CMD_SET_SOURCE, bytes([1]), lambda pl: (f"sigStatus=0x{rd_u32(pl, 0):08X}（b3 sourceReady=1）"))
    show_fields(CMD_SET_GATE, u32(2000), lambda pl: f"gateTarget={rd_u32(pl, 0)}")
    show_fields(CMD_SET_MODE, bytes([0]), lambda pl: f"mode={pl[0]}（single）")
    show_fields(CMD_START, b"", lambda pl: "空响应；结果由 GET_MEAS 取")
    pl = show_fields(
        CMD_GET_MEAS,
        b"",
        lambda pl: (
            f"seq={rd_u32(pl, 0)} gate={rd_u32(pl, 4)} edge={rd_u32(pl, 8)} phase={rd_u32(pl, 12)} "
            f"freq={rd_u32(pl, 16)}Hz phase={rd_u32(pl, 20)}mdeg"
        ),
    )
    got = (rd_u32(pl, 4), rd_u32(pl, 8), rd_u32(pl, 12), rd_u32(pl, 16), rd_u32(pl, 20))
    exp = (row["gate_cnt"], row["edge_cnt"], row["phase_cnt"], row["freq_milli_hz"], row["phase_milli_deg"])
    print(
        f"      金标准（25MHz/90°）: gate={exp[0]} edge={exp[1]} phase={exp[2]} "
        f"freq={exp[3]} phase={exp[4]} → {'逐位一致' if got == exp else '不一致'}"
    )
    show_fields(
        CMD_GET_RESULT,
        b"",
        lambda pl: (f"valid={pl[0]} seq={rd_u32(pl, 1)} gate={rd_u32(pl, 5)} edge={rd_u32(pl, 9)} " f"phase={rd_u32(pl, 13)}"),
    )
    show_fields(CMD_CLEAR, b"", lambda pl: "空响应（清结果与标志）")
    show_fields(CMD_SET_MODE, bytes([2]), None)
    show_fields(CMD_SET_GATE, u32(0), None)
    show_fields(CMD_SET_SIG, u32(3_000_000) + u32(0), None)
    show_fields(CMD_SET_SIG, u32(25_000_000) + u32(180_000), None)
    show_fields(CMD_STREAM, bytes([1]) + u16(20), lambda pl: (f"enable={pl[0]} intervalMs={rd_u16(pl, 1)}"))
    show_fields(CMD_SET_MODE, bytes([1]), lambda pl: f"mode={pl[0]}（continuous）")
    print("-- 等待通知帧（连续模式 + 流上报）…")
    n_evt = n_data = 0
    for _ in range(40):
        f = sv.next_frame(0.5)
        if f is None:
            continue
        code, pl = f
        if code == NTF_STREAM_DATA:
            n_data += 1
            print(
                f"RX {code:02X} {pl.hex(' ')}   → STREAM_DATA seq={rd_u32(pl, 0)} "
                f"gate={rd_u32(pl, 4)} edge={rd_u32(pl, 8)} phase={rd_u32(pl, 12)} "
                f"freq={rd_u32(pl, 16)}Hz phase={rd_u32(pl, 20)}mdeg lostDelta={rd_u16(pl, 24)}"
            )
        elif code == NTF_EVENT:
            n_evt += 1
            print(f"RX {code:02X} {pl.hex(' ')}   → EVENT code={pl[0]} detail={rd_u32(pl, 1)}")
        if n_data >= 2:
            break
    print(f"-- 收到 STREAM_DATA {n_data} 帧、EVENT {n_evt} 帧")
    show_fields(CMD_STREAM, bytes([0]) + u16(100), lambda pl: "关闭流上报")
    show_fields(CMD_SET_MODE, bytes([0]), lambda pl: "回到 single")
    show_fields(CMD_RESET_COUNTERS, b"", lambda pl: "空响应")
    rc = sv.close()
    print(f"\n-- 服务端 SIGTERM 退出码: {rc}")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
