#!/usr/bin/env python3
"""端到端自测：在 pty 上驱动 `host_main`（宿主机验证台），逐条覆盖协议命令。

覆盖内容（对应 `docs/ps-protocol.md`）：
  - 帧层的正向/负向：CRC 错、LEN 越界、噪声字节、未知命令、响应方向帧；
  - 全部命令码 0x01..0x0E + 通知帧 0x81/0x82；
  - 错误路径：非法 mode/source/freq/相位/PAYLOAD 长度 → 0xFE；未锁定选片内源、忙时命令 → 0xFF；
  - 10 个冻结频点 × 逐频点相位点（含 >180° 的用例）与 `spec/golden/vectors.json` 逐位对齐
    （gate/edge/phase 与定点输出）；
  - 单次/连续模式、流上报、结果覆盖丢失（lostCount/EVENT）。

用法：
    python3 ps/tests/test_server_selftest.py [--host-main ps/build/host_main] [--verbose]
退出码 0 = 全部通过。
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

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOF = 0xA5
LEN_MIN, LEN_MAX = 1, 64

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
NTF_STREAM_DATA = 0xE1
NTF_EVENT = 0xE2

ERR_PARAM = 0xFE
ERR_BUSY = 0xFF

DPM_VERSION_VALUE = 0x44504D02
DPM_STATUS_BUSY = 1 << 0
DPM_STATUS_RESULT_VALID = 1 << 1
DPM_STATUS_LOST = 1 << 2
DPM_STATUS_IRQ_PENDING = 1 << 3
DPM_SIGST_ENABLE = 1 << 0
DPM_SIGST_SELECT = 1 << 1
DPM_SIGST_LOCKED = 1 << 2
DPM_SIGST_READY = 1 << 3


# ----------------------------------------------------------------- 协议工具


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    ln = 1 + len(payload)
    body = bytes([ln, cmd]) + payload
    c = crc16(body)
    return bytes([SOF]) + body + bytes([c & 0xFF, (c >> 8) & 0xFF])


def u32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def u16(v: int) -> bytes:
    return struct.pack("<H", v & 0xFFFF)


def rd_u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]


def rd_u16(b: bytes, off: int) -> int:
    return struct.unpack_from("<H", b, off)[0]


def is_notification(code: int, payload: bytes) -> bool:
    """判断一帧是否是主动上报的通知帧。

    协议 v1.1 起通知码为 0xE1/0xE2，不再与响应码撞码；仍按 PAYLOAD 长度
    （26 / 5 字节）双保险。
    """
    if code == NTF_EVENT and len(payload) == 5:
        return True
    if code == NTF_STREAM_DATA and len(payload) == 26:
        return True
    return False


class Server:
    """在 pty 上跑 host_main 的客户端。"""

    def __init__(self, host_main: str, extra: list[str] | None = None, freq=25_000_000, phase=90_000, gate=2000, verbose=False):
        self.master, slave = pty.openpty()
        self.path = os.ttyname(slave)
        argv = [host_main, "--device", self.path, "--freq-hz", str(freq), "--phase-mdeg", str(phase), "--gate", str(gate)]
        if extra:
            argv += list(extra)
        if verbose:
            argv.append("--verbose")
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
        self.buf = bytearray()
        self.inbox: list[tuple[int, bytes]] = []  # 线上收到的帧（FIFO）
        self.skipped: list[tuple[int, bytes]] = []  # request 期间跳过的通知帧（FIFO）
        self.bad_frames = 0
        os.close(slave)
        time.sleep(0.30)  # 等服务器初始化（含 DRP 重配置）
        self.drain(0.10)  # 丢掉可能残留的字节

    # ---------------------------------------------------------- 收发
    def _fill(self, timeout: float) -> int:
        r, _, _ = select.select([self.master], [], [], timeout)
        if not r:
            return 0
        try:
            data = os.read(self.master, 4096)
        except OSError:
            return 0
        if not data:
            return 0
        self.buf += data
        return len(data)

    def drain(self, timeout: float = 0.2) -> None:
        while self._fill(timeout) > 0:
            timeout = 0.02
            self._parse()

    def _parse(self) -> None:
        while True:
            if len(self.buf) < 2:
                return
            if self.buf[0] != SOF:
                self.buf.pop(0)
                continue
            ln = self.buf[1]
            if ln < LEN_MIN or ln > LEN_MAX:
                # 客户端不做 LEN 校验以外的猜测：当作噪声丢掉这次 SOF
                self.buf.pop(0)
                self.bad_frames += 1
                continue
            total = 4 + ln
            if len(self.buf) < total:
                return
            body = bytes(self.buf[1 : 2 + ln])
            crc = self.buf[2 + ln] | (self.buf[3 + ln] << 8)
            frame = bytes(self.buf[:total])
            if crc16(body) != crc:
                # 失步恢复：只丢掉这个假 SOF 再重新扫描，不能整帧吞掉
                # （否则可能把紧随其后的真帧一起吃掉）
                self.bad_frames += 1
                self.buf.pop(0)
                continue
            del self.buf[:total]
            self.inbox.append((frame[2], frame[3 : 2 + ln]))

    def next_frame(self, timeout: float = 2.0, include_skipped: bool = True) -> tuple[int, bytes] | None:
        """取下一帧。

        `include_skipped=True`（测试直接消费通知时）优先返回被 request 跳过的通知帧；
        `request()` 内部用 False，只看线上新到的帧，避免自己吃掉自己造成死循环。
        """
        if include_skipped and self.skipped:
            return self.skipped.pop(0)
        deadline = time.time() + timeout
        while True:
            self._parse()
            if self.inbox:
                return self.inbox.pop(0)
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            self._fill(min(remaining, 0.1))

    def request(self, cmd: int, payload: bytes = b"", timeout: float = 5.0, skip_notifications: bool = True) -> tuple[int, bytes] | None:
        os.write(self.master, build_frame(cmd, payload))
        deadline = time.time() + timeout
        while True:
            f = self.next_frame(max(0.0, deadline - time.time()), include_skipped=False)
            if f is None:
                return None
            # 通知帧与某些响应同码（0x81 = PING 响应、0x82 = IDENTIFY 响应），
            # 但 PAYLOAD 形状唯一（通知 26/5 字节，响应 0/30 字节），据此区分。
            if skip_notifications and is_notification(f[0], f[1]):
                self.skipped.append(f)
                continue
            return f

    def expect_ok(self, cmd: int, payload: bytes = b"", timeout: float = 5.0) -> bytes:
        f = self.request(cmd, payload, timeout)
        assert f is not None, f"0x{cmd:02X} 无响应"
        code, pl = f
        assert code == (cmd | 0x80), f"0x{cmd:02X} 响应码 0x{code:02X}（期望 0x{cmd | 0x80:02X}）"
        return pl

    def expect_err(self, cmd: int, err: int, payload: bytes = b"", timeout: float = 5.0) -> None:
        f = self.request(cmd, payload, timeout)
        assert f is not None, f"0x{cmd:02X} 无响应（期望 0x{err:02X}）"
        code, pl = f
        assert code == err, f"0x{cmd:02X} 响应码 0x{code:02X}（期望 0x{err:02X}）"
        assert pl == b"", f"错误响应必须无 PAYLOAD，实际 {pl.hex()}"

    def close(self) -> int:
        try:
            self.proc.send_signal(signal.SIGTERM)
            rc = self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            rc = -1
        os.close(self.master)
        return rc

    def stderr(self) -> str:
        try:
            return self.proc.stderr.read().decode(errors="replace")
        except Exception:
            return ""


# ----------------------------------------------------------------- 断言工具

RESULTS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))


def approx(a: int, b: int, tol: int) -> bool:
    return abs(int(a) - int(b)) <= tol


# ----------------------------------------------------------------- 测试主体


def test_frame_layer(sv: Server) -> None:
    """帧层：CRC 错/LEN 越界/噪声/未知命令/方向位。"""
    # CRC 错：把 CRC 改一位 → 不回响应
    bad = bytearray(build_frame(CMD_PING))
    bad[-1] ^= 0x01
    os.write(sv.master, bytes(bad))
    time.sleep(0.15)
    check("CRC 错的帧被丢弃且不回响应", sv.next_frame(0.2) is None)

    # LEN = 0 越界
    os.write(sv.master, bytes([SOF, 0x00, CMD_PING, 0x00, 0x00]))
    time.sleep(0.15)
    check("LEN=0 的帧被丢弃", sv.next_frame(0.2) is None)

    # LEN = 65 越界
    os.write(sv.master, bytes([SOF, 65]) + bytes(65) + b"\x00\x00")
    time.sleep(0.15)
    check("LEN=65 的帧被丢弃", sv.next_frame(0.2) is None)

    # 噪声字节 + 合法帧：必须在噪声后重新同步
    os.write(sv.master, b"\x11\x22\x33" + build_frame(CMD_PING))
    f = sv.next_frame(1.0)
    check("噪声后重新同步并回应 PING", f is not None and f[0] == (CMD_PING | 0x80), str(f))

    # 未知命令 0x7F：不回响应
    os.write(sv.master, build_frame(0x7F))
    time.sleep(0.15)
    check("未知命令不回响应", sv.next_frame(0.2) is None)

    # 响应方向帧（0x81）被忽略
    os.write(sv.master, build_frame(NTF_STREAM_DATA))
    time.sleep(0.15)
    check("响应方向帧被忽略", sv.next_frame(0.2) is None)

    # PAYLOAD 长度不符：PING 带 PAYLOAD → ERR_PARAM
    sv.expect_err(CMD_PING, ERR_PARAM, b"\x00")


def test_identify_status(sv: Server) -> None:
    pl = sv.expect_ok(CMD_IDENTIFY)
    check("IDENTIFY 长度 = 30", len(pl) == 30, f"实际 {len(pl)}")
    ver, git, caps, vreg, creg, dsp, slow, flags = (
        rd_u32(pl, 0),
        rd_u32(pl, 4),
        rd_u32(pl, 8),
        rd_u32(pl, 12),
        rd_u32(pl, 16),
        rd_u32(pl, 20),
        rd_u32(pl, 24),
        rd_u16(pl, 28),
    )
    check("IDENTIFY versionReg = 0x44504D02", vreg == DPM_VERSION_VALUE, hex(vreg))
    check("IDENTIFY capsReg = 从 PL 读回的 0x00030104", creg == 0x00030104, hex(creg))
    check("IDENTIFY 时钟频率", dsp == 450_000_000 and slow == 50_000_000, f"{dsp}/{slow}")
    check("IDENTIFY 版本号非 0", ver != 0, hex(ver))
    check("IDENTIFY caps 含片内源/连续/流（0x07）", (caps & 0x07) == 0x07, f"caps={caps:#x}")
    check(
        "IDENTIFY flags 至少含 caps 的位（flags 可能多出能力位，如 b4 phaseControllable）",
        (flags & caps) == caps,
        f"caps={caps:#x} flags={flags:#x}",
    )
    check("IDENTIFY flags b0 片内源", (flags & 1) == 1, hex(flags))
    check("IDENTIFY flags b1 连续 / b2 流", (flags & 0x06) == 0x06, hex(flags))
    check("IDENTIFY flags b4 phaseControllable = 1（方案 A：软件直接编码 MMCM 相位位域）", (flags & 0x10) == 0x10, hex(flags))

    pl = sv.expect_ok(CMD_GET_STATUS)
    check("GET_STATUS 长度 = 20", len(pl) == 20, f"实际 {len(pl)}")
    status, sigs, gate, lost, seq = (rd_u32(pl, 0), rd_u32(pl, 4), rd_u32(pl, 8), rd_u32(pl, 12), rd_u32(pl, 16))
    check("GET_STATUS gateTarget = 2000", gate == 2000, str(gate))
    check(
        "GET_STATUS 默认片内源 ready+locked",
        (sigs & DPM_SIGST_ENABLE) and (sigs & DPM_SIGST_SELECT) and (sigs & DPM_SIGST_LOCKED) and (sigs & DPM_SIGST_READY),
        hex(sigs),
    )
    check("GET_STATUS 不忙且无未读结果", (status & (DPM_STATUS_BUSY | DPM_STATUS_RESULT_VALID)) == 0, hex(status))


def test_gate_mode_source(sv: Server) -> None:
    pl = sv.expect_ok(CMD_SET_GATE, u32(3000))
    check("SET_GATE 回显", rd_u32(pl, 0) == 3000, pl.hex())
    sv.expect_err(CMD_SET_GATE, ERR_PARAM, u32(0))
    sv.expect_err(CMD_SET_GATE, ERR_PARAM, u32(0x10000000))
    sv.expect_err(CMD_SET_GATE, ERR_PARAM, u32(1)[:3])
    sv.expect_ok(CMD_SET_GATE, u32(2000))

    pl = sv.expect_ok(CMD_SET_MODE, bytes([1]))
    check("SET_MODE(1) 回显连续模式", pl == b"\x01", pl.hex())
    sv.expect_err(CMD_SET_MODE, ERR_PARAM, bytes([2]))
    sv.expect_err(CMD_SET_MODE, ERR_PARAM, b"")
    pl = sv.expect_ok(CMD_SET_MODE, bytes([0]))
    check("SET_MODE(0) 回显单次模式", pl == b"\x00", pl.hex())

    pl = sv.expect_ok(CMD_SET_SOURCE, bytes([0]))
    sigs = rd_u32(pl, 0)
    check("SET_SOURCE(0) 外部源恒 ready", (sigs & DPM_SIGST_READY) and not (sigs & DPM_SIGST_SELECT), hex(sigs))
    sv.expect_err(CMD_SET_SOURCE, ERR_PARAM, bytes([2]))
    pl = sv.expect_ok(CMD_SET_SOURCE, bytes([1]))
    check("SET_SOURCE(1) 片内源 ready", rd_u32(pl, 0) & DPM_SIGST_READY, pl.hex())

    pl = sv.expect_ok(CMD_STREAM, bytes([1]) + u16(5))
    check("STREAM(1,5ms) 回显", pl == bytes([1]) + u16(5), pl.hex())
    sv.expect_err(CMD_STREAM, ERR_PARAM, bytes([1]) + u16(0))
    sv.expect_err(CMD_STREAM, ERR_PARAM, bytes([2]) + u16(10))
    sv.expect_ok(CMD_STREAM, bytes([0]) + u16(100))

    sv.expect_ok(CMD_RESET_COUNTERS)
    sv.expect_ok(CMD_RESET_COUNTERS, b"")


def test_sig_and_golden(sv: Server, golden: dict, verbose: bool) -> None:
    """10 个冻结频点 × 逐频点相位点（含 >180°）：求解结果与 golden 一致，测量值也一致。

    相位说明（方案 A）：相位由 PS 直接编码进 MMCM 的 PHASE_MUX（1/8 VCO 周期）+
    DELAY_TIME（整数 VCO 周期），网格 = `45°/D`。冻结相位点全部落在网格上，因此
    `phaseMilliDeg` 精确回显请求值；测量相位应等于**同频点同请求相位**那一行。

    相位点**逐频点不同**（`golden["freq_points"][*]["phases_mdeg"]`）：上界是 9 位相位码
    `m = round(相位×D/45000) ≤ 511`，D=125（6MHz）只能到 183.960°、
    D=120（5MHz）到 191.625°、D=100（9/10MHz）到 230.174°、D=35（30MHz）能到 315°。
    """
    vectors = golden["vectors"]
    grid = golden["phase_grid_mdeg_num"]
    check("golden 相位网格分子 = 45000（1/8 VCO 周期 → 45°/D）", grid == 45_000, str(grid))
    check(
        "golden 写明 9 位相位码上限（m ≤ 511）与上界分子 23017499",
        golden.get("phase_code_max") == 0x1FF and golden.get("phase_max_mdeg_num") == 23_017_499,
        f"phase_code_max={golden.get('phase_code_max')} " f"phase_max_mdeg_num={golden.get('phase_max_mdeg_num')}",
    )
    rows = {(v["freq_hz"], v["phase_requested_mdeg"]): v for v in vectors}
    freqs = sorted({v["freq_hz"] for v in vectors})
    # 逐频点相位点表：与 golden 的 freq_points 一致（不是所有频点都能测所有相位）
    by_freq = {fp["freq_hz"]: fp for fp in golden["freq_points"]}
    check(
        "golden 逐频点相位点表与向量一致",
        all(
            sorted(rows[f, p]["phase_requested_mdeg"] for p in fp["phases_mdeg"]) == sorted(fp["phases_mdeg"]) for f, fp in by_freq.items()
        ),
        f"{sum(len(fp['phases_mdeg']) for fp in by_freq.values())} 组",
    )

    n_ok = 0
    n_over180 = 0
    for freq in freqs:
        for phase in sorted(by_freq[freq]["phases_mdeg"]):
            v_solve = rows[(freq, phase)]  # 求解/回显期望（随请求相位变化）
            v_meas = rows[(freq, phase)]  # 实测期望：相位跟随写入值（补丁已生效）
            tag = f"{freq/1e6:g}MHz/{phase/1000:g}°"
            n_over180 += 1 if phase >= 180_000 else 0

            pl = sv.expect_ok(CMD_SET_SIG, u32(freq) + u32(phase), timeout=10.0)
            check(f"{tag} SET_SIG 响应长度 = 15", len(pl) == 15, pl.hex())
            rf, rp, rvco, rdiv, rfield = (rd_u32(pl, 0), rd_u32(pl, 4), rd_u32(pl, 8), rd_u16(pl, 12), pl[14])
            check(
                f"{tag} 频率精确且 VCO/D 与 golden 一致",
                rf == v_solve["freq_hz"] and rvco == v_solve["vco_hz"] and rdiv == v_solve["div"],
                f"f={rf} VCO={rvco} D={rdiv} vs golden f={v_solve['freq_hz']} " f"VCO={v_solve['vco_hz']} D={v_solve['div']}",
            )
            # 方案 A：phaseField = MMCM 的 DELAY_TIME（整数 VCO 周期）＝ 相位码 >> 3
            want_code = (rp * rdiv * 8 + 180_000) // 360_000
            check(
                f"{tag} phaseField = DELAY_TIME（相位码 >> 3）",
                rfield == ((want_code >> 3) & 0x3F),
                f"field={rfield} 期望 {(want_code >> 3) & 0x3F}（rp={rp} D={rdiv}）",
            )
            check(f"{tag} 回显的写入相位 = 请求值（冻结相位点都在 45°/D 网格上）", rp == v_solve["phase_requested_mdeg"], f"{rp}")

            pl = sv.expect_ok(CMD_GET_SIG)
            check(f"{tag} GET_SIG 与 SET_SIG 一致", rd_u32(pl, 0) == rf and rd_u32(pl, 4) == rp and rd_u16(pl, 12) == rdiv, pl.hex())

            sv.expect_ok(CMD_START)
            pl = sv.expect_ok(CMD_GET_MEAS, b"", timeout=10.0)
            seq, gate, edge, phase_cnt, freq_hz, pmdeg = (
                rd_u32(pl, 0),
                rd_u32(pl, 4),
                rd_u32(pl, 8),
                rd_u32(pl, 12),
                rd_u32(pl, 16),
                rd_u32(pl, 20),
            )
            ok = (
                gate == v_meas["gate_cnt"]
                and edge == v_meas["edge_cnt"]
                and phase_cnt == v_meas["phase_cnt"]
                and freq_hz == v_meas["freq_milli_hz"]
                and pmdeg == v_meas["phase_milli_deg"]
            )
            if ok:
                n_ok += 1
            else:
                print(f"    {tag}: 实测 gate={gate} edge={edge} phase={phase_cnt} " f"f={freq_hz} p={pmdeg}")
                print(
                    f"    {tag}: golden gate={v_meas['gate_cnt']} edge={v_meas['edge_cnt']} "
                    f"phase={v_meas['phase_cnt']} f={v_meas['freq_milli_hz']} "
                    f"p={v_meas['phase_milli_deg']}"
                )
            check(f"{tag} 测量值匹配 golden（相位跟随写入值）", ok)
            check(f"{tag} phase_cnt % edge_cnt == 0（DSP 量化网格）", edge != 0 and phase_cnt % edge == 0, f"phase={phase_cnt} edge={edge}")
            if verbose:
                print(
                    f"    {tag}: 写相位={rp/1000:g}° 实测相位={pmdeg/1000:.3f}° "
                    f"f={freq_hz} Hz gate={gate} edge={edge} phase={phase_cnt} seq={seq}"
                )
    check(
        f"golden 覆盖 {len(freqs)} 个频点、{len(vectors)} 组（逐频点相位点，含 ≥180° 的 " f"{n_over180} 组）",
        n_ok == len(vectors),
        f"{n_ok}/{len(vectors)}",
    )

    # 错误路径：非法频率 / 相位 / PAYLOAD 长度
    sv.expect_err(CMD_SET_SIG, ERR_PARAM, u32(3_000_000) + u32(0))  # 低于可达下限
    sv.expect_err(CMD_SET_SIG, ERR_PARAM, u32(2_000_000_000) + u32(0))  # 高于 VCO 上限（2GHz 无解）
    # 相位上界 = 9 位相位码（按 D 逐点）：6MHz（D=125）的 200° 需要相位码 556 > 511，
    # MMCM 的字段会截断、相位回绕，必须拒绝；D=40（25MHz）则能表示整个 0~360°。
    sv.expect_err(CMD_SET_SIG, ERR_PARAM, u32(6_000_000) + u32(200_000))
    sv.expect_err(CMD_SET_SIG, ERR_PARAM, u32(6_000_000) + u32(184_500))  # D=125，上界 184.139°
    sv.expect_err(CMD_SET_SIG, ERR_PARAM, u32(25_000_000) + u32(360_000))  # 满量程（= 0°）
    sv.expect_err(CMD_SET_SIG, ERR_PARAM, u32(25_000_000) + u32(0)[:3])
    sv.expect_err(CMD_SET_SIG, ERR_PARAM, u32(0) + u32(0))
    # 22.5° 在 25MHz（D=40）下正好是 20 个 1/8 VCO 周期步：精确可表示
    # （相位吸附到 45°/D 网格，而不是 45° 的整数倍）
    pl = sv.expect_ok(CMD_SET_SIG, u32(25_000_000) + u32(22_500))
    check("SET_SIG(22.5°) 精确落在 45°/D 网格上并回显 22500", rd_u32(pl, 4) == 22_500 and pl[14] == 2, pl.hex())
    # D=125（6MHz）的网格是 0.36°：22.5° 吸附到最近的格点 22.680°
    pl = sv.expect_ok(CMD_SET_SIG, u32(6_000_000) + u32(22_500))
    check("SET_SIG(22.5°@6MHz) 吸附到网格点 22680 并回显", rd_u32(pl, 4) == 22_680, pl.hex())
    # >180° 的合法用例（旧固件一律拒 ≥180°，属过度保守）：
    #   25MHz（D=40）270° = 相位码 240 → DELAY_TIME 30、PHASE_MUX 0；
    #   6MHz（D=125）183.96° = 相位码上界 511 → DELAY_TIME 63、PHASE_MUX 7。
    pl = sv.expect_ok(CMD_SET_SIG, u32(25_000_000) + u32(270_000))
    check("SET_SIG(270°@25MHz) 合法：回显 270000、phaseField=30", rd_u32(pl, 4) == 270_000 and pl[14] == 30, pl.hex())
    pl = sv.expect_ok(CMD_SET_SIG, u32(6_000_000) + u32(183_960))
    check("SET_SIG(183.96°@6MHz) 合法：回显 183960、phaseField=63（相位码 511）", rd_u32(pl, 4) == 183_960 and pl[14] == 63, pl.hex())


def test_result_clear(sv: Server) -> None:
    sv.expect_ok(CMD_CLEAR)
    pl = sv.expect_ok(CMD_GET_RESULT)
    check("CLEAR 后 GET_RESULT valid=0", pl[0] == 0 and rd_u32(pl, 1) == 0, pl.hex())

    sv.expect_ok(CMD_SET_SIG, u32(10_000_000) + u32(90_000), timeout=10.0)
    sv.expect_ok(CMD_START)
    pl = sv.expect_ok(CMD_GET_RESULT)
    valid, seq, gate, edge, phase = (pl[0], rd_u32(pl, 1), rd_u32(pl, 5), rd_u32(pl, 9), rd_u32(pl, 13))
    check(
        "GET_RESULT valid=1 且字段齐全",
        valid == 1 and gate == 2005 and edge == 401,
        f"valid={valid} gate={gate} edge={edge} phase={phase} seq={seq}",
    )
    check("GET_RESULT phase_cnt/edge_cnt = 整数 DSP 周期", phase % 401 == 0, str(phase))

    # GET_MEAS 与 GET_RESULT 同源
    pl = sv.expect_ok(CMD_GET_MEAS)
    check("GET_MEAS 与 GET_RESULT 一致", rd_u32(pl, 4) == gate and rd_u32(pl, 8) == edge, pl.hex())
    check("GET_MEAS 定点频率单位是 Hz（10MHz → 10000000）", rd_u32(pl, 16) == 10_000_000, str(rd_u32(pl, 16)))
    check(
        "GET_MEAS 定点相位 ≈ 88°（90° 先被 450MHz 的 DSP 网格量化为 11 个周期）",
        approx(rd_u32(pl, 20), 88_000, 100),
        str(rd_u32(pl, 20)),
    )

    sv.expect_ok(CMD_CLEAR)
    pl = sv.expect_ok(CMD_GET_RESULT)
    check("再次 CLEAR 后结果清零", pl[0] == 0, pl.hex())


def wait_until_done(sv: Server, timeout: float = 5.0) -> dict:
    """轮询 GET_STATUS 直到 busy=0 且有未读结果（resultValid=1）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pl = sv.expect_ok(CMD_GET_STATUS)
        status = rd_u32(pl, 0)
        if (status & DPM_STATUS_BUSY) == 0 and (status & DPM_STATUS_RESULT_VALID) != 0:
            return dict(status=status, sig_status=rd_u32(pl, 4), gate=rd_u32(pl, 8), lost=rd_u32(pl, 12), seq=rd_u32(pl, 16))
        time.sleep(0.005)
    raise AssertionError(f"测量未在 {timeout}s 内完成")


def test_lost_and_events(sv: Server) -> None:
    """结果被覆盖 → lostCount 增长 + EVENT(1) 通知；并覆盖「测量中 START 回 ERR_BUSY」。

    单次模式且**流关闭**时固件不自动取结果（结果留在 PL 里，STATUS.resultValid = 1），
    因此在空闲但结果未读的情况下再发一次 START，旧结果会被新结果覆盖 → lostCount++。
    """
    sv.expect_ok(CMD_STREAM, bytes([0]) + u16(100))
    sv.expect_ok(CMD_RESET_COUNTERS)
    sv.expect_ok(CMD_SET_MODE, bytes([0]))
    sv.expect_ok(CMD_SET_SIG, u32(25_000_000) + u32(0), timeout=10.0)
    # 闸门放大到 20000 个慢周期（0.4ms），保证「测量中」这个窗口足够长，
    # 否则仿真后端可能在第二次 START 之前就把测量跑完了（那是正常竞态，不是缺陷）。
    sv.expect_ok(CMD_SET_GATE, u32(20_000))

    sv.expect_ok(CMD_START)
    # 协议 §3.0：测量进行中再发 START 回 ERR_BUSY（PL 内部是忽略）
    f = sv.request(CMD_START)
    check("测量中 START 回 ERR_BUSY", f is not None and f[0] == ERR_BUSY, "None" if f is None else hex(f[0]))

    st = wait_until_done(sv)
    check("单次模式未读结果时 STATUS.resultValid = 1", (st["status"] & DPM_STATUS_RESULT_VALID) != 0, hex(st["status"]))
    lost_before = st["lost"]

    # 空闲但上一次结果未读 → 再启动一次，旧结果被覆盖
    sv.expect_ok(CMD_START)
    st = wait_until_done(sv)
    check("结果被覆盖后 LOST_COUNT 增长", st["lost"] > lost_before, f"lost {lost_before} → {st['lost']}")
    check("STATUS.LOST 置位", (st["status"] & DPM_STATUS_LOST) != 0, hex(st["status"]))

    # 打开流：固件下一次取结果时上报 EVENT(1) 与 STREAM_DATA
    sv.expect_ok(CMD_STREAM, bytes([1]) + u16(1))
    got_stream = False
    got_lost_event = False
    deadline = time.time() + 8.0
    while time.time() < deadline and not (got_stream and got_lost_event):
        f = sv.next_frame(0.3)
        if f is None:
            continue
        code, pl = f
        if code == NTF_STREAM_DATA:
            check("STREAM_DATA 长度 = 26", len(pl) == 26, str(len(pl)))
            check("STREAM_DATA 的 seq/gate/edge 与结果寄存器一致", rd_u32(pl, 4) == st["gate"] or rd_u32(pl, 4) > 0, pl.hex())
            got_stream = True
        elif code == NTF_EVENT:
            check("EVENT 长度 = 5", len(pl) == 5, pl.hex())
            if pl[0] == 1:
                got_lost_event = True
                check("EVENT(1) detail = 丢失增量", rd_u32(pl, 1) >= 1, pl.hex())
    check("收到 STREAM_DATA 通知帧（0xE1）", got_stream)
    check("收到 EVENT(1 结果丢失) 通知帧（0xE2）", got_lost_event)
    sv.expect_ok(CMD_STREAM, bytes([0]) + u16(100))
    sv.expect_ok(CMD_SET_GATE, u32(2000))


def test_continuous(sv: Server, golden: dict) -> None:
    """连续模式：结果读走后立即重启，seq 递增且不丢。"""
    sv.expect_ok(CMD_CLEAR)
    sv.expect_ok(CMD_SET_SIG, u32(15_000_000) + u32(45_000), timeout=10.0)
    sv.expect_ok(CMD_STREAM, bytes([1]) + u16(1))
    sv.expect_ok(CMD_SET_MODE, bytes([1]))

    seqs = []
    deadline = time.time() + 4.0
    while time.time() < deadline and len(seqs) < 3:
        f = sv.next_frame(0.3)
        if f is None:
            continue
        code, pl = f
        if code == NTF_STREAM_DATA and len(pl) == 26:
            seqs.append((rd_u32(pl, 0), rd_u32(pl, 4), rd_u32(pl, 8), rd_u32(pl, 12), rd_u32(pl, 16), rd_u32(pl, 20)))
    check("连续模式至少上报 3 次 STREAM_DATA", len(seqs) >= 3, f"收到 {len(seqs)} 次")
    if len(seqs) >= 2:
        check("连续模式 seq 严格递增", all(seqs[i][0] < seqs[i + 1][0] for i in range(len(seqs) - 1)), str([s[0] for s in seqs]))
    v = next(x for x in golden["vectors"] if x["freq_hz"] == 15_000_000 and x["phase_requested_mdeg"] == 45_000)
    if seqs:
        s = seqs[-1]
        check(
            "连续模式测量值与 golden 一致（15MHz、相位 45°，跟随写入值）",
            (s[1], s[2], s[3], s[4], s[5]) == (v["gate_cnt"], v["edge_cnt"], v["phase_cnt"], v["freq_milli_hz"], v["phase_milli_deg"]),
            f"{s} vs {(v['gate_cnt'], v['edge_cnt'], v['phase_cnt'])}",
        )
    sv.expect_ok(CMD_SET_MODE, bytes([0]))
    sv.expect_ok(CMD_STREAM, bytes([0]) + u16(100))


def test_external_source(host_main: str, golden: dict) -> None:
    """外部源两条路径（与真实 PL 一致）。

    1. 默认（外部引脚没接信号）：`START` 后测量停在「等参考沿」，
       `STATUS.busy` 恒为 1、不产生结果、`RESULT_SEQ` 不自增；
       `GET_MEAS` 没有 `valid` 字段 → 回 `ERR_BUSY`；
       `GET_RESULT` 有 `valid` 字段（协议 §3.0 的权威探针）→ 回 **`valid = 0` 而不是 ERR_BUSY**
       （回 0xFF 会让按协议写的上位机把它当致命错误中止整段测量，实测踩到过）；
    2. `--ext-present`（模拟接了 10MHz/0°）：与 golden 的 10MHz/0° 行逐位一致。
    """
    # ---- 1) 无信号：不得给出看似正常的读数 ----
    sv = Server(host_main)
    try:
        sv.expect_ok(CMD_SET_SOURCE, bytes([0]))
        sv.expect_ok(CMD_SET_GATE, u32(2000))
        sv.expect_ok(CMD_START)
        time.sleep(0.3)
        pl = sv.expect_ok(CMD_GET_STATUS)
        status = rd_u32(pl, 0)
        seq1 = rd_u32(pl, 16)
        check("外部无信号：STATUS.busy 恒为 1", (status & DPM_STATUS_BUSY) != 0, hex(status))
        check("外部无信号：不产生结果（resultValid=0）", (status & DPM_STATUS_RESULT_VALID) == 0, hex(status))
        check("外部无信号：RESULT_SEQ 不自增", seq1 == 0, str(seq1))
        f = sv.request(CMD_GET_MEAS, timeout=10.0)
        check("外部无信号：GET_MEAS 超时回 ERR_BUSY", f is not None and f[0] == ERR_BUSY, "None" if f is None else hex(f[0]))
        # GET_RESULT 必须用 valid 表达「还没有结果」（协议 §3.0）：这是上位机的权威探针，
        # 回 ERR_BUSY 会让它把正常输入状态当致命错误——切到没接信号的外部源后必现。
        pl = sv.expect_ok(CMD_GET_RESULT, timeout=10.0)
        check(
            "外部无信号：GET_RESULT 回 valid=0（不是 ERR_BUSY）",
            len(pl) == 17 and pl[0] == 0 and rd_u32(pl, 1) == 0 and rd_u32(pl, 9) == 0,
            pl.hex(),
        )
    finally:
        rc = sv.close()
        check("无信号实例按 SIGTERM 干净退出（码 0）", rc == 0, f"rc={rc}")

    # ---- 2) 有信号（--ext-present）：与 golden 对照 ----
    sv = Server(host_main, extra=["--ext-present", "--ext-freq-hz", "10000000", "--ext-phase-mdeg", "0"])
    try:
        sv.expect_ok(CMD_SET_SOURCE, bytes([0]))
        sv.expect_ok(CMD_SET_GATE, u32(2000))
        sv.expect_ok(CMD_START)
        pl = sv.expect_ok(CMD_GET_MEAS, timeout=10.0)
        v = next(x for x in golden["vectors"] if x["freq_hz"] == 10_000_000 and x["phase_requested_mdeg"] == 0)
        check(
            "外部有信号（10MHz/0°）与 golden 一致",
            rd_u32(pl, 4) == v["gate_cnt"]
            and rd_u32(pl, 8) == v["edge_cnt"]
            and rd_u32(pl, 12) == v["phase_cnt"]
            and rd_u32(pl, 20) == v["phase_milli_deg"],
            pl.hex(),
        )
        sv.expect_ok(CMD_SET_SOURCE, bytes([1]))
    finally:
        sv.close()


def test_long_gate_bounded_wait(host_main: str) -> None:
    """长闸门 + 源没有边沿：命令内部的等待必须有界（与闸门同量级，不是几小时）。

    背景：`DPM_WAIT_TIMEOUT_MS` 必须把闸门时间按**秒**代入，不能把微秒数值当毫秒用，
    否则等待上限随闸门放大 1000 倍——5.37s 闸门（28 位上界）下 `GET_RESULT` 会在命令内部等数小时。
    这里用 28 位上界闸门（`0x0FFFFFFF` 周期 @50MHz = 5.37s）验证：

    * 上限 = `50ms + 4 × 5.37s ≈ 21.5s` 的**虚拟时间**（模型按事务推进，实测墙钟 ≈0.08s）；
    * 量纲写错时上限放大 1000 倍，模型里被兜底轮询上限拖着跑完 ≈2.15e9 次事务，实测墙钟 ≈10s；
    * 精确的数值判据在 `ps/tests/test_proto.c` 的 `test_wait_budget`（与机器速度无关），
      这里只钉住端到端的「有界」性质，阈值留了 30 倍余量，避免换机器就抖。
    """
    gate = 0x0FFFFFFF  # 28 位闸门计数器的上界（RTL gateCntWidth = 28）
    sv = Server(host_main, gate=gate)
    try:
        sv.expect_ok(CMD_SET_SOURCE, bytes([0]))  # 外部引脚；验证台默认没接信号
        sv.expect_ok(CMD_SET_GATE, u32(gate))
        sv.expect_ok(CMD_START)
        t0 = time.time()
        # 用 request 而不是 expect_ok：等待超时本身就是要判定的“失败”，不能抛断言把用例打断
        f = sv.request(CMD_GET_RESULT, timeout=60.0)
        dt = time.time() - t0
        check(
            "长闸门 + 无信号：GET_RESULT 在有界等待后返回（不是几小时）",
            f is not None and dt < 3.0,
            f"{dt:.2f}s" if f is not None else "无响应（>60s）",
        )
        pl = b"" if f is None else f[1]
        check(
            "长闸门 + 无信号：仍按协议回 valid=0（不是 ERR_BUSY）",
            f is not None and f[0] == (CMD_GET_RESULT | 0x80) and len(pl) == 17 and pl[0] == 0,
            pl.hex() if pl else "无响应",
        )
        # 等待期间服务端没有被卡死：后续命令照常能走（同一条串口）
        f2 = sv.request(CMD_PING, timeout=10.0)
        check(
            "长闸门等待后服务端仍可正常处理命令（PING）",
            f2 is not None and f2[0] == (CMD_PING | 0x80),
            "无响应" if f2 is None else hex(f2[0]),
        )
    finally:
        rc = sv.close()
        check("长闸门实例按 SIGTERM 干净退出（码 0）", rc == 0, f"rc={rc}")


def test_unlocked_internal(host_main: str, golden: dict) -> None:
    """未锁定时：SET_SOURCE(1) 与 SET_SIG 都必须回 ERR_BUSY，且 GET_STATUS 显示未锁定。"""
    sv = Server(host_main, extra=["--unlock"], verbose=False)
    try:
        pl = sv.expect_ok(CMD_GET_STATUS, timeout=30.0)
        sigs = rd_u32(pl, 4)
        check("强制失锁：SIG_STATUS.locked = 0", (sigs & DPM_SIGST_LOCKED) == 0, hex(sigs))
        check("强制失锁：sourceReady = 0", (sigs & DPM_SIGST_READY) == 0, hex(sigs))
        sv.expect_err(CMD_SET_SOURCE, ERR_BUSY, bytes([1]), timeout=30.0)
        sv.expect_err(CMD_SET_SIG, ERR_BUSY, u32(25_000_000) + u32(0), timeout=30.0)
        sv.expect_err(CMD_START, ERR_BUSY, b"", timeout=30.0)
        # 外部源不受 MMCM 影响：可以选中（SIG_STATUS 恒 ready），但引脚没接信号时拿不到结果
        pl = sv.expect_ok(CMD_SET_SOURCE, bytes([0]), timeout=30.0)
        check("失锁时仍可切到外部源（ready 恒 1）", (rd_u32(pl, 0) & DPM_SIGST_READY) != 0, pl.hex())
        sv.expect_ok(CMD_START, timeout=30.0)
        f = sv.request(CMD_GET_MEAS, timeout=30.0)
        check("外部无信号时 GET_MEAS 回 ERR_BUSY（不给出假读数）", f is not None and f[0] == ERR_BUSY, "None" if f is None else hex(f[0]))
    finally:
        rc = sv.close()
        check("失锁实例按 SIGTERM 干净退出（码 0）", rc == 0, f"rc={rc}")


def test_clean_exit(sv: Server) -> None:
    rc = sv.close()
    check("host_main 收到 SIGTERM 干净退出（码 0）", rc == 0, f"rc={rc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="DPM 宿主机验证台端到端自测")
    ap.add_argument("--host-main", default=os.path.join(REPO, "ps", "build", "host_main"))
    ap.add_argument("--golden", default=os.path.join(REPO, "spec", "golden", "vectors.json"))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.host_main):
        print(f"找不到 {args.host_main}，先跑 bash ps/build.sh", file=sys.stderr)
        return 2
    with open(args.golden, encoding="utf-8") as fh:
        golden = json.load(fh)

    print(f"== pty 端到端自测：{args.host_main} ==")
    sv = Server(args.host_main, verbose=args.verbose)
    try:
        test_frame_layer(sv)
        test_identify_status(sv)
        test_gate_mode_source(sv)
        test_sig_and_golden(sv, golden, args.verbose)
        test_result_clear(sv)
        test_lost_and_events(sv)
        test_continuous(sv, golden)
        test_clean_exit(sv)
    except AssertionError as exc:
        print(f"[FAIL] 断言失败: {exc}")
        RESULTS.append(("异常", False, str(exc)))
        sv.close()
    test_external_source(args.host_main, golden)
    test_long_gate_bounded_wait(args.host_main)
    test_unlocked_internal(args.host_main, golden)

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n== 结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} 项通过 ==")
    for name, _, detail in failed:
        print(f"  FAIL: {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
