#!/usr/bin/env python3
"""端到端扫描验证：PS 侧 C 服务端（ps/build/host_main）+ 仿真 PL 后端 + 本脚本的独立协议客户端。

设计要点（见 `docs/verify-plan.md`）：

- **不 import host/ 的代码**：本脚本自带最小的协议实现（`spec/lib/ptl.py`），
  与 `host/dpm_host/protocol.py`、`ps/src/dpm_proto.c` 构成三方独立实现，
  三方一致才算协议正确（`--use-host-client` 可额外用上位机客户端复跑一遍）。
- 串口是**真正的 pty**：脚本 `pty.openpty()` 建一对，slave 路径交给 C 服务端，
  master 端由本脚本收发，因此串口收发路径、超时、粘包/拆包都被真实覆盖。
- 期望值来自 `spec/golden/vectors.json`（独立参考实现 `spec/gen_golden.py` 生成）。

用法：
    python3 spec/validate_sweep.py --server ps/build/host_main
    python3 spec/validate_sweep.py --server ps/build/host_main --repeat 3 --out spec/verify_results.json
    python3 spec/validate_sweep.py --server "python3 -m dpm_host.sim_server --device {device}"
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import time
import tty

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ------------------------------------------------------------------ 协议实现

SOF = 0xA5
CRC16_INIT = 0xFFFF
CRC16_POLY_REFLECTED = 0xA001

CMD_PING = 0x01
CMD_IDENTIFY = 0x02
CMD_GET_STATUS = 0x03
CMD_START = 0x04
CMD_GET_RESULT = 0x05
CMD_GET_MEAS = 0x06
CMD_SET_GATE = 0x07
CMD_SET_MODE = 0x08
CMD_SET_SOURCE = 0x09
CMD_SET_SIG = 0x0A
CMD_GET_SIG = 0x0B
CMD_CLEAR = 0x0C
CMD_STREAM = 0x0D
CMD_RESET_COUNTERS = 0x0E

RESP_ERR_PARAM = 0xFE
RESP_ERR_BUSY = 0xFF

STATUS_BUSY = 1 << 0
STATUS_RESULT_VALID = 1 << 1
STATUS_LOST = 1 << 2
STATUS_IRQ_PENDING = 1 << 3

F_SLOW_HZ = 50_000_000
F_DSP_HZ = 400_000_000

SIGST_ENABLE = 1 << 0
SIGST_SELECT = 1 << 1
SIGST_LOCKED = 1 << 2
SIGST_READY = 1 << 3


def crc16(data: bytes) -> int:
    crc = CRC16_INIT
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ CRC16_POLY_REFLECTED
            else:
                crc >>= 1
    return crc & 0xFFFF


def round_half_even(num: int, den: int) -> int:
    """整数 RNE（round-half-to-even），与金标准/仿真的相位量化一致。"""
    q, r = divmod(num, den)
    twice = 2 * r
    return q + 1 if (twice > den or (twice == den and (q & 1))) else q


def encode(cmd: int, payload: bytes = b"") -> bytes:
    body = bytes([cmd]) + payload
    assert 1 <= len(body) <= 64, "LEN 必须在 1..64"
    head = bytes([SOF, len(body)]) + body
    crc = crc16(head[1:])
    return head + struct.pack("<H", crc)


class ProtocolError(RuntimeError):
    pass


class BusyError(RuntimeError):
    pass


class ParamError(RuntimeError):
    pass


class SerialLink:
    """基于 pty(master fd) 的最小串口客户端：带超时的请求-响应。"""

    def __init__(self, fd: int, timeout: float = 2.0):
        self.fd = fd
        self.timeout = timeout
        self.rx = bytearray()
        self.notifications: list[tuple[int, bytes]] = []
        self.stats = {"sent": 0, "recv": 0, "crc_err": 0, "dropped": 0, "timeouts": 0}

    # ---- 低层 ----
    def write(self, data: bytes) -> None:
        while data:
            n = os.write(self.fd, data)
            data = data[n:]
        self.stats["sent"] += 1

    def _read_some(self, deadline: float) -> None:
        remain = deadline - time.monotonic()
        if remain <= 0:
            return
        r, _, _ = select.select([self.fd], [], [], remain)
        if not r:
            return
        try:
            chunk = os.read(self.fd, 4096)
        except OSError:
            return
        if chunk:
            self.rx.extend(chunk)

    def _extract(self) -> tuple[int, bytes] | None:
        """从缓冲里取出一帧；返回 (cmd, payload) 或 None（需要更多数据）。"""
        while True:
            if not self.rx:
                return None
            if self.rx[0] != SOF:
                # 丢弃到下一个 SOF
                idx = self.rx.find(bytes([SOF]), 1)
                dropped = len(self.rx) if idx < 0 else idx
                del self.rx[:dropped]
                self.stats["dropped"] += dropped
                if not self.rx:
                    return None
                continue
            if len(self.rx) < 2:
                return None
            length = self.rx[1]
            if length < 1 or length > 64:
                del self.rx[0]
                self.stats["dropped"] += 1
                continue
            total = 2 + length + 2
            if len(self.rx) < total:
                return None
            frame = bytes(self.rx[:total])
            del self.rx[:total]
            crc_rx = struct.unpack("<H", frame[-2:])[0]
            if crc16(frame[1:-2]) != crc_rx:
                self.stats["crc_err"] += 1
                continue
            self.stats["recv"] += 1
            return frame[2], frame[3:-2]

    def request(self, cmd: int, payload: bytes = b"", timeout: float | None = None) -> bytes:
        """发一帧请求并等响应；把通知帧（0x80|）缓存起来。"""
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        self.write(encode(cmd, payload))
        while True:
            frame = self._extract()
            if frame is None:
                if time.monotonic() >= deadline:
                    self.stats["timeouts"] += 1
                    raise TimeoutError(f"命令 0x{cmd:02X} 超时（{timeout or self.timeout}s）")
                self._read_some(deadline)
                continue
            rcmd, rpayload = frame
            if rcmd == RESP_ERR_PARAM:
                raise ParamError(f"命令 0x{cmd:02X} 返回 ERR_PARAM")
            if rcmd == RESP_ERR_BUSY:
                raise BusyError(f"命令 0x{cmd:02X} 返回 ERR_BUSY")
            if rcmd & 0x80 and rcmd not in (cmd | 0x80,):
                # 通知帧
                self.notifications.append((rcmd, rpayload))
                continue
            if rcmd != (cmd | 0x80):
                raise ProtocolError(f"命令 0x{cmd:02X} 收到不匹配的响应 0x{rcmd:02X}")
            return rpayload

    # ---- 高层命令 ----
    def ping(self) -> None:
        self.request(CMD_PING)

    def identify(self) -> dict:
        p = self.request(CMD_IDENTIFY)
        if len(p) != 30:
            raise ProtocolError(f"IDENTIFY 响应长度 {len(p)} != 30")
        fields = struct.unpack("<IIIIIIIH", p)
        return dict(
            ver=fields[0],
            git_rev=fields[1],
            caps=fields[2],
            version_reg=fields[3],
            caps_reg=fields[4],
            dsp_clk_hz=fields[5],
            slow_clk_hz=fields[6],
            flags=fields[7],
        )

    def status(self) -> dict:
        p = self.request(CMD_GET_STATUS)
        if len(p) != 20:
            raise ProtocolError(f"GET_STATUS 响应长度 {len(p)} != 20")
        st, sigst, gate, lost, seq = struct.unpack("<IIIII", p)
        return dict(status=st, sig_status=sigst, gate_target=gate, lost_count=lost, seq=seq)

    def start(self) -> None:
        self.request(CMD_START)

    def get_result(self) -> dict:
        p = self.request(CMD_GET_RESULT)
        if len(p) != 17:
            raise ProtocolError(f"GET_RESULT 响应长度 {len(p)} != 17")
        valid = p[0]
        seq, gate, edge, phase = struct.unpack("<IIII", p[1:])
        return dict(valid=valid, seq=seq, gate_cnt=gate, edge_cnt=edge, phase_cnt=phase)

    def get_meas(self) -> dict:
        p = self.request(CMD_GET_MEAS)
        if len(p) != 24:
            raise ProtocolError(f"GET_MEAS 响应长度 {len(p)} != 24")
        seq, gate, edge, phase, freq_hz, pmdeg = struct.unpack("<IIIIII", p)
        # 注意：协议字段名是 freqMilliHz，但**单位是 Hz**（毫赫兹会溢出 u32），见 docs/ps-protocol.md
        return dict(seq=seq, gate_cnt=gate, edge_cnt=edge, phase_cnt=phase, freq_hz=freq_hz, phase_milli_deg=pmdeg)

    def set_gate(self, gate: int) -> int:
        return struct.unpack("<I", self.request(CMD_SET_GATE, struct.pack("<I", gate)))[0]

    def set_mode(self, mode: int) -> int:
        return self.request(CMD_SET_MODE, bytes([mode]))[0]

    def set_source(self, source: int) -> int:
        return struct.unpack("<I", self.request(CMD_SET_SOURCE, bytes([source])))[0]

    def set_sig(self, freq_hz: int, phase_mdeg: int) -> dict:
        p = self.request(CMD_SET_SIG, struct.pack("<II", freq_hz, phase_mdeg))
        if len(p) != 15:
            raise ProtocolError(f"SET_SIG 响应长度 {len(p)} != 15")
        f, ph, vco, div, field = struct.unpack("<IIIHB", p[:15])
        return dict(freq_hz=f, phase_mdeg=ph, vco_hz=vco, div=div, phase_field=field)

    def get_sig(self) -> dict:
        p = self.request(CMD_GET_SIG)
        f, ph, vco, div, field = struct.unpack("<IIIHB", p[:15])
        return dict(freq_hz=f, phase_mdeg=ph, vco_hz=vco, div=div, phase_field=field)

    def clear(self) -> None:
        self.request(CMD_CLEAR)

    def stream(self, enable: int, interval_ms: int) -> tuple[int, int]:
        p = self.request(CMD_STREAM, struct.pack("<BH", enable, interval_ms))
        return p[0], struct.unpack("<H", p[1:3])[0]

    def reset_counters(self) -> None:
        self.request(CMD_RESET_COUNTERS)


# ------------------------------------------------------------------ 服务端进程


class ServerProcess:
    """启动被测服务端：{device} 会替换成 pty slave 路径。"""

    def __init__(self, cmdline: str, log_path: str):
        self.master, self.slave = pty.openpty()
        # pty 默认处于 canonical 模式（会回显、按行缓冲、把 0x0D 映射成 0x0A），
        # 二进制帧必须走 raw 模式，否则服务端读不到完整帧、客户端会读到自己的回显。
        for fd in (self.master, self.slave):
            try:
                tty.setraw(fd)
            except OSError:
                pass
        self.slave_name = os.ttyname(self.slave)
        cmd = cmdline.replace("{device}", self.slave_name)
        self.log = open(log_path, "wb")
        self.proc = subprocess.Popen(
            cmd, shell=True, stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
        )
        self.cmdline = cmd
        self.link = SerialLink(self.master)

    def wait_ready(self, timeout: float = 10.0) -> None:
        """等到服务端能回 PING。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"服务端提前退出（退出码 {self.proc.returncode}），见日志")
            try:
                self.link.ping()
                return
            except TimeoutError, ProtocolError:
                time.sleep(0.1)
        raise TimeoutError("服务端在超时时间内未响应 PING")

    def close(self) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.log.close()
        os.close(self.master)
        os.close(self.slave)
        return self.proc.returncode


# ------------------------------------------------------------------- 扫描逻辑


def load_golden(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def wait_idle(link: SerialLink, timeout: float = 5.0) -> dict:
    """轮询 STATUS 直到 busy = 0；返回最后一次状态。"""
    deadline = time.monotonic() + timeout
    st = link.status()
    while st["status"] & STATUS_BUSY:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"测量未在 {timeout}s 内完成（STATUS=0x{st['status']:X}）")
        time.sleep(0.005)
        st = link.status()
    return st


def wait_source_ready(link: SerialLink, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    st = link.status()
    while not (st["sig_status"] & SIGST_READY):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"sourceReady 未在 {timeout}s 内拉高（SIG_STATUS=0x{st['sig_status']:X}）")
        time.sleep(0.005)
        st = link.status()
    return st


def measure_once(link: SerialLink, gate: int, timeout: float = 5.0) -> dict:
    """一次「启动 → 等完成 → 读结果」；返回 GET_MEAS 的字段。

    两个后端（`ps/sim/host_main` 与 `spec/sim_server.py`）都用「AXI 事务推进模型时间」的简化，
    因此 START 之后可能还需要几次事务才让测量状态机走完；这里对 `ERR_BUSY` 做有限重试，
    并要求每次启动前先等到空闲，避免把模型的时间特性误判成协议错误。
    """
    link.set_gate(gate)
    deadline = time.monotonic() + timeout
    started = False
    while not started:
        try:
            link.start()
            started = True
        except BusyError:
            if time.monotonic() >= deadline:
                raise TimeoutError("START 一直被回 ERR_BUSY（等待超时）") from None
            link.status()  # 用一次读推动后端模型时间
            time.sleep(0.002)
    wait_idle(link, timeout)
    return link.get_meas()


def run_point(link: SerialLink, vec: dict, gate: int, repeat: int, timeout: float, phase_controllable: bool, fixed_phase_mdeg: int) -> dict:
    """跑一个测量点：设源 → 确认就绪 → 重复测量 → 汇总。

    相位判据分两种（见 docs/verify-plan.md 第 3 节）：
      - `phase_controllable=True`：与金标准的 DSP 量化期望值比，容限 = 半个量化步长 + 1 毫度；
      - `phase_controllable=False`（当前 bitstream 实测）：相位固定为 `fixed_phase_mdeg`（90°），
        因此只断言「读数落在固定相位 ±1 个 DSP 周期内」，并把请求相位与实测的差异记为
        `phase_not_controllable=True`（这是预期行为，不算失败）。
    """
    got = link.set_sig(vec["freq_hz"], vec["phase_requested_mdeg"])
    if got["freq_hz"] != vec["freq_hz"] or got["vco_hz"] != vec["vco_hz"] or got["div"] != vec["div"]:
        raise ProtocolError(f"SET_SIG 回读不一致：{got} vs 期望 VCO={vec['vco_hz']} D={vec['div']}")
    link.set_source(1)
    wait_source_ready(link)
    link.clear()

    samples = []
    for _ in range(repeat):
        m = measure_once(link, gate, timeout)
        m["phase_cycles"] = (m["phase_cnt"] // m["edge_cnt"]) if m["edge_cnt"] else 0
        m["phase_cycles_exact"] = (m["phase_cnt"] % m["edge_cnt"] == 0) if m["edge_cnt"] else False
        samples.append(m)
        time.sleep(0.002)

    # 频率判据：与标称值比（容限见金标准）
    f_nom = vec["freq_hz"]
    f_err = [abs(smp["freq_hz"] - f_nom) for smp in samples]
    ok_freq = all(e <= vec["freq_tol_milli_hz"] and e <= max(1000, f_nom // 1000) for e in f_err)
    # 相位判据
    if phase_controllable:
        p_exp = vec["phase_milli_deg"]
        p_tol = vec["phase_tol_mdeg"]
        phase_mode = "controllable"
    else:
        # 固定相位也必须先量化到 DSP 采样网格：报告出来的相位是「实际延迟 / 信号周期」，
        # 实际延迟只能是整数个 DSP 周期，因此 8MHz/90° 会报 ~86.4°（12 个周期）
        # 而不是 90°——这是测量网格的量化下界，不是相位计误差。
        t_sig_ps = (vec["div"] * 10**12) // vec["vco_hz"]
        t_dsp_ps = 10**12 // F_DSP_HZ
        cycles = round_half_even(fixed_phase_mdeg * t_sig_ps, 360_000 * t_dsp_ps)
        if vec["gate_cnt"] and vec["edge_cnt"]:
            v_model = (360_000 * (cycles * vec["edge_cnt"])) // vec["gate_cnt"]
            p_exp = (v_model * (F_SLOW_HZ // 1000)) // (F_DSP_HZ // 1000)
        else:
            p_exp = 0
        p_tol = vec["phase_step_mdeg"] // 2 + 1
        phase_mode = "fixed"
    p_err = [abs(smp["phase_milli_deg"] - p_exp) for smp in samples]
    ok_phase = all(e <= p_tol for e in p_err)
    ok_quant = all(smp["phase_cycles_exact"] for smp in samples if smp["edge_cnt"])
    return {
        "freq_hz": vec["freq_hz"],
        "phase_requested_mdeg": vec["phase_requested_mdeg"],
        "vco_hz": vec["vco_hz"],
        "div": vec["div"],
        "expected": {
            k: vec[k]
            for k in (
                "gate_cnt",
                "edge_cnt",
                "phase_cnt",
                "freq_hz",
                "phase_milli_deg",
                "phase_dsp_cycles",
                "phase_step_mdeg",
                "freq_tol_milli_hz",
                "phase_tol_mdeg",
                "phase_quant_error_mdeg",
            )
        },
        "phase_mode": phase_mode,
        "phase_expected_mdeg": p_exp,
        "phase_expected_is_quantized": (phase_mode == "fixed"),
        "expected_freq_milli_hz": vec["freq_hz"],  # 兼容旧字段名（单位是 Hz）
        "expected_phase_milli_deg": p_exp,
        "samples": samples,
        "freq_err_max_hz": max(f_err),
        "phase_err_max_mdeg": max(p_err),
        "pass_freq": ok_freq,
        "pass_phase": ok_phase,
        "pass_quant": ok_quant,
        "pass": ok_freq and ok_phase and ok_quant,
    }


def fmt(v: dict) -> str:
    e = v["expected"]
    s0 = v["samples"][0]
    tag = "" if v["phase_mode"] == "controllable" else "（相位固定）"
    return (
        f"{v['freq_hz']/1e6:>7.4f}MHz 请求φ={v['phase_requested_mdeg']/1000:>6.1f}°{tag} | "
        f"gate={s0['gate_cnt']:>5} edge={s0['edge_cnt']:>5} phaseCnt={s0['phase_cnt']:>7} | "
        f"f={s0['freq_hz']/1e6:>10.4f}MHz (Δ{v['freq_err_max_hz']:>6}Hz≤"
        f"{max(1000, v['freq_hz']//1000)}Hz) | "
        f"φ={s0['phase_milli_deg']/1000:>8.3f}° (Δ{v['phase_err_max_mdeg']/1000:.3f}≤"
        f"{v['expected']['phase_tol_mdeg']/1000:.3f}/实测容限, 期望"
        f"{v['phase_expected_mdeg']/1000:.3f}°) | {'PASS' if v['pass'] else 'FAIL'}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="端到端扫描验证")
    ap.add_argument("--server", default="ps/build/host_main --device {device}", help="服务端命令行，{device} 会替换为 pty slave 路径")
    ap.add_argument("--golden", default=os.path.join(HERE, "golden", "vectors.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "verify_results.json"))
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--gate", type=int, default=0, help="0 = 用金标准里的 target_gate")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--logdir", default="/tmp")
    args = ap.parse_args()

    golden = load_golden(args.golden)
    gate = args.gate or golden["target_gate"]
    log_path = os.path.join(args.logdir, "dpm_validate_server.log")
    srv = ServerProcess(args.server, log_path)
    print(f"服务端：{srv.cmdline}")
    print(f"pty：{srv.slave_name}（服务端日志 {log_path}）")
    result = {
        "server": srv.cmdline,
        "golden": args.golden,
        "gate": gate,
        "repeat": args.repeat,
        "points": [],
        "passed": 0,
        "failed": 0,
        "notes": [],
    }
    rc = 0
    try:
        srv.wait_ready()
        ident = srv.link.identify()
        print(
            f"IDENTIFY：ver=0x{ident['ver']:08X} caps=0x{ident['caps']:08X} "
            f"versionReg=0x{ident['version_reg']:08X} dsp={ident['dsp_clk_hz']/1e6:.0f}MHz "
            f"slow={ident['slow_clk_hz']/1e6:.0f}MHz flags=0x{ident['flags']:X}"
        )
        result["identify"] = ident

        # 相位是否可控：优先读 spec/drp_semantics.json（实测结论），缺省按「不可控」
        semantics_path = os.path.join(ROOT, "spec", "drp_semantics.json")
        phase_controllable = False
        fixed_phase_mdeg = 90_000
        if os.path.exists(semantics_path):
            with open(semantics_path, "r", encoding="utf-8") as fh:
                sem = json.load(fh)
            phase_controllable = bool(sem.get("phase_controllable", False))
            fixed_phase_mdeg = int(sem.get("static_phase_mdeg", 90_000))
        result["phase_controllable"] = phase_controllable
        result["fixed_phase_mdeg"] = fixed_phase_mdeg
        print(
            f"相位可控 = {phase_controllable}"
            + ("" if phase_controllable else f"（DRP 相位字不生效，实测相位固定 {fixed_phase_mdeg/1000:.1f}°）")
        )

        srv.link.set_mode(0)  # 单次模式
        srv.link.stream(0, 100)  # 关流
        for vec in golden["vectors"]:
            try:
                pt = run_point(srv.link, vec, gate, args.repeat, args.timeout, phase_controllable, fixed_phase_mdeg)
            except (TimeoutError, ProtocolError, BusyError, ParamError) as exc:
                pt = {
                    "freq_hz": vec["freq_hz"],
                    "phase_requested_mdeg": vec["phase_requested_mdeg"],
                    "pass": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            result["points"].append(pt)
            if pt.get("pass"):
                result["passed"] += 1
                print("  " + fmt(pt))
            else:
                result["failed"] += 1
                rc = 1
                print(
                    f"  FAIL {pt.get('freq_hz', 0)/1e6:.4f}MHz φ={pt.get('phase_requested_mdeg',0)/1000:.1f}°"
                    f" → {pt.get('error') or json.dumps(pt, ensure_ascii=False)[:200]}"
                )
        result["link_stats"] = srv.link.stats
    finally:
        srv_rc = srv.close()
    result["server_exit_code"] = srv_rc

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(
        f"\n通过 {result['passed']} / 失败 {result['failed']}（共 {len(result['points'])} 点）"
        f"，串口统计 {result.get('link_stats')}，服务端退出码 {srv_rc}"
    )
    print(f"结果写入 {args.out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
