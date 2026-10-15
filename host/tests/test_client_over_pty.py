"""端到端测试：`sim_server` 起 pty，`DpmClient` 连上去跑完整交互。

覆盖：命令往返、连续/流模式、通知帧、错误响应、超时路径、坏帧鲁棒性。
运行：`cd host && python3 -m unittest tests.test_client_over_pty -v`
"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import threading
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpm_host import mathx as M  # noqa: E402
from dpm_host import protocol as P  # noqa: E402
from dpm_host.client import DpmClient  # noqa: E402

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SimServerProcess:
    """以子进程方式跑 `python3 -m dpm_host.sim_server --pty --pty-link <临时路径>`。"""

    def __init__(self, tmpdir: str, *extra: str) -> None:
        self.link = os.path.join(tmpdir, "dpm_pty")
        self.child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "dpm_host.sim_server",
                "--pty",
                "--pty-link",
                self.link,
                *extra,
            ],
            cwd=HOST_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.slave = self._wait_for_pty(timeout=10.0)

    def _wait_for_pty(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                raise AssertionError(f"sim_server 提前退出：{self.child.stdout.read() if self.child.stdout else ''}")
            if os.path.islink(self.link) and os.path.exists(self.link):
                return os.path.realpath(self.link)
            time.sleep(0.05)
        raise AssertionError("等待 pty 路径超时")

    def close(self) -> None:
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.child.kill()
        if self.child.stdout is not None:
            self.child.stdout.close()


class PtyClientTestBase(unittest.TestCase):
    """共用的服务端/客户端生命周期。"""

    server_args: tuple[str, ...] = ()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = SimServerProcess(self._tmp.name, *self.server_args)
        self.addCleanup(self.server.close)
        self.client = DpmClient(port=self.server.slave, timeout=1.0, retries=1)
        self.addCleanup(self.client.close)
        self.client.ping()

    def expectation(self, freq_hz: int, phase_mdeg: int) -> tuple[int, int, int]:
        """按冻结模型算期望的 (gateCnt, edgeCnt, phaseCnt)。"""
        gate = 2000 + -(-50_000_000 // freq_hz)
        edge = (gate * freq_hz) // 50_000_000
        dsp_cycles = M.round_half_even(phase_mdeg * (10**12 // freq_hz), 360_000 * 2500)
        return gate, edge, dsp_cycles * edge


class BasicInteractionTests(PtyClientTestBase):
    """基本命令 + 一次完整测量。"""

    server_args = ("--phase-model", "controllable")

    def test_ping_and_identify(self) -> None:
        info = self.client.identify()
        self.assertEqual(info.version_reg, 0x44504D02)
        self.assertEqual(info.caps, 0x00030104)
        self.assertEqual(info.slow_clk_hz, 50_000_000)
        self.assertEqual(info.dsp_clk_hz, 400_000_000)
        self.assertTrue(info.has_signal_source and info.supports_continuous and info.supports_stream)

    def test_status_and_gate(self) -> None:
        self.assertEqual(self.client.set_gate(4000), 4000)
        status = self.client.get_status()
        self.assertEqual(status.gate_target, 4000)
        self.assertTrue(status.sig_select and status.sig_enable and status.source_ready)

    def test_source_switch(self) -> None:
        sig_status = self.client.set_source(P.SOURCE_EXTERNAL)
        self.assertFalse(sig_status & P.SIGST_SELECT)
        self.assertFalse(self.client.get_status().sig_select)
        sig_status = self.client.set_source(P.SOURCE_INTERNAL)
        self.assertTrue(sig_status & P.SIGST_SELECT)
        self.client.wait_source_ready(timeout=2.0)

    def test_set_sig_and_measure(self) -> None:
        for freq_hz, phase_mdeg in ((10_000_000, 45_000), (25_000_000, 90_000), (6_250_000, 22_500)):
            with self.subTest(freq=freq_hz, phase=phase_mdeg):
                self.client.wait_idle(timeout=2.0)
                info = self.client.set_sig(freq_hz, phase_mdeg)
                self.assertEqual(info.freq_hz, freq_hz)
                self.assertEqual(info.phase_milli_deg, phase_mdeg)
                self.client.wait_source_ready(timeout=2.0)
                self.client.start()
                meas = self.client.wait_meas(timeout=2.0)
                calc = self.client.calc_meas(meas)
                gate, edge, phase_cnt = self.expectation(freq_hz, phase_mdeg)
                self.assertEqual(meas.gate_cnt, gate)
                self.assertEqual(meas.edge_cnt, edge)
                self.assertEqual(meas.phase_cnt, phase_cnt)
                self.assertEqual(calc.freq_hz, freq_hz)
                self.assertEqual(calc.phase_milli_deg, phase_mdeg)
                # 线的字段与本地换算一致（协议字段单位 = Hz / 毫度）
                self.assertEqual(meas.freq_milli_hz, calc.freq_hz)
                self.assertEqual(meas.phase_milli_deg, calc.phase_milli_deg)
                # 主判据：phaseCnt / edgeCnt 是整数个 DSP 周期
                self.assertEqual(meas.phase_cnt % meas.edge_cnt, 0)

    def test_monitor_samples(self) -> None:
        self.client.set_sig(12_500_000, 90_000)
        samples = self.client.monitor(count=3, timeout=2.0)
        self.assertEqual(len(samples), 3)
        for meas, calc in samples:
            self.assertEqual(calc.freq_hz, 12_500_000)
            self.assertEqual(calc.phase_milli_deg, 90_000)
        self.assertEqual(self.client.get_status().sig_select, True)

    def test_monitor_over_stream(self) -> None:
        received = []
        self.client.on_stream_data = received.append
        self.client.stream(True, 10)
        self.client.set_mode(P.MODE_CONTINUOUS)
        self.client.start()
        try:
            deadline = time.monotonic() + 3.0
            while len(received) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            self.client.stream(False, 10)
            self.client.on_stream_data = None
            self.client.set_mode(P.MODE_SINGLE)
        self.assertGreaterEqual(len(received), 3)
        first = received[0]
        self.assertEqual(first.lost_delta, 0)
        self.assertEqual(self.client.calc_meas(first.meas).freq_hz, 25_000_000)

    def test_clear_and_reset(self) -> None:
        self.client.start()
        self.client.wait_meas(timeout=2.0)
        self.client.clear()
        status = self.client.get_status()
        self.assertFalse(status.result_valid)
        self.assertEqual(status.seq, 0)
        self.client.reset_counters()

    def test_error_response_raises(self) -> None:
        with self.assertRaises(P.DpmError) as ctx:
            self.client.set_mode(7)
        self.assertEqual(ctx.exception.code, P.Cmd.ERR_PARAM)
        # 出错的请求不会破坏后续通信
        self.assertEqual(self.client.set_mode(P.MODE_SINGLE), P.MODE_SINGLE)
        frame = self.client.request(P.Cmd.GET_MEAS, accept_busy=True)
        self.assertIn(frame.cmd, (P.Cmd.GET_MEAS.response, P.Cmd.ERR_BUSY))

    def test_bad_frames_are_ignored(self) -> None:
        """往串口灌坏帧（噪声 + 坏 CRC），服务端必须继续正常工作。"""
        ser = self.client._transport._ser  # 测试里直接用底层串口（pyserial）
        good = P.encode_frame(P.Cmd.PING)
        bad = bytearray(good)
        bad[-1] ^= 0xFF
        ser.write(b"\x00\x11\x22" + bytes(bad) + bytes([P.SOF, 0x00]))
        time.sleep(0.05)
        self.assertTrue(self.client.ping())
        self.assertEqual(self.client.identify().version_reg, 0x44504D02)

    def test_stats_count_frames(self) -> None:
        before = self.client.stats.snapshot()
        self.client.ping()
        after = self.client.stats.snapshot()
        self.assertGreater(after["tx"], before["tx"])
        self.assertGreater(after["rx"], before["rx"])


class StaticPhaseModelTests(PtyClientTestBase):
    """`--phase-model static`：复现 PHASE_DUTY_CONFIG=false 的旧 bitstream
    （相位字写进去也不生效，clk_out2 恒为静态 90°）。"""

    server_args = ("--phase-model", "static")

    def test_phase_is_fixed_at_90deg(self) -> None:
        self.client.set_sig(10_000_000, 22_500)
        info = self.client.get_sig()
        self.assertEqual(info.freq_hz, 10_000_000)
        self.assertEqual(info.phase_milli_deg, 90_000)  # 相位字写不进去
        self.client.start()
        meas = self.client.wait_meas(timeout=2.0)
        self.assertEqual(self.client.calc_meas(meas).phase_milli_deg, 90_000)


class TimeoutBehaviourTests(unittest.TestCase):
    """源不可用时测量永不完成：客户端必须超时而不是死等。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = SimServerProcess(self._tmp.name, "--no-sig-enable")
        self.addCleanup(self.server.close)
        self.client = DpmClient(port=self.server.slave, timeout=0.3, retries=0)
        self.addCleanup(self.client.close)

    def test_source_not_ready(self) -> None:
        status = self.client.get_status()
        self.assertFalse(status.source_ready)

    def test_measurement_times_out(self) -> None:
        self.client.start()
        t0 = time.monotonic()
        with self.assertRaises(P.DpmTimeoutError):
            self.client.wait_result(timeout=0.5, poll_interval=0.02)
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_request_timeout_and_retry(self) -> None:
        """pty 上没有对端应答时，请求超时并按 retries 重发。"""
        import pty

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        path = os.ttyname(slave)
        client = DpmClient(port=path, timeout=0.05, retries=2)
        self.addCleanup(client.close)
        with self.assertRaises(P.DpmTimeoutError):
            client.ping()
        # timeouts 按「超时的尝试次数」计数：首发 + 2 次重发各超时一次
        self.assertEqual(client.stats.timeouts, 3)
        self.assertEqual(client.stats.retries, 2)
        self.assertEqual(client.stats.tx_frames, 3)  # 首发 + 2 次重发

    def test_write_error_on_closed_pty(self) -> None:
        """对端消失后写串口会失败：应当报 DpmError（而不是假装成功）。"""
        self.server.close()
        time.sleep(0.2)
        with self.assertRaises(P.DpmError):
            self.client.ping()




class NotificationDispatchTests(unittest.TestCase):
    """通知帧分流的**独立**验证：用一个「外来」迷你对端（不是 dpm_host 的 sim_server）
    通过裸 pty 收发，确认 0xE1/0xE2 被当通知、0x81/0x82 只当响应（协议 v1.1）。
    """

    def setUp(self) -> None:
        self.master, slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.port = os.ttyname(slave)
        self.requests: list[bytes] = []
        self.send_notifications = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._peer_loop, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_peer)
        self.client = DpmClient(port=self.port, timeout=1.0, log=lambda _m: None)
        self.addCleanup(self.client.close)

    def _stop_peer(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2.0)

    def _peer_loop(self) -> None:
        """极简对端：回 PING 响应，并主动发一条 STREAM_DATA(0xE1) 与 EVENT(0xE2)。"""
        parser = P.FrameParser()
        sent_notifications = False
        while not self.stop.is_set():
            try:
                ready, _, _ = select.select([self.master], [], [], 0.02)
            except OSError:  # pragma: no cover - pty 关闭
                return
            if ready:
                try:
                    chunk = os.read(self.master, 256)
                except OSError:  # pragma: no cover
                    return
                if chunk:
                    self.requests.append(chunk)
                    for ev in parser.feed(chunk):
                        if isinstance(ev, P.Frame) and ev.cmd == P.Cmd.PING:
                            os.write(self.master, P.encode_frame(P.Cmd.PING.response))
                            if self.send_notifications and not sent_notifications:
                                sent_notifications = True
                                data = P.StreamData(P.Meas(7, 2002, 1001, 4004, 25_000_000, 90_000), 3)
                                os.write(self.master, P.encode_frame(P.Cmd.STREAM_DATA, P.encode_stream_data(data)))
                                os.write(self.master, P.encode_frame(P.Cmd.EVENT, P.encode_event(P.Event(1, 42))))

    def test_stream_and_event_are_dispatched_as_notifications(self) -> None:
        self.send_notifications = True  # 让对端在第一次 PING 后主动发两条通知
        streams: list[P.StreamData] = []
        events: list[P.Event] = []
        self.client.on_stream_data = streams.append
        self.client.on_event = events.append
        self.assertTrue(self.client.ping())
        deadline = time.monotonic() + 2.0
        while not (streams and events) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].meas.seq, 7)
        self.assertEqual(streams[0].lost_delta, 3)
        self.assertEqual(streams[0].meas.freq_milli_hz, 25_000_000)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].code, events[0].detail), (1, 42))
        self.assertEqual(self.client.stats.notifications, 2)
        # 通知帧不会污染请求/响应事务
        self.assertEqual(self.client.stats.unexpected, 0)

    def test_unknown_notification_code_is_ignored(self) -> None:
        """0xE0..0xEF 里的未知通知码：忽略并计数，不影响事务。"""
        self.client.ping()  # 先建立连接
        os.write(self.master, P.encode_frame(0xE7, b"\x01\x02"))
        time.sleep(0.05)
        self.assertTrue(self.client.ping())
        self.assertGreaterEqual(self.client.stats.notifications, 1)
        self.assertEqual(self.client.stats.unexpected, 0)

    def test_notification_codes_are_not_confused_with_responses(self) -> None:
        """0x81/0x82 只按响应处理：伪造一个 26 字节的 0x81 不会被当成 STREAM_DATA。"""
        streams: list[P.StreamData] = []
        self.client.on_stream_data = streams.append
        self.client.ping()
        os.write(self.master, P.encode_frame(0x81, b"\x00" * P.STREAM_DATA_SIZE))
        time.sleep(0.1)
        self.assertEqual(streams, [])
        # 该帧字长不符（0x81 响应应为空），客户端只丢弃 + 计数，不抛异常
        self.assertTrue(self.client.ping())
        self.assertGreaterEqual(self.client.stats.unexpected, 1)
        self.assertEqual(self.client.stats.notifications, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
