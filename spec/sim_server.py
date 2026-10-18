#!/usr/bin/env python3
"""最小串口协议代理：把真实串口接到 `spec/` 的独立后端上，供验证脚本使用。

用途（`docs/verify-plan.md` 的验证链路 C）：
    - 当被测对象是 **PC 侧上位机**（`host/dpm_host`）时，用它冒充 PS 侧服务端，
      在 pty 上跑真实串口协议，验证上位机的帧编解码、超时、错误处理与流程；
    - 后端是一个**独立的**寄存器级 + 测量级 PL 模型（本文件自带，不 import `host/`），
      因此与 C 侧实现、与上位机实现构成三方独立实现。

用法：
    python3 spec/sim_server.py --pty                       # 建 pty，打印从端路径后常驻
    python3 spec/sim_server.py --device /dev/pts/N         # 在既有串口上跑
    python3 spec/sim_server.py --pty --freq-hz 25e6 --phase-mdeg 90e3 --gate 2000
    python3 spec/sim_server.py --pty --lock-lost           # 模拟 MMCM 失锁（sourceReady=0）

实现的寄存器语义与 `src/main/scala/DigitalPhaseMeter/ControlRegs.scala` 一致：
CTRL/STATUS/GATE_TARGET/四个结果寄存器/LOST_COUNT/IRQ_STATUS(W1C)/VERSION/CAPS/
SIG_CTRL/SIG_STATUS，以及「结果未读走即被覆盖 → lostCount++」「busy 期间忽略 start」
「CTRL.clear 在 busy 期间挂起」「片内源使能前两路输出恒低 → 测量永不完成」。
"""

from __future__ import annotations

import argparse
import os
import pty
import select
import struct
import sys
import time

# ------------------------------------------------------------------ 协议

SOF = 0xA5
CMD = {
    "PING": 0x01,
    "IDENTIFY": 0x02,
    "GET_STATUS": 0x03,
    "START": 0x04,
    "GET_RESULT": 0x05,
    "GET_MEAS": 0x06,
    "SET_GATE": 0x07,
    "SET_MODE": 0x08,
    "SET_SOURCE": 0x09,
    "SET_SIG": 0x0A,
    "GET_SIG": 0x0B,
    "CLEAR": 0x0C,
    "STREAM": 0x0D,
    "RESET_COUNTERS": 0x0E,
}
ERR_PARAM, ERR_BUSY = 0xFE, 0xFF

F_SLOW = 50_000_000
F_DSP = 450_000_000


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def encode(cmd: int, payload: bytes = b"") -> bytes:
    body = bytes([cmd]) + payload
    if not 1 <= len(body) <= 64:
        raise ValueError(f"帧长度 {len(body)} 越界（LEN 只有 1 字节且协议限 64）")
    head = bytes([SOF, len(body)]) + body
    return head + struct.pack("<H", crc16(head[1:]))


def round_half_even(num: int, den: int) -> int:
    q, r = divmod(num, den)
    twice = 2 * r
    return q + 1 if (twice > den or (twice == den and (q & 1))) else q


def solve_sig(target_hz: int, f_in: int = F_SLOW) -> tuple[int, int, int]:
    """与 `spec/gen_golden.py::solve_sig`、`ps/src/dpm_sig.c::dpm_sig_solve` 同一规则。"""
    best = None
    for mult in range(12, 25):
        vco = f_in * mult
        if not (600_000_000 <= vco <= 1_200_000_000):
            continue
        for d in range(1, 127):  # D ≤ 126：HIGH_TIME + LOW_TIME 各 6 位
            err_num = abs(vco - target_hz * d)
            cost = abs(vco - 1_000_000_000) // 1_000_000 + abs(d - 40)
            if best is None:
                best = (err_num, d, cost, vco, mult, d)
                continue
            lhs, rhs = err_num * best[1], best[0] * d
            if lhs < rhs:
                better = True
            elif lhs > rhs:
                better = False
            else:
                better = cost < best[2]
            if better:
                best = (err_num, d, cost, vco, mult, d)
    if best is None:
        raise ValueError("无解")
    return best[3], best[4], best[5]


# 相位编码（方案 A）：MMCM 的相位 = PHASE_MUX（1/8 VCO 周期）+ DELAY_TIME（整数 VCO 周期），
# 因此 9 位相位码 m = round(phase_mdeg × D / 45000)，网格 = 45000/D 毫度 = 45/D 度。
# 这里**独立重写一遍**（不 import host/ps 的代码），三处实现对拍才有意义。
PHASE_GRID_MDEG_NUM = 45_000
#: 9 位相位码上限（DELAY_TIME 6 位 + PHASE_MUX 3 位）。
PHASE_CODE_MAX = 0x1FF
#: `m <= 511` ⟺ `phase_mdeg × D <= 23017499`（推导见 ps/include/dpm_regs.h）。
PHASE_MAX_MDEG_NUM = 23_017_499
#: 相位满量程（毫度）：360° 与 0° 同义，请求范围是 [0, 360000)。
PHASE_FULL_SCALE_MDEG = 360_000


def mmcm_phase_code(phase_mdeg: int, div: int) -> int:
    """9 位相位码：`round(phase_mdeg × div / 45000)`（与 C/Python 同一公式，独立实现）。"""
    return (int(phase_mdeg) * int(div) * 8 + 180_000) // 360_000


def phase_max_mdeg(div: int) -> int:
    """给定 D 时可请求的最大相位（毫度，含）：9 位相位码与满量程谁先撞上。"""
    return min(PHASE_MAX_MDEG_NUM // int(div), PHASE_FULL_SCALE_MDEG - 1)


def phase_code_fits(phase_mdeg: int, div: int) -> bool:
    """相位请求是否落在 9 位相位码内（越界 = MMCM 字段截断、相位回绕，必须回 ERR_PARAM）。

    旧规则是「相位必须 < 180°」，比硬件保守得多：`m <= 511` ⟺ `phase < 23017499/D`，
    因此 D ≤ 63 时整个 0~360° 都可表示，D=125（8MHz）也能到 184.139°。
    """
    phase = int(phase_mdeg)
    return 0 <= phase < PHASE_FULL_SCALE_MDEG and mmcm_phase_code(phase, div) <= PHASE_CODE_MAX


def mmcm_achievable_mdeg(phase_mdeg: int, div: int) -> int:
    """请求相位经 MMCM 网格量化后**真正能输出**的相位（毫度）。"""
    code = mmcm_phase_code(phase_mdeg, div)
    return ((code * PHASE_GRID_MDEG_NUM) + int(div) // 2) // int(div)


def mmcm_delay_time(phase_mdeg: int, div: int) -> int:
    """SET_SIG 回包的 `phaseField`：MMCM 的 DELAY_TIME 字段（= 相位码 >> 3）。"""
    return (mmcm_phase_code(phase_mdeg, div) >> 3) & 0x3F


# --------------------------------------------------------------- 仿真后端


def _fixed(gate: int, edge: int, phase: int) -> tuple[int, int]:
    """按 docs/ps-protocol.md 第 5 节算 (freq_hz, phase_milli_deg)，整除截断。"""
    if not gate or not edge:
        return 0, 0
    freq = (edge * F_SLOW) // gate  # 单位：Hz
    c, d = divmod(phase, gate)
    v = 360_000 * c + (360_000 * d) // gate
    return freq, (v * (F_SLOW // 1000)) // (F_DSP // 1000)


class SimBackend:
    """寄存器级 + 测量级 PL 模型（默认内部源 25MHz / 90°）。"""

    def __init__(
        self, freq_hz: int = 25_000_000, phase_mdeg: int = 90_000, gate: int = 2000, locked: bool = True, phase_controllable: bool = True
    ):
        self.f_slow, self.f_dsp = F_SLOW, F_DSP
        self.gate_target = gate
        self.mode = 0
        self.locked = locked
        self.phase_controllable = phase_controllable
        self.sig_enable = 0
        self.src_select = 0
        self.config_pending = 0
        self.config_pending_until = 0.0
        self.ctrl_irq_enable = 0
        self.busy = 0
        self.result_valid = 0
        self.irq_pending = 0
        self.lost_count = 0
        self.seq = 0
        self.res_gate = self.res_edge = self.res_phase = self.res_seq = 0
        self.clear_req = 0
        self.freq_hz = freq_hz
        self.phase_mdeg = phase_mdeg
        self.finish_at: float | None = None  # 测量完成的绝对时间
        self.pending_result: tuple[int, int, int] | None = None
        self.solve_sig(freq_hz, phase_mdeg)

    # ---- 片内源 ----
    def solve_sig(self, freq_hz: int, phase_mdeg: int) -> bool:
        """求解工作点并把相位吸附到 MMCM 网格（与 PS 侧 SET_SIG 的回显口径一致）。"""
        try:
            vco, mult, div = solve_sig(freq_hz)
        except ValueError:
            return False
        self.freq_hz, self.phase_mdeg = freq_hz, phase_mdeg
        self.vco_hz, self.mult, self.div = vco, mult, div
        if self.phase_controllable:
            self.phase_mdeg = mmcm_achievable_mdeg(phase_mdeg, div)
            self.phase_field = mmcm_delay_time(phase_mdeg, div)
        else:
            self.phase_mdeg = self.FIXED_PHASE_MDEG
            self.phase_field = 0
        return True

    # ---- 时序推进 ----
    def tick(self) -> None:
        now = time.monotonic()
        if self.config_pending and now >= self.config_pending_until:
            self.config_pending = 0
        if self.busy and self.finish_at is not None and now >= self.finish_at:
            gate, edge, phase = self.pending_result or (0, 0, 0)
            self.res_gate, self.res_edge, self.res_phase = gate, edge, phase
            self.res_seq = self.seq
            self.seq += 1
            self.busy = 0
            self.irq_pending = 1
            if self.result_valid:
                self.lost_count += 1
            self.result_valid = 1
            self.pending_result = None
            self.finish_at = None

    # ---- 测量模型 ----
    # 方案 A（上板已实测）：相位可控，写 MMCM 的 PHASE_MUX/DELAY_TIME 即生效，
    # 可实现相位 = 请求值吸附到 45°/D 网格（见 mmcm_achievable_mdeg）。
    # 旧 clk_wiz_src bitstream 的行为（相位固定 90°）用 --phase-static 复现。
    PHASE_CONTROLLABLE = True
    FIXED_PHASE_MDEG = 90_000

    def measure_model(self) -> tuple[int, int, int]:
        """按 docs/verify-plan.md 的模型算 (gate, edge, phase)。

        片内源：相位先被 DSP 采样网格量化成整数个高速周期再乘参考沿数。450MHz 的 DSP 周期
        不是整数 ps，因此用 `相位 × f_dsp /(360000 × f_sig)` 的有理数形式做 RNE
        （与 `spec/gen_golden.py`、`ps/sim/fpga_model.c` 的整数公式逐位一致）。
        `PHASE_CONTROLLABLE = False` 时改用固定的 90°（模拟旧 bitstream 的真实行为）。
        """
        # gate = target_gate + ceil(T_sig/T_slow)，T_sig/T_slow = D·f_slow/VCO
        gate = self.gate_target + -(-(self.div * self.f_slow) // self.vco_hz)
        edge = (gate * self.vco_hz) // (self.div * self.f_slow)
        if edge == 0:
            return gate, 0, 0
        phase_mdeg = self.phase_mdeg if self.phase_controllable else self.FIXED_PHASE_MDEG
        # phase/T_dsp = phase_mdeg × f_dsp / (360000 × f_sig)，f_sig = VCO/D
        cycles = round_half_even(phase_mdeg * self.div * self.f_dsp, 360_000 * self.vco_hz)
        return gate, edge, cycles * edge

    def start(self) -> None:
        if self.busy:
            return
        if self.src_select and not (self.sig_enable and self.locked):
            return  # 源恒低：测量挂在「等参考沿」，永不完成
        if not self.src_select:
            # 外部引脚输入：没有信号源，PL 会一直等参考沿（busy 永远为 1）。
            # 这个状态正是「docs/verify-plan.md 用例 5」要验证的容错路径。
            self.busy = 1
            self.finish_at = None
            self.pending_result = None
            return
        gate, edge, phase = self.measure_model()
        gate &= 0x0FFFFFFF
        self.pending_result = (gate, edge, phase)
        # 仿真时间：闸门时长（+ 一个信号周期握手）压缩 1000 倍，便于自动化
        t_sig = self.div / self.vco_hz
        span = (gate / self.f_slow + t_sig) / 1000.0
        self.finish_at = time.monotonic() + max(0.001, span)
        self.busy = 1

    # ---- 总线访问 ----
    def write(self, off: int, value: int) -> None:
        idx = (off >> 2) & 0xF
        if idx == 0:  # CTRL
            self.ctrl_irq_enable = (value >> 1) & 1
            if value & 1:
                self.start()
            if value & 4:
                self.clear_req = 1
        elif idx == 2:
            self.gate_target = value & 0x0FFFFFFF
        elif idx == 8:  # IRQ_STATUS，W1C
            if value & 1:
                self.irq_pending = 0
            if value & 2:
                self.result_valid = 0
            if value & 4:
                self.lost_count = 0
        elif idx == 11:  # SIG_CTRL
            self.sig_enable = value & 1
            self.src_select = (value >> 1) & 1
            self.config_pending = 1
            self.config_pending_until = time.monotonic() + 0.02
        if self.clear_req and not self.busy:
            self.clear_req = 0
            self.seq = self.lost_count = self.irq_pending = self.result_valid = 0
            self.res_gate = self.res_edge = self.res_phase = self.res_seq = 0
        if self.clear_req and self.busy and self.finish_at is None:
            # 「外部输入无信号 → 一直等参考沿」这种挂起状态没有完成时刻，
            # 真实 PL 里也只有 CTRL.clear / 重新配置能恢复，因此这里让 clear 直接取消它。
            # （PL 侧的 clear 在 busy 期间是挂起的：真实硬件要等测量结束；
            #   对「永远不会结束」的测量，软件需要先切回可用信号源再 clear。）
            self.clear_req = 0
            self.busy = 0
            self.seq = self.lost_count = self.irq_pending = self.result_valid = 0
            self.res_gate = self.res_edge = self.res_phase = self.res_seq = 0

    def read(self, off: int) -> int:
        idx = (off >> 2) & 0xF
        ctrl_rd = self.ctrl_irq_enable << 1
        sig_ready = 1 if (not self.src_select or (self.sig_enable and self.locked)) else 0
        table = {
            0: ctrl_rd,
            1: self.busy | (self.result_valid << 1) | ((1 if self.lost_count else 0) << 2) | (self.irq_pending << 3),
            2: self.gate_target,
            3: self.res_gate,
            4: self.res_edge,
            5: self.res_phase,
            6: self.res_seq,
            7: self.lost_count,
            8: self.irq_pending | (self.result_valid << 1) | ((1 if self.lost_count else 0) << 2),
            9: 0x44504D02,
            10: 4 | (1 << 8) | (1 << 16) | (1 << 17),
            11: self.sig_enable | (self.src_select << 1),
            12: self.sig_enable | (self.src_select << 1) | ((1 if self.locked else 0) << 2) | (sig_ready << 3),
        }
        return table.get(idx, 0)


# ---------------------------------------------------------- 串口 ↔ 命令分发


class Session:
    def __init__(self, backend: SimBackend):
        self.b = backend
        self.stream = (0, 100)
        self.rx = bytearray()
        self.stream_next = 0.0

    def handle(self, cmd: int, payload: bytes) -> bytes | None:
        b = self.b
        resp = cmd | 0x80
        if cmd == CMD["PING"]:
            return encode(resp)
        if cmd == CMD["IDENTIFY"]:
            # ver(u32) gitRev(u32) caps(u32) versionReg(u32) capsReg(u32)
            # dspClkHz(u32) slowClkHz(u32) flags(u16) —— 共 30 字节
            return encode(resp, struct.pack("<IIIIIIIH", 0x00010000, 0, 0, b.read(0x24), b.read(0x28), b.f_dsp, b.f_slow, 0x0F))
        if cmd == CMD["GET_STATUS"]:
            return encode(resp, struct.pack("<IIIII", b.read(0x04), b.read(0x30), b.gate_target, b.lost_count, b.seq))
        if cmd == CMD["START"]:
            b.write(0x00, (b.ctrl_irq_enable << 1) | 1)
            return encode(resp)
        if cmd == CMD["GET_RESULT"]:
            payload = struct.pack("<BIIII", b.result_valid, b.res_seq, b.res_gate, b.res_edge, b.res_phase)
            if b.result_valid:
                b.result_valid = 0  # 读走结果
            return encode(resp, payload)
        if cmd == CMD["GET_MEAS"]:
            freq, ph = self.fixed(b.res_gate, b.res_edge, b.res_phase)
            payload = struct.pack("<IIIIII", b.res_seq, b.res_gate, b.res_edge, b.res_phase, freq, ph)
            b.result_valid = 0  # 结果已被读走（等价于写 IRQ_STATUS 的 b1）
            return encode(resp, payload)
        if cmd == CMD["SET_GATE"]:
            if len(payload) != 4:
                return encode(ERR_PARAM)
            b.write(0x08, struct.unpack("<I", payload)[0])
            return encode(resp, struct.pack("<I", b.gate_target))
        if cmd == CMD["SET_MODE"]:
            if len(payload) != 1 or payload[0] > 1:
                return encode(ERR_PARAM)
            b.mode = payload[0]
            return encode(resp, bytes([b.mode]))
        if cmd == CMD["SET_SOURCE"]:
            if len(payload) != 1 or payload[0] > 1:
                return encode(ERR_PARAM)
            if payload[0] == 1 and not b.locked:
                return encode(ERR_BUSY)  # 未锁定就选片内源
            if payload[0] == 1:
                b.write(0x2C, 1 | (1 << 1))  # enable | srcSelect
            else:
                b.write(0x2C, 0)  # 切回外部输入
            return encode(resp, struct.pack("<I", b.read(0x30)))
        if cmd == CMD["SET_SIG"]:
            if len(payload) != 8:
                return encode(ERR_PARAM)
            freq, phase = struct.unpack("<II", payload)
            # 约束与 docs/ps-protocol.md 第 6.4 节一致：VCO ≥ 600MHz 且 D ≤ 126
            # → 可实现的最低频率约 600/126 = 4.76MHz，因此 4.8MHz 以下直接拒绝。
            if not (4_800_000 <= freq <= 60_000_000) or phase >= PHASE_FULL_SCALE_MDEG:
                return encode(ERR_PARAM)
            if not b.solve_sig(freq, phase):
                return encode(ERR_PARAM)
            # 相位上界 = 9 位相位码（与 D 有关，见 phase_code_fits）：越界请求会让 MMCM
            # 的字段截断、相位回绕，必须显式拒绝而不是静默改相位。
            if not phase_code_fits(phase, b.div):
                return encode(ERR_PARAM)
            return encode(resp, struct.pack("<IIIHB", b.freq_hz, b.phase_mdeg, b.vco_hz, b.div, b.phase_field))
        if cmd == CMD["GET_SIG"]:
            return encode(resp, struct.pack("<IIIHB", b.freq_hz, b.phase_mdeg, b.vco_hz, b.div, b.phase_field))
        if cmd == CMD["CLEAR"]:
            b.write(0x00, (b.ctrl_irq_enable << 1) | 4)
            return encode(resp)
        if cmd == CMD["STREAM"]:
            if len(payload) != 3:
                return encode(ERR_PARAM)
            en, interval = struct.unpack("<BH", payload)
            self.stream = (en, interval)
            return encode(resp, struct.pack("<BH", en, interval))
        if cmd == CMD["RESET_COUNTERS"]:
            b.write(0x20, 7)
            return encode(resp)
        return encode(ERR_PARAM)

    @staticmethod
    def fixed(gate: int, edge: int, phase: int) -> tuple[int, int]:
        """按 docs/ps-protocol.md 第 5 节：整除截断；gate/edge 为 0 时输出 0。

        用等价的安全形式（频率先算 kHz 再降 Hz；相位按整除分解后乘 kHz 量级时钟），
        避免 `edge*f_slow*1000` 与 `360000*phase*f_slow` 溢出 2^64。
        """
        return _fixed(gate, edge, phase)

    def feed(self, data: bytes) -> bytes:
        self.rx.extend(data)
        out = bytearray()
        while True:
            if len(self.rx) < 2:
                break
            if self.rx[0] != SOF:
                del self.rx[0]
                continue
            length = self.rx[1]
            if not 1 <= length <= 64:
                del self.rx[0]
                continue
            if len(self.rx) < 2 + length + 2:
                break
            frame = bytes(self.rx[: 2 + length + 2])
            del self.rx[: 2 + length + 2]
            if crc16(frame[1:-2]) != struct.unpack("<H", frame[-2:])[0]:
                continue
            cmd, payload = frame[2], frame[3:-2]
            try:
                r = self.handle(cmd, payload)
            except (ValueError, struct.error) as exc:
                # 内部错误不能让服务端崩掉：回 ERR 并把原因写到 stderr
                print(f"# 命令 0x{cmd:02X} 处理失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                r = encode(ERR_PARAM)
            if r:
                out.extend(r)
            self.b.tick()
        return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="spec 侧独立仿真服务端")
    ap.add_argument("--pty", action="store_true")
    ap.add_argument("--device")
    ap.add_argument("--freq-hz", type=float, default=25e6)
    ap.add_argument("--phase-mdeg", type=float, default=90e3)
    ap.add_argument("--gate", type=int, default=2000)
    ap.add_argument("--lock-lost", action="store_true", help="模拟 MMCM 失锁")
    ap.add_argument("--phase-static", action="store_true", help="复现旧 clk_wiz_src bitstream：相位不可控，固定为 BD 初值 90°")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    backend = SimBackend(
        int(args.freq_hz), int(args.phase_mdeg), args.gate, locked=not args.lock_lost, phase_controllable=not args.phase_static
    )
    session = Session(backend)

    if args.pty:
        master, slave = pty.openpty()
        name = os.ttyname(slave)
        print(name, flush=True)
        fd = master
        keep = slave
    elif args.device:
        fd = os.open(args.device, os.O_RDWR | os.O_NOCTTY)
        keep = None
    else:
        print(__doc__)
        return 2

    if not args.quiet:
        print(
            f"# spec 仿真服务端：f={backend.freq_hz/1e6:.4f}MHz φ={backend.phase_mdeg/1000:.1f}° "
            f"VCO={backend.vco_hz/1e6:.0f}MHz D={backend.div} gate={backend.gate_target}",
            file=sys.stderr,
            flush=True,
        )
    try:
        while True:
            backend.tick()
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                out = session.feed(data)
                if out:
                    os.write(fd, out)
    except KeyboardInterrupt:
        pass
    finally:
        os.close(fd)
        if keep is not None:
            os.close(keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
