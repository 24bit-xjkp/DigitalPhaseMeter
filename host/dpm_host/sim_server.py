"""仿真服务端：把 `sim_fpga` 接到串口上，模拟 PS 侧固件。

分三层：

1. :class:`~dpm_host.sim_fpga.SimFpga` —— 寄存器级 + 测量级 PL 模型（AXI 语义）；
2. :class:`SimPs` —— **PS 侧固件的等价逻辑**：命令分发、`IRQ_STATUS` 的 W1C 应答、
   连续模式「结果读走后立即重启下一次测量」的轮转、流模式通知（`STREAM_DATA` / `EVENT`）；
3. :class:`SimServer` —— 串口/pty 收发循环，`feed()` 是纯函数形式（便于单测）。

用法（无硬件时把上位机跑通）::

    python3 -m dpm_host.sim_server --pty                     # 打印 pty 从设备路径
    python3 -m dpm_host.sim_server --device /dev/pts/5        # 别人建好的 pty / 真串口
    python3 -m dpm_host.sim_server --pty --freq 10e6 --phase 45

`SET_SOURCE` 的约定：协议没有单独的「使能片内源」命令，因此选片内源（source=1）时同时
把 `SIG_CTRL.enable` 置 1，选外部时清零（否则 PC 侧永远无法让片内源可用）。
"""

from __future__ import annotations

import argparse
import errno
import os
import select
import signal
import sys
import termios
import time
import tty
from dataclasses import dataclass, field
from typing import Callable, Sequence

from . import mathx as M
from . import protocol as P
from . import sig as S
from .sim_fpga import (
    CAPS_VALUE,
    CTRL_CLEAR,
    CTRL_IRQ_ENABLE,
    CTRL_START,
    GATE_TARGET_MAX,
    IRQ_LOST_CLEAR,
    REG_SIG_CTRL,
    REG_GATE_TARGET,
    SimFpga,
    VERSION_VALUE,
)

__all__ = ["SimPs", "SimServer", "ServerOptions", "main", "serve_pty", "serve_device"]

#: `IDENTIFY.flags`：片内源可用 + 连续模式 + 流模式 + 中断驱动。
DEFAULT_FLAGS = (
    P.FLAG_SIGNAL_SOURCE | P.FLAG_CONTINUOUS | P.FLAG_STREAM | P.FLAG_IRQ
)


@dataclass
class ServerOptions:
    """仿真服务端的可调参数（命令行同名选项）。"""

    slow_hz: int = P.DEFAULT_SLOW_HZ
    dsp_hz: int = P.DEFAULT_DSP_HZ
    gate_target: int = P.DEFAULT_GATE_TARGET
    source: int = P.SOURCE_INTERNAL
    sig_enable: bool = True
    freq_hz: int = P.DEFAULT_SIG_FREQ_HZ
    phase_milli_deg: int = P.DEFAULT_SIG_PHASE_MILLI_DEG
    mode: int = P.MODE_SINGLE
    stream_interval_ms: int = P.DEFAULT_STREAM_INTERVAL_MS
    meas_latency_ms: float = 0.5
    config_delay_ms: float = 0.2
    mmcm_lock_ms: float = 0.5
    ext_freq_hz: int = 10_000_000
    ext_phase_milli_deg: int = 0
    ext_enabled: bool = True
    phase_model: str = "auto"
    version: int = 0x0001_0000
    git_rev: int = 0
    verbose: bool = False


class SimPs:
    """PS 侧固件逻辑（命令分发 + 连续模式轮转 + 流模式通知）。"""

    def __init__(
        self,
        fpga: SimFpga,
        *,
        options: ServerOptions | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.fpga = fpga
        self.opt = options or ServerOptions()
        self.mode = int(self.opt.mode)
        self.stream_enabled = False
        self.stream_interval_ms = int(self.opt.stream_interval_ms)
        self._log = log or (lambda _msg: None)
        self._last_stream_at = fpga.now()
        self._last_lost_reported = 0
        self._prev_locked = fpga.mmcm_locked
        self._prev_results = fpga.results_latched
        self._pending_since: float | None = None
        self._unread_event_sent = False
        self._freq_overflow_warned = False
        #: 统计（诊断用）。
        self.commands_handled = 0
        self.errors_returned = 0
        self.notifications_sent = 0

    # ------------------------------------------------------------ 命令分发

    def handle(self, frame: P.Frame) -> bytes | None:
        """处理一帧请求，返回要发送的响应字节（``None`` = 不响应）。"""
        self.fpga.poll()
        cmd = frame.cmd
        try:
            if cmd == P.Cmd.PING:
                return self._ok(cmd)
            if cmd == P.Cmd.IDENTIFY:
                info = P.Identify(
                    ver=self.opt.version,
                    git_rev=self.opt.git_rev,
                    caps=CAPS_VALUE,
                    version_reg=VERSION_VALUE,
                    caps_reg=CAPS_VALUE,
                    dsp_clk_hz=self.fpga.dsp_hz,
                    slow_clk_hz=self.fpga.slow_hz,
                    flags=DEFAULT_FLAGS,
                )
                return P.encode_frame(cmd | 0x80, P.encode_identify(info))
            if cmd == P.Cmd.GET_STATUS:
                status = P.Status(
                    status=self.fpga.status_word(),
                    sig_status=self.fpga.sig_status_word(),
                    gate_target=self.fpga.target_gate,
                    lost_count=self.fpga.lost_count,
                    # 协议里的 seq = 最新结果的记录序号（RTL 的 RESULT_SEQ）
                    seq=self.fpga.result_seq,
                )
                return P.encode_frame(cmd | 0x80, P.encode_status(status))
            if cmd == P.Cmd.START:
                self.fpga.write(0x00, CTRL_IRQ_ENABLE | CTRL_START)
                return self._ok(cmd)
            if cmd == P.Cmd.GET_RESULT:
                return self._get_result()
            if cmd == P.Cmd.GET_MEAS:
                return self._get_meas()
            if cmd == P.Cmd.SET_GATE:
                return self._set_gate(frame.payload)
            if cmd == P.Cmd.SET_MODE:
                return self._set_mode(frame.payload)
            if cmd == P.Cmd.SET_SOURCE:
                return self._set_source(frame.payload)
            if cmd == P.Cmd.SET_SIG:
                return self._set_sig(frame.payload)
            if cmd == P.Cmd.GET_SIG:
                return self._get_sig()
            if cmd == P.Cmd.CLEAR:
                self.fpga.write(0x00, CTRL_IRQ_ENABLE | CTRL_CLEAR)
                return self._ok(cmd)
            if cmd == P.Cmd.STREAM:
                return self._set_stream(frame.payload)
            if cmd == P.Cmd.RESET_COUNTERS:
                self.fpga.write(0x20, IRQ_LOST_CLEAR)
                if not self.fpga.busy:
                    self.fpga.write(0x00, CTRL_IRQ_ENABLE | CTRL_CLEAR)
                return self._ok(cmd)
            if frame.is_notification:
                # 通知帧是 PS → PC 的方向，收到就当噪声忽略
                return None
            self._log(f"未定义命令 0x{cmd:02X} → ERR_PARAM")
            return self._err(P.Cmd.ERR_PARAM)
        except P.PayloadError as exc:
            self._log(f"命令 0x{cmd:02X} 字长不符：{exc} → ERR_PARAM")
            return self._err(P.Cmd.ERR_PARAM)
        finally:
            self.commands_handled += 1

    # ---- 各命令实现

    def _ok(self, cmd: int) -> bytes:
        return P.encode_frame(int(cmd) | 0x80, b"")

    def _err(self, code: int) -> bytes:
        self.errors_returned += 1
        return P.encode_frame(code, b"")

    def _get_result(self) -> bytes:
        if not self.fpga.result_valid:
            return P.encode_frame(
                P.Cmd.GET_RESULT.response,
                P.encode_result(P.Result(False, 0, 0, 0, 0)),
            )
        result = self._read_result()
        self._after_result_read()
        return P.encode_frame(P.Cmd.GET_RESULT.response, P.encode_result(result))

    def _get_meas(self) -> bytes:
        if not self.fpga.result_valid:
            # GET_MEAS 没有 valid 字段，无法表达「还没有结果」→ 用 ERR_BUSY
            return self._err(P.Cmd.ERR_BUSY)
        result = self._read_result()
        self._after_result_read()
        return P.encode_frame(P.Cmd.GET_MEAS.response, P.encode_meas(self._meas_from(result)))

    def _read_result(self) -> P.Result:
        return P.Result(
            valid=True,
            seq=self.fpga.result_seq,
            gate_cnt=self.fpga.result_gate,
            edge_cnt=self.fpga.result_edge,
            phase_cnt=self.fpga.result_phase,
        )

    def _meas_from(self, result: P.Result) -> P.Meas:
        """把原始计数换算成 `GET_MEAS`/`STREAM_DATA` 的 6 个字段。

        字段单位按 `docs/ps-protocol.md` 第 5 节：`freqMilliHz` 承载**整数 Hz**
        （25MHz → 25000000），`phaseMilliDeg` 承载毫度。两侧用同一套安全整数形式，
        32 位字段足够（4.29GHz / 4.29e6 度）。
        """
        calc = M.meas_calc(result.gate_cnt, result.edge_cnt, result.phase_cnt, self.fpga.slow_hz, self.fpga.dsp_hz)
        return P.Meas(
            seq=result.seq,
            gate_cnt=result.gate_cnt,
            edge_cnt=result.edge_cnt,
            phase_cnt=result.phase_cnt,
            freq_milli_hz=calc.freq_hz,          # 字段名沿用协议，值 = Hz
            phase_milli_deg=calc.phase_milli_deg,
        )

    def _after_result_read(self) -> None:
        """结果被读走：W1C 应答 + 连续模式下立即重启下一次测量（协议第 3.1 节）。"""
        self.fpga.ack_result()
        self._pending_since = None
        self._unread_event_sent = False
        if self.mode == P.MODE_CONTINUOUS:
            self.fpga.write(0x00, CTRL_IRQ_ENABLE | CTRL_START)

    def _set_gate(self, payload: bytes) -> bytes:
        value = P.decode_set_gate(payload)
        if value > GATE_TARGET_MAX:
            return self._err(P.Cmd.ERR_PARAM)
        self.fpga.write(REG_GATE_TARGET, value)
        return P.encode_frame(P.Cmd.SET_GATE.response, P.encode_set_gate(self.fpga.target_gate))

    def _set_mode(self, payload: bytes) -> bytes:
        mode = P.decode_set_mode(payload)
        if mode not in (P.MODE_SINGLE, P.MODE_CONTINUOUS):
            return self._err(P.Cmd.ERR_PARAM)
        self.mode = mode
        return P.encode_frame(P.Cmd.SET_MODE.response, P.encode_set_mode(mode))

    def _set_source(self, payload: bytes) -> bytes:
        source = P.decode_set_source(payload)
        if source not in (P.SOURCE_EXTERNAL, P.SOURCE_INTERNAL):
            return self._err(P.Cmd.ERR_PARAM)
        # 协议没有独立的 enable 命令：选片内源时同时使能，选外部时关闭（见模块 docstring）
        enable = 1 if source == P.SOURCE_INTERNAL else 0
        self.fpga.write(REG_SIG_CTRL, (P.SIGST_SELECT | P.SIGST_ENABLE) if source else 0)
        _ = enable
        return P.encode_frame(P.Cmd.SET_SOURCE.response, P._U32.pack(self.fpga.sig_status_word()))

    def _set_sig(self, payload: bytes) -> bytes:
        freq_hz, phase_milli_deg = P.decode_set_sig(payload)
        self.fpga.poll()
        if self.fpga.busy:
            # 协议第 6 节：重配置期间两路输出停止，必须等 STATUS.busy = 0
            return self._err(P.Cmd.ERR_BUSY)
        # AXI 写通道在配置快照送出前会拉低 ready，PS 侧表现为写被挂起（有界等待，避免
        # 在 FakeClock 下死等：模型时间由 poll 推进，真实时钟下 200×0.5ms 也够配置送达）
        for _ in range(200):
            if self.fpga.wr_ready:
                break
            self.fpga.poll()
            time.sleep(0.0005)
        sol = S.solve_sig(freq_hz, phase_milli_deg, self.fpga.slow_hz, self.fpga.semantics)
        if not sol.ok:
            return self._err(P.Cmd.ERR_PARAM)
        self.fpga.configure_sig(freq_hz, phase_milli_deg)
        info = self._current_sig_info()
        return P.encode_frame(P.Cmd.SET_SIG.response, P.encode_sig_info(info))

    def _get_sig(self) -> bytes:
        return P.encode_frame(P.Cmd.GET_SIG.response, P.encode_sig_info(self._current_sig_info()))

    def _current_sig_info(self) -> P.SigInfo:
        f = self.fpga
        return P.SigInfo(
            freq_hz=f.int_freq_hz,
            phase_milli_deg=f.int_phase_milli_deg,
            vco_hz=f.vco_hz,
            div=f.div,
            phase_field=f.phase_field,
        )

    def _set_stream(self, payload: bytes) -> bytes:
        enable, interval_ms = P.decode_stream(payload)
        if enable not in (0, 1):
            return self._err(P.Cmd.ERR_PARAM)
        self.stream_enabled = bool(enable)
        if interval_ms:
            self.stream_interval_ms = int(interval_ms)
        self._last_stream_at = self.fpga.now()
        self._last_lost_reported = self.fpga.lost_count
        self._log(f"流模式 {'开' if self.stream_enabled else '关'}，intervalMs={self.stream_interval_ms}")
        return P.encode_frame(P.Cmd.STREAM.response, P.encode_stream(enable, self.stream_interval_ms))

    # ------------------------------------------------------------ 通知帧

    def notifications(self, now: float | None = None) -> list[bytes]:
        """按需产生 `STREAM_DATA` / `EVENT` 通知（服务端主循环周期调用）。"""
        t = self.fpga.now() if now is None else float(now)
        out: list[bytes] = []
        self.fpga.poll()

        # MMCM 失锁→恢复（EVENT code 2）
        locked = self.fpga.mmcm_locked
        if self.stream_enabled and locked and not self._prev_locked:
            out.append(P.encode_frame(P.Cmd.EVENT, P.encode_event(P.Event(2, self.fpga.vco_hz // 1000))))
        self._prev_locked = locked

        # 新结果到达：记录「未被读走」的起始时刻，用于 EVENT code 3
        if self.fpga.results_latched != self._prev_results:
            self._prev_results = self.fpga.results_latched
            self._pending_since = t
            self._unread_event_sent = False

        if not self.stream_enabled:
            return out

        interval = max(0.001, self.stream_interval_ms / 1000.0)
        if self.fpga.result_valid and t - self._last_stream_at >= interval:
            result = self._read_result()
            lost_delta = (self.fpga.lost_count - self._last_lost_reported) & 0xFFFF
            self._last_lost_reported = self.fpga.lost_count
            data = P.StreamData(self._meas_from(result), lost_delta)
            out.append(P.encode_frame(P.Cmd.STREAM_DATA, P.encode_stream_data(data)))
            self.notifications_sent += 1
            if lost_delta:
                out.append(
                    P.encode_frame(P.Cmd.EVENT, P.encode_event(P.Event(1, self.fpga.lost_count)))
                )
            self._after_result_read()
            self._last_stream_at = t

        # 上位机长时间没读走结果（EVENT code 3）
        if (
            self.fpga.result_valid
            and self._pending_since is not None
            and not self._unread_event_sent
            and t - self._pending_since > max(2 * interval, 0.05)
        ):
            out.append(P.encode_frame(P.Cmd.EVENT, P.encode_event(P.Event(3, self.fpga.result_seq))))
            self._unread_event_sent = True

        return out


# ---------------------------------------------------------------- 收发循环


class SimServer:
    """把 :class:`SimPs` 接到一个字节通道上（`feed()` 便于单测）。"""

    def __init__(self, ps: SimPs, *, log: Callable[[str], None] | None = None) -> None:
        self.ps = ps
        self.parser = P.FrameParser()
        self._log = log or (lambda _msg: None)

    def feed(self, data: bytes) -> list[bytes]:
        """喂入收到的字节，返回本次要回送的帧（响应 + 通知）字节列表。"""
        out: list[bytes] = []
        for ev in self.parser.feed(data):
            if isinstance(ev, P.ParseError):
                self._log(f"丢弃坏数据（{ev.kind}）：{ev.detail}")
                continue
            self._log(f"← {ev.name} {ev.raw.hex(' ')}")
            response = self.ps.handle(ev)
            if response is not None:
                out.append(response)
                # 报原始码 + 长度（响应 0x81..0x8E / 通知 0xE1..0xE2）
                cmd_byte = response[2]
                self._log(f"→ 响应 0x{cmd_byte:02X}（{len(response)} 字节）{response.hex(' ')}")
        out.extend(self.ps.notifications())
        return out


class _Channel:
    """字节通道抽象：pty 主设备 fd 或 pyserial 端口。"""

    def read(self) -> bytes:  # pragma: no cover - 接口
        raise NotImplementedError

    def write(self, data: bytes) -> None:  # pragma: no cover - 接口
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 接口
        pass

    @property
    def description(self) -> str:  # pragma: no cover - 接口
        return "channel"


class PtyMasterChannel(_Channel):
    """pty 主设备（服务端持 master，客户端打开 slave 路径）。"""

    def __init__(self, fd: int, slave_path: str) -> None:
        self.fd = fd
        self.slave_path = slave_path

    def read(self) -> bytes:
        try:
            ready, _, _ = select.select([self.fd], [], [], 0.005)
            if not ready:
                return b""
            return os.read(self.fd, 4096)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EAGAIN):
                # 从设备还没被打开 / 已被关闭：忽略，继续等
                time.sleep(0.005)
                return b""
            raise

    def write(self, data: bytes) -> None:
        try:
            os.write(self.fd, data)
        except OSError as exc:
            if exc.errno not in (errno.EIO, errno.EAGAIN):
                raise
            time.sleep(0.005)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:  # pragma: no cover
            pass

    @property
    def description(self) -> str:
        return f"pty:{self.slave_path}"


class SerialChannel(_Channel):
    """pyserial 通道（真串口，或别人建好的 pty 从设备路径）。"""

    def __init__(self, port: str, baud: int = P.DEFAULT_BAUD) -> None:
        import serial

        self.port = port
        self._ser = serial.Serial(port=port, baudrate=int(baud), timeout=0.01, write_timeout=1.0)

    def read(self) -> bytes:
        return bytes(self._ser.read(4096))

    def write(self, data: bytes) -> None:
        self._ser.write(data)

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:  # pragma: no cover
            pass

    @property
    def description(self) -> str:
        return f"serial:{self.port}"


def serve_pty(
    options: ServerOptions,
    *,
    link_path: str | None = None,
    run_for: float | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    """建一对 pty，把从设备路径打印出来并开始服务。"""
    master_fd, slave_fd = os.openpty()
    slave_path = os.ttyname(slave_fd)
    # 原始模式：不要回显、不要行缓冲，与 8N1 串口一致
    try:
        tty.setraw(master_fd, termios.TCSANOW)
    except termios.error:  # pragma: no cover
        pass
    channel = PtyMasterChannel(master_fd, slave_path)
    if link_path:
        try:
            if os.path.islink(link_path) or os.path.exists(link_path):
                os.unlink(link_path)
            os.symlink(slave_path, link_path)
            print(f"PTY_LINK={link_path}", flush=True)
        except OSError as exc:
            print(f"创建软链接失败（{link_path}）：{exc}", file=sys.stderr, flush=True)
    print(f"PTY_SLAVE={slave_path}", flush=True)
    print(f"PTY 从设备路径: {slave_path}", flush=True)
    try:
        return _serve(channel, options, run_for=run_for, log=log)
    finally:
        os.close(slave_fd)
        channel.close()
        if link_path and os.path.islink(link_path):
            try:
                os.unlink(link_path)
            except OSError:  # pragma: no cover
                pass


def serve_device(
    device: str,
    options: ServerOptions,
    *,
    baud: int = P.DEFAULT_BAUD,
    run_for: float | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    """在已有串口/pty 从设备上服务。"""
    channel = SerialChannel(device, baud)
    print(f"DEVICE={device}", flush=True)
    try:
        return _serve(channel, options, run_for=run_for, log=log)
    finally:
        channel.close()


def _serve(
    channel: _Channel,
    options: ServerOptions,
    *,
    run_for: float | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    def _log(msg: str) -> None:
        if log is not None:
            log(msg)
        elif options.verbose:
            print(f"[sim] {msg}", file=sys.stderr, flush=True)

    fpga = SimFpga(
        slow_hz=options.slow_hz,
        dsp_hz=options.dsp_hz,
        gate_target=options.gate_target,
        source=options.source,
        sig_enable=options.sig_enable,
        freq_hz=options.freq_hz,
        phase_milli_deg=options.phase_milli_deg,
        meas_latency_s=options.meas_latency_ms / 1000.0,
        config_delay_s=options.config_delay_ms / 1000.0,
        mmcm_lock_s=options.mmcm_lock_ms / 1000.0,
        ext_freq_hz=options.ext_freq_hz,
        ext_phase_milli_deg=options.ext_phase_milli_deg,
        ext_enabled=options.ext_enabled,
        phase_model=options.phase_model,
    )
    ps = SimPs(fpga, options=options, log=_log)
    server = SimServer(ps, log=_log)
    _log(
        f"仿真 PL 就绪：slow={fpga.slow_hz / 1e6:g}MHz dsp={fpga.dsp_hz / 1e6:g}MHz "
        f"gate={fpga.target_gate} source={'片内' if fpga.src_select else '外部'} "
        f"f={fpga.int_freq_hz / 1e6:g}MHz phase={fpga.int_phase_milli_deg / 1000:g}° "
        f"相位模型={fpga.phase_model}（auto/controllable/static 见 --phase-model）"
    )

    deadline = None if not run_for else time.monotonic() + float(run_for)
    stop = False

    def _on_signal(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except ValueError:  # pragma: no cover - 非主线程
            pass

    try:
        while not stop:
            data = channel.read()
            if data:
                for frame_bytes in server.feed(data):
                    channel.write(frame_bytes)
            else:
                # 空闲时也要推进时间相关状态并产生流通知
                for frame_bytes in ps.notifications():
                    channel.write(frame_bytes)
            if deadline is not None and time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


# ---------------------------------------------------------------- 命令行


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python3 -m dpm_host.sim_server",
        description="DPM 仿真 PL + PS 固件服务端（无硬件时让上位机跑通）",
    )
    ap.add_argument("--pty", action="store_true", help="创建一对 pty，打印从设备路径并服务")
    ap.add_argument("--pty-link", metavar="PATH", default=None, help="同时给 pty 从设备建一个固定路径的软链接")
    ap.add_argument("--device", metavar="PATH", default=None, help="在已有串口/pty 从设备上服务")
    ap.add_argument("--baud", type=int, default=P.DEFAULT_BAUD, help=f"波特率（默认 {P.DEFAULT_BAUD}）")
    ap.add_argument("--freq", type=float, default=P.DEFAULT_SIG_FREQ_HZ, help="片内源初始频率（Hz，支持 25e6 写法）")
    ap.add_argument("--phase", type=float, default=P.DEFAULT_SIG_PHASE_MILLI_DEG / 1000.0, help="片内源初始相位差（度）")
    ap.add_argument("--gate", type=int, default=P.DEFAULT_GATE_TARGET, help="闸门目标（低速周期数）")
    ap.add_argument("--mode", choices=("single", "continuous"), default="single", help="初始测量模式")
    ap.add_argument("--source", choices=("ext", "int"), default="int", help="初始输入源")
    ap.add_argument("--no-sig-enable", action="store_true", help="片内源初始不使能（测量永不完成，用于测超时）")
    ap.add_argument("--slow-hz", type=int, default=P.DEFAULT_SLOW_HZ, help="低速时钟频率（模型参数）")
    ap.add_argument("--dsp-hz", type=int, default=P.DEFAULT_DSP_HZ, help="高速/DSP 时钟频率（模型参数）")
    ap.add_argument("--stream-interval-ms", type=int, default=P.DEFAULT_STREAM_INTERVAL_MS, help="流模式默认周期")
    ap.add_argument("--meas-latency-ms", type=float, default=0.5, help="测量完成后的额外锁存延迟（模型参数）")
    ap.add_argument("--config-delay-ms", type=float, default=0.2, help="SIG_CTRL 配置跨到测量域的延迟（模型参数）")
    ap.add_argument("--mmcm-lock-ms", type=float, default=0.5, help="MMCM 重新锁定时间（模型参数）")
    ap.add_argument("--ext-freq", type=float, default=10e6, help="外部输入的模拟频率（Hz）")
    ap.add_argument("--ext-phase", type=float, default=0.0, help="外部输入的模拟相位差（度）")
    ap.add_argument("--ext-disable", action="store_true", help="外部输入无信号（测量永不完成）")
    ap.add_argument(
        "--phase-model",
        choices=("auto", "controllable", "static"),
        default="auto",
        help="片内源相位模型：auto=跟随 spec/drp_semantics.json（当前为 static，相位恒 90°）；"
             "controllable=假装相位字已接线（上位机相位通路自测）；static=固定静态相位",
    )
    ap.add_argument("--git-rev", type=lambda s: int(s, 0), default=0, help="IDENTIFY 返回的 gitRev")
    ap.add_argument("--version", type=lambda s: int(s, 0), default=0x00010000, help="IDENTIFY 返回的 ver")
    ap.add_argument("--run-for", type=float, default=0.0, help="只运行这么多秒后退出（0 = 一直运行）")
    ap.add_argument("--verbose", action="store_true", help="把收发日志打到 stderr")
    ap.add_argument("--quiet", action="store_true", help="不打印启动信息")
    return ap


def options_from_args(args: argparse.Namespace) -> ServerOptions:
    return ServerOptions(
        slow_hz=args.slow_hz,
        dsp_hz=args.dsp_hz,
        gate_target=args.gate,
        source=P.str_to_source(args.source),
        sig_enable=not args.no_sig_enable,
        freq_hz=int(round(args.freq)),
        phase_milli_deg=int(round(args.phase * 1000)),
        mode=P.str_to_mode(args.mode),
        stream_interval_ms=args.stream_interval_ms,
        meas_latency_ms=args.meas_latency_ms,
        config_delay_ms=args.config_delay_ms,
        mmcm_lock_ms=args.mmcm_lock_ms,
        ext_freq_hz=int(round(args.ext_freq)),
        ext_phase_milli_deg=int(round(args.ext_phase * 1000)),
        ext_enabled=not args.ext_disable,
        phase_model=args.phase_model,
        version=args.version,
        git_rev=args.git_rev,
        verbose=args.verbose,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """`python3 -m dpm_host.sim_server` 入口。"""
    args = build_arg_parser().parse_args(argv)
    if not args.pty and not args.device:
        print("必须指定 --pty 或 --device（-h 看用法）", file=sys.stderr)
        return 2
    options = options_from_args(args)
    log = (lambda msg: print(f"[sim] {msg}", file=sys.stderr, flush=True)) if args.verbose else None
    run_for = args.run_for or None
    if args.pty:
        if not args.quiet:
            print(
                f"启动仿真服务端：slow={options.slow_hz / 1e6:g}MHz dsp={options.dsp_hz / 1e6:g}MHz "
                f"gate={options.gate_target} f={options.freq_hz / 1e6:g}MHz phase={options.phase_milli_deg / 1000:g}°",
                flush=True,
            )
        return serve_pty(options, link_path=args.pty_link, run_for=run_for, log=log)
    return serve_device(args.device, options, baud=args.baud, run_for=run_for, log=log)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
