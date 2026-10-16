"""串口客户端：请求/响应、超时重试、线程安全与通知帧回调。

设计要点：

- **单事务串行**：协议是半双工的请求/响应，`:meth:`DpmClient.request`` 用一把锁把「写请求 →
  等响应」做成一个原子事务，多个线程调用会被串行化；
- **后台读线程**：唯一的读线程负责拆帧，把响应交给等待中的事务，把通知帧
  （`0xE1 STREAM_DATA` / `0xE2 EVENT`，协议 v1.1）交给注册的回调，坏帧只计数不抛异常；
- **超时与重试**：等待超时抛 :class:`~dpm_host.protocol.DpmTimeoutError`；`retries` 控制重发次数，
  重发前会清空未匹配的残留帧（避免把上一次的迟到响应当成本次的响应）；
- **超时随闸门时间自动放大**（长闸门不误报超时）：调用方给出的超时只当**下限**，实际预算不小于
  `2 × 闸门窗口 + 0.25s`（闸门窗口 = `gateTarget / f_slow`，见 :meth:`DpmClient.gate_wait_budget`）。
  闸门目标由 `GET_STATUS` / `SET_GATE` 自动缓存；
- **错误响应**：`0xFE ERR_PARAM` / `0xFF ERR_BUSY` 抛 :class:`~dpm_host.protocol.DpmError`；
  需要把「无结果」当正常情况处理时用 `accept_busy=True`（返回原始帧）或 :meth:`DpmClient.try_get_meas`。

回调在**读线程**里执行，GUI 之类的场景请自己转成队列再进主线程。
"""

from __future__ import annotations

import csv
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from . import mathx as M
from . import protocol as P
from . import sig as S
from .protocol import (
    Cmd,
    DpmError,
    DpmTimeoutError,
    Event,
    Frame,
    FrameParser,
    Identify,
    Meas,
    ParseError,
    Result,
    SigInfo,
    Status,
    StreamData,
)

__all__ = [
    "DpmClient",
    "SerialTransport",
    "ClientStats",
    "SCAN_CSV_FIELDS",
    "scan_rows_to_csv",
    "GATE_TIMEOUT_FACTOR",
    "GATE_TIMEOUT_SLACK_S",
    "gate_wait_budget_seconds",
]


#: 长闸门下的等待预算系数：预算 = `GATE_TIMEOUT_FACTOR × 闸门窗口 + GATE_TIMEOUT_SLACK_S`。
#:
#: 依据（`docs/ps-protocol.md` 第 5 节）：一次测量的时长就是闸门窗口
#: `T_gate = gateCnt / f_slow ≈ gateTarget / f_slow`（`gateCnt = gateTarget + ceil(T_sig/T_slow)`，
#: 多出的一个信号周期可忽略）。旧的固定超时（单次请求 0.5s、`--result-timeout` 2.0s）在闸门
#: 时间接近或超过它时，**测量本身没问题也会报超时**：闸门 2.4s 时必然超过 2.0s 的等结果预算。
#: ×2 覆盖「等到一半时结果才锁存」「连续模式读完立刻重启下一次」这类最坏情况，固定余量再吸收
#: 命令往返与结果锁存延迟。
GATE_TIMEOUT_FACTOR = 2.0
GATE_TIMEOUT_SLACK_S = 0.25


def gate_wait_budget_seconds(gate_target: int | None, slow_hz: int) -> float:
    """闸门目标 + 低速时钟 → 等一次结果的等待预算（秒）。

    = `GATE_TIMEOUT_FACTOR × gateTarget/f_slow + GATE_TIMEOUT_SLACK_S`；闸门目标未知或设备
    报 0 时返回 0（不放大，沿用调用方给的超时）。做成模块级函数是为了让 GUI/CLI 在还没连上
    设备（或还没读到 `GET_STATUS`）时也能把预算显示出来；:meth:`DpmClient.gate_wait_budget`
    是它在当前连接上的快捷方式。
    """
    if not gate_target or int(slow_hz) <= 0:
        return 0.0
    return GATE_TIMEOUT_FACTOR * (float(gate_target) / float(slow_hz)) + GATE_TIMEOUT_SLACK_S


class Transport:
    """客户端用到的传输层接口（`SerialTransport` 或测试用的内存实现）。"""

    def write(self, data: bytes) -> int:  # pragma: no cover - 接口
        raise NotImplementedError

    def read(self, size: int = 1, timeout: float | None = None) -> bytes:  # pragma: no cover - 接口
        raise NotImplementedError

    def reset_input_buffer(self) -> None:  # pragma: no cover - 接口
        pass

    def close(self) -> None:  # pragma: no cover - 接口
        pass

    @property
    def description(self) -> str:  # pragma: no cover - 接口
        return "transport"


class SerialTransport(Transport):
    """pyserial 传输层：8N1、无流控（`docs/ps-protocol.md` 第 1 节）。"""

    def __init__(self, port: str, baudrate: int = P.DEFAULT_BAUD, read_timeout: float = 0.05) -> None:
        import serial  # 延迟导入：无串口需求时不依赖 pyserial 的加载

        self.port = port
        self.baudrate = int(baudrate)
        self._ser = serial.Serial(
            port=port,
            baudrate=int(baudrate),
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=read_timeout,
            write_timeout=1.0,
        )

    def write(self, data: bytes) -> int:
        return int(self._ser.write(data))

    def read(self, size: int = 1, timeout: float | None = None) -> bytes:
        if timeout is not None:
            self._ser.timeout = timeout
        return bytes(self._ser.read(size))

    def reset_input_buffer(self) -> None:
        try:
            self._ser.reset_input_buffer()
        except Exception:  # pragma: no cover - pty 上的兜底
            pass

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:  # pragma: no cover
            pass

    @property
    def description(self) -> str:
        return f"serial:{self.port}@{self.baudrate}"

    @property
    def is_open(self) -> bool:
        return bool(self._ser.is_open)


@dataclass
class ClientStats:
    """收发统计（诊断用）。"""

    tx_frames: int = 0
    rx_frames: int = 0
    parse_errors: int = 0
    timeouts: int = 0
    retries: int = 0
    unexpected: int = 0
    notifications: int = 0
    last_parse_error: ParseError | None = None

    def snapshot(self) -> dict[str, int]:
        return {
            "tx": self.tx_frames,
            "rx": self.rx_frames,
            "parseErrors": self.parse_errors,
            "timeouts": self.timeouts,
            "retries": self.retries,
            "unexpected": self.unexpected,
            "notifications": self.notifications,
        }


class DpmClient:
    """DPM 串口客户端（线程安全的请求/响应 + 通知回调）。"""

    def __init__(
        self,
        port: str | None = None,
        *,
        baud: int = P.DEFAULT_BAUD,
        timeout: float = 0.5,
        retries: int = 1,
        retry_delay: float = 0.02,
        transport: Transport | None = None,
        slow_hz: int = M.SLOW_HZ_DEFAULT,
        dsp_hz: int = M.DSP_HZ_DEFAULT,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.port = port
        self.baud = int(baud)
        self.timeout = float(timeout)
        self.retries = int(retries)
        self.retry_delay = float(retry_delay)
        self.slow_hz = int(slow_hz)
        self.dsp_hz = int(dsp_hz)
        #: 设备当前的目标闸门计数（`GET_STATUS.gateTarget` / `SET_GATE` 回显）：等待预算按它换算；
        #: 未知或设备报 0 时为 ``None``（此时不放大，只用调用方给的超时）。
        self.gate_target: int | None = None
        self.stats = ClientStats()
        #: 通知帧回调（在读线程里执行）。
        self.on_stream_data: Callable[[StreamData], None] | None = None
        self.on_event: Callable[[Event], None] | None = None
        self.on_frame: Callable[[Frame], None] | None = None
        self._log = log or (lambda _msg: None)

        self._transport: Transport | None = transport
        self._tx_lock = threading.Lock()
        self._cond = threading.Condition()
        self._pending: deque[Frame] = deque()
        self._parser = FrameParser()
        self._reader: threading.Thread | None = None
        self._closing = threading.Event()
        self._reader_error: BaseException | None = None
        #: `IDENTIFY` 结果缓存（`slow_hz` / `dsp_hz` 可被它覆盖）。
        self.identify_info: Identify | None = None

        if port is not None and transport is None:
            self.connect(port, baud=baud)

    # ------------------------------------------------------------ 连接管理

    @property
    def is_open(self) -> bool:
        return self._transport is not None and (self._reader is not None and self._reader.is_alive())

    def connect(self, port: str | None = None, *, baud: int | None = None, timeout: float | None = None) -> None:
        """打开串口并启动读线程（重复调用会先关闭旧连接）。"""
        self.close()
        target = port or self.port
        if target is None:
            raise ValueError("未指定串口（port）")
        if timeout is not None:
            self.timeout = float(timeout)
        if baud is not None:
            self.baud = int(baud)
        self.port = target
        self._transport = SerialTransport(target, self.baud)
        self._closing.clear()
        self._reader_error = None
        self._parser.reset()
        with self._cond:
            self._pending.clear()
        self._reader = threading.Thread(target=self._reader_loop, name=f"dpm-reader-{target}", daemon=True)
        self._reader.start()
        self._log(f"已连接 {target}@{self.baud}")

    def close(self) -> None:
        """关闭串口并停止读线程。"""
        self._closing.set()
        with self._cond:
            self._cond.notify_all()
        reader = self._reader
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        self._reader = None
        if self._transport is not None:
            try:
                self._transport.close()
            finally:
                self._transport = None
        self._log("已断开")

    def __enter__(self) -> "DpmClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ 读线程

    def _reader_loop(self) -> None:  # pragma: no cover - 线程体，靠集成测试覆盖
        transport = self._transport
        assert transport is not None
        while not self._closing.is_set():
            try:
                chunk = transport.read(1, timeout=0.05)
            except Exception as exc:  # 串口被拔掉 / pty 关闭
                if not self._closing.is_set():
                    self._reader_error = exc
                    self._log(f"读线程异常退出：{exc!r}")
                break
            if not chunk:
                continue
            events: list[object] = []
            try:
                events = list(self._parser.feed(chunk))
            except Exception as exc:  # pragma: no cover - 解析器不应抛异常
                self._log(f"解析异常：{exc!r}")
                continue
            for ev in events:
                if isinstance(ev, ParseError):
                    self.stats.parse_errors += 1
                    self.stats.last_parse_error = ev
                    self._log(f"丢弃坏数据（{ev.kind}）：{ev.detail}")
                    continue
                self.stats.rx_frames += 1
                self._dispatch(ev)
        with self._cond:
            self._cond.notify_all()

    def _dispatch(self, frame: Frame) -> None:
        """分发一帧：通知帧走回调，其余交给等待中的事务。

        通知码是 `0xE1/0xE2`（协议 v1.1；`cmd & 0xF0 == 0xE0` 即通知），与响应码
        `0x81..0x8E` 没有交集，因此**按码判定**即可，不再需要任何长度兜底。
        """
        if frame.cmd == Cmd.STREAM_DATA:
            try:
                data = P.decode_stream_data(frame.payload)
            except P.ProtocolError as exc:
                self._log(f"STREAM_DATA 解析失败：{exc}")
                return
            self.stats.notifications += 1
            if self.on_stream_data is not None:
                self._call_callback(self.on_stream_data, data)
            return
        if frame.cmd == Cmd.EVENT:
            try:
                event = P.decode_event(frame.payload)
            except P.ProtocolError as exc:
                self._log(f"EVENT 解析失败：{exc}")
                return
            self.stats.notifications += 1
            if self.on_event is not None:
                self._call_callback(self.on_event, event)
            return
        if P.is_notification_code(frame.cmd):
            # 0xE0..0xEF 里的未知通知码：忽略并计数，不影响等待中的事务
            self.stats.notifications += 1
            self._log(f"忽略未知通知帧 0x{frame.cmd:02X}")
            return
        if self.on_frame is not None:
            self._call_callback(self.on_frame, frame)
        with self._cond:
            self._pending.append(frame)
            self._cond.notify_all()

    def _call_callback(self, callback: Callable[[object], None], arg: object) -> None:
        try:
            callback(arg)  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover - 回调异常不能拖垮读线程
            self._log(f"回调异常：{exc!r}")

    # ------------------------------------------------------------ 闸门时间与等待预算

    def gate_window_s(self) -> float | None:
        """当前闸门目标对应的测量窗口时长（秒）：`gateTarget / f_slow`；未知返回 ``None``。

        `gateCnt = gateTarget + ceil(T_sig / T_slow)`（协议第 5 节），多出的一个信号周期
        相对整个窗口可以忽略，因此这就是「启动一次测量到结果锁存」的时长。
        """
        gate = self.gate_target
        if not gate or self.slow_hz <= 0:
            return None
        return float(gate) / float(self.slow_hz)

    def gate_wait_budget(self) -> float:
        """长闸门下的等待预算（秒）：`GATE_TIMEOUT_FACTOR × 闸门窗口 + GATE_TIMEOUT_SLACK_S`。

        闸门目标未知时返回 0（不放大，沿用调用方给的超时）。它是**下限**：调用方给的超时比它
        长时以调用方为准，比它短时按它放大——否则长闸门下必然误报「等待测量结果超时」。
        """
        return gate_wait_budget_seconds(self.gate_target, self.slow_hz)

    def effective_timeout(self, requested: float | None = None, *, gate_aware: bool = True) -> float:
        """把调用方给的超时放大到「不小于等待预算」的值（`requested=None` 用 `self.timeout`）。

        `gate_aware=False` 时原样返回：只用于**不会**被闸门拖住的诊断请求（如
        :meth:`DpmClient._status_or_none` 的 `GET_STATUS`），避免链路已死时还要多等一个闸门窗口。
        """
        base = float(self.timeout if requested is None else requested)
        if not gate_aware:
            return base
        return max(base, self.gate_wait_budget())

    def refresh_gate_target(self) -> int | None:
        """读一次 `GET_STATUS` 刷新缓存的闸门目标（`GET_STATUS` 不阻塞，可在等结果前放心调用）。

        读不到时沿用缓存并记一条日志：调用方随后要发的请求自己会带上放大后的超时，
        这里的失败不该盖住真正的原因。
        """
        try:
            return self.get_status().gate_target
        except Exception as exc:  # pragma: no cover - 诊断/前置路径
            self._log(f"读取闸门目标失败（沿用缓存 {self.gate_target}）：{exc!r}")
            return self.gate_target

    def _ensure_gate_target(self) -> None:
        """闸门目标未知时先读一次 `GET_STATUS` 学会它——否则等待预算会按「未知」算成不放大。

        典型场景：`start --wait` / `meas` 这类**没有先读过状态**的调用路径——设备上闸门是
        2.4s，而上位机只知道自己的基线 2.0s，必然误报超时。`GET_STATUS` 不会被闸门拖住，
        因此这一次额外往返很便宜；目标已知时直接返回，不额外发请求。
        """
        if self.gate_target is None:
            self.refresh_gate_target()

    # ------------------------------------------------------------ 事务

    def _clear_pending(self) -> None:
        """丢弃上一次事务残留的帧（迟到/不匹配的响应），计入 `unexpected`。"""
        with self._cond:
            if self._pending:
                dropped = len(self._pending)
                self._pending.clear()
                self.stats.unexpected += dropped
                self._log(f"丢弃 {dropped} 个残留帧（上一次事务迟到/不匹配的响应）")

    def request(
        self,
        cmd: int | Cmd,
        payload: bytes = b"",
        *,
        timeout: float | None = None,
        retries: int | None = None,
        raise_on_error: bool = True,
        accept_busy: bool = False,
        accept_param: bool = False,
        gate_aware: bool = True,
    ) -> Frame:
        """发一条请求并等它的响应（`cmd | 0x80`，或错误响应码）。

        - 超时（含 `retries` 次重发后仍无响应）抛 :class:`DpmTimeoutError`；
        - 收到 `0xFE/0xFF` 抛 :class:`DpmError`，除非 `accept_busy` / `accept_param` 对应打开
          （此时返回原始帧，由调用方判断）；
        - `timeout` 只是**下限**：闸门时间更长时按 :meth:`DpmClient.effective_timeout` 放大
          （对端可能在命令内部等一个闸门窗口，见 `docs/ps-protocol.md` §3.0）；
          `gate_aware=False` 可关掉放大（诊断请求用）。
        """
        if self._transport is None:
            raise RuntimeError("客户端未连接（先 connect()）")
        cmd_enum = Cmd(int(cmd) & 0xFF)
        expected = cmd_enum.response
        timeout = self.effective_timeout(timeout, gate_aware=gate_aware)
        retries = self.retries if retries is None else int(retries)
        attempt = 0
        while True:
            attempt += 1
            request_bytes = P.encode_frame(cmd_enum, payload)
            with self._tx_lock:
                self._clear_pending()
                try:
                    self._transport.write(request_bytes)
                except Exception as exc:
                    raise DpmError(0, cmd_enum, f"写串口失败：{exc!r}") from exc
                self.stats.tx_frames += 1
                self._log(f"→ {cmd_enum.name} {P.encode_frame(cmd_enum, payload).hex(' ')}")
                deadline = time.monotonic() + timeout
                frame = self._wait_response(expected, deadline)

            if frame is not None:
                if frame.is_error:
                    if frame.cmd == Cmd.ERR_BUSY and accept_busy:
                        return frame
                    if frame.cmd == Cmd.ERR_PARAM and accept_param:
                        return frame
                    if raise_on_error:
                        raise DpmError(frame.cmd, cmd_enum)
                    return frame
                return frame

            self.stats.timeouts += 1
            if attempt <= retries:
                self.stats.retries += 1
                self._log(f"← {cmd_enum.name} 超时（{timeout:g}s），第 {attempt} 次重发")
                if self._transport is not None:
                    self._transport.reset_input_buffer()
                time.sleep(self.retry_delay)
                continue
            raise DpmTimeoutError(cmd_enum, timeout)

    def _wait_response(self, expected: int, deadline: float) -> Frame | None:
        with self._cond:
            while True:
                while self._pending:
                    frame = self._pending.popleft()
                    if frame.cmd == expected or frame.cmd in (Cmd.ERR_PARAM, Cmd.ERR_BUSY):
                        if not frame.is_error:
                            try:
                                # 字长校验放在这里：对端用错编码/半帧时只丢弃并继续等，
                                # 不把长度不符的数据当成有效响应返回
                                P.check_response_payload(frame.cmd, frame.payload)
                            except P.PayloadError as exc:
                                self.stats.unexpected += 1
                                self._log(f"响应字长不符，已丢弃并继续等待：{exc}")
                                continue
                        return frame
                    self.stats.unexpected += 1
                    self._log(f"忽略不匹配的响应：{frame.name}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    # ------------------------------------------------------------ 高层命令

    def ping(self, **kw: object) -> bool:
        """`PING`：连通性探测。"""
        self.request(Cmd.PING, **kw)  # type: ignore[arg-type]
        return True

    def identify(self, *, refresh: bool = True, **kw: object) -> Identify:
        """`IDENTIFY`：版本、能力与两侧时钟频率（同时更新客户端用的 `slow_hz`/`dsp_hz`）。"""
        if self.identify_info is not None and not refresh:
            return self.identify_info
        frame = self.request(Cmd.IDENTIFY, **kw)  # type: ignore[arg-type]
        info = P.decode_identify(frame.payload)
        self.identify_info = info
        if info.slow_clk_hz:
            self.slow_hz = int(info.slow_clk_hz)
        if info.dsp_clk_hz:
            self.dsp_hz = int(info.dsp_clk_hz)
        return info

    def get_status(self, **kw: object) -> Status:
        """`GET_STATUS`（顺带把 `gateTarget` 记进缓存，供长闸门的等待预算使用）。"""
        frame = self.request(Cmd.GET_STATUS, **kw)  # type: ignore[arg-type]
        st = P.decode_status(frame.payload)
        if st.gate_target > 0:
            self.gate_target = int(st.gate_target)
        return st

    def start(self, *, accept_busy: bool = True, **kw: object) -> bool:
        """`START`：启动一次测量，返回「是否真的发出了新的启动」。

        CTRL.start 在 `busy` 期间会被 PL 忽略（协议第 3 节），而 PS 侧实现可能直接回
        `ERR_BUSY`；两种语义对调用方等价（「已经在测量中」），因此默认把 `ERR_BUSY`
        当正常情况（`accept_busy=True`）。
        """
        frame = self.request(Cmd.START, accept_busy=accept_busy, **kw)  # type: ignore[arg-type]
        if frame.is_error:
            self._log("START 被忽略：当前已在测量中（ERR_BUSY）")
            return False
        return True

    def get_result(self, **kw: object) -> Result:
        """`GET_RESULT`：读结果寄存器（`valid=0` 表示还没有新结果）。"""
        frame = self.request(Cmd.GET_RESULT, **kw)  # type: ignore[arg-type]
        return P.decode_result(frame.payload)

    def get_meas(self, **kw: object) -> Meas:
        """`GET_MEAS`：读频率/相位（无有效结果时 PS 回 `ERR_BUSY`）。"""
        frame = self.request(Cmd.GET_MEAS, **kw)  # type: ignore[arg-type]
        return P.decode_meas(frame.payload)

    def try_get_meas(self, **kw: object) -> Meas | None:
        """`GET_MEAS` 的宽松版本：`ERR_BUSY`（暂无有效结果）返回 ``None``。"""
        frame = self.request(Cmd.GET_MEAS, accept_busy=True, **kw)  # type: ignore[arg-type]
        if frame.is_error:
            return None
        return P.decode_meas(frame.payload)

    def set_gate(self, gate_target: int, **kw: object) -> int:
        """`SET_GATE`：返回生效的目标闸门计数（顺带更新本地缓存，之后的等待预算按它换算）。"""
        frame = self.request(Cmd.SET_GATE, P.encode_set_gate(gate_target), **kw)  # type: ignore[arg-type]
        got = P.decode_set_gate(frame.payload)
        if got > 0:
            self.gate_target = int(got)
        return got

    def set_mode(self, mode: int | str, **kw: object) -> int:
        """`SET_MODE`：0/single、1/continuous。"""
        value = P.str_to_mode(mode) if isinstance(mode, str) else int(mode)
        frame = self.request(Cmd.SET_MODE, P.encode_set_mode(value), **kw)  # type: ignore[arg-type]
        return P.decode_set_mode(frame.payload)

    def set_source(self, source: int | str, **kw: object) -> int:
        """`SET_SOURCE`：返回 `SIG_STATUS`。"""
        value = P.str_to_source(source) if isinstance(source, str) else int(source)
        frame = self.request(Cmd.SET_SOURCE, P.encode_set_source(value), **kw)  # type: ignore[arg-type]
        return P.decode_sig_status_u32(frame.payload)

    def set_sig(self, freq_hz: int, phase_milli_deg: int, **kw: object) -> SigInfo:
        """`SET_SIG`：设置片内源频率与相位差（返回实际生效值）。"""
        frame = self.request(Cmd.SET_SIG, P.encode_set_sig(freq_hz, phase_milli_deg), **kw)  # type: ignore[arg-type]
        return P.decode_sig_info(frame.payload)

    def get_sig(self, **kw: object) -> SigInfo:
        """`GET_SIG`：读回片内源当前工作点。"""
        frame = self.request(Cmd.GET_SIG, **kw)  # type: ignore[arg-type]
        return P.decode_sig_info(frame.payload)

    def clear(self, **kw: object) -> None:
        """`CLEAR`：复位结果区与标志（busy 期间 PL 会挂起）。"""
        self.request(Cmd.CLEAR, **kw)  # type: ignore[arg-type]

    def stream(self, enable: bool | int, interval_ms: int = P.DEFAULT_STREAM_INTERVAL_MS, **kw: object) -> tuple[int, int]:
        """`STREAM`：开关流模式，返回生效的 (enable, intervalMs)。"""
        frame = self.request(Cmd.STREAM, P.encode_stream(1 if enable else 0, interval_ms), **kw)  # type: ignore[arg-type]
        return P.decode_stream(frame.payload)

    def reset_counters(self, **kw: object) -> None:
        """`RESET_COUNTERS`：清丢失计数等统计量。"""
        self.request(Cmd.RESET_COUNTERS, **kw)  # type: ignore[arg-type]

    # ------------------------------------------------------------ 等待/采样

    def wait_idle(self, timeout: float = 2.0, poll_interval: float = 0.002) -> Status:
        """等到 `STATUS.busy = 0`（配置 DRP / SET_SIG 前必须空闲）。

        `timeout` 是**下限**：长闸门下一次测量本来就要跑一个闸门窗口（对端在 `SET_SIG`/
        `CLEAR` 内部也会等到空闲，见协议 §3.0），因此实际按 :meth:`DpmClient.effective_timeout`
        放大。
        """
        self._ensure_gate_target()
        budget = self.effective_timeout(timeout)
        deadline = time.monotonic() + budget
        last: Status | None = None
        while True:
            last = self.get_status()
            if not last.busy:
                return last
            if time.monotonic() >= deadline:
                raise DpmTimeoutError(None, budget, "等待 STATUS.busy=0 超时")
            time.sleep(poll_interval)

    def _no_result_detail(self, st: Status | None) -> str:
        """等不到结果时的诊断文本（给 :class:`DpmTimeoutError` 的 detail）。

        「等不到结果」是**正常的输入状态**（协议 §3.0：源没有边沿时测量永不完成），
        不是致命错误：切到没接信号的外部输入源时必现。因此消息要说清现在选的是哪个源、
        为什么等不到、以及怎么恢复——否则调用方只会看到一句无信息量的「超时」。
        """
        parts = ["等待测量结果超时（源没有边沿时测量永不完成）"]
        window = self.gate_window_s()
        if window is not None:
            parts.append(
                f"当前闸门目标 {self.gate_target} 低速周期 = {window:g}s，"
                f"已按 {self.gate_wait_budget():g}s 的预算等待"
                f"（{GATE_TIMEOUT_FACTOR:g}×闸门 + {GATE_TIMEOUT_SLACK_S:g}s）"
            )
        if st is not None:
            source = "片内信号源" if st.sig_select else "外部引脚 sig1/sig2"
            parts.append(
                f"当前输入源 = {source}，status=0x{st.status:08X}"
                f"（busy={int(st.busy)} resultValid={int(st.result_valid)}）"
                f"、sigStatus=0x{st.sig_status:08X}（sourceReady={int(st.source_ready)}）"
            )
            if not st.source_ready:
                parts.append("片内源未使能或 MMCM 未锁定：先 SET_SOURCE(1) 等 sourceReady=1，或改用外部输入")
            elif not st.sig_select:
                parts.append(
                    "外部引脚没有边沿：确认 sig1/sig2 已接两路同频信号、与开发板共地，"
                    "电平满足引脚 IO 标准（默认 LVCMOS33），且频率落在闸门窗口内"
                )
            if st.busy and st.result_valid:
                parts.append(
                    "结果槽里还留着上一次未读的结果（resultValid=1），本次测量还没产出新结果——"
                    "对端按「不把旧结果当新结果」处理，所以这里继续等的是本次的结果"
                )
        parts.append("挂起的这次测量会一直等边沿：接好信号或切回片内信号源后它才会结束")
        return "；".join(parts)

    def _status_or_none(self) -> Status | None:
        """诊断用：尽量读一次 `GET_STATUS`，读不到就返回 ``None``（不掩盖原来的错误）。

        这里显式 `gate_aware=False`：`GET_STATUS` 不会被闸门拖住，链路已经出问题时不该再按
        闸门预算多等一个窗口才把诊断信息交出去。
        """
        try:
            return self.get_status(timeout=min(float(self.timeout), 0.2), retries=0, gate_aware=False)
        except Exception as exc:  # pragma: no cover - 诊断路径，失败不影响原错误
            self._log(f"诊断用 GET_STATUS 失败：{exc!r}")
            return None

    def _poll_result(self) -> Result | None:
        """`GET_RESULT` 的**容错**探针：没有结果时返回 ``None``，不抛异常。

        `ERR_BUSY`（0xFF）在这里表示「还没有结果」而不是「命令被拒」：协议 §3.0 规定
        结果就绪的权威探针就是 `GET_RESULT.valid`，因此固件可能用两种等价方式表达
        「暂无结果」——`valid=0`（参照实现与协议定稿），或等不到结果时回 `ERR_BUSY`
        （早期 C 侧固件的行为，见 `docs/ps-protocol.md` §3.0）。两种都要当「继续轮询」，
        不能当致命错误：切到没有信号的外部输入源时必现，把它当错误会让测量程序直接中止。
        """
        frame = self.request(Cmd.GET_RESULT, accept_busy=True)  # type: ignore[arg-type]
        if frame.is_error:
            return None
        result = P.decode_result(frame.payload)
        return result if result.valid else None

    def wait_result(self, timeout: float = 2.0, poll_interval: float = 0.002) -> Result:
        """等一次结果，返回 `GET_RESULT` 的原始观测量。

        以 `GET_RESULT.valid` 为**权威探针**（协议里它就是这个用途）：读到 `valid=1`
        的同时也就把结果读走了。不能拿 `GET_RESULT` 之外的 `GET_STATUS.resultValid`
        当唯一依据——PS 侧实现可能是「事件驱动」的（例如每次 AXI 事务才推进一次模型时间），
        状态位与结果锁存之间存在时序差；本方法对两种实现都成立。
        超时抛 :class:`DpmTimeoutError`（带输入源诊断），**不**把「暂无结果」当设备错误。

        `timeout` 是**下限**：实际预算还会按闸门时间放大（见 :meth:`gate_wait_budget`），
        否则长闸门下必然误报超时。单次 `GET_RESULT` 请求超时也只当「这一拍没问出结果」
        （对端可能在命令内部等闸门窗口），继续轮询到总预算再报超时 + 诊断。
        """
        self._ensure_gate_target()  # 先学一次闸门目标，好把预算算对（GET_STATUS 不阻塞）
        budget = self.effective_timeout(timeout)
        deadline = time.monotonic() + budget
        while True:
            try:
                result = self._poll_result()
            except DpmTimeoutError as exc:
                self._log(f"GET_RESULT 单次请求超时（{exc.timeout:g}s），继续等结果……")
                result = None
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                raise DpmTimeoutError(None, budget, self._no_result_detail(self._status_or_none()))
            time.sleep(poll_interval)

    def wait_meas(self, timeout: float = 2.0, poll_interval: float = 0.002) -> Meas:
        """等一次结果并返回 `GET_MEAS` 的测量值。

        路径：`GET_STATUS.resultValid` 置位时用 `GET_MEAS`（顺便验证 GET_MEAS 通路）；
        否则退回 `GET_RESULT.valid` 探针，并**用计数本地换算**成 `Meas`
        （对「结果锁存与状态位有时序差」的 PS 实现也成立）。

        两条路径上的 `ERR_BUSY` 都只表示「**还没有结果**」（`GET_MEAS` 没有 `valid`
        字段，只能用错误码表达同一件事），一律继续轮询到 `timeout` 再抛
        :class:`DpmTimeoutError`——「源没有边沿」是正常的输入状态，不是设备错误。

        `timeout` 是**下限**：实际预算按闸门时间放大，长闸门下不会因为闸门比预算长而误报。
        """
        self._ensure_gate_target()
        budget = self.effective_timeout(timeout)
        deadline = time.monotonic() + budget
        status_miss_logged = False
        while True:
            st = self.get_status()  # 顺带刷新闸门目标
            if st.result_valid:
                try:
                    return self.get_meas()
                except DpmTimeoutError as exc:
                    # 单次请求超时 = 这一拍没问出来（对端可能正阻塞在闸门等待里），继续轮询
                    self._log(f"GET_MEAS 单次请求超时（{exc.timeout:g}s），继续等结果……")
                except DpmError as exc:
                    if exc.code != int(Cmd.ERR_BUSY):
                        raise
            try:
                result = self._poll_result()
            except DpmTimeoutError as exc:
                self._log(f"GET_RESULT 单次请求超时（{exc.timeout:g}s），继续等结果……")
                result = None
            if result is not None:
                if not status_miss_logged and not st.result_valid:
                    self._log(
                        "对端在 GET_RESULT.valid=1 时仍未置 STATUS.resultValid（协议第 3 节 STATUS b1）；"
                        "已用 GET_RESULT 兜底（若为 C 侧实现请确认状态位时序）"
                    )
                    status_miss_logged = True
                calc = self.to_meas(result)
                return Meas(
                    seq=result.seq,
                    gate_cnt=result.gate_cnt,
                    edge_cnt=result.edge_cnt,
                    phase_cnt=result.phase_cnt,
                    freq_milli_hz=calc.freq_hz,
                    phase_milli_deg=calc.phase_milli_deg,
                )
            if time.monotonic() >= deadline:
                raise DpmTimeoutError(None, budget, self._no_result_detail(self._status_or_none()))
            time.sleep(poll_interval)

    def wait_source_ready(self, timeout: float = 2.0, poll_interval: float = 0.002) -> Status:
        """等到 `SIG_STATUS.sourceReady = 1`（配置快照送达 + MMCM 锁定）。

        `timeout` 是**下限**：切换输入源/改片内源时对端可能先等当前测量结束（`SET_SOURCE` /
        `SET_SIG` 的有界等待，见协议 §3.0），长闸门下这一步也要一个闸门窗口。
        """
        self._ensure_gate_target()
        budget = self.effective_timeout(timeout)
        deadline = time.monotonic() + budget
        last: Status | None = None
        while True:
            last = self.get_status()
            if last.source_ready:
                return last
            if time.monotonic() >= deadline:
                raise DpmTimeoutError(
                    None,
                    budget,
                    f"等待 sourceReady 超时（status=0x{last.status:08X} sigStatus=0x{last.sig_status:08X}）",
                )
            time.sleep(poll_interval)

    def measure_once(self, timeout: float = 2.0, *, start: bool = True) -> tuple[Result, Meas, M.Meas]:
        """启动一次测量并读回 (Result, Meas, 本地换算结果)。

        `timeout` 是等结果的下限，实际按闸门时间放大（见 :meth:`wait_meas`）。
        """
        if start:
            self.start()
        meas = self.wait_meas(timeout=timeout)
        result = Result(
            valid=True,
            seq=meas.seq,
            gate_cnt=meas.gate_cnt,
            edge_cnt=meas.edge_cnt,
            phase_cnt=meas.phase_cnt,
        )
        return result, meas, self.calc_meas(meas)

    def to_meas(self, result: Result) -> M.Meas:
        """把原始观测量按当前时钟换算成定点结果（与 C 侧同契约）。"""
        return M.meas_calc(result.gate_cnt, result.edge_cnt, result.phase_cnt, self.slow_hz, self.dsp_hz)

    def calc_meas(self, meas: Meas) -> M.Meas:
        """把 `GET_MEAS`/`STREAM_DATA` 的原始计数按两侧契约换算（**显示与判据用这个**）。

        协议第 5 节的字段单位是「整数 Hz / 毫度」（早期毫赫兹口径会溢出 u32，已作废）；
        即便如此，显示与判据仍一律以 `gateCnt/edgeCnt/phaseCnt` 的本地换算为准
        （对端若用错单位，本地值仍然正确，必要时由 :func:`fmt` 提示不一致）。
        """
        return M.meas_calc(meas.gate_cnt, meas.edge_cnt, meas.phase_cnt, self.slow_hz, self.dsp_hz)

    def monitor(
        self,
        *,
        count: int | None = None,
        duration: float | None = None,
        interval: float = 0.0,
        use_stream: bool = False,
        interval_ms: int = P.DEFAULT_STREAM_INTERVAL_MS,
        on_sample: Callable[[Meas, M.Meas], None] | None = None,
        timeout: float = 2.0,
        continuous: bool = True,
    ) -> list[tuple[Meas, M.Meas]]:
        """连续测量 N 次或指定时长，返回 [(Meas, 换算结果)]。

        - `use_stream=True` 时打开 `STREAM`，用通知帧回调收集数据；
        - 否则用 `GET_MEAS` 轮询（连续模式下 PS 会在结果读走后立即重启下一次）；
        - `timeout` 是每次等结果的下限，长闸门下自动放大（见 :meth:`wait_meas`）。
        """
        samples: list[tuple[Meas, M.Meas]] = []
        deadline = None if duration is None else time.monotonic() + duration

        if use_stream:

            def _on_stream(data: StreamData) -> None:  # 读线程回调
                meas = data.meas
                calc = M.meas_calc(meas.gate_cnt, meas.edge_cnt, meas.phase_cnt, self.slow_hz, self.dsp_hz)
                samples.append((meas, calc))
                if on_sample is not None:
                    on_sample(meas, calc)

            self.on_stream_data = _on_stream
            self.stream(True, interval_ms)
            try:
                if count is not None:
                    while len(samples) < count:
                        if deadline is not None and time.monotonic() >= deadline:
                            break
                        time.sleep(0.002)
                else:
                    while deadline is None or time.monotonic() < deadline:
                        time.sleep(0.005)
            finally:
                self.stream(False, interval_ms)
                self.on_stream_data = None
            return samples

        if continuous:
            self.set_mode(P.MODE_CONTINUOUS)
        self.start()
        try:
            while True:
                if count is not None and len(samples) >= count:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                meas = self.wait_meas(timeout=timeout)
                calc = self.calc_meas(meas)
                samples.append((meas, calc))
                if on_sample is not None:
                    on_sample(meas, calc)
                if interval > 0:
                    time.sleep(interval)
        finally:
            if continuous:
                self.set_mode(P.MODE_SINGLE)
        return samples

    def scan(
        self,
        freqs_hz: Sequence[int],
        phases_deg: Sequence[float] = (0.0,),
        *,
        gate_target: int | None = None,
        source: int | str = P.SOURCE_INTERNAL,
        settle: float = 0.0,
        timeout: float = 2.0,
        result_timeout: float = 2.0,
        on_row: Callable[[dict], None] | None = None,
        log: Callable[[str], None] | None = None,
        max_error_milli_hz: int | None = None,
    ) -> list[dict]:
        """多频点 × 多相位扫描，返回每行的原始数据（CLI 的 `scan` 子命令使用）。

        每个 (频点, 相位) 组合：等空闲 → `SET_SIG` → 等 `sourceReady` → `START` → 等结果 →
        `GET_MEAS`。`gate_target` 给出时先 `SET_GATE`；`max_error_milli_hz` 给出时，
        `SET_SIG` 后的实际频率偏差超过它就跳过该行并记一条日志（避免把「实现不了的频点」
        混进 CSV）。

        `timeout` / `result_timeout` 都是**下限**：设置了长闸门时，等空闲、等 `sourceReady`
        与等结果都会按闸门时间自动放大（见 :meth:`gate_wait_budget`），不会误报超时。
        """
        emit = log or (lambda _msg: None)
        rows: list[dict] = []
        if gate_target is not None:
            emit(f"设置闸门目标 = {gate_target}")
            self.set_gate(gate_target)
        self.set_mode(P.MODE_SINGLE)
        status = self.get_status()
        if isinstance(source, str):
            source_value = P.str_to_source(source)
        else:
            source_value = int(source)
        if not status.sig_select and source_value == P.SOURCE_INTERNAL:
            emit("切换到片内信号源")
            self.set_source(P.SOURCE_INTERNAL)
        elif status.sig_select and source_value == P.SOURCE_EXTERNAL:
            emit("切换到外部输入")
            self.set_source(P.SOURCE_EXTERNAL)

        for freq_hz in freqs_hz:
            freq_hz = int(round(float(freq_hz)))
            for phase_deg in phases_deg:
                phase_mdeg = int(round(float(phase_deg) * 1000))
                self.wait_idle(timeout=timeout)
                try:
                    info = self.set_sig(freq_hz, phase_mdeg)
                except DpmError as exc:
                    # 相位上界 = 9 位相位码（按 D 逐点，见 docs/ps-protocol.md §6.2）：越界的
                    # (频点, 相位) 组合服务端回 ERR_PARAM。这是**该点不可实现**，不是链路故障，
                    # 因此跳过该点继续扫（否则一个越界点会让整轮扫描中断）。
                    # 其它错误码（如 ERR_BUSY）仍然抛出。
                    if exc.code != P.Cmd.ERR_PARAM:
                        raise
                    emit(f"跳过 {freq_hz / 1e6:g}MHz/{phase_mdeg / 1000:g}°：" f"SET_SIG 回 ERR_PARAM（相位超出该频点的 9 位相位码上界）")
                    continue
                if max_error_milli_hz is not None and abs(info.freq_hz - freq_hz) * 1000 > max_error_milli_hz:
                    emit(f"跳过 {freq_hz / 1e6:g}MHz：实现频率 {info.freq_hz / 1e6:g}MHz 偏差过大")
                    continue
                if settle > 0:
                    time.sleep(settle)
                self.wait_source_ready(timeout=timeout)
                result, meas, calc = self.measure_once(timeout=result_timeout)
                sem = S.current_semantics()
                quantum = S.phase_quantum_milli_deg(info.div, sem)
                # 相位是否被设备真的接受（当前 bitstream 不可控时，实现值恒为静态 90°）
                tolerated = 0 if quantum is None else max(1, quantum // 2 + 1)
                phase_honored = abs(info.phase_milli_deg - phase_mdeg) <= tolerated
                row = {
                    "freq_nominal": freq_hz,
                    "phase_nominal": float(phase_deg),
                    # 列名沿用验收要求；单位 = 协议字段单位（Hz，见 docs/ps-protocol.md 第 5 节）
                    "f_meas_mHz": calc.freq_hz,
                    "phase_meas_mdeg": calc.phase_milli_deg,
                    "gateCnt": result.gate_cnt,
                    "edgeCnt": result.edge_cnt,
                    "phaseCnt": result.phase_cnt,
                    "phaseCycles": M.phase_cycles_float(result.phase_cnt, result.edge_cnt),
                    "err_deg": M.phase_err_deg(calc.phase_milli_deg, float(phase_deg)),
                    # 第 10 列（Lead 要求）：相位是否可在线设定
                    "phase_controllable": bool(sem.phase_controllable),
                    # 扩展列（人类可读/诊断用）
                    "seq": result.seq,
                    "f_meas_Hz": calc.freq_hz,
                    "phase_meas_deg": calc.phase_milli_deg / 1000.0,
                    "freq_err_ppm": M.freq_err_ppm(calc.freq_hz, freq_hz),
                    "phase_step_mdeg": M.phase_quant_milli_deg(calc.freq_hz, self.dsp_hz),
                    "vco_MHz": info.vco_hz / 1e6,
                    "div": info.div,
                    "phase_word": info.phase_field,
                    "phase_set_mdeg": info.phase_milli_deg,
                    "phase_honored": phase_honored,
                    "phase_grid_mdeg": float(S.mmcm_phase_grid_milli_deg(info.div)) if info.div else None,
                }
                rows.append(row)
                if on_row is not None:
                    on_row(row)
        return rows


#: `scan --out` 的 CSV 列：前 9 列是验收要求的列（列名保留，`f_meas_mHz` 的单位是 Hz），
#: 第 10 列 `phase_controllable` 由 Lead 要求追加（当前 bitstream 下相位不可在线改）。
SCAN_CSV_FIELDS: tuple[str, ...] = (
    "freq_nominal",
    "phase_nominal",
    "f_meas_mHz",
    "phase_meas_mdeg",
    "gateCnt",
    "edgeCnt",
    "phaseCnt",
    "phaseCycles",
    "err_deg",
    "phase_controllable",
)


def scan_rows_to_csv(rows: Iterable[dict], path: str, fields: Sequence[str] = SCAN_CSV_FIELDS) -> None:
    """把扫描结果写成 CSV（`phaseCycles` 保留 6 位小数，`err_deg` 保留 4 位）。"""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            if isinstance(out.get("phaseCycles"), float):
                out["phaseCycles"] = f"{out['phaseCycles']:.6f}"
            if isinstance(out.get("err_deg"), float):
                out["err_deg"] = f"{out['err_deg']:.4f}"
            writer.writerow(out)
