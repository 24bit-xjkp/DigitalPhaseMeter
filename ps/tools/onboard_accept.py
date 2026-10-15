#!/usr/bin/env python3
"""片上（上板）验收：一条命令跑完并给出 PASS/FAIL 与证据报告。

    python3 ps/tools/onboard_accept.py --port /dev/ttyUSB0            # 含 JTAG 冷启动
    python3 ps/tools/onboard_accept.py --port /dev/ttyUSB0 --no-boot  # 板子已在跑，只做验收

流程（每一步都记进报告，失败也会写出已完成部分的证据）：

  1. 前置检查：bitstream / 裸机镜像 / 串口 / xsdb 是否就绪；
  2. JTAG 冷启动：`ps/tools/jtag_boot.tcl`（复位停核 → 配 PL → ps7_init → 下载到 OCM → 运行）；
  3. 串口链路：等上电横幅，核对波特率与固件自报的 UART 参考时钟；
  4. 协议层：PING / IDENTIFY / GET_STATUS / SET_GATE / SET_MODE / SET_SOURCE 正常路径，
     以及 ERR_PARAM 错误路径（非法相位、非法频率）；
  5. 连续模式 + 流上报：至少收到 3 条 STREAM_DATA 且 seq 递增；
  6. 测量扫描：8 频点 × 4 相位 = 32 点，逐点判频率与相位；
  7. 汇总：写 build/onboard_acceptance.json 与 .md，打印 PASS/FAIL，失败时退出码非 0。

判据（相位期望按 **MMCM 的移相精度**量化，不是按 DSP 采样网格量化）：

  * 频率：|f_meas − f_nom| ≤ max(1kHz, f_nom/1000, 2·f_nom/edgeCnt)；
  * 相位：先把相位字（`SET_SIG` 回显的 `phaseMilliDeg`）按 MMCM 的移相精度量化，得到
    **MMCM 实际输出相位差**——MMCM 的相位最小步长是 1/8 VCO 周期（VCO=1GHz 时 125ps），
    折算到输出时钟周期就是 `45°/D`（D = 回显的分频比；D=125 → 0.36°，D=40 → 1.125°）：

        exp = round(相位字 / (45°/D)) × (45°/D)

    再要求 |实测 − exp|（取模 360 的最小差）**不大于 1 个 DSP 量化步长**
    `360·f_sig/f_dsp`：1 个 DSP 周期（400MHz → 2.5ns）是测量端的分辨率下界
    （相位计数只能是整数），因此这一条实际上是在问「实测有没有落在 MMCM 实给相位的
    1 个计数范围内」。另加 1 毫度吸收定点取整（协议相位分辨率）。

    注意口径：DSP 量化不再是**期望值**的一部分，而是**容限**；
  * 计数自检：gateCnt ∈ [target, target+ceil(f_slow/f_sig)+5]，
    edgeCnt 与 gateCnt·f_sig/f_slow 相差 ≤ 1。

MMCM 相位模型复用宿主机参考实现 `host/dpm_host/sig.py`（与 `ps/src/dpm_sig.c` 同一套
冻结规则），本脚本不另写一份公式，避免两边漂移。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 上位机是纯 Python 包：装了（.venv / pip install -e）就用装好的，没装也能直接跑
if (ROOT / "host").is_dir():
    sys.path.insert(0, str(ROOT / "host"))

from dpm_host.client import DpmClient  # noqa: E402
from dpm_host.protocol import Cmd, DpmError, DpmTimeoutError  # noqa: E402
from dpm_host.sig import mmcm_phase_grid_milli_deg, phase_word_value  # noqa: E402

FREQS = [5_000_000, 6_250_000, 8_000_000, 10_000_000, 12_500_000, 20_000_000, 25_000_000, 30_000_000]
PHASES_DEG = [0, 45, 90, 135]
SLOW_HZ = 50_000_000
DSP_HZ = 400_000_000
# 闸门时间 (s)
GATE_TIME = 0.1
# 闸门计数
GATE = round(GATE_TIME * SLOW_HZ)


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.rows: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.items.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
        return bool(ok)

    @property
    def failed(self) -> list[dict]:
        return [i for i in self.items if not i["ok"]]


def preflight(rep: Report, args: argparse.Namespace) -> None:
    bit = ROOT / "build/vivado/bd_proj/dpm_bd.runs/impl_1/dpm_system_wrapper.bit"
    bin_ = ROOT / "ps/build/dpm_ps.bin"
    rep.check("bitstream 存在（先跑 vivado/create_bd.tcl + build_bitstream.tcl）", bit.exists(), str(bit))
    rep.check("裸机镜像存在（先跑 bash ps/build.sh --ps-only）", bin_.exists(), str(bin_))
    rep.check("串口设备存在", Path(args.port).exists(), args.port)
    if not args.no_boot:
        xsdb = os.environ.get("XSDB", str(Path.home() / "Vivado/2025.2/Vivado/bin/xsdb"))
        rep.check("xsdb 可用（JTAG 电缆需装好 udev 规则，见文档）", shutil.which(xsdb) is not None or Path(xsdb).exists(), xsdb)


def jtag_boot(rep: Report, args: argparse.Namespace) -> None:
    xsdb = os.environ.get("XSDB", str(Path.home() / "Vivado/2025.2/Vivado/bin/xsdb"))
    tcl = ROOT / "ps/tools/jtag_boot.tcl"
    print(f"== JTAG 冷启动：{xsdb} -quiet {tcl}", flush=True)
    try:
        p = subprocess.run([xsdb, "-quiet", str(tcl)], capture_output=True, text=True, timeout=args.boot_timeout)
    except subprocess.TimeoutExpired:
        rep.check("JTAG 冷启动", False, f"超时 {args.boot_timeout}s")
        return
    out = (p.stdout or "") + (p.stderr or "")
    (ROOT / "build/onboard_boot.log").write_text(out, encoding="utf-8")
    ok = "BOOT7_DONE" in out and "rror" not in out.split("BOOT7_DONE")[0][-400:]
    rep.check("JTAG 冷启动（复位停核 → 配 PL → ps7_init → 下载 → 运行）", ok, "见 build/onboard_boot.log" if not ok else "")


def check_link(rep: Report, args: argparse.Namespace) -> bool:
    """确认串口链路与波特率：先 PING（对已运行很久、横幅已停发的板子同样有效），
    对刚冷启动的板子再看一眼上电横幅里的固件自报参数。"""
    import serial
    from dpm_host.protocol import encode_frame, parse_stream

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.2)
    except Exception as exc:  # pragma: no cover
        rep.check(f"打开串口 {args.port}@{args.baud}", False, str(exc))
        return False
    try:
        buf = b""
        banner = ""
        deadline = time.time() + args.banner_timeout
        while time.time() < deadline and not banner:
            buf += ser.read(4096)
            for part in buf.split(b"\r\n"):
                if b"DPM-PS ready" in part:
                    banner = part.decode("utf-8", "replace")
        for _ in range(3):
            ser.write(encode_frame(Cmd.PING))
            time.sleep(0.05)
        t0 = time.time()
        while time.time() - t0 < 1.5:
            buf += ser.read(4096)
        frames, _errs = parse_stream(buf)
        pong = any(int(f.cmd) == int(Cmd.PING.response) for f in frames)
        ok = rep.check(
            f"串口链路 @{args.baud}：PING 有响应",
            pong,
            "" if pong else f"收到 {len(buf)} 字节、无有效响应帧；" f"用 ps/tests/find_uart_baud.py 重测波特率",
        )
        if banner:
            rep.check("上电横幅可读（固件自报波特率/参考时钟）", True, banner)
        else:
            print("[INFO] 未看到上电横幅：程序收到过有效帧后会停发横幅，属正常（" "刚冷启动时才看得到）")
        return ok
    finally:
        ser.close()


def protocol_checks(rep: Report, c: DpmClient) -> None:
    rep.check("PING 有响应", c.ping() is not None)
    ident = c.identify()
    rep.check(
        "IDENTIFY 返回 DSP/慢时钟与 PL 侧一致",
        ident.dsp_clk_hz == DSP_HZ and ident.slow_clk_hz == SLOW_HZ,
        f"dsp={ident.dsp_clk_hz} slow={ident.slow_clk_hz}",
    )
    st = c.get_status()
    rep.check("GET_STATUS 可读且此刻不忙", not st.busy, f"status=0x{st.status:x}")
    for label, freq, phase in (("相位 ≥180°", 25_000_000, 180_000), ("频率不可达（3MHz）", 3_000_000, 0)):
        try:
            c.set_sig(freq, phase)
            ok, detail = False, "居然接受了"
        except DpmError as exc:
            ok, detail = exc.code == 0xFE, f"{exc.name}(0x{exc.code:02X})"
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        rep.check(f"错误路径：{label} 回 ERR_PARAM", ok, detail)


def stream_check(rep: Report, c: DpmClient) -> None:
    """连续模式 + 流上报：客户端把 STREAM_DATA 交给 `on_stream_data` 回调。

    回调参数是 `dpm_host.protocol.StreamData`，**seq 在 `data.meas.seq` 里**
    （通知 = GET_MEAS 的字段 + lostDelta，见协议 §5）。这里分别统计「收到几条帧」与
    「读到几个 seq」：帧数用回调计数，避免字段名写错时把「有帧但读不到 seq」误报成
    「一条都没收到」（这正是本脚本早先版本踩过的坑）。
    """
    frames = 0
    seqs: list[int] = []

    def on_stream_data(data: object) -> None:
        nonlocal frames
        frames += 1
        seq = getattr(getattr(data, "meas", None), "seq", None)
        if isinstance(seq, int):
            seqs.append(seq)

    c.on_stream_data = on_stream_data
    c.set_source(1)  # 片内源
    c.set_sig(20_000_000, 90_000)  # 连续模式下必须有信号可测，否则不会有 STREAM_DATA
    c.clear()  # 清掉上一轮遗留的忙状态/结果，否则 START 会回 ERR_BUSY
    time.sleep(0.1)
    c.set_mode(1)  # 连续模式
    c.stream(True, 100)  # 流上报周期 100ms
    if not c.start():  # 连续模式也要先启动一次
        c.clear()
        time.sleep(0.2)
        c.start()
    deadline = time.time() + 4.0
    while time.time() < deadline and frames < 3:
        time.sleep(0.1)
    c.stream(False)
    c.set_mode(0)
    rep.check("连续模式 + 流上报：收到 ≥3 条 STREAM_DATA", frames >= 3, f"收到 {frames} 条；seq={seqs[:6]}")
    rep.check("流上报 seq 递增", len(seqs) >= 2 and all(a < b for a, b in zip(seqs, seqs[1:])), str(seqs[:8]))


def mmcm_actual_phase_deg(phase_word_mdeg: int, div: int) -> tuple[float, float, str]:
    """按 MMCM 的移相精度把相位字量化成**实际输出相位差**。

    返回 `(实际相位差[度], 量化网格[度], 说明)`。

    MMCM 的相位最小步长是 1/8 VCO 周期（VCO=1GHz 时 125ps）；折算到输出时钟（周期 = D 个
    VCO 周期）就是 `45°/D`，这也是 DRP 相位字（毫度）实际能表达的最小间隔
    （见 `spec/drp_semantics.json` 的 `phase_hw_quantum_ps`、`docs/ps-protocol.md` §6.2）。

    量化用的是宿主机参考实现 `host/dpm_host/sig.py::phase_word_value`（与
    `ps/src/dpm_sig.c::dpm_sig_solve` 同一套冻结规则），本脚本不另写一份公式。
    D 非法时抛 `ValueError`（`SET_SIG` 回显不完整，判为失败点）。
    """
    if int(div) <= 0:
        raise ValueError(f"SET_SIG 回显的分频比 D 非法（{div}）")
    _word, actual_mdeg, _grid_mdeg, notes = phase_word_value(int(phase_word_mdeg), int(div))
    grid_deg = float(mmcm_phase_grid_milli_deg(int(div))) / 1000.0
    return int(actual_mdeg) / 1000.0, grid_deg, "；".join(notes)


def sweep(rep: Report, c: DpmClient, csv_path: Path) -> None:
    c.set_source(1)  # 片内源
    c.set_gate(GATE)
    lines = [
        "freq_nominal,phase_nominal,freq_actual,div,vco_hz,phase_word_mdeg,"
        "freq_meas,phase_meas_deg,gate_cnt,edge_cnt,phase_cnt,"
        "exp_deg,mmcm_grid_deg,dsp_quantum_deg,err_deg,err_over_quantum,ok"
    ]
    n_ok = 0
    for f in FREQS:
        for pdeg in PHASES_DEG:
            pmdeg = pdeg * 1000
            try:
                info = c.set_sig(f, pmdeg)
                # 相位期望 = MMCM 实际输出相位差：相位字按 1/8 VCO 周期（= 45°/D）网格量化。
                # 用 SET_SIG 回显的相位字（而不是请求值）作为输入：固件若对相位字做了吸附，
                # 输出的就是吸附后的值，验收要判的是「测量 ↔ MMCM 实给相位」这一环。
                exp_deg, mmcm_grid, mmcm_note = mmcm_actual_phase_deg(info.phase_milli_deg, info.div)
                time.sleep(0.1)
                _, meas, local = c.measure_once(timeout=3.0)
            except (DpmError, DpmTimeoutError, ValueError) as exc:
                lines.append(",".join([str(f), str(pdeg)] + [""] * 14 + ["-1"]))
                rep.check(f"{f/1e6:g}MHz/{pdeg}° 测量完成", False, f"{type(exc).__name__}: {exc}")
                continue
            f_meas = local.freq_hz  # 本地由计数换算，避免对端字段口径差异
            ph_meas = local.phase_milli_deg / 1000.0
            # 相位容限 = 测量端的量化下界：1 个 DSP 周期（相位计数只能是整数）对应的度数，
            # 另加 1 毫度（协议相位分辨率）吸收定点取整。DSP 量化在这里是**容限**不是期望值。
            f_actual = info.freq_hz if info.freq_hz else f
            quantum = 360.0 * f_actual / DSP_HZ
            err = abs((ph_meas - exp_deg + 180.0) % 360.0 - 180.0)
            err_steps = err / quantum if quantum > 0.0 else 0.0
            # 频率判据：计数粒度决定分辨率（1 个信号计数 = f/edgeCnt），
            # 另加一条更紧的"edgeCnt 与理想值差 ≤1 计数"的检查。
            f_tol = max(1_000.0, f / 1000.0, 2.0 * f / max(meas.edge_cnt, 1))
            ideal_edge = round(meas.gate_cnt * f / SLOW_HZ)
            ok_f = abs(f_meas - f) <= f_tol
            ok_p = err <= quantum + 1e-3
            # 闸门窗口在 sig2 的下一个上升沿关闭，跨时钟域同步还有几拍延迟，
            # 因此上界给 target + ceil(T_sig/T_slow) + 5 个慢时钟周期的余量。
            ok_c = GATE <= meas.gate_cnt <= GATE + int(SLOW_HZ / max(f, 1)) + 5 and abs(meas.edge_cnt - ideal_edge) <= 1
            ok = ok_f and ok_p and ok_c
            n_ok += 1 if ok else 0
            lines.append(
                f"{f},{pdeg},{f_actual},{info.div},{info.vco_hz},{info.phase_milli_deg},"
                f"{f_meas},{ph_meas:.3f},{meas.gate_cnt},{meas.edge_cnt},{meas.phase_cnt},"
                f"{exp_deg:.3f},{mmcm_grid:.3f},{quantum:.3f},{err:.3f},{err_steps:.2f},{ok}"
            )
            extra = ""
            if info.phase_milli_deg != pmdeg:
                extra += f"；相位字被吸附 请求{pmdeg/1000:g}°→{info.phase_milli_deg/1000:.3f}°"
            if mmcm_note:
                extra += f"；{mmcm_note}"
            rep.check(
                f"{f/1e6:g}MHz/{pdeg}° 频率+相位+计数",
                ok,
                f"f={f_meas/1e6:.6f}MHz(±{f_tol/1e3:.0f}kHz) φ={ph_meas:.3f}° "
                f"MMCM期望={exp_deg:.3f}°(1/8VCO网格{mmcm_grid:.3f}°,D={info.div}) "
                f"err={err:.3f}°≤{quantum:.3f}°({err_steps:.2f}步) "
                f"gate={meas.gate_cnt} edge={meas.edge_cnt}" + extra,
            )
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rep.check(
        f"扫描 {len(FREQS)*len(PHASES_DEG)} 点全部通过",
        n_ok == len(FREQS) * len(PHASES_DEG),
        f"通过 {n_ok}/{len(FREQS)*len(PHASES_DEG)}；明细见 {csv_path}",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="片上验收（AX7010/串口 + JTAG）")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--no-boot", action="store_true", help="板子已在跑，跳过 JTAG 冷启动")
    ap.add_argument("--boot-timeout", type=float, default=300.0)
    ap.add_argument("--banner-timeout", type=float, default=6.0)
    ap.add_argument("--outdir", default=str(ROOT / "build"))
    args = ap.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    rep = Report()
    preflight(rep, args)
    if rep.failed:
        print("前置检查失败，先补齐再验收。")
        return 2
    if not args.no_boot:
        jtag_boot(rep, args)
    if not check_link(rep, args):
        (outdir / "onboard_acceptance.json").write_text(json.dumps({"items": rep.items}, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    c = DpmClient(port=args.port, baud=args.baud, timeout=1.5, retries=2)
    try:
        protocol_checks(rep, c)
        try:
            stream_check(rep, c)
        except Exception as exc:
            rep.check("连续模式 + 流上报", False, f"{type(exc).__name__}: {exc}")
        sweep(rep, c, outdir / "onboard_acceptance.csv")
    finally:
        c.close()

    ok = not rep.failed
    (outdir / "onboard_acceptance.json").write_text(
        json.dumps(
            {"verdict": "PASS" if ok else "FAIL", "items": rep.items, "sweep_csv": str(outdir / "onboard_acceptance.csv")},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md = [
        "# 片上验收报告",
        "",
        f"- 结论：**{'PASS' if ok else 'FAIL'}**",
        f"- 串口：`{args.port}@{args.baud}`；JTAG 冷启动：{'跳过' if args.no_boot else '已执行'}",
        f"- 检查项：{len(rep.items)} 项，失败 {len(rep.failed)} 项",
        "",
    ]
    md += [f"- [{'x' if i['ok'] else ' '}] {i['name']}" + (f" — {i['detail']}" if i["detail"] else "") for i in rep.items]
    (outdir / "onboard_acceptance.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"\n==== 片上验收：{'PASS' if ok else 'FAIL'} " f"（{len(rep.items)-len(rep.failed)}/{len(rep.items)} 项通过）====")
    print(f"报告：{outdir/'onboard_acceptance.md'} / {outdir/'onboard_acceptance.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
