#!/usr/bin/env python3
"""验收 4 的可复跑脚本：用 host 侧客户端与 C 侧服务端（`ps/build/host_main`）做完整互通。

用法::

    # 终端 A（或本脚本 --spawn 自动拉起）
    ./ps/build/host_main --pty                # 打印 /dev/pts/N
    python3 host/tools/interop_host_main.py --port /dev/pts/N

    # 自动拉起（推荐，脚本负责存活期）
    python3 host/tools/interop_host_main.py --spawn ./ps/build/host_main

覆盖：PING / IDENTIFY / SET_GATE / SET_SOURCE / SET_MODE / SET_SIG / START / GET_MEAS /
monitor（连续模式）/ 错误响应 / **外部输入源无信号（容错路径）**；`--stream` 时额外验证
STREAM 通知（注意 C 侧模型时间由 AXI 事务推进，`intervalMs` 很小才容易到点，脚本会用持续
PING 推进）。

最后一段复现并回归一个真实故障：切到**没接信号**的外部输入源后 `START`，PL 会一直停在
「等参考沿」（`STATUS.busy = 1`、结果永不锁存）。此时：

* `GET_RESULT` 必须回 `valid = 0`（协议 §3.0 的权威探针），**不能回 `ERR_BUSY`**；
* 上位机必须把「等不到结果」当**超时**处理并给出输入源诊断，而不是报
  「设备错误：PS 返回错误响应 0xFF（ERR_BUSY），请求 0x05（GET_RESULT）」。

这一段要求服务端是**修复后**的固件（`ps/src/dpm_cmd.c`：无结果时 `GET_RESULT` 回 `valid = 0`）。
拿旧固件（回 `ERR_BUSY`）跑：上位机侧的容错仍然成立（不再报设备错误、只报超时+诊断），
但脚本会打印 `[FAIL] GET_RESULT 回了错误响应 0xFF` 并以退出码 1 结束——那说明板上的固件
需要重新烧写（`bash ps/build.sh --ps-only` 后按 `docs/bringup.md` 更新 BOOT.bin）。
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

FROZEN = ((25_000_000, 90_000), (10_000_000, 90_000), (6_000_000, 90_000))


def check_external_source_without_signal(client: DpmClient) -> bool:
    """外部输入源容错路径（放在最后：C 侧无信号时测量永不结束，会一直占着 busy）。

    返回是否通过。服务端带 `--ext-present` 时这一段退化成「外部有信号」的正常测量。
    """
    print("\n=== 外部输入源（无信号容错路径）===")
    client.set_source(P.SOURCE_EXTERNAL)
    st = client.get_status()
    print(f"SET_SOURCE(ext) → sigStatus=0x{st.sig_status:08X} sourceReady={int(st.source_ready)}")
    client.set_mode(P.MODE_SINGLE)
    client.start()
    st = client.get_status()
    print(f"START → status=0x{st.status:08X}（busy={int(st.busy)} resultValid={int(st.result_valid)}）")
    ok = True
    try:
        meas = client.wait_meas(timeout=1.5)
    except P.DpmTimeoutError as exc:
        # 期望路径：报告超时 + 输入源诊断，而不是 0xFF 设备错误
        print(f"[OK] 等不到结果 → 超时（不是设备错误）：{exc}")
        ok = "外部引脚" in str(exc)
        if not ok:
            print("[FAIL] 超时诊断里没有指出输入源（期望提到「外部引脚」）", file=sys.stderr)
    except P.DpmError as exc:  # pragma: no cover - 回归时才会走到
        print(f"[FAIL] 又报成设备错误了：{exc}", file=sys.stderr)
        return False
    else:
        print(f"[OK] 服务端带 --ext-present：外部输入有信号，测得 seq={meas.seq} edgeCnt={meas.edge_cnt}")

    # 固件侧：GET_RESULT 用 valid=0 表达「还没有结果」（回 ERR_BUSY 会让按协议写的上位机中止）
    frame = client.request(P.Cmd.GET_RESULT, accept_busy=True)
    if frame.is_error:
        print(f"[FAIL] GET_RESULT 回了错误响应 0x{frame.cmd:02X}（协议 §3.0 要求 valid=0）", file=sys.stderr)
        return False
    result = P.decode_result(frame.payload)
    print(f"[OK] GET_RESULT → valid={int(result.valid)}（等不到结果时必须用 valid=0，不是 ERR_BUSY）")
    return ok


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

    # 外部输入源容错路径必须放在最后：真机上「源没有边沿」时这次测量会一直占着 busy
    external_ok = check_external_source_without_signal(client)

    print("统计:", client.stats.snapshot())
    client.close()
    if not external_ok:
        return 1
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
