"""命令行界面：`python3 -m dpm_host <子命令>`。

子命令与 `docs/ps-protocol.md` 的命令一一对应（外加 `scan` / `monitor` / `gui` 这类上位机功能）：

.. code-block:: text

    ping        PING
    identify    IDENTIFY（版本、能力、两侧时钟）
    status      GET_STATUS（状态位 + 片内源状态）
    start       START（可等结果）
    meas        GET_MEAS（无有效结果时自动启动一次）
    monitor     连续测量 N 次或指定时长
    mode        SET_MODE single|continuous
    source      SET_SOURCE ext|int
    sig         SET_SIG --freq --phase（并显示求解出的 MMCM 工作点与 DRP 字）
    gate        SET_GATE N
    stream      STREAM on|off
    clear       CLEAR
    reset       RESET_COUNTERS
    scan        多频点 × 多相位扫描，写 CSV
    sigtable    打印相位字段语义与冻结频点表（离线，不连串口）
    gui         启动 PySide6 图形界面

`--port` 既可以是真串口（`/dev/ttyUSB0`）也可以是 pty 路径（`/dev/pts/N`，配合
`python3 -m dpm_host.sim_server --pty` 使用）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable, Sequence

from . import mathx as M
from . import protocol as P
from . import sig as S
from .client import DpmClient, SCAN_CSV_FIELDS, scan_rows_to_csv
from .protocol import DpmError, DpmTimeoutError, Meas, Result

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

#: `scan` 在 stdout 上回显的行数上限（完整结果在 CSV 里）。
SCAN_ECHO_ROWS = 8


# ------------------------------------------------------------------ 小工具


def parse_freq(text: str) -> int:
    """解析频率：`25e6`、`25MHz`、`25M`、`1000000` 都接受，返回 Hz。"""
    t = str(text).strip().replace(" ", "")
    units = (("ghz", 1e9), ("mhz", 1e6), ("khz", 1e3), ("hz", 1.0))
    lower = t.lower()
    for suffix, scale in units:
        if lower.endswith(suffix):
            return int(round(float(t[: -len(suffix)]) * scale))
    if lower.endswith("g"):
        return int(round(float(t[:-1]) * 1e9))
    if lower.endswith("m"):
        return int(round(float(t[:-1]) * 1e6))
    if lower.endswith("k"):
        return int(round(float(t[:-1]) * 1e3))
    return int(round(float(t)))


def parse_deg(text: str) -> float:
    """解析相位（度）：允许 `90`、`90deg`、`90°`。"""
    t = str(text).strip().replace(" ", "")
    for suffix in ("deg", "°", "度"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return float(t)


def parse_list(text: str, conv: Callable[[str], Any]) -> list[Any]:
    """解析逗号分隔的列表（空字符串 → 空列表）。"""
    items = [part for part in str(text).replace(";", ",").split(",") if part.strip()]
    return [conv(item) for item in items]


def fmt_status(st: P.Status) -> str:
    """状态行的可读形式。"""
    return (
        f"status=0x{st.status:08X} [{st.status_text}]  "
        f"sigStatus=0x{st.sig_status:08X} [{st.sig_status_text}]  "
        f"gateTarget={st.gate_target}  lostCount={st.lost_count}  seq={st.seq}"
    )


def fmt_meas(meas: Meas, client: DpmClient) -> str:
    """把一次测量结果格式化成一行（频率、相位差、计数、主判据）。

    频率/相位用 `gateCnt/edgeCnt/phaseCnt` 本地换算（`DpmClient.calc_meas`）：协议里的
    `freqMilliHz` 是 u32，5MHz 以上会溢出，不能当显示来源。
    """
    calc = client.calc_meas(meas)
    cycles = M.phase_cycles_float(meas.phase_cnt, meas.edge_cnt)
    step = M.phase_quant_milli_deg(calc.freq_hz, client.dsp_hz)
    extra = ""
    if meas.freq_milli_hz != calc.freq_hz:
        extra = (
            f"  [wire freqMilliHz={meas.freq_milli_hz} 与本地换算 {calc.freq_hz} 不同："
            f"对端字段单位口径差异，显示以本地换算为准]"
        )
    return (
        f"seq={meas.seq:<5d} f={M.format_freq_hz(calc.freq_hz):>14s} "
        f"phase={M.format_milli_deg(calc.phase_milli_deg):>10s} "
        f"gate={meas.gate_cnt:<6d} edge={meas.edge_cnt:<7d} phaseCnt={meas.phase_cnt:<10d} "
        f"phaseCycles={cycles:.4f} 量化步长={step / 1000:.3f}°" + extra
    )


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def make_client(args: argparse.Namespace) -> DpmClient:
    """按命令行参数建客户端（连接失败抛异常，由 :func:`main` 统一处理）。"""
    if not args.port:
        raise SystemExit("必须指定 --port（真串口或 pty 路径）")
    return DpmClient(
        port=args.port,
        baud=args.baud,
        timeout=args.timeout,
        retries=args.retries,
        slow_hz=args.slow_hz,
        dsp_hz=args.dsp_hz,
        log=(lambda msg: print(f"[debug] {msg}", file=sys.stderr)) if args.debug else None,
    )


def open_client(args: argparse.Namespace) -> DpmClient:
    """建客户端并做一次 PING 握手（失败给出可操作的提示）。"""
    client = make_client(args)
    try:
        client.ping()
    except Exception:
        client.close()
        raise
    return client


# ------------------------------------------------------------------ 各子命令


def cmd_ping(args: argparse.Namespace, client: DpmClient) -> int:
    t0 = time.monotonic()
    client.ping()
    dt_ms = (time.monotonic() - t0) * 1000
    if args.json:
        print_json({"ok": True, "rtt_ms": dt_ms, "stats": client.stats.snapshot()})
    else:
        print(f"PING 成功：往返 {dt_ms:.2f} ms，统计 {client.stats.snapshot()}")
    return EXIT_OK


def cmd_identify(args: argparse.Namespace, client: DpmClient) -> int:
    info = client.identify()
    flags = []
    for flag, name in (
        (P.FLAG_SIGNAL_SOURCE, "片内信号源"),
        (P.FLAG_CONTINUOUS, "连续模式"),
        (P.FLAG_STREAM, "流模式"),
        (P.FLAG_IRQ, "中断驱动"),
    ):
        if info.flags & flag:
            flags.append(name)
    if args.json:
        print_json(
            {
                "ver": info.ver,
                "ver_text": info.ver_text,
                "gitRev": info.git_rev,
                "caps": info.caps,
                "versionReg": info.version_reg,
                "capsReg": info.caps_reg,
                "dspClkHz": info.dsp_clk_hz,
                "slowClkHz": info.slow_clk_hz,
                "flags": info.flags,
                "flag_names": flags,
            }
        )
        return EXIT_OK
    print(f"ver          = 0x{info.ver:08X}（{info.ver_text}）")
    print(f"gitRev       = 0x{info.git_rev:08X}")
    print(f"caps         = 0x{info.caps:08X}（每条 {info.caps & 0xFF} 字，深度 {(info.caps >> 8) & 0xFF}）")
    print(f"versionReg   = 0x{info.version_reg:08X}（期望 0x44504D02）")
    print(f"capsReg      = 0x{info.caps_reg:08X}")
    print(f"dspClkHz     = {info.dsp_clk_hz / 1e6:g} MHz")
    print(f"slowClkHz    = {info.slow_clk_hz / 1e6:g} MHz")
    print(f"flags        = 0x{info.flags:04X} [{'|'.join(flags) if flags else '-'}]")
    return EXIT_OK


def cmd_status(args: argparse.Namespace, client: DpmClient) -> int:
    st = client.get_status()
    if args.json:
        print_json(
            {
                "status": st.status,
                "sigStatus": st.sig_status,
                "gateTarget": st.gate_target,
                "lostCount": st.lost_count,
                "seq": st.seq,
                "busy": st.busy,
                "resultValid": st.result_valid,
                "lost": st.lost,
                "irqPending": st.irq_pending,
                "sigEnable": st.sig_enable,
                "sigSelect": st.sig_select,
                "srcLocked": st.src_locked,
                "sourceReady": st.source_ready,
            }
        )
        return EXIT_OK
    print(fmt_status(st))
    if st.result_valid:
        # `STATUS.resultValid = 1` 只说明结果槽里有一个**未读走**的结果；对端可能拒绝把它
        # 当成本次的结果交出来（`START` 之后本次结果还没产出时的旧结果保护，见
        # `docs/ps-protocol.md` §3.0），此时 `GET_MEAS` 回 `ERR_BUSY`——那是「暂无可用结果」
        # 而不是设备错误，`status` 是只读查询，不该因此中止。
        meas = client.try_get_meas()
        if meas is None:
            print("最新结果：暂时读不到（结果槽里是上一次未读的结果，或本次测量尚未结束；用 meas 等一次新结果）")
        else:
            print("最新结果：" + fmt_meas(meas, client))
    return EXIT_OK


def cmd_start(args: argparse.Namespace, client: DpmClient) -> int:
    client.start()
    if args.json:
        print_json({"started": True})
    else:
        print("已发送 START")
    if not args.wait:
        return EXIT_OK
    try:
        meas = client.wait_meas(timeout=args.result_timeout)
    except DpmTimeoutError as exc:
        print(f"测量未完成：{exc}", file=sys.stderr)
        print("提示：片内源未使能/未锁定，或外部输入没有边沿时，测量永远不会完成。", file=sys.stderr)
        return EXIT_ERROR
    result = Result(True, meas.seq, meas.gate_cnt, meas.edge_cnt, meas.phase_cnt)
    if args.json:
        print_json({"result": result.__dict__, "meas": meas.as_row()})
    else:
        print("结果：" + fmt_meas(meas, client))
    return EXIT_OK


def cmd_meas(args: argparse.Namespace, client: DpmClient) -> int:
    st = client.get_status()
    meas: Meas | None = None
    if st.result_valid:
        # 结果槽里有未读结果：通常一次 `GET_MEAS` 就能读走。例外是 `START` 之后本次结果
        # 还没产出——对端按「不把上一次的结果当成本次结果」拒绝（协议 §3.0 的旧结果保护），
        # 回 `ERR_BUSY`。那是「暂无可用结果」，不能当设备错误，退回「等一次新结果」的路径。
        meas = client.try_get_meas()
    if meas is None:
        if args.no_start:
            print("没有可用的新结果（--no-start：不主动启动测量）", file=sys.stderr)
            return EXIT_ERROR
        if st.result_valid:
            print("结果槽里还有上一次未读的结果，本次测量尚未产出新结果，等一次新结果……")
        elif st.busy:
            print("测量进行中，等待结果……")
        else:
            print("无有效结果，启动一次测量……")
            client.start()
        try:
            meas = client.wait_meas(timeout=args.result_timeout)
        except DpmTimeoutError as exc:
            print(f"测量未完成：{exc}", file=sys.stderr)
            print("提示：片内源未使能/未锁定，或外部输入没有边沿时，测量永远不会完成。", file=sys.stderr)
            return EXIT_ERROR
    calc = client.calc_meas(meas)
    if args.json:
        print_json({**meas.as_row(), "freq_hz_local": calc.freq_hz,
                    "phase_milli_deg_local": calc.phase_milli_deg})
        return EXIT_OK
    print(fmt_meas(meas, client))

    # 主判据：phaseCnt / edgeCnt（DSP 周期数）
    if meas.edge_cnt:
        cycles = M.phase_cycles_float(meas.phase_cnt, meas.edge_cnt)
        step_deg = 360.0 * calc.freq_hz / client.dsp_hz
        print(
            f"主判据 phaseCnt/edgeCnt = {cycles:.6f} DSP 周期"
            f"（1 个 DSP 周期 = {step_deg:.4f}°，即相位量化下界）"
        )
        if args.expect_deg is not None:
            err = M.phase_err_deg(calc.phase_milli_deg, args.expect_deg)
            tol = 360.0 * calc.freq_hz / client.dsp_hz / 2.0 + 0.001
            verdict = "通过" if abs(err) <= tol else "超出容限"
            print(f"与期望 {args.expect_deg:g}° 的差 = {err:+.4f}°（容限 ±{tol:.4f}°，{verdict}）")
    return EXIT_OK


def cmd_monitor(args: argparse.Namespace, client: DpmClient) -> int:
    samples: list[tuple[Meas, M.Meas]] = []
    header = f"{'#':>4} {'seq':>6} {'频率':>15} {'相位差':>11} {'gate':>7} {'edge':>8} {'phaseCnt':>11} {'DSP周期':>9}"

    def on_sample(meas: Meas, calc: M.Meas) -> None:
        samples.append((meas, calc))
        if not args.json:
            print(
                f"{len(samples):>4} {meas.seq:>6} {M.format_freq_hz(calc.freq_hz):>15} "
                f"{M.format_milli_deg(calc.phase_milli_deg):>11} {meas.gate_cnt:>7} {meas.edge_cnt:>8} "
                f"{meas.phase_cnt:>11} {M.phase_cycles_float(meas.phase_cnt, meas.edge_cnt):>9.4f}"
            )

    if not args.json:
        print(f"连续测量：次数={args.count if args.count else '∞'} 时长={args.duration if args.duration else '∞'}s "
              f"流模式={'开' if args.stream else '关'}")
        print(header)
    try:
        samples = client.monitor(
            count=args.count,
            duration=args.duration,
            interval=args.interval,
            use_stream=args.stream,
            interval_ms=args.interval_ms,
            on_sample=on_sample,
            timeout=args.result_timeout,
        )
    except DpmTimeoutError as exc:
        print(f"测量超时：{exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        pass

    if not samples:
        print("没有采到任何样本", file=sys.stderr)
        return EXIT_ERROR
    if args.csv:
        rows = [
            {
                "freq_nominal": "",
                "phase_nominal": "",
                "f_meas_mHz": c.freq_hz,
                "phase_meas_mdeg": c.phase_milli_deg,
                "gateCnt": m.gate_cnt,
                "edgeCnt": m.edge_cnt,
                "phaseCnt": m.phase_cnt,
                "phaseCycles": M.phase_cycles_float(m.phase_cnt, m.edge_cnt),
                "err_deg": "",
            }
            for m, c in samples
        ]
        scan_rows_to_csv(rows, args.csv)
        print(f"已写入 {args.csv}（{len(rows)} 行）")
    if args.json:
        print_json([m.as_row() for m, _c in samples])
        return EXIT_OK
    freqs = [c.freq_hz for _m, c in samples]
    phases = [c.phase_milli_deg for _m, c in samples]
    print(
        f"共 {len(samples)} 个样本：频率 min={M.format_freq_hz(min(freqs))} "
        f"max={M.format_freq_hz(max(freqs))}；相位 min={M.format_milli_deg(min(phases))} "
        f"max={M.format_milli_deg(max(phases))}"
    )
    return EXIT_OK


def cmd_mode(args: argparse.Namespace, client: DpmClient) -> int:
    value = client.set_mode(args.mode)
    if args.json:
        print_json({"mode": value})
    else:
        print(f"mode = {value}（{P.MODE_NAMES.get(value, '未知')}）")
    return EXIT_OK


def cmd_source(args: argparse.Namespace, client: DpmClient) -> int:
    sig_status = client.set_source(args.source)
    st = client.get_status()
    if args.json:
        print_json({"sigStatus": sig_status, "status": st.__dict__})
    else:
        print(f"source = {P.SOURCE_NAMES[P.str_to_source(args.source)]}，sigStatus=0x{sig_status:08X} [{st.sig_status_text}]")
    if args.wait_ready:
        try:
            st = client.wait_source_ready(timeout=args.ready_timeout)
        except DpmTimeoutError as exc:
            print(f"等待 sourceReady 超时：{exc}", file=sys.stderr)
            return EXIT_ERROR
        if not args.json:
            print(f"sourceReady 就绪：{fmt_status(st)}")
    return EXIT_OK


def cmd_sig(args: argparse.Namespace, client: DpmClient) -> int:
    freq_hz = parse_freq(args.freq) if args.freq is not None else None
    phase_mdeg = int(round(parse_deg(args.phase) * 1000)) if args.phase is not None else None
    semantics = S.current_semantics()

    if freq_hz is None and phase_mdeg is None:
        info = client.get_sig()
        if args.json:
            print_json({"freqHz": info.freq_hz, "phaseMilliDeg": info.phase_milli_deg, "vcoHz": info.vco_hz,
                        "div": info.div, "phaseField": info.phase_field})
        else:
            print(f"片内源：f={info.freq_hz / 1e6:g} MHz 相位差={info.phase_deg:g}° "
                  f"VCO={info.vco_hz / 1e6:g} MHz D={info.div} 相位字段={info.phase_field}")
        return EXIT_OK

    if freq_hz is None or phase_mdeg is None:
        current = client.get_sig()
        freq_hz = current.freq_hz if freq_hz is None else freq_hz
        phase_mdeg = current.phase_milli_deg if phase_mdeg is None else phase_mdeg

    sol = S.solve_sig(freq_hz, phase_mdeg, client.slow_hz, semantics)
    if not args.json:
        print(f"求解：{sol.describe()}")
        print(f"      {semantics.describe()}")
        for note in sol.notes:
            print(f"      提示：{note}")
        if sol.ok:
            words = S.drp_words(sol)
            print("      DRP 字：" + "  ".join(f"0x{off:03X}=0x{value:08X}" for off, value in words.as_pairs()))
            grid = S.mmcm_phase_grid_milli_deg(sol.div) if sol.div else None
            print(f"      相位网格 45/D = {float(grid) / 1000:.4f}°（D={sol.div}）")
        supported, reason = S.phase_supported(phase_mdeg, semantics)
        if not supported:
            print(f"      ⚠ {reason}")
            print("      ⚠ 当前 bitstream 相位固定 90°（DRP 相位字未接入），要在线扫描相位需重建 IP/BD")
    if not sol.ok:
        print("该频率在 MMCM 约束内无解，未下发", file=sys.stderr)
        return EXIT_ERROR
    if args.dry_run:
        return EXIT_OK
    client.wait_idle(timeout=args.timeout)
    info = client.set_sig(freq_hz, phase_mdeg)
    echo = client.get_sig()
    if args.json:
        print_json({"requested": {"freqHz": freq_hz, "phaseMilliDeg": phase_mdeg},
                    "set_sig": info.__dict__, "get_sig": echo.__dict__})
    else:
        print(f"SET_SIG → f={info.freq_hz / 1e6:g} MHz 相位差={info.phase_deg:g}° "
              f"VCO={info.vco_hz / 1e6:g} MHz D={info.div} 相位字(0x218)={phase_mdeg} 毫度"
              f"（phaseField={info.phase_field} 是历史字段，恒 0）")
        print(f"GET_SIG → f={echo.freq_hz / 1e6:g} MHz 相位差={echo.phase_deg:g}° "
              f"VCO={echo.vco_hz / 1e6:g} MHz D={echo.div}")
        if info.phase_milli_deg != phase_mdeg:
            grid = S.mmcm_phase_grid_milli_deg(info.div) if info.div else None
            kind = "量化" if semantics.phase_controllable else "相位字未接入（当前 bitstream 不可在线改相位）"
            print(f"注意：请求相位 {phase_mdeg / 1000:g}° 与实现相位 {info.phase_milli_deg / 1000:g}° 不同（{kind}"
                  + (f"，网格 45/D = {float(grid) / 1000:.4f}°" if grid else "") + "）")
    return EXIT_OK


def cmd_gate(args: argparse.Namespace, client: DpmClient) -> int:
    value = client.set_gate(args.target)
    if args.json:
        print_json({"gateTarget": value})
    else:
        seconds = value / client.slow_hz
        print(f"gateTarget = {value}（{seconds * 1e6:.1f} µs @ {client.slow_hz / 1e6:g} MHz）")
    return EXIT_OK


def cmd_stream(args: argparse.Namespace, client: DpmClient) -> int:
    enable = args.state in ("on", "1", "true", "yes")
    got_enable, interval = client.stream(enable, args.interval_ms)
    if args.json:
        print_json({"enable": got_enable, "intervalMs": interval})
    else:
        print(f"流模式 = {'开' if got_enable else '关'}，intervalMs={interval}")
    if not got_enable or args.count <= 0:
        return EXIT_OK

    # 流数据来自「有效结果」；没有结果时切到 continuous 并启动一次，否则空等
    if args.autostart and not client.get_status().result_valid:
        if not args.json:
            print("（当前没有有效结果：切到 continuous 并 START，以便产生流数据）")
        client.set_mode(P.MODE_CONTINUOUS)
        client.start()

    received: list[P.StreamData] = []

    def on_stream(data: P.StreamData) -> None:
        received.append(data)
        if not args.json:
            print(f"[{len(received):>3}] lostΔ={data.lost_delta:<5d} " + fmt_meas(data.meas, client))

    client.on_stream_data = on_stream
    try:
        deadline = time.monotonic() + args.duration
        while len(received) < args.count and time.monotonic() < deadline:
            time.sleep(0.005)
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        pass
    finally:
        client.stream(False, interval)
        client.on_stream_data = None
    if args.json:
        print_json([{"meas": d.meas.as_row(), "lostDelta": d.lost_delta} for d in received])
    if not received:
        print("没有收到 STREAM_DATA（检查 intervalMs 与测量是否在跑）", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def cmd_clear(args: argparse.Namespace, client: DpmClient) -> int:
    client.clear()
    st = client.get_status()
    if args.json:
        print_json({"status": st.__dict__})
    else:
        print("已发送 CLEAR：" + fmt_status(st))
    return EXIT_OK


def cmd_reset(args: argparse.Namespace, client: DpmClient) -> int:
    client.reset_counters()
    st = client.get_status()
    if args.json:
        print_json({"status": st.__dict__})
    else:
        print("已发送 RESET_COUNTERS：" + fmt_status(st))
    return EXIT_OK


def cmd_sigtable(args: argparse.Namespace, client: DpmClient | None) -> int:
    """打印相位字段语义与冻结频点表（离线命令，不需要串口）。"""
    semantics = S.current_semantics()
    problems = S.verify_frozen_table()
    if args.json:
        print_json(
            {
                "semantics": {
                    "phase_controllable": semantics.phase_controllable,
                    "phase_grid_mdeg_num": S.PHASE_GRID_MDEG_NUM,
                    "path": semantics.path,
                    "source": semantics.source,
                },
                "frozen_points": [
                    {
                        "freq_hz": pt.freq_hz,
                        "vco_hz": pt.vco_hz,
                        "mult": pt.mult,
                        "div": pt.div,
                        "divide_field": pt.divide_field,
                        "phase_step_ps": float(pt.phase_step_ps),
                    }
                    for pt in S.FROZEN_POINTS
                ],
                "problems": problems,
            }
        )
        return EXIT_OK if not problems else EXIT_ERROR
    print(semantics.describe())
    print(f"结论文件：{semantics.path or '（不存在，已回退）'}")
    print()
    print(f"{'目标频率':>12} {'VCO/MHz':>9} {'MULT':>5} {'D':>5} {'分频字段':>9} {'相位步长/ps':>12} {'求解器':>16}")
    for pt in S.FROZEN_POINTS:
        sol = S.solve_sig(pt.freq_hz, 0)
        mark = "一致" if (sol.vco_hz, sol.mult, sol.div) == (pt.vco_hz, pt.mult, pt.div) else "不一致"
        print(
            f"{pt.freq_mhz:>10g}MHz {pt.vco_hz / 1e6:>9g} {pt.mult:>5d} {pt.div:>5d} {pt.divide_field:>9d} "
            f"{float(pt.phase_step_ps):>12.2f} {mark:>16}"
        )
    print()
    if problems:
        print("自校验失败：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return EXIT_ERROR
    print("冻结频点表自校验：全部通过（求解器输出与表逐位一致）")
    return EXIT_OK


def cmd_scan(args: argparse.Namespace, client: DpmClient) -> int:
    freqs = parse_list(args.freqs, parse_freq)
    phases = parse_list(args.phases, parse_deg)
    if not freqs:
        print("--freqs 不能为空", file=sys.stderr)
        return EXIT_USAGE
    if not phases:
        print("--phases 不能为空", file=sys.stderr)
        return EXIT_USAGE
    sem = S.current_semantics()
    if args.print_plan:
        print(f"扫描计划：{len(freqs)} 个频点 × {len(phases)} 个相位点 = {len(freqs) * len(phases)} 行")
        print(f"相位可控性：{'是' if sem.phase_controllable else '否（实现相位恒为静态 ' + f'{sem.phase_static_milli_deg / 1000:.3f}' + '°）'}"
              f"，来源 {sem.path or '回退'}")
        skipped: list[str] = []
        for f in freqs:
            sol = S.solve_sig(f, 0, client.slow_hz, S.current_semantics())
            print(f"  {f / 1e6:>9.4f} MHz → {sol.describe()}")
            for p in phases:
                # 相位上界 = 9 位相位码（按 D 逐点）：这些点服务端会回 ERR_PARAM，扫描会跳过
                if sol.div and not S.phase_code_fits(int(round(p * 1000)), sol.div):
                    skipped.append(f"{f / 1e6:g}MHz/{p:g}°")
        if skipped:
            head = "、".join(skipped[:6]) + ("…" if len(skipped) > 6 else "")
            print(f"  其中 {len(skipped)} 个 (频点, 相位) 超出相位码上界（`m ≤ 511`），会被跳过：{head}")
            print(f"  实际可扫 {len(freqs) * len(phases) - len(skipped)} 行")
    rows: list[dict] = []

    def on_row(row: dict) -> None:
        rows.append(row)
        if not args.json:
            print(
                f"[{len(rows):>3}] {row['freq_nominal'] / 1e6:>9.4f}MHz {row['phase_nominal']:>7.1f}° → "
                f"f={M.format_freq_hz(row['f_meas_mHz']):>15} "
                f"phase={M.format_milli_deg(row['phase_meas_mdeg']):>10} "
                f"err={row['err_deg']:+.4f}° gate={row['gateCnt']} edge={row['edgeCnt']} "
                f"phaseCnt={row['phaseCnt']} cycles={row['phaseCycles']:.4f}"
                + ("" if row.get("phase_honored", True) else "  ⚠相位未生效")
            )

    try:
        rows = client.scan(
            freqs,
            phases,
            gate_target=args.gate,
            source=args.source,
            settle=args.settle,
            timeout=args.timeout,
            result_timeout=args.result_timeout,
            on_row=on_row,
            log=(lambda msg: print(f"[scan] {msg}")) if not args.json else None,
        )
    except DpmTimeoutError as exc:
        print(f"扫描超时：{exc}", file=sys.stderr)
        if rows and args.out:
            scan_rows_to_csv(rows, args.out)
            print(f"已把已完成的 {len(rows)} 行写入 {args.out}", file=sys.stderr)
        return EXIT_ERROR
    if not rows:
        print("没有得到任何扫描行", file=sys.stderr)
        return EXIT_ERROR
    if args.out:
        scan_rows_to_csv(rows, args.out)
    if args.json:
        print_json(rows)
        return EXIT_OK

    # 回显 CSV 头几行（完整结果在 --out 指定的文件里）
    print()
    print("CSV 前几行（" + ",".join(SCAN_CSV_FIELDS) + "）：")
    echo = rows[:SCAN_ECHO_ROWS] if args.echo_rows is None else rows[: args.echo_rows]
    for row in echo:
        print("  " + ",".join(str(row[col]) for col in SCAN_CSV_FIELDS))
    if len(rows) > len(echo):
        print(f"  ...（共 {len(rows)} 行）")
    errs = [abs(row["err_deg"]) for row in rows]
    print()
    print(f"扫描完成：{len(rows)} 行，|err_deg| 最大 {max(errs):.4f}° 平均 {sum(errs) / len(errs):.4f}°")
    if rows and not rows[0].get("phase_controllable", True):
        n_bad = sum(1 for row in rows if not row.get("phase_honored", True))
        if n_bad:
            print(f"⚠ 当前 bitstream 相位不可在线改（spec/drp_semantics.json: phase_controllable=false）："
                  f"实现相位恒为静态 90°，{n_bad}/{len(rows)} 行的相位与请求值不符（err_deg 已如实反映）")
            print("  要在线扫描相位需按 docs/ps-protocol.md 6.2 给 clk_wiz_src 加 CONFIG.PHASE_DUTY_CONFIG 并重建 IP/BD")
        else:
            print("说明：spec/drp_semantics.json 记录当前 bitstream 相位不可在线改（phase_controllable=false），"
                  "但本次扫描的设备接受了相位设置（仿真后端 --phase-model controllable 模拟的「补丁后」行为）；"
                  "真实硬件上相位仍恒为 90°，需重建 IP/BD 后才生效")
    if args.out:
        print(f"CSV 已写入 {args.out}")
    return EXIT_OK


def cmd_gui(args: argparse.Namespace, client: DpmClient | None) -> int:
    """转交给 PySide6 界面（不在这里建连接，避免占用端口）。"""
    from . import gui

    argv: list[str] = []
    if args.port:
        argv += ["--port", args.port]
    if args.baud:
        argv += ["--baud", str(args.baud)]
    if args.timeout:
        argv += ["--timeout", str(args.timeout)]
    argv += list(getattr(args, "gui_args", None) or [])
    return gui.main(argv)


# ------------------------------------------------------------------ 参数解析


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python3 -m dpm_host",
        description="DigitalPhaseMeter PC 侧上位机（串口协议客户端）",
        epilog="示例：python3 -m dpm_host --port /dev/pts/5 scan --freqs 5e6,10e6 --phases 0,90 --out /tmp/scan.csv",
    )
    ap.add_argument("--port", metavar="PORT", default=None, help="串口设备或 pty 路径（如 /dev/pts/5）")
    ap.add_argument("--baud", type=int, default=P.DEFAULT_BAUD, help=f"波特率（默认 {P.DEFAULT_BAUD}）")
    ap.add_argument("--timeout", type=float, default=0.5, help="单次请求超时（秒，默认 0.5）")
    ap.add_argument("--result-timeout", type=float, default=2.0, help="等测量结果的总超时（秒，默认 2.0）")
    ap.add_argument("--retries", type=int, default=1, help="超时重发次数（默认 1）")
    ap.add_argument("--slow-hz", type=int, default=M.SLOW_HZ_DEFAULT, help="低速时钟频率（IDENTIFY 会覆盖）")
    ap.add_argument("--dsp-hz", type=int, default=M.DSP_HZ_DEFAULT, help="DSP 时钟频率（IDENTIFY 会覆盖）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")
    ap.add_argument("--debug", action="store_true", help="把收发日志打到 stderr")

    sub = ap.add_subparsers(dest="cmd", metavar="子命令")

    p = sub.add_parser("ping", help="PING：探测连通性")
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("identify", help="IDENTIFY：版本与能力")
    p.set_defaults(func=cmd_identify)

    p = sub.add_parser("status", help="GET_STATUS：状态位")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("start", help="START：启动一次测量")
    p.add_argument("--no-wait", action="store_false", dest="wait", help="只发 START，不等结果")
    p.set_defaults(func=cmd_start, wait=True)

    p = sub.add_parser("meas", help="GET_MEAS：读频率与相位差")
    p.add_argument("--no-start", action="store_true", help="不自动触发测量，只读现有结果")
    p.add_argument("--expect-deg", type=float, default=None, metavar="DEG", help="给出期望相位做判据比较")
    p.set_defaults(func=cmd_meas)

    p = sub.add_parser("monitor", help="连续测量")
    p.add_argument("-n", "--count", type=int, default=5, help="采样次数（默认 5）")
    p.add_argument("-d", "--duration", type=float, default=None, help="持续时长（秒，给出后忽略 -n 的上限）")
    p.add_argument("--interval", type=float, default=0.0, help="两次采样之间的间隔（秒）")
    p.add_argument("--stream", action="store_true", help="用流模式（STREAM 通知）而不是轮询")
    p.add_argument("--interval-ms", type=int, default=P.DEFAULT_STREAM_INTERVAL_MS, help="流模式上报周期")
    p.add_argument("--csv", metavar="PATH", default=None, help="把样本写成 CSV")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("mode", help="SET_MODE")
    p.add_argument("mode", choices=("single", "continuous"), help="测量模式")
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser("source", help="SET_SOURCE")
    p.add_argument("source", choices=("ext", "int"), help="输入源")
    p.add_argument("--wait-ready", action="store_true", help="等 sourceReady 置位")
    p.add_argument("--ready-timeout", type=float, default=2.0, help="等 sourceReady 的超时")
    p.set_defaults(func=cmd_source)

    p = sub.add_parser("sig", help="SET_SIG / GET_SIG：片内源频率与相位")
    p.add_argument("--freq", metavar="HZ", default=None, help="目标频率（支持 25e6 / 25MHz）")
    p.add_argument("--phase", metavar="DEG", default=None, help="目标相位差（度）")
    p.add_argument("--dry-run", action="store_true", help="只算不下发")
    p.set_defaults(func=cmd_sig)

    p = sub.add_parser("gate", help="SET_GATE：闸门目标")
    p.add_argument("target", type=int, help="目标闸门计数（低速周期数）")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("stream", help="STREAM on|off")
    p.add_argument("state", choices=("on", "off"), help="开关")
    p.add_argument("--interval-ms", type=int, default=P.DEFAULT_STREAM_INTERVAL_MS, help="上报周期（ms）")
    p.add_argument("-n", "--count", type=int, default=5, help="收到多少条通知后退出（on 时）")
    p.add_argument("-d", "--duration", type=float, default=2.0, help="最长等待时间（秒）")
    p.add_argument("--no-autostart", action="store_false", dest="autostart",
                   help="没有有效结果时不要自动切 continuous + START")
    p.set_defaults(func=cmd_stream, autostart=True)

    p = sub.add_parser("clear", help="CLEAR：复位结果区与标志")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("reset", help="RESET_COUNTERS：复位统计计数")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("scan", help="多频点 × 多相位扫描并输出 CSV")
    p.add_argument("--freqs", required=True, metavar="LIST", help="逗号分隔的频率，如 5e6,6.25e6,25e6")
    p.add_argument("--phases", default="0", metavar="LIST", help="逗号分隔的相位（度），如 0,45,90")
    p.add_argument("--out", metavar="PATH", default=None, help="CSV 输出路径")
    p.add_argument("--gate", type=int, default=None, help="扫描前设置闸门目标")
    p.add_argument("--source", choices=("ext", "int"), default="int", help="扫描用的输入源（默认片内）")
    p.add_argument("--settle", type=float, default=0.0, help="SET_SIG 后额外等待（秒）")
    p.add_argument("--echo-rows", type=int, default=None, help="stdout 回显的行数（默认 8）")
    p.add_argument("--no-print-plan", action="store_false", dest="print_plan", help="不打印扫描计划")
    p.set_defaults(func=cmd_scan, print_plan=True)

    p = sub.add_parser("sigtable", help="打印相位字段语义与冻结频点表（离线）")
    p.set_defaults(func=cmd_sigtable)

    p = sub.add_parser("gui", help="启动 PySide6 图形界面")
    # 这三个选项在 `gui` 前后都能写（子解析器用 SUPPRESS，避免默认值覆盖主解析器的值）
    p.add_argument("--port", metavar="PORT", default=argparse.SUPPRESS, help="串口或 pty 路径（启动后自动连接）")
    p.add_argument("--baud", type=int, default=argparse.SUPPRESS, help="波特率")
    p.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="单次请求超时（秒）")
    p.add_argument("--gui-args", nargs=argparse.REMAINDER, default=argparse.SUPPRESS, dest="gui_args",
                   help="额外透传给 GUI 的参数（放在 -- 之后）")
    p.set_defaults(func=cmd_gui)
    return ap


#: 不需要串口的子命令。
OFFLINE_COMMANDS = {"sigtable", "gui"}


def main(argv: Sequence[str] | None = None) -> int:
    """`python3 -m dpm_host` 入口，返回进程退出码。"""
    args = build_parser().parse_args(argv)
    if not getattr(args, "cmd", None):
        build_parser().print_help()
        return EXIT_USAGE
    offline = args.cmd in OFFLINE_COMMANDS
    client: DpmClient | None = None
    try:
        if not offline:
            client = open_client(args)
        return int(args.func(args, client))
    except DpmTimeoutError as exc:
        print(f"超时：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except DpmError as exc:
        print(f"设备错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"串口不存在：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        print("已中断", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # 串口异常/协议异常统一给出可读提示
        name = type(exc).__name__
        if name in ("SerialException", "ValueError"):
            print(f"打开/使用串口失败：{exc}", file=sys.stderr)
            return EXIT_ERROR
        raise
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
