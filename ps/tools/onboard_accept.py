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
  6. 测量扫描：10 个频点 × **逐频点相位点**（含 >180° 的用例）= 80 点，逐点判频率与相位；
  7. 汇总：写 build/onboard_acceptance.json 与 .md，打印 PASS/FAIL，失败时退出码非 0。

判据（相位期望按 **MMCM 的移相精度**量化，不是按 DSP 采样网格量化）：

  * 频率：|f_meas − f_nom| ≤ max(1kHz, f_nom/1000, 2·f_nom/edgeCnt)；
  * 相位：先把相位字（`SET_SIG` 回显的 `phaseMilliDeg`）按 MMCM 的移相精度量化，得到
    **MMCM 实际输出相位差**——MMCM 的相位最小步长是 1/8 VCO 周期（VCO=1GHz 时 125ps），
    折算到输出时钟周期就是 `45°/D`（D = 回显的分频比；D=125 → 0.36°，D=40 → 1.125°）：

        exp = round(相位字 / (45°/D)) × (45°/D)

    再要求 |实测 − exp|（取模 360 的最小差）**不大于 1 个 DSP 量化步长**
    `360·f_sig/f_dsp`：1 个 DSP 周期（450MHz → 2.222ns）是测量端的分辨率下界
    （相位计数只能是整数），因此这一条实际上是在问「实测有没有落在 MMCM 实给相位的
    1 个计数范围内」。另加 1 毫度吸收定点取整（协议相位分辨率）。

    注意口径：DSP 量化不再是**期望值**的一部分，而是**容限**；
  * 计数自检：gateCnt ∈ [target, target+ceil(f_slow/f_sig)+5]，
    edgeCnt 与 gateCnt·f_sig/f_slow 相差 ≤ 1。

相位上界（本次新增 >180° 用例的由来）：MMCM 的相位码 `m = round(相位 × D / 45000)`
只有 9 位（DELAY_TIME 6 位 + PHASE_MUX 3 位），因此可请求的相位上界是
`min(23017499/D, 359999)` 毫度——D=35（30MHz）覆盖整个 0~360°，D=125（6MHz）
只能到 184.139°。越界请求会被固件回 `ERR_PARAM`（字段截断会让相位**回绕**）。
每个频点的相位点由 `phases_for()` 生成，规则与 `spec/gen_golden.py::phase_points_for`
一致：0/45/90/135/180 必测，D 为偶数时加 22.5°，225/270/315 按相位码裁剪，
一个都放不下时改用相位码上界点（m = 511）兜底。

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
from dpm_host.sig import (  # noqa: E402
    FULL_SCALE_MILLI_DEG,
    mmcm_phase_grid_milli_deg,
    phase_code_fits,
    phase_code_top_milli_deg,
    phase_code_value,
    solve_sig,
)

# 冻结验证频点（与 spec/gen_golden.py::FREQ_POINTS_HZ、docs/ps-protocol.md §6.4 一致）：
# 450MHz = 9×50MHz，取 5~50MHz 内能整除 450MHz 的整数 MHz 频点，10 个频点全部由求解
# 规则精确解出，覆盖 VCO 600/750/900/1000/1050MHz、MULT 12/15/18/20/21 与
# D = 20/35/40/50/70/100/120/125。此时 `f_dsp/f_sig` 为整数，每个信号周期的相位计数
# 窗口都是同一个整数，`phaseCnt % edgeCnt == 0` 严格成立。450MHz 下多数信号周期不再是
# 整数 ps（6/9/15/18/30/45MHz），金标准改用有理数精确运算，不再要求整数 ps 周期。
FREQS = [5_000_000, 6_000_000, 9_000_000, 10_000_000, 15_000_000, 18_000_000, 25_000_000, 30_000_000, 45_000_000, 50_000_000]
SLOW_HZ = 50_000_000
DSP_HZ = 450_000_000
# 闸门时间 (s)
GATE_TIME = 0.1
# 闸门计数
GATE = round(GATE_TIME * SLOW_HZ)


def phases_for(freq_hz: int) -> list[int]:
    """该频点要测的相位点（毫度）：与 `spec/gen_golden.py::phase_points_for` 同一规则。

    0/45/90/135/180 必测（180° 的相位码是 4D ≤ 504，所有合法 D 都能表示）；
    D 为偶数时加 22.5°（需要 PHASE_MUX 细调）；225/270/315 中相位码 ≤ 511 的才可测；
    一个 >180° 的 45° 倍数都放不下时（D ≥ 103），用相位码上界点（m = 511）兜底，
    保证每个频点都有 >180° 的用例。
    """
    div = int(solve_sig(int(freq_hz)).div)
    if div <= 0:
        return []
    points = [0, 45_000, 90_000, 135_000, 180_000]
    if div % 2 == 0:
        points.append(22_500)
    over = [p for p in (225_000, 270_000, 315_000) if phase_code_fits(p, div)]
    if over:
        points.extend(over)
    else:
        points.append(min(phase_code_top_milli_deg(div), FULL_SCALE_MILLI_DEG - 1))
    return sorted(set(points))


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
    # 错误路径（相位上界 = 9 位相位码，按 D 逐点，见 docs/ps-protocol.md §6.2）：
    #   6MHz（D=125）的 200° 需要相位码 556 > 511 → ERR_PARAM（字段截断会让相位回绕）；
    #   360° 是满量程（与 0° 同义）→ ERR_PARAM；
    #   3MHz 低于可实现下限（600/126 ≈ 4.76MHz）→ ERR_PARAM。
    for label, freq, phase in (
        ("相位码越界（6MHz/200°）", 6_000_000, 200_000),
        ("相位满量程（360°）", 25_000_000, 360_000),
        ("频率不可达（3MHz）", 3_000_000, 0),
    ):
        try:
            c.set_sig(freq, phase)
            ok, detail = False, "居然接受了"
        except DpmError as exc:
            ok, detail = exc.code == 0xFE, f"{exc.name}(0x{exc.code:02X})"
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        rep.check(f"错误路径：{label} 回 ERR_PARAM", ok, detail)
    # 反向确认：>180° 的请求现在是**合法**的（旧固件一律拒 ≥180°，比硬件保守得多）
    try:
        info = c.set_sig(25_000_000, 270_000)
        rep.check(
            "正常路径：>180°（25MHz/270°）被接受且回显一致",
            info.phase_milli_deg == 270_000 and info.div == 40,
            f"回显 {info.phase_milli_deg / 1000:g}° D={info.div} phaseField={info.phase_field}",
        )
    except Exception as exc:
        rep.check("正常路径：>180°（25MHz/270°）被接受且回显一致", False, f"{type(exc).__name__}: {exc}")


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
    # 等 3 个**不同**的 seq：流上报的语义是「周期性推送最新结果」（见固件 dpm_cmd.c 的
    # tick 分支），闸门与上报周期都是 100ms 时，两次 tick 之间可能还没出新结果，
    # 于是同一条结果被推两次——所以判据是「单调不减 + 确实在推进」，不是严格递增。
    deadline = time.time() + 6.0
    while time.time() < deadline and len(set(seqs)) < 3:
        time.sleep(0.1)
    c.stream(False)
    c.set_mode(0)
    rep.check("连续模式 + 流上报：收到 ≥3 条 STREAM_DATA", frames >= 3, f"收到 {frames} 条；seq={seqs[:6]}")
    rep.check(
        "流上报 seq 单调不减且推进（≥3 个不同 seq）",
        len(seqs) >= 2 and all(a <= b for a, b in zip(seqs, seqs[1:])) and len(set(seqs)) >= 3,
        f"{seqs[:8]}",
    )


def mmcm_actual_phase_deg(phase_echo_mdeg: int, div: int) -> tuple[float, float, str]:
    """按 MMCM 的移相精度把相位字量化成**实际输出相位差**。

    返回 `(实际相位差[度], 量化网格[度], 说明)`。

    MMCM 的相位最小步长是 1/8 VCO 周期（VCO=1GHz 时 125ps）；折算到输出时钟（周期 = D 个
    VCO 周期）就是 `45°/D`，这也是 DRP 相位字（毫度）实际能表达的最小间隔
    （见 `spec/drp_semantics.json` 的 `phase_hw_quantum_ps`、`docs/ps-protocol.md` §6.2）。

    量化用的是宿主机参考实现 `host/dpm_host/sig.py::phase_code_value`（与
    `ps/src/dpm_sig.c::dpm_sig_solve` 同一套冻结规则），本脚本不另写一份公式。
    D 非法时抛 `ValueError`（`SET_SIG` 回显不完整，判为失败点）。
    """
    if int(div) <= 0:
        raise ValueError(f"SET_SIG 回显的分频比 D 非法（{div}）")
    _code, actual_mdeg, _grid_mdeg, notes = phase_code_value(int(phase_echo_mdeg), int(div))
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
    n_total = 0
    n_over180 = 0
    per_freq: dict[int, list[int]] = {f: phases_for(f) for f in FREQS}
    for f in FREQS:
        for pmdeg in per_freq[f]:
            n_total += 1
            n_over180 += 1 if pmdeg >= 180_000 else 0
            pdeg = pmdeg / 1000.0
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
                rep.check(f"{f/1e6:g}MHz/{pdeg:g}° 测量完成", False, f"{type(exc).__name__}: {exc}")
                continue
            f_meas = local.freq_hz  # 本地由计数换算，避免对端字段口径差异
            ph_meas = local.phase_milli_deg / 1000.0
            # 相位容限 = 测量端的量化下界：1 个 DSP 周期（相位计数只能是整数）对应的度数，
            # 另加 1 毫度（协议相位分辨率）吸收定点取整。DSP 量化在这里是**容限**不是期望值。
            f_actual = info.freq_hz if info.freq_hz else f
            quantum = 360.0 * f_actual / DSP_HZ
            err = abs((ph_meas - exp_deg + 180.0) % 360.0 - 180.0)
            err_steps = err / quantum if quantum > 0.0 else 0.0
            # 频率判据：计数粒度决定分辨率（1 个信号计数 = f/edgeCnt）。
            f_tol = max(1_000.0, f / 1000.0, 2.0 * f / max(meas.edge_cnt, 1))
            # 计数自检（与 xsim 联调激励同一口径）：
            #   * 闸门窗口在 sig2 的下一个上升沿关闭，跨时钟域同步还有几拍延迟，
            #     因此 gate ∈ [GATE, GATE + ceil(T_sig/T_slow) + 5]；
            #   * 相位计的 DSP 测量窗口还与闸门计数窗口差几拍（CDC + 启动对齐），
            #     折算成参考沿数是 f/f_slow 的比例，所以 edge 只能按闸门**区间**折算
            #     再放宽 1 个沿——按实测 gate 反推（`abs(edge - gate×f/f_slow) <= 1`）
            #     在高频点会误判：45MHz 时 1 个信号周期 ≈ 1.11 个慢时钟，实测偏差 2 个沿
            #     仍在正常范围内。
            gate_lo = GATE
            gate_hi = GATE + int(SLOW_HZ / max(f, 1)) + 5
            edge_lo = int(gate_lo * f / SLOW_HZ) - 1
            edge_hi = int(gate_hi * f / SLOW_HZ) + 1
            ok_f = abs(f_meas - f) <= f_tol
            ok_p = err <= quantum + 1e-3
            ok_c = gate_lo <= meas.gate_cnt <= gate_hi and edge_lo <= meas.edge_cnt <= edge_hi
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
                f"{f/1e6:g}MHz/{pdeg:g}° 频率+相位+计数",
                ok,
                f"f={f_meas/1e6:.6f}MHz(±{f_tol/1e3:.0f}kHz) φ={ph_meas:.3f}° "
                f"MMCM期望={exp_deg:.3f}°(1/8VCO网格{mmcm_grid:.3f}°,D={info.div}) "
                f"err={err:.3f}°≤{quantum:.3f}°({err_steps:.2f}步) "
                f"gate={meas.gate_cnt} edge={meas.edge_cnt}" + extra,
            )
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rep.check(
        f"扫描 {n_total} 点（{len(FREQS)} 个频点，其中 ≥180° 的 {n_over180} 点）全部通过",
        n_ok == n_total,
        f"通过 {n_ok}/{n_total}；明细见 {csv_path}",
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
