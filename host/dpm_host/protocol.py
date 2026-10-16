"""DPM 串口帧协议：帧编解码、CRC-16/MODBUS 与流式解析器。

本模块是 `docs/ps-protocol.md` 第 2、3 节的纯逻辑实现，**不做任何 IO**，便于单元测试：

帧格式（字段之间无填充、无转义）::

    偏移  长度   字段     说明
    0     1      SOF      固定 0xA5
    1     1      LEN      CMD + PAYLOAD 的字节数（1..64）
    2     1      CMD      命令/响应码（bit7 = 方向：请求 0x00..0x7F，响应 = 请求 | 0x80）
    3     LEN-1  PAYLOAD  命令参数或响应数据（多字节字段一律小端）
    3+LEN 2      CRC16    CRC-16/MODBUS（poly 0x8005 反射 / 0xA001、init 0xFFFF），
                          计算范围 = LEN、CMD、PAYLOAD（不含 SOF），低字节在前

错帧处理：CRC 错、LEN 越界都只丢弃并继续找下一个 SOF（`:class:`FrameParser` 每次只丢 1 个
字节再重新同步，等价于「丢弃本帧、回到等待 SOF」，但对「坏 LEN 吞掉后续好帧」更稳健）。

**编码空间（协议 v1.1）**：

.. code-block:: text

    0x01..0x0E  请求（PC → PS）
    0x81..0x8E  响应（PS → PC），值 = 请求码 | 0x80
    0xE1..0xE2  通知（PS → PC 主动上报，无需请求）：0xE1 STREAM_DATA、0xE2 EVENT
    0xFE/0xFF   错误响应 ERR_PARAM / ERR_BUSY

判定规则：`cmd & 0xF0 == 0xE0` 即通知帧（见 :func:`is_notification_code`），其余按响应处理。

> v1.1 之前通知码是 0x81/0x82，与 `PING`（0x01|0x80）/`IDENTIFY`（0x02|0x80）的响应撞码，
> 只能靠 PAYLOAD 长度猜；现在通知码整体挪到 0xE1/0xE2，编码空间不再有交集。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from fractions import Fraction
from typing import Iterator, Union

# ------------------------------------------------------------------ 常量

#: 帧起始字节。
SOF = 0xA5
#: LEN 字段的合法范围（= CMD + PAYLOAD 字节数）。
LEN_MIN = 1
LEN_MAX = 64
#: SOF + LEN 两个字节。
HEADER_LEN = 2
#: CRC16 占 2 字节。
CRC_LEN = 2
#: 整帧最大字节数 = 4 + LEN_MAX。
MAX_FRAME_BYTES = HEADER_LEN + LEN_MAX + CRC_LEN  # 68

# ------------------------------------------------------------------ 命令码


class Cmd(IntEnum):
    """命令/响应码（值即线上字节）。"""

    PING = 0x01
    IDENTIFY = 0x02
    GET_STATUS = 0x03
    START = 0x04
    GET_RESULT = 0x05
    GET_MEAS = 0x06
    SET_GATE = 0x07
    SET_MODE = 0x08
    SET_SOURCE = 0x09
    SET_SIG = 0x0A
    GET_SIG = 0x0B
    CLEAR = 0x0C
    STREAM = 0x0D
    RESET_COUNTERS = 0x0E

    # 通知帧（PS → PC 主动上报）：0xE0..0xEF 保留给通知（协议 v1.1）
    STREAM_DATA = 0xE1
    EVENT = 0xE2

    # 错误响应（PAYLOAD 为空）
    ERR_PARAM = 0xFE
    ERR_BUSY = 0xFF

    @property
    def is_notification(self) -> bool:
        """是否为通知帧（PS 主动上报，不需要 PC 请求）：`cmd & 0xF0 == 0xE0`。"""
        return is_notification_code(int(self))

    @property
    def is_error(self) -> bool:
        """是否为错误响应码。"""
        return self in (Cmd.ERR_PARAM, Cmd.ERR_BUSY)

    @property
    def response(self) -> int:
        """本请求对应的成功响应码（请求 | 0x80）。"""
        return int(self) | 0x80


#: 通知帧的编码区间（协议 v1.1）：`cmd & 0xF0 == 0xE0`。
NOTIFY_BASE = 0xE0
NOTIFY_MASK = 0xF0


def is_notification_code(cmd: int) -> bool:
    """命令码是否为通知帧（`cmd & 0xF0 == 0xE0`）。"""
    return (int(cmd) & NOTIFY_MASK) == NOTIFY_BASE


#: 模式枚举（`SET_MODE`）。
MODE_SINGLE = 0
MODE_CONTINUOUS = 1
MODE_NAMES = {MODE_SINGLE: "single", MODE_CONTINUOUS: "continuous"}

#: 输入源枚举（`SET_SOURCE`）。
SOURCE_EXTERNAL = 0
SOURCE_INTERNAL = 1
SOURCE_NAMES = {SOURCE_EXTERNAL: "ext", SOURCE_INTERNAL: "int"}


def str_to_mode(text: str) -> int:
    """把 ``single`` / ``continuous``（也接受 ``0`` / ``1``）解析为 mode 值。"""
    t = str(text).strip().lower()
    if t in ("single", "s", "0"):
        return MODE_SINGLE
    if t in ("continuous", "cont", "c", "1"):
        return MODE_CONTINUOUS
    raise ValueError(f"未知测量模式: {text!r}（可选 single / continuous）")


def str_to_source(text: str) -> int:
    """把 ``ext`` / ``int``（也接受 ``external`` / ``internal`` / ``0`` / ``1``）解析为 source 值。"""
    t = str(text).strip().lower()
    if t in ("ext", "external", "e", "0"):
        return SOURCE_EXTERNAL
    if t in ("int", "internal", "i", "1"):
        return SOURCE_INTERNAL
    raise ValueError(f"未知输入源: {text!r}（可选 ext / int）")


# ------------------------------------------------------------ 位域定义

#: STATUS（`GET_STATUS` 的第一个字段）位。
STATUS_BUSY = 1 << 0
STATUS_RESULT_VALID = 1 << 1
STATUS_LOST = 1 << 2
STATUS_IRQ_PENDING = 1 << 3

#: SIG_STATUS（`GET_STATUS` 的第二个字段）位。
SIGST_ENABLE = 1 << 0
SIGST_SELECT = 1 << 1
SIGST_LOCKED = 1 << 2
SIGST_READY = 1 << 3

#: `IDENTIFY.flags` 位（对应 `ps/include/dpm_regs.h` 的能力位）。
FLAG_SIGNAL_SOURCE = 1 << 0  # b0 片内信号源可用
FLAG_CONTINUOUS = 1 << 1  # b1 支持连续模式
FLAG_STREAM = 1 << 2  # b2 支持流模式
FLAG_IRQ = 1 << 3  # b3 中断驱动
FLAG_PHASE_CTRL = 1 << 4  # b4 相位可在线设定（方案 A；旧 clk_wiz_src bitstream 为 0）

#: EVENT 通知的 code 语义（协议第 3.3 节）。
EVENT_NAMES = {
    1: "结果丢失",
    2: "MMCM 失锁并恢复",
    3: "上位机未及时读取",
}

#: 协议默认参数（`docs/ps-protocol.md` 第 7 节）。
DEFAULT_BAUD = 115200
DEFAULT_GATE_TARGET = 2000
DEFAULT_SLOW_HZ = 50_000_000
DEFAULT_DSP_HZ = 400_000_000
DEFAULT_SIG_FREQ_HZ = 25_000_000
DEFAULT_SIG_PHASE_MILLI_DEG = 90_000
DEFAULT_STREAM_INTERVAL_MS = 100

# ------------------------------------------------------------------ 异常


class ProtocolError(ValueError):
    """协议层错误（帧格式非法、PAYLOAD 字长不符等）。"""


class DpmError(Exception):
    """PS 返回错误响应（0xFE / 0xFF）时抛出。"""

    def __init__(self, code: int, cmd: int | None = None, detail: str = ""):
        self.code = int(code) & 0xFF
        self.cmd = cmd
        self.detail = detail
        self.name = error_name(self.code)
        msg = f"PS 返回错误响应 0x{self.code:02X}（{self.name}）"
        if cmd is not None:
            msg += f"，请求 0x{int(cmd) & 0xFF:02X}（{cmd_name(cmd)}）"
        if detail:
            msg += f"：{detail}"
        super().__init__(msg)


class DpmTimeoutError(DpmError):
    """等待响应超时（协议层无响应，不是错误响应帧）。"""

    def __init__(self, cmd: int | None = None, timeout: float | None = None, detail: str = ""):
        self.code = 0
        self.cmd = cmd
        self.name = "timeout"
        self.timeout = timeout
        msg = "等待响应超时"
        if cmd is not None:
            msg += f"（请求 0x{int(cmd) & 0xFF:02X} {cmd_name(cmd)}）"
        if timeout is not None:
            msg += f"，timeout={timeout:g}s"
        if detail:
            msg += f"：{detail}"
        Exception.__init__(self, msg)


def error_name(code: int) -> str:
    """错误码 → 名称（0 表示「没有响应/超时」这类非协议错误，未知码返回 ``UNKNOWN``）。"""
    value = int(code) & 0xFF
    if value == 0:
        return "NO_RESPONSE"
    try:
        cmd = Cmd(value)
    except ValueError:
        return "UNKNOWN"
    return {Cmd.ERR_PARAM: "ERR_PARAM", Cmd.ERR_BUSY: "ERR_BUSY"}.get(cmd, cmd.name)


def cmd_name(cmd: int | Cmd | None) -> str:
    """命令码 → 名称。

    - 请求码 / 通知码 / 错误码：枚举名；
    - 响应码 `0x81..0x8E`：``"<请求名> 响应"``（响应码不在枚举里，这样日志更好读）；
    - 其它：``0xNN``。
    """
    if cmd is None:
        return "None"
    value = int(cmd) & 0xFF
    try:
        return Cmd(value).name
    except ValueError:
        pass
    if 0x80 <= value <= 0x8F:
        try:
            return f"{Cmd(value & 0x7F).name} 响应"
        except ValueError:
            pass
    return f"0x{value:02X}"


# ------------------------------------------------------------------ CRC16


def _make_crc_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _make_crc_table()


def crc16_modbus(data: bytes | bytearray | memoryview) -> int:
    """CRC-16/MODBUS：poly 0xA001（0x8005 反射）、init 0xFFFF、无输出异或。

    这是协议里 CRC 字段使用的算法，计算范围 = LEN + CMD + PAYLOAD。
    """
    crc = 0xFFFF
    table = _CRC_TABLE
    for byte in data:
        crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
    return crc & 0xFFFF


# ------------------------------------------------------------------ 帧


@dataclass(frozen=True)
class Frame:
    """一个已验证 CRC 的帧。"""

    cmd: int
    payload: bytes = b""
    raw: bytes = b""

    @property
    def cmd_enum(self) -> Cmd | None:
        try:
            return Cmd(self.cmd)
        except ValueError:
            return None

    @property
    def name(self) -> str:
        return cmd_name(self.cmd)

    @property
    def is_error(self) -> bool:
        return self.cmd in (Cmd.ERR_PARAM, Cmd.ERR_BUSY)

    @property
    def is_notification(self) -> bool:
        return is_notification_code(self.cmd)

    def hex(self) -> str:
        """字符化形式，便于日志（如 ``A5 02 01 07 D2``）。"""
        return " ".join(f"{b:02X}" for b in (self.raw or encode_frame(self.cmd, self.payload)))

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Frame(cmd=0x{self.cmd:02X} {self.name}, payload={self.payload.hex(' ')})"


@dataclass(frozen=True)
class ParseError:
    """解析过程中丢弃的坏数据（诊断用，不影响继续同步）。"""

    kind: str  # "noise" | "len" | "crc"
    detail: str
    raw: bytes = b""

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"ParseError({self.kind}: {self.detail}, raw={self.raw.hex(' ')})"


ParseEvent = Union[Frame, ParseError]


def encode_frame(cmd: int | Cmd, payload: bytes | bytearray = b"") -> bytes:
    """把命令码与 PAYLOAD 打包成完整帧。"""
    cmd_byte = int(cmd) & 0xFF
    body = bytes([(len(payload) + 1), cmd_byte]) + bytes(payload)
    if not (LEN_MIN <= body[0] <= LEN_MAX):
        raise ProtocolError(f"LEN={body[0]} 越界（合法范围 {LEN_MIN}..{LEN_MAX}，PAYLOAD {len(payload)} 字节）")
    crc = crc16_modbus(body)
    return bytes([SOF]) + body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def frame_len_from_len_field(len_field: int) -> int:
    """LEN 字段 → 整帧字节数（= 4 + LEN）。"""
    return CRC_LEN + HEADER_LEN + len_field


def decode_frame(raw: bytes) -> Frame:
    """严格解析一个完整帧；任何不符合协议的地方抛 :class:`ProtocolError`。"""
    if len(raw) < HEADER_LEN + CRC_LEN:
        raise ProtocolError(f"帧太短：{len(raw)} 字节")
    if raw[0] != SOF:
        raise ProtocolError(f"SOF 不是 0x{SOF:02X}：0x{raw[0]:02X}")
    len_field = raw[1]
    if not (LEN_MIN <= len_field <= LEN_MAX):
        raise ProtocolError(f"LEN={len_field} 越界（合法范围 {LEN_MIN}..{LEN_MAX}）")
    if len(raw) != frame_len_from_len_field(len_field):
        raise ProtocolError(f"帧长不符：LEN={len_field} 要求 {frame_len_from_len_field(len_field)} 字节，实际 {len(raw)} 字节")
    body = raw[1 : 2 + len_field]
    want = crc16_modbus(body)
    got = raw[-2] | (raw[-1] << 8)
    if want != got:
        raise ProtocolError(f"CRC 错：计算 0x{want:04X}，帧内 0x{got:04X}")
    return Frame(cmd=raw[2], payload=bytes(raw[3 : 2 + len_field]), raw=bytes(raw))


class FrameParser:
    """流式帧解析器：喂入任意切分的字节流，产出帧与坏数据事件。"""

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        """清空内部缓冲（重新同步时用）。"""
        self._buf.clear()

    @property
    def pending(self) -> bytes:
        """尚未组成完整帧的残留字节。"""
        return bytes(self._buf)

    def feed(self, data: bytes | bytearray | memoryview) -> list[ParseEvent]:
        """喂入一段字节，返回本次新产生的 :class:`Frame` / :class:`ParseError` 列表。"""
        if data:
            self._buf.extend(data)
        events: list[ParseEvent] = []
        buf = self._buf
        while True:
            # 1) 同步到 SOF：丢弃 SOF 之前的噪声字节
            if not buf:
                break
            if buf[0] != SOF:
                idx = buf.find(SOF)
                if idx < 0:
                    noise = bytes(buf)
                    # 保留最后一个字节：它可能是被截断的 SOF（不需要，SOF 单字节，直接全丢）
                    buf.clear()
                    events.append(ParseError("noise", f"丢弃 {len(noise)} 个非 SOF 字节", noise))
                    break
                noise = bytes(buf[:idx])
                del buf[:idx]
                events.append(ParseError("noise", f"丢弃 {len(noise)} 个非 SOF 字节", noise))
                continue
            # 2) 需要 LEN
            if len(buf) < HEADER_LEN:
                break
            len_field = buf[1]
            if not (LEN_MIN <= len_field <= LEN_MAX):
                bad = bytes(buf[:2])
                del buf[:1]  # 只丢 SOF，重新同步（避免坏 LEN 吞掉后续好帧）
                events.append(ParseError("len", f"LEN={len_field} 越界（合法 {LEN_MIN}..{LEN_MAX}）", bad))
                continue
            total = frame_len_from_len_field(len_field)
            if len(buf) < total:
                break  # 等更多字节
            raw = bytes(buf[:total])
            body = raw[1 : 2 + len_field]
            want = crc16_modbus(body)
            got = raw[-2] | (raw[-1] << 8)
            if want != got:
                del buf[:1]  # 只丢 SOF，重新同步
                events.append(ParseError("crc", f"CRC 错：计算 0x{want:04X}，帧内 0x{got:04X}", raw))
                continue
            del buf[:total]
            events.append(Frame(cmd=raw[2], payload=bytes(raw[3 : 2 + len_field]), raw=raw))
        return events


def parse_stream(data: bytes) -> tuple[list[Frame], list[ParseError]]:
    """一次性解析整段字节（等价于「新建解析器 + feed + 收尾」），便于测试。"""
    parser = FrameParser()
    frames: list[Frame] = []
    errors: list[ParseError] = []
    for ev in parser.feed(data):
        (frames if isinstance(ev, Frame) else errors).append(ev)  # type: ignore[arg-type]
    return frames, errors


def iter_frames(data: bytes) -> Iterator[Frame]:
    """遍历一段字节中的全部合法帧。"""
    frames, _ = parse_stream(data)
    return iter(frames)


# ------------------------------------------------------- PAYLOAD 编解码
#
# 所有字段小端。解码函数严格要求字长（协议 3.2：字长不符 → ERR_PARAM）。

_U8 = struct.Struct("<B")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")


class PayloadError(ProtocolError):
    """PAYLOAD 字长/取值不符。"""


def _need(payload: bytes, size: int, what: str) -> None:
    if len(payload) != size:
        raise PayloadError(f"{what} PAYLOAD 应为 {size} 字节，实际 {len(payload)} 字节")


def _need_at_least(payload: bytes, size: int, what: str) -> None:
    if len(payload) < size:
        raise PayloadError(f"{what} PAYLOAD 至少 {size} 字节，实际 {len(payload)} 字节")


def _u32(value: int, what: str) -> int:
    v = int(value)
    if not (0 <= v <= 0xFFFFFFFF):
        raise PayloadError(f"{what} 超出 u32 范围：{value}")
    return v


def _u16(value: int, what: str) -> int:
    v = int(value)
    if not (0 <= v <= 0xFFFF):
        raise PayloadError(f"{what} 超出 u16 范围：{value}")
    return v


def _u8(value: int, what: str) -> int:
    v = int(value)
    if not (0 <= v <= 0xFF):
        raise PayloadError(f"{what} 超出 u8 范围：{value}")
    return v


# ---- IDENTIFY (0x02)：ver, gitRev, caps, versionReg, capsReg, dspClkHz, slowClkHz, flags


@dataclass(frozen=True)
class Identify:
    """`IDENTIFY` 响应。"""

    ver: int
    git_rev: int
    caps: int
    version_reg: int
    caps_reg: int
    dsp_clk_hz: int
    slow_clk_hz: int
    flags: int

    @property
    def has_signal_source(self) -> bool:
        return bool(self.flags & FLAG_SIGNAL_SOURCE)

    @property
    def supports_continuous(self) -> bool:
        return bool(self.flags & FLAG_CONTINUOUS)

    @property
    def supports_stream(self) -> bool:
        return bool(self.flags & FLAG_STREAM)

    @property
    def irq_driven(self) -> bool:
        return bool(self.flags & FLAG_IRQ)

    @property
    def phase_controllable(self) -> bool:
        """b4 = phaseControllable：写 MMCM 相位字段能否真的改变输出相位。"""
        return bool(self.flags & FLAG_PHASE_CTRL)

    @property
    def ver_text(self) -> str:
        """把 ver 字段按 ``主.次.补丁`` 显示（高位在前：8/8/16）。"""
        return f"{(self.ver >> 24) & 0xFF}.{(self.ver >> 16) & 0xFF}.{self.ver & 0xFFFF}"


IDENTIFY_SIZE = 30


def encode_identify(info: Identify) -> bytes:
    """打包 `IDENTIFY` 响应（30 字节）。"""
    return (
        _U32.pack(_u32(info.ver, "ver"))
        + _U32.pack(_u32(info.git_rev, "gitRev"))
        + _U32.pack(_u32(info.caps, "caps"))
        + _U32.pack(_u32(info.version_reg, "versionReg"))
        + _U32.pack(_u32(info.caps_reg, "capsReg"))
        + _U32.pack(_u32(info.dsp_clk_hz, "dspClkHz"))
        + _U32.pack(_u32(info.slow_clk_hz, "slowClkHz"))
        + _U16.pack(_u16(info.flags, "flags"))
    )


def decode_identify(payload: bytes) -> Identify:
    """解析 `IDENTIFY` 响应。"""
    _need(payload, IDENTIFY_SIZE, "IDENTIFY")
    (
        ver,
        git_rev,
        caps,
        version_reg,
        caps_reg,
        dsp_clk_hz,
        slow_clk_hz,
        flags,
    ) = struct.unpack_from("<IIIIIIIH", payload, 0)
    return Identify(ver, git_rev, caps, version_reg, caps_reg, dsp_clk_hz, slow_clk_hz, flags)


# ---- GET_STATUS (0x03)：status, sigStatus, gateTarget, lostCount, seq


@dataclass(frozen=True)
class Status:
    """`GET_STATUS` 响应（含位域解读）。"""

    status: int
    sig_status: int
    gate_target: int
    lost_count: int
    seq: int

    @property
    def busy(self) -> bool:
        return bool(self.status & STATUS_BUSY)

    @property
    def result_valid(self) -> bool:
        return bool(self.status & STATUS_RESULT_VALID)

    @property
    def lost(self) -> bool:
        return bool(self.status & STATUS_LOST)

    @property
    def irq_pending(self) -> bool:
        return bool(self.status & STATUS_IRQ_PENDING)

    @property
    def sig_enable(self) -> bool:
        return bool(self.sig_status & SIGST_ENABLE)

    @property
    def sig_select(self) -> bool:
        return bool(self.sig_status & SIGST_SELECT)

    @property
    def src_locked(self) -> bool:
        return bool(self.sig_status & SIGST_LOCKED)

    @property
    def source_ready(self) -> bool:
        return bool(self.sig_status & SIGST_READY)

    @property
    def status_text(self) -> str:
        bits = []
        for mask, name in (
            (STATUS_BUSY, "busy"),
            (STATUS_RESULT_VALID, "resultValid"),
            (STATUS_LOST, "lost"),
            (STATUS_IRQ_PENDING, "irqPending"),
        ):
            if self.status & mask:
                bits.append(name)
        return "|".join(bits) if bits else "-"

    @property
    def sig_status_text(self) -> str:
        bits = []
        for mask, name in (
            (SIGST_ENABLE, "enable"),
            (SIGST_SELECT, "srcSelect"),
            (SIGST_LOCKED, "srcLocked"),
            (SIGST_READY, "sourceReady"),
        ):
            if self.sig_status & mask:
                bits.append(name)
        return "|".join(bits) if bits else "-"


STATUS_SIZE = 20


def encode_status(st: Status) -> bytes:
    """打包 `GET_STATUS` 响应（20 字节，仿真服务端用）。"""
    return struct.pack(
        "<IIIII",
        _u32(st.status, "status"),
        _u32(st.sig_status, "sigStatus"),
        _u32(st.gate_target, "gateTarget"),
        _u32(st.lost_count, "lostCount"),
        _u32(st.seq, "seq"),
    )


def decode_status(payload: bytes) -> Status:
    """解析 `GET_STATUS` 响应。"""
    _need(payload, STATUS_SIZE, "GET_STATUS")
    return Status(*struct.unpack_from("<IIIII", payload, 0))


# ---- GET_RESULT (0x05)：valid u8, seq, gateCnt, edgeCnt, phaseCnt


@dataclass(frozen=True)
class Result:
    """`GET_RESULT` 响应（原始观测量）。"""

    valid: bool
    seq: int
    gate_cnt: int
    edge_cnt: int
    phase_cnt: int

    @property
    def phase_cycles(self) -> Fraction:
        """`phaseCnt / edgeCnt`（DSP 周期数，相位主判据）；edgeCnt=0 时为 Fraction(0)。"""
        if self.edge_cnt == 0:
            return Fraction(0)
        return Fraction(self.phase_cnt, self.edge_cnt)


RESULT_SIZE = 17


def encode_result(r: Result) -> bytes:
    """打包 `GET_RESULT` 响应（17 字节：1 + 4×4）。"""
    return _U8.pack(1 if r.valid else 0) + struct.pack(
        "<IIII",
        _u32(r.seq, "seq"),
        _u32(r.gate_cnt, "gateCnt"),
        _u32(r.edge_cnt, "edgeCnt"),
        _u32(r.phase_cnt, "phaseCnt"),
    )


def decode_result(payload: bytes) -> Result:
    """解析 `GET_RESULT` 响应。"""
    _need(payload, RESULT_SIZE, "GET_RESULT")
    valid, seq, gate_cnt, edge_cnt, phase_cnt = struct.unpack_from("<BIIII", payload, 0)
    return Result(bool(valid), seq, gate_cnt, edge_cnt, phase_cnt)


# ---- GET_MEAS (0x06) / STREAM_DATA (0xE1)


@dataclass(frozen=True)
class Meas:
    """`GET_MEAS` 响应；`STREAM_DATA` 通知即在末尾多一个 `lostDelta(u16)`。

    `freq_milli_hz` 承载的是**整数 Hz**（字段名是历史遗留，见 `docs/ps-protocol.md`
    第 5 节的字段单位表：25MHz → 25000000）；`phase_milli_deg` 是毫度。
    显示/判据请优先用 `gateCnt/edgeCnt/phaseCnt` 本地换算（`DpmClient.calc_meas`），
    这样不受对端字段口径差异影响。
    """

    seq: int
    gate_cnt: int
    edge_cnt: int
    phase_cnt: int
    #: `freqMilliHz` 字段：整数 Hz（名字沿用协议文档）。
    freq_milli_hz: int
    #: `phaseMilliDeg` 字段：毫度。
    phase_milli_deg: int

    @property
    def freq_hz_field(self) -> int:
        """字段原值（整数 Hz）。"""
        return self.freq_milli_hz

    @property
    def phase_deg(self) -> float:
        """相位差（度，浮点仅用于显示）。"""
        return self.phase_milli_deg / 1000.0

    @property
    def phase_cycles(self) -> Fraction:
        """`phaseCnt / edgeCnt`（DSP 周期数，整数定点主判据）。"""
        if self.edge_cnt == 0:
            return Fraction(0)
        return Fraction(self.phase_cnt, self.edge_cnt)

    def as_row(self) -> dict[str, int]:
        """导出为表格用的原始整数行（`f_meas_mHz` 列按协议字段单位 = Hz）。"""
        return {
            "seq": self.seq,
            "gateCnt": self.gate_cnt,
            "edgeCnt": self.edge_cnt,
            "phaseCnt": self.phase_cnt,
            "f_meas_mHz": self.freq_milli_hz,
            "phase_meas_mdeg": self.phase_milli_deg,
        }


MEAS_SIZE = 24


def encode_meas(m: Meas) -> bytes:
    """打包 `GET_MEAS` 响应（24 字节）。"""
    return struct.pack(
        "<IIIIII",
        _u32(m.seq, "seq"),
        _u32(m.gate_cnt, "gateCnt"),
        _u32(m.edge_cnt, "edgeCnt"),
        _u32(m.phase_cnt, "phaseCnt"),
        _u32(m.freq_milli_hz, "freqMilliHz"),
        _u32(m.phase_milli_deg, "phaseMilliDeg"),
    )


def decode_meas(payload: bytes) -> Meas:
    """解析 `GET_MEAS` 响应。"""
    _need(payload, MEAS_SIZE, "GET_MEAS")
    return Meas(*struct.unpack_from("<IIIIII", payload, 0))


@dataclass(frozen=True)
class StreamData:
    """`STREAM_DATA` 通知（`GET_MEAS` 字段 + `lostDelta(u16)`）。"""

    meas: Meas
    lost_delta: int


STREAM_DATA_SIZE = MEAS_SIZE + 2


def encode_stream_data(data: StreamData) -> bytes:
    """打包 `STREAM_DATA` 通知（26 字节）。"""
    return encode_meas(data.meas) + _U16.pack(_u16(data.lost_delta, "lostDelta"))


def decode_stream_data(payload: bytes) -> StreamData:
    """解析 `STREAM_DATA` 通知。"""
    _need(payload, STREAM_DATA_SIZE, "STREAM_DATA")
    meas = decode_meas(payload[:MEAS_SIZE])
    (lost_delta,) = _U16.unpack_from(payload, MEAS_SIZE)
    return StreamData(meas, lost_delta)


# ---- EVENT (0xE2)：code u8, detail u32


@dataclass(frozen=True)
class Event:
    """`EVENT` 通知。"""

    code: int
    detail: int

    @property
    def name(self) -> str:
        return EVENT_NAMES.get(self.code, f"未知事件 {self.code}")


EVENT_SIZE = 5


def encode_event(ev: Event) -> bytes:
    """打包 `EVENT` 通知（5 字节）。"""
    return _U8.pack(_u8(ev.code, "code")) + _U32.pack(_u32(ev.detail, "detail"))


def decode_event(payload: bytes) -> Event:
    """解析 `EVENT` 通知。"""
    _need(payload, EVENT_SIZE, "EVENT")
    code, detail = struct.unpack_from("<BI", payload, 0)
    return Event(code, detail)


# ---- SET_GATE (0x07)：gateTarget u32


def encode_set_gate(gate_target: int) -> bytes:
    return _U32.pack(_u32(gate_target, "gateTarget"))


def decode_set_gate(payload: bytes) -> int:
    _need(payload, 4, "SET_GATE")
    (gate_target,) = _U32.unpack_from(payload, 0)
    return gate_target


# ---- SET_MODE (0x08)：mode u8


def encode_set_mode(mode: int) -> bytes:
    return _U8.pack(_u8(mode, "mode"))


def decode_set_mode(payload: bytes) -> int:
    _need(payload, 1, "SET_MODE")
    (mode,) = _U8.unpack_from(payload, 0)
    return mode


# ---- SET_SOURCE (0x09)：source u8 → sigStatus u32


def encode_set_source(source: int) -> bytes:
    return _U8.pack(_u8(source, "source"))


def decode_set_source(payload: bytes) -> int:
    _need(payload, 1, "SET_SOURCE")
    (source,) = _U8.unpack_from(payload, 0)
    return source


def decode_sig_status_u32(payload: bytes) -> int:
    """`SET_SOURCE` 响应只有一个 u32（sigStatus）。"""
    _need(payload, 4, "SET_SOURCE 响应")
    (value,) = _U32.unpack_from(payload, 0)
    return value


# ---- SET_SIG (0x0A) / GET_SIG (0x0B)


@dataclass(frozen=True)
class SigInfo:
    """片内信号源当前/已设置的工作点（`SET_SIG` 响应与 `GET_SIG` 响应同构）。"""

    freq_hz: int
    phase_milli_deg: int
    vco_hz: int
    div: int
    phase_field: int

    @property
    def phase_deg(self) -> float:
        return self.phase_milli_deg / 1000.0

    @property
    def divide_field(self) -> int:
        """DRP 分频字段（低字节 = 分频比 D 本体，实测 `divide_field_scale = 1`）。"""
        return self.div & 0xFF


SIG_INFO_SIZE = 15


def encode_set_sig(freq_hz: int, phase_milli_deg: int) -> bytes:
    """打包 `SET_SIG` 请求（8 字节）。"""
    return struct.pack("<II", _u32(freq_hz, "freqHz"), _u32(phase_milli_deg, "phaseMilliDeg"))


def decode_set_sig(payload: bytes) -> tuple[int, int]:
    """解析 `SET_SIG` 请求 → (freqHz, phaseMilliDeg)。"""
    _need(payload, 8, "SET_SIG")
    return struct.unpack_from("<II", payload, 0)  # type: ignore[return-value]


def encode_sig_info(info: SigInfo) -> bytes:
    """打包 `SET_SIG` / `GET_SIG` 响应（15 字节）。"""
    return (
        struct.pack("<III", _u32(info.freq_hz, "freqHz"), _u32(info.phase_milli_deg, "phaseMilliDeg"), _u32(info.vco_hz, "vcoHz"))
        + _U16.pack(_u16(info.div, "div"))
        + _U8.pack(_u8(info.phase_field, "phaseField"))
    )


def decode_sig_info(payload: bytes) -> SigInfo:
    """解析 `SET_SIG` / `GET_SIG` 响应。"""
    _need(payload, SIG_INFO_SIZE, "SET_SIG/GET_SIG 响应")
    freq_hz, phase_milli_deg, vco_hz, div, phase_field = struct.unpack_from("<IIIHB", payload, 0)
    return SigInfo(freq_hz, phase_milli_deg, vco_hz, div, phase_field)


# ---- STREAM (0x0D)：enable u8, intervalMs u16


def encode_stream(enable: int, interval_ms: int) -> bytes:
    """打包 `STREAM` 请求（3 字节）。"""
    return _U8.pack(_u8(enable, "enable")) + _U16.pack(_u16(interval_ms, "intervalMs"))


def decode_stream(payload: bytes) -> tuple[int, int]:
    """解析 `STREAM` 请求/响应 → (enable, intervalMs)。"""
    _need(payload, 3, "STREAM")
    enable, interval_ms = struct.unpack_from("<BH", payload, 0)
    return enable, interval_ms


# ------------------------------------------- 命令元数据（字长校验用）

#: 命令号 → (名称, 请求 PAYLOAD 长度或 None=可变, 响应 PAYLOAD 长度或 None)
CMD_SPECS: dict[int, tuple[str, int | None, int | None]] = {
    Cmd.PING: ("PING", 0, 0),
    Cmd.IDENTIFY: ("IDENTIFY", 0, IDENTIFY_SIZE),
    Cmd.GET_STATUS: ("GET_STATUS", 0, STATUS_SIZE),
    Cmd.START: ("START", 0, 0),
    Cmd.GET_RESULT: ("GET_RESULT", 0, RESULT_SIZE),
    Cmd.GET_MEAS: ("GET_MEAS", 0, MEAS_SIZE),
    Cmd.SET_GATE: ("SET_GATE", 4, 4),
    Cmd.SET_MODE: ("SET_MODE", 1, 1),
    Cmd.SET_SOURCE: ("SET_SOURCE", 1, 4),
    Cmd.SET_SIG: ("SET_SIG", 8, SIG_INFO_SIZE),
    Cmd.GET_SIG: ("GET_SIG", 0, SIG_INFO_SIZE),
    Cmd.CLEAR: ("CLEAR", 0, 0),
    Cmd.STREAM: ("STREAM", 3, 3),
    Cmd.RESET_COUNTERS: ("RESET_COUNTERS", 0, 0),
    Cmd.STREAM_DATA: ("STREAM_DATA", None, None),
    Cmd.EVENT: ("EVENT", None, None),
    Cmd.ERR_PARAM: ("ERR_PARAM", 0, 0),
    Cmd.ERR_BUSY: ("ERR_BUSY", 0, 0),
}


def check_request_payload(cmd: int, payload: bytes) -> None:
    """按命令表检查请求 PAYLOAD 字长，不符抛 :class:`PayloadError`。"""
    spec = CMD_SPECS.get(int(cmd) & 0xFF)
    if spec is None:
        return  # 未知命令由服务端自行决定（协议未定义 ERR_UNKNOWN）
    name, req_len, _ = spec
    if req_len is not None and len(payload) != req_len:
        raise PayloadError(f"{name} 请求 PAYLOAD 应为 {req_len} 字节，实际 {len(payload)} 字节")


def check_response_payload(cmd: int, payload: bytes) -> None:
    """按命令表检查响应 PAYLOAD 字长，不符抛 :class:`PayloadError`。"""
    spec = CMD_SPECS.get(int(cmd) & 0xFF)
    if spec is None:
        return
    name, _, resp_len = spec
    if resp_len is not None and len(payload) != resp_len:
        raise PayloadError(f"{name} 响应 PAYLOAD 应为 {resp_len} 字节，实际 {len(payload)} 字节")


def decode_response(frame: Frame) -> object | None:
    """把响应帧解码成对应的数据类（无 PAYLOAD 的响应返回 ``None``）。

    仅处理成功响应；错误响应请先自行判断 ``frame.is_error``。
    """
    payload = frame.payload
    cmd = frame.cmd
    if cmd == Cmd.IDENTIFY.response:
        return decode_identify(payload)
    if cmd == Cmd.GET_STATUS.response:
        return decode_status(payload)
    if cmd == Cmd.GET_RESULT.response:
        return decode_result(payload)
    if cmd == Cmd.GET_MEAS.response:
        return decode_meas(payload)
    if cmd == Cmd.SET_GATE.response:
        return decode_set_gate(payload)
    if cmd == Cmd.SET_MODE.response:
        return decode_set_mode(payload)
    if cmd == Cmd.SET_SOURCE.response:
        return decode_sig_status_u32(payload)
    if cmd in (Cmd.SET_SIG.response, Cmd.GET_SIG.response):
        return decode_sig_info(payload)
    if cmd == Cmd.STREAM.response:
        return decode_stream(payload)
    # 通知码（0xE1/0xE2）与响应码没有交集，直接按码分发（见模块 docstring 的编码空间）
    if cmd == Cmd.STREAM_DATA:
        return decode_stream_data(payload)
    if cmd == Cmd.EVENT:
        return decode_event(payload)
    check_response_payload(cmd, payload)
    return None


__all__ = [
    "SOF",
    "LEN_MIN",
    "LEN_MAX",
    "MAX_FRAME_BYTES",
    "Cmd",
    "Frame",
    "ParseError",
    "ParseEvent",
    "FrameParser",
    "ProtocolError",
    "PayloadError",
    "DpmError",
    "DpmTimeoutError",
    "encode_frame",
    "decode_frame",
    "parse_stream",
    "iter_frames",
    "frame_len_from_len_field",
    "crc16_modbus",
    "cmd_name",
    "error_name",
    "is_notification_code",
    "NOTIFY_BASE",
    "NOTIFY_MASK",
    "decode_response",
    "check_request_payload",
    "check_response_payload",
    "str_to_mode",
    "str_to_source",
    "Identify",
    "Status",
    "Result",
    "Meas",
    "StreamData",
    "Event",
    "SigInfo",
    "encode_identify",
    "decode_identify",
    "encode_status",
    "decode_status",
    "encode_result",
    "decode_result",
    "encode_meas",
    "decode_meas",
    "encode_stream_data",
    "decode_stream_data",
    "encode_event",
    "decode_event",
    "encode_set_gate",
    "decode_set_gate",
    "encode_set_mode",
    "decode_set_mode",
    "encode_set_source",
    "decode_set_source",
    "decode_sig_status_u32",
    "encode_set_sig",
    "decode_set_sig",
    "encode_sig_info",
    "decode_sig_info",
    "encode_stream",
    "decode_stream",
    "CMD_SPECS",
    "MODE_SINGLE",
    "MODE_CONTINUOUS",
    "SOURCE_EXTERNAL",
    "SOURCE_INTERNAL",
    "STATUS_BUSY",
    "STATUS_RESULT_VALID",
    "STATUS_LOST",
    "STATUS_IRQ_PENDING",
    "SIGST_ENABLE",
    "SIGST_SELECT",
    "SIGST_LOCKED",
    "SIGST_READY",
    "DEFAULT_BAUD",
    "DEFAULT_GATE_TARGET",
    "DEFAULT_SLOW_HZ",
    "DEFAULT_DSP_HZ",
    "DEFAULT_SIG_FREQ_HZ",
    "DEFAULT_SIG_PHASE_MILLI_DEG",
    "DEFAULT_STREAM_INTERVAL_MS",
]
