#!/usr/bin/env python3
"""验收 4 的可复跑脚本：用 host 侧客户端与 C 侧服务端（`ps/build/host_main`）做完整互通。

用法::

    # 终端 A（或本脚本 --spawn 自动拉起）
    ./ps/build/host_main --pty                # 打印 /dev/pts/N
    python3 host/tools/interop_host_main.py --port /dev/pts/N

    # 自动拉起（推荐，脚本负责存活期）
    python3 host/tools/interop_host_main.py --spawn ./ps/build/host_main

覆盖：PING / IDENTIFY / SET_GATE / SET_SOURCE / SET_MODE / SET_SIG / START / GET_MEAS /
monitor（连续模式）/ 错误响应；`--stream` 时额外验证 STREAM 通知（注意 C 侧模型时间由
AXI 事务推进，`intervalMs` 很小才容易到点，脚本会用持续 PING 推进）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpm_host import mathx as M  # noqa: E402
from dpm_host import protocol as P  # noqa: E402
from dpm_host.client import DpmClient  # noqa: E402

FROZEN = ((25_000_000, 90_000), (10_000_000, 90_000), (6_250_000, 90_000))


def run(port: str, *, stream: bool) -> int:
    client = DpmClient(port=port, timeout=1.0, log=lambda _m: None)
    print("=== host 侧 Python 客户端 ↔ C 侧服务端互通 ===")
    client.ping()
    info = client.identify()
    print(
        f"PING ok；IDENTIFY: ver=0x{info.ver:08X} versionReg=0x{info.version_reg:08X} "
        f"caps=0x{info.caps:08X} capsReg=0x{info.caps_reg:08X} "
        f"dsp={info.dsp_clk_hz / 1e6:g}MHz slow={info.slow_clk_hz / 1e6:g}MHz flags=0x{info.flags:04X}"
    )
    print("SET_GATE(2000) →", client.set_gate(2000))
    print("SET_SOURCE(int) → sigStatus=0x%08X" % client.set_source(P.SOURCE_INTERNAL))
    print("SET_MODE(single) →", client.set_mode(P.MODE_SINGLE))

    for freq_hz, phase_mdeg in FROZEN:
        client.wait_idle(timeout=2.0)
        sig = client.set_sig(freq_hz, phase_mdeg)
        print(
            f"\nSET_SIG {freq_hz / 1e6:g}MHz/{phase_mdeg / 1000:g}° 回显: f={sig.freq_hz / 1e6:g}MHz "
            f"phase={sig.phase_deg:g}° VCO={sig.vco_hz / 1e6:g}MHz D={sig.div} phaseField={sig.phase_field}"
        )
        client.wait_source_ready(timeout=2.0)
        client.start()
        meas = client.wait_meas(timeout=3.0)
        calc = client.calc_meas(meas)
        print(
            f"GET_MEAS: seq={meas.seq} gate={meas.gate_cnt} edge={meas.edge_cnt} phaseCnt={meas.phase_cnt} "
            f"wire(freqHz={meas.freq_milli_hz}, phase_mdeg={meas.phase_milli_deg})"
        )
        print(
            f"  本地换算: f={M.format_freq_hz(calc.freq_hz)} phase={M.format_milli_deg(calc.phase_milli_deg)} "
            f"phaseCnt/edgeCnt={M.phase_cycles_float(meas.phase_cnt, meas.edge_cnt):.4f} DSP周期"
        )

    print("\nmonitor 连续测量 3 次:")
    for meas, calc in client.monitor(count=3, timeout=3.0):
        print(
            f"  seq={meas.seq} f={M.format_freq_hz(calc.freq_hz)} phase={M.format_milli_deg(calc.phase_milli_deg)} "
            f"gate={meas.gate_cnt} edge={meas.edge_cnt} phaseCnt={meas.phase_cnt}"
        )

    if stream:
        got: list[P.StreamData] = []
        client.on_stream_data = got.append
        client.stream(True, 1)
        client.set_mode(P.MODE_CONTINUOUS)
        client.start()
        deadline = time.monotonic() + 20
        while len(got) < 3 and time.monotonic() < deadline:
            client.ping()  # C 侧模型时间由 AXI 事务推进，需要流量才会到 intervalMs
        client.stream(False, 1)
        client.on_stream_data = None
        client.set_mode(P.MODE_SINGLE)
        print(f"\nSTREAM 通知 {len(got)} 条:")
        for data in got[:3]:
            calc = client.calc_meas(data.meas)
            print(
                f"  seq={data.meas.seq} lostΔ={data.lost_delta} "
                f"wire(freqHz={data.meas.freq_milli_hz}, phase_mdeg={data.meas.phase_milli_deg}) "
                f"local(f={M.format_freq_hz(calc.freq_hz)}, phase={M.format_milli_deg(calc.phase_milli_deg)})"
            )

    try:
        client.set_mode(7)
        print("\n错误路径：SET_MODE(7) 未抛错（异常）")
    except P.DpmError as exc:
        print(f"\n错误路径：SET_MODE(7) → {exc}")
    print("统计:", client.stats.snapshot())
    client.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="host 客户端 ↔ C 服务端互通脚本")
    ap.add_argument("--port", default=None, help="C 服务端已建好的 pty 路径")
    ap.add_argument("--spawn", default=None, help="自动拉起服务端，例如 ./ps/build/host_main")
    ap.add_argument("--stream", action="store_true", help="额外验证 STREAM 通知（会发很多请求）")
    args = ap.parse_args()
    if args.spawn:
        child = subprocess.Popen([args.spawn, "--pty"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.monotonic() + 10
            port = ""
            while time.monotonic() < deadline:
                line = child.stdout.readline() if child.stdout else ""
                if line.strip().startswith("/dev/pts/"):
                    port = line.strip()
                    break
            if not port:
                print("未能从服务端 stdout 拿到 pty 路径", file=sys.stderr)
                return 1
            print(f"服务端 pty: {port}")
            return run(port, stream=args.stream)
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                child.kill()
    if not args.port:
        print("需要 --port 或 --spawn", file=sys.stderr)
        return 2
    return run(args.port, stream=args.stream)


if __name__ == "__main__":
    raise SystemExit(main())
