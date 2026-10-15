"""串口客户端：请求/响应、超时重试、线程安全与通知帧回调。

设计要点：

- **单事务串行**：协议是半双工的请求/响应，`:meth:`DpmClient.request`` 用一把锁把「写请求 →
  等响应」做成一个原子事务，多个线程调用会被串行化；
- **后台读线程**：唯一的读线程负责拆帧，把响应交给等待中的事务，把通知帧
  （`0xE1 STREAM_DATA` / `0xE2 EVENT`，协议 v1.1）交给注册的回调，坏帧只计数不抛异常；
- **超时与重试**：等待超时抛 :class:`~dpm_host.protocol.DpmTimeoutError`；`retries` 控制重发次数，
  重发前会清空未匹配的残留帧（避免把上一次的迟到响应当成本次的响应）；
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
    Frame,
    FrameParser,
    Meas,
    ParseError,
    Result,
    SigInfo,
    Status,
    Identify,
    StreamData,
    Event,
)

__all__ = ["DpmClient", "SerialTransport", "ClientStats", "SCAN_CSV_FIELDS", "scan_rows_to_csv"]


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
    ) -> Frame:
        """发一条请求并等它的响应（`cmd | 0x80`，或错误响应码）。

        - 超时（含 `retries` 次重发后仍无响应）抛 :class:`DpmTimeoutError`；
        - 收到 `0xFE/0xFF` 抛 :class:`DpmError`，除非 `accept_busy` / `accept_param` 对应打开
          （此时返回原始帧，由调用方判断）。
        """
        if self._transport is None:
            raise RuntimeError("客户端未连接（先 connect()）")
        cmd_enum = Cmd(int(cmd) & 0xFF)
        expected = cmd_enum.response
        timeout = self.timeout if timeout is None else float(timeout)
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
        """`GET_STATUS`。"""
        frame = self.request(Cmd.GET_STATUS, **kw)  # type: ignore[arg-type]
        return P.decode_status(frame.payload)

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
        """`SET_GATE`：返回生效的目标闸门计数。"""
        frame = self.request(Cmd.SET_GATE, P.encode_set_gate(gate_target), **kw)  # type: ignore[arg-type]
        return P.decode_set_gate(frame.payload)

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
        """等到 `STATUS.busy = 0`（配置 DRP / SET_SIG 前必须空闲）。"""
        deadline = time.monotonic() + timeout
        last: Status | None = None
        while True:
            last = self.get_status()
            if not last.busy:
                return last
            if time.monotonic() >= deadline:
                raise DpmTimeoutError(None, timeout, "等待 STATUS.busy=0 超时")
            time.sleep(poll_interval)

    def wait_result(self, timeout: float = 2.0, poll_interval: float = 0.002) -> Result:
        """等一次结果，返回 `GET_RESULT` 的原始观测量。

        以 `GET_RESULT.valid` 为**权威探针**（协议里它就是这个用途）：读到 `valid=1`
        的同时也就把结果读走了。不能拿 `GET_RESULT` 之外的 `GET_STATUS.resultValid`
        当唯一依据——PS 侧实现可能是「事件驱动」的（例如每次 AXI 事务才推进一次模型时间），
        状态位与结果锁存之间存在时序差；本方法对两种实现都成立。
        """
        deadline = time.monotonic() + timeout
        while True:
            result = self.get_result()
            if result.valid:
                return result
            if time.monotonic() >= deadline:
                raise DpmTimeoutError(None, timeout, "等待测量结果超时（源没有边沿时测量永不完成）")
            time.sleep(poll_interval)

    def wait_meas(self, timeout: float = 2.0, poll_interval: float = 0.002) -> Meas:
        """等一次结果并返回 `GET_MEAS` 的测量值。

        路径：`GET_STATUS.resultValid` 置位时用 `GET_MEAS`（顺便验证 GET_MEAS 通路）；
        否则退回 `GET_RESULT.valid` 探针，并**用计数本地换算**成 `Meas`
        （对「结果锁存与状态位有时序差」的 PS 实现也成立）。
        """
        deadline = time.monotonic() + timeout
        status_miss_logged = False
        while True:
            st = self.get_status()
            if st.result_valid:
                try:
                    return self.get_meas()
                except DpmError as exc:
                    if exc.code != int(Cmd.ERR_BUSY):
                        raise
            result = self.get_result()
            if result.valid:
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
                raise DpmTimeoutError(None, timeout, "等待测量结果超时（源没有边沿时测量永不完成）")
            time.sleep(poll_interval)

    def wait_source_ready(self, timeout: float = 2.0, poll_interval: float = 0.002) -> Status:
        """等到 `SIG_STATUS.sourceReady = 1`（配置快照送达 + MMCM 锁定）。"""
        deadline = time.monotonic() + timeout
        last: Status | None = None
        while True:
            last = self.get_status()
            if last.source_ready:
                return last
            if time.monotonic() >= deadline:
                raise DpmTimeoutError(
                    None, timeout,
                    f"等待 sourceReady 超时（status=0x{last.status:08X} sigStatus=0x{last.sig_status:08X}）",
                )
            time.sleep(poll_interval)

    def measure_once(self, timeout: float = 2.0, *, start: bool = True) -> tuple[Result, Meas, M.Meas]:
        """启动一次测量并读回 (Result, Meas, 本地换算结果)。"""
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
        - 否则用 `GET_MEAS` 轮询（连续模式下 PS 会在结果读走后立即重启下一次）。
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
                info = self.set_sig(freq_hz, phase_mdeg)
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
