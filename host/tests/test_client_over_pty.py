"""端到端测试：`sim_server` 起 pty，`DpmClient` 连上去跑完整交互。

覆盖：命令往返、连续/流模式、通知帧、错误响应、超时路径、坏帧鲁棒性。

运行：
    仓库根目录：`uv run pytest host/tests/test_client_over_pty.py -q`
    host 目录： `uv run pytest tests/test_client_over_pty.py -q`

这些用例起**真实子进程 + 真实 pty**（标记 `pty`），比其他文件慢，但默认照常执行。
"""

from __future__ import annotations

import argparse
import os
import pty
import select
import subprocess
import sys
import threading
import time

import pytest
from dpm_host import cli
from dpm_host import mathx as M
from dpm_host import protocol as P
from dpm_host import sig as S
from dpm_host.client import DpmClient

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 整个文件都依赖真实 pty/子进程。
pytestmark = pytest.mark.pty


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


@pytest.fixture
def sim_server(tmp_path):
    """启动 `sim_server` 子进程的工厂：用例结束（含失败）时统一回收。"""
    servers: list[SimServerProcess] = []

    def _start(*extra: str) -> SimServerProcess:
        srv = SimServerProcess(str(tmp_path), *extra)
        servers.append(srv)
        return srv

    yield _start
    for srv in servers:
        srv.close()


@pytest.fixture
def server(sim_server, request):
    """按 `indirect` 参数启动的 sim_server（参数即命令行附加选项）。"""
    return sim_server(*getattr(request, "param", ()))


@pytest.fixture
def client(server):
    """已 PING 通的客户端（timeout=1.0、retries=1，与原 setUp 一致）。"""
    cli = DpmClient(port=server.slave, timeout=1.0, retries=1)
    try:
        cli.ping()
        yield cli
    finally:
        cli.close()


#: 服务端参数（`indirect`）：相位字可写的 `controllable` 模型 —— 大多数用例的前提。
CONTROLLABLE = pytest.mark.parametrize("server", [pytest.param(("--phase-model", "controllable"), id="controllable")], indirect=True)
#: 服务端参数（`indirect`）：`static` 模型 —— 复现 PHASE_DUTY_CONFIG=false 的旧 bitstream。
STATIC_PHASE = pytest.mark.parametrize("server", [pytest.param(("--phase-model", "static"), id="static")], indirect=True)


def expectation(freq_hz: int, phase_mdeg: int) -> tuple[int, int, int]:
    """按冻结模型算期望的 (gateCnt, edgeCnt, phaseCnt)。"""
    gate = 2000 + -(-50_000_000 // freq_hz)
    edge = (gate * freq_hz) // 50_000_000
    dsp_cycles = M.round_half_even(phase_mdeg * (10**12 // freq_hz), 360_000 * 2500)
    return gate, edge, dsp_cycles * edge


# ------------------------------------------------------------------ 基本命令 + 一次完整测量


@CONTROLLABLE
def test_ping_and_identify(client: DpmClient) -> None:
    info = client.identify()
    assert info.version_reg == 0x44504D02
    assert info.caps == 0x00030104
    assert info.slow_clk_hz == 50_000_000
    assert info.dsp_clk_hz == 400_000_000
    assert info.has_signal_source and info.supports_continuous and info.supports_stream


@CONTROLLABLE
def test_status_and_gate(client: DpmClient) -> None:
    assert client.set_gate(4000) == 4000
    status = client.get_status()
    assert status.gate_target == 4000
    assert status.sig_select and status.sig_enable and status.source_ready


@CONTROLLABLE
def test_source_switch(client: DpmClient) -> None:
    sig_status = client.set_source(P.SOURCE_EXTERNAL)
    assert not sig_status & P.SIGST_SELECT
    assert not client.get_status().sig_select
    sig_status = client.set_source(P.SOURCE_INTERNAL)
    assert sig_status & P.SIGST_SELECT
    client.wait_source_ready(timeout=2.0)


@CONTROLLABLE
@pytest.mark.parametrize(
    "freq_hz, phase_mdeg",
    [(10_000_000, 45_000), (25_000_000, 90_000), (6_250_000, 22_500)],
    ids=["10MHz-45mdeg", "25MHz-90mdeg", "6.25MHz-22.5mdeg"],
)
def test_set_sig_and_measure(client: DpmClient, freq_hz: int, phase_mdeg: int) -> None:
    """写片内源参数 → 起测 → 结果与冻结模型逐项一致（原用例的 3 个子用例各占一个参数）。"""
    client.wait_idle(timeout=2.0)
    info = client.set_sig(freq_hz, phase_mdeg)
    assert info.freq_hz == freq_hz
    assert info.phase_milli_deg == phase_mdeg
    client.wait_source_ready(timeout=2.0)
    client.start()
    meas = client.wait_meas(timeout=2.0)
    calc = client.calc_meas(meas)
    gate, edge, phase_cnt = expectation(freq_hz, phase_mdeg)
    assert meas.gate_cnt == gate
    assert meas.edge_cnt == edge
    assert meas.phase_cnt == phase_cnt
    assert calc.freq_hz == freq_hz
    assert calc.phase_milli_deg == phase_mdeg
    # 线的字段与本地换算一致（协议字段单位 = Hz / 毫度）
    assert meas.freq_milli_hz == calc.freq_hz
    assert meas.phase_milli_deg == calc.phase_milli_deg
    # 主判据：phaseCnt / edgeCnt 是整数个 DSP 周期
    assert meas.phase_cnt % meas.edge_cnt == 0


@CONTROLLABLE
@pytest.mark.parametrize(
    "freq_hz, phase_mdeg, div",
    [(25_000_000, 315_000, 40), (8_000_000, 183_960, 125), (5_000_000, 191_625, 120), (12_500_000, 270_000, 80)],
    ids=["25MHz-315", "8MHz-183.96", "5MHz-191.625", "12.5MHz-270"],
)
def test_over_180_deg_is_accepted(client: DpmClient, freq_hz: int, phase_mdeg: int, div: int) -> None:
    """>180° 的请求在 9 位相位码范围内是**合法**的（旧固件一律拒 ≥180°，比硬件保守）。

    这一条同时是仿真后端与固件契约的一致性检查：Python 仿真服务端必须与
    `ps/src/dpm_cmd.c` 一样按 `m ≤ 511` 判，而不是按 `< 180°`。
    """
    client.wait_idle(timeout=2.0)
    info = client.set_sig(freq_hz, phase_mdeg)
    assert info.div == div
    assert info.phase_milli_deg == phase_mdeg  # 冻结相位点都精确落在 45°/D 网格上
    assert S.phase_code_fits(phase_mdeg, div)
    client.wait_source_ready(timeout=2.0)
    client.start()
    meas = client.wait_meas(timeout=2.0)
    calc = client.calc_meas(meas)
    gate, edge, phase_cnt = expectation(freq_hz, phase_mdeg)
    assert (meas.gate_cnt, meas.edge_cnt, meas.phase_cnt) == (gate, edge, phase_cnt)
    # 测量端还有一重量化：1 个 DSP 周期 = 360·f_sig/400MHz，实测值落在半格 + 1 毫度内
    dsp_quantum_mdeg = 360_000.0 * freq_hz / 400_000_000.0
    assert abs(calc.phase_milli_deg - phase_mdeg) <= dsp_quantum_mdeg / 2 + 1000


@CONTROLLABLE
@pytest.mark.parametrize(
    "freq_hz, phase_mdeg",
    [(8_000_000, 200_000), (8_000_000, 184_140), (5_000_000, 200_000), (25_000_000, 360_000)],
    ids=["8MHz-200", "8MHz-上界+1", "5MHz-200", "25MHz-360(满量程)"],
)
def test_phase_code_overflow_is_rejected(client: DpmClient, freq_hz: int, phase_mdeg: int) -> None:
    """相位码越界（`m > 511`）或满量程必须回 ERR_PARAM，不能静默改相位。

    越界请求会让 MMCM 的 PHASE_MUX/DELAY_TIME 按位截断、相位**回绕**，因此这与「吸附到
    最近网格点」是两回事（见 docs/ps-protocol.md §6.2）。
    """
    sol = S.solve_sig(freq_hz, 0)
    assert not S.phase_code_fits(phase_mdeg, sol.div)
    with pytest.raises(P.DpmError) as ctx:
        client.set_sig(freq_hz, phase_mdeg)
    assert ctx.value.code == P.Cmd.ERR_PARAM
    # 出错的请求不会破坏后续通信
    assert client.ping() is not None


@CONTROLLABLE
def test_scan_skips_unrepresentable_points(client: DpmClient) -> None:
    """扫描遇到越界相位点时**跳过该点继续**，而不是中断整轮扫描。

    相位上界逐频点不同（8MHz/D=125 只到 184.139°），因此一份固定的相位列表里必然有
    越界组合；扫描必须能跑完并给出可实现的那些行。
    """
    logs: list[str] = []
    rows = client.scan(
        [8_000_000, 25_000_000],
        [0.0, 90.0, 225.0],
        gate_target=2000,
        timeout=2.0,
        result_timeout=2.0,
        log=logs.append,
    )
    # 越界点：8MHz/225°（D=125 → 需要相位码 625）；其余 5 点可实现
    assert len(rows) == 5
    assert [(r["freq_nominal"], r["phase_nominal"]) for r in rows] == [
        (8_000_000, 0.0),
        (8_000_000, 90.0),
        (25_000_000, 0.0),
        (25_000_000, 90.0),
        (25_000_000, 225.0),
    ]
    assert any("跳过 8MHz/225" in line for line in logs)


@CONTROLLABLE
def test_monitor_samples(client: DpmClient) -> None:
    client.set_sig(12_500_000, 90_000)
    samples = client.monitor(count=3, timeout=2.0)
    assert len(samples) == 3
    for meas, calc in samples:
        assert calc.freq_hz == 12_500_000
        assert calc.phase_milli_deg == 90_000
    assert client.get_status().sig_select is True


@CONTROLLABLE
@pytest.mark.slow
def test_monitor_over_stream(client: DpmClient) -> None:
    """流模式：连续测量 + 0xE1 通知，直到至少收到 3 条。"""
    received = []
    client.on_stream_data = received.append
    client.stream(True, 10)
    client.set_mode(P.MODE_CONTINUOUS)
    client.start()
    try:
        deadline = time.monotonic() + 3.0
        while len(received) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        client.stream(False, 10)
        client.on_stream_data = None
        client.set_mode(P.MODE_SINGLE)
    assert len(received) >= 3
    first = received[0]
    assert first.lost_delta == 0
    assert client.calc_meas(first.meas).freq_hz == 25_000_000


@CONTROLLABLE
def test_clear_and_reset(client: DpmClient) -> None:
    client.start()
    client.wait_meas(timeout=2.0)
    client.clear()
    status = client.get_status()
    assert not status.result_valid
    assert status.seq == 0
    client.reset_counters()


@CONTROLLABLE
def test_error_response_raises(client: DpmClient) -> None:
    with pytest.raises(P.DpmError) as ctx:
        client.set_mode(7)
    assert ctx.value.code == P.Cmd.ERR_PARAM
    # 出错的请求不会破坏后续通信
    assert client.set_mode(P.MODE_SINGLE) == P.MODE_SINGLE
    frame = client.request(P.Cmd.GET_MEAS, accept_busy=True)
    assert frame.cmd in (P.Cmd.GET_MEAS.response, P.Cmd.ERR_BUSY)


@CONTROLLABLE
def test_bad_frames_are_ignored(client: DpmClient) -> None:
    """往串口灌坏帧（噪声 + 坏 CRC），服务端必须继续正常工作。"""
    ser = client._transport._ser  # 测试里直接用底层串口（pyserial）
    good = P.encode_frame(P.Cmd.PING)
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    ser.write(b"\x00\x11\x22" + bytes(bad) + bytes([P.SOF, 0x00]))
    time.sleep(0.05)
    assert client.ping()
    assert client.identify().version_reg == 0x44504D02


@CONTROLLABLE
def test_stats_count_frames(client: DpmClient) -> None:
    before = client.stats.snapshot()
    client.ping()
    after = client.stats.snapshot()
    assert after["tx"] > before["tx"]
    assert after["rx"] > before["rx"]


# ------------------------------------------------------------------ `--phase-model static`：旧 bitstream


@STATIC_PHASE
def test_phase_is_fixed_at_90deg(client: DpmClient) -> None:
    """相位字写进去也不生效，clk_out2 恒为静态 90°。"""
    client.set_sig(10_000_000, 22_500)
    info = client.get_sig()
    assert info.freq_hz == 10_000_000
    assert info.phase_milli_deg == 90_000  # 相位字写不进去
    client.start()
    meas = client.wait_meas(timeout=2.0)
    assert client.calc_meas(meas).phase_milli_deg == 90_000


# ------------------------------------------------------------------ 源不可用时测量永不完成：客户端必须超时


@pytest.fixture
def no_sig_server(sim_server):
    """`--no-sig-enable`：片内源不使能 → 两路恒低 → 测量永不完成。"""
    return sim_server("--no-sig-enable")


@pytest.fixture
def timeout_client(no_sig_server):
    """短超时、不重试的客户端（连到 `--no-sig-enable` 的服务端）。"""
    cli = DpmClient(port=no_sig_server.slave, timeout=0.3, retries=0)
    try:
        yield cli
    finally:
        cli.close()


def test_source_not_ready(timeout_client: DpmClient) -> None:
    status = timeout_client.get_status()
    assert not status.source_ready


@pytest.mark.slow
def test_measurement_times_out(timeout_client: DpmClient) -> None:
    timeout_client.start()
    t0 = time.monotonic()
    with pytest.raises(P.DpmTimeoutError):
        timeout_client.wait_result(timeout=0.5, poll_interval=0.02)
    assert time.monotonic() - t0 < 3.0


def test_request_timeout_and_retry() -> None:
    """pty 上没有对端应答时，请求超时并按 retries 重发。"""
    master, slave = pty.openpty()
    path = os.ttyname(slave)
    client = DpmClient(port=path, timeout=0.05, retries=2)
    try:
        with pytest.raises(P.DpmTimeoutError):
            client.ping()
        # timeouts 按「超时的尝试次数」计数：首发 + 2 次重发各超时一次
        assert client.stats.timeouts == 3
        assert client.stats.retries == 2
        assert client.stats.tx_frames == 3  # 首发 + 2 次重发
    finally:
        client.close()
        os.close(master)


def test_write_error_on_closed_pty(no_sig_server: SimServerProcess, timeout_client: DpmClient) -> None:
    """对端消失后写串口会失败：应当报 DpmError（而不是假装成功）。"""
    no_sig_server.close()
    time.sleep(0.2)
    with pytest.raises(P.DpmError):
        timeout_client.ping()


# ------------------------------------------- 长闸门：等待预算随闸门时间放大（不误报超时）

#: 长闸门：120000000 个低速周期 @50MHz = 2.4s（超过旧的固定「等结果 2.0s」预算）。
LONG_GATE_TARGET = 120_000_000


class LongGatePeer:
    """模拟长闸门真机的两个阻塞点（语义对齐 `ps/src/dpm_cmd.c`）。

    - `GET_STATUS` 立刻回，`gateTarget` = `gate_target`（上位机靠它算等待预算）；
    - `GET_RESULT` / `GET_MEAS` 在**命令内部**先阻塞 `hold_s`（真机是 `50 + 4×闸门时间` 的
      有界等待，协议 §3.0 允许），再按「本次测量是否已经跑完一个闸门窗口」决定回
      `valid=1` 还是 `valid=0`；
    - `START` 记下测量起点，结果要等 `window_s` 才就绪。

    用它复现两类真实的超时误报：闸门 0.6s 而单次请求超时 0.1s（请求级），
    以及闸门 0.6s 而等结果预算 0.2s（结果级）。
    """

    def __init__(self, *, gate_target: int, window_s: float, hold_s: float | None = None) -> None:
        self.master, slave = pty.openpty()
        self.port = os.ttyname(slave)
        self.gate_target = int(gate_target)
        self.window_s = float(window_s)
        #: 命令内部阻塞（0 = 立刻回，由上位机自己轮询；None = 等同闸门窗口）
        self.hold_s = float(window_s if hold_s is None else hold_s)
        self.ready_at: float | None = None
        self.counts: dict[int, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._peer_loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        os.close(self.master)

    @property
    def result_ready(self) -> bool:
        return self.ready_at is not None and time.monotonic() >= self.ready_at

    def _peer_loop(self) -> None:
        parser = P.FrameParser()
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.master], [], [], 0.02)
            except OSError:  # pragma: no cover - pty 关闭
                return
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 256)
            except OSError:  # pragma: no cover
                return
            if not chunk:
                continue
            for ev in parser.feed(chunk):
                if not isinstance(ev, P.Frame):
                    continue
                reply = self._handle(ev)
                if reply:
                    os.write(self.master, reply)

    def _handle(self, frame: P.Frame) -> bytes:
        cmd = int(frame.cmd)
        self.counts[cmd] = self.counts.get(cmd, 0) + 1
        if cmd == P.Cmd.GET_STATUS:
            # busy=1（测量进行中）、resultValid=0：结果还没到，请继续问 GET_RESULT
            st = P.Status(int(P.STATUS_BUSY), 0, self.gate_target, 0, 0)
            return P.encode_frame(P.Cmd.GET_STATUS.response, P.encode_status(st))
        if cmd == P.Cmd.START:
            self.ready_at = time.monotonic() + self.window_s
            return P.encode_frame(P.Cmd.START.response)
        if cmd in (P.Cmd.GET_RESULT, P.Cmd.GET_MEAS):
            if self.hold_s > 0:
                # 真机在命令内部等结果：单次请求超时必须容得下这一等
                time.sleep(self.hold_s)
            if self.result_ready:
                if cmd == P.Cmd.GET_RESULT:
                    result = P.Result(True, 1, self.gate_target, 8, 16)
                    return P.encode_frame(P.Cmd.GET_RESULT.response, P.encode_result(result))
                meas = P.Meas(1, self.gate_target, 8, 16, 0, 0)
                return P.encode_frame(P.Cmd.GET_MEAS.response, P.encode_meas(meas))
            if cmd == P.Cmd.GET_RESULT:
                return P.encode_frame(P.Cmd.GET_RESULT.response, P.encode_result(P.Result(False, 0, 0, 0, 0)))
            return P.encode_frame(P.Cmd.ERR_BUSY)
        return P.encode_frame(cmd | 0x80)


def test_long_gate_single_request_timeout_is_scaled() -> None:
    """对端在 `GET_RESULT` 内部阻塞一个闸门窗口：单次请求超时 0.1s 必须被放大到预算。

    闸门 0.6s（`slow_hz=1MHz` × `gateTarget=600000`）→ 预算 = 2×0.6+0.25 = 1.45s。
    修复前：请求级超时 0.1s 先到，`wait_result` 直接抛 `DpmTimeoutError`（测量其实没问题）。
    """
    peer = LongGatePeer(gate_target=600_000, window_s=0.6)
    client = DpmClient(port=peer.port, timeout=0.1, retries=0, slow_hz=1_000_000, log=lambda _m: None)
    try:
        client.get_status()  # 学到 gateTarget，之后的预算按它算
        assert client.gate_wait_budget() == pytest.approx(1.45)
        client.start()
        t0 = time.monotonic()
        result = client.wait_result(timeout=0.2, poll_interval=0.02)
        elapsed = time.monotonic() - t0
    finally:
        client.close()
        peer.close()
    assert result.valid
    assert elapsed >= 0.5  # 真的等满了闸门窗口，而不是提前放弃
    assert client.stats.timeouts == 0  # 一次请求超时都没有（旧代码在 0.1s 就超时）


def test_long_gate_result_budget_is_scaled() -> None:
    """结果按闸门窗口锁存、对端立刻回 `valid=0`：等结果预算 0.2s 也要放大。

    修复前：`wait_result(timeout=0.2)` 在 0.2s 就抛超时，而结果 0.6s 才就绪。
    """
    peer = LongGatePeer(gate_target=600_000, window_s=0.6, hold_s=0.0)
    client = DpmClient(port=peer.port, timeout=0.1, retries=0, slow_hz=1_000_000, log=lambda _m: None)
    try:
        client.get_status()
        client.start()
        t0 = time.monotonic()
        meas = client.wait_meas(timeout=0.2, poll_interval=0.01)
        elapsed = time.monotonic() - t0
    finally:
        client.close()
        peer.close()
    assert meas.gate_cnt == 600_000
    assert elapsed >= 0.5
    # 是「轮询到结果」而不是「一次请求阻塞着等」：对端立刻回 valid=0
    assert peer.counts.get(int(P.Cmd.GET_RESULT), 0) >= 2


def test_long_gate_budget_is_learned_inside_wait() -> None:
    """闸门未知时也要在等待开始前学会它：`start --wait` / `meas` 这类路径不会先读一次状态。

    客户端只知道自己给的基线（这里 0.2s），设备上的闸门却是 0.6s——等待一开始就得先读
    `GET_STATUS` 把闸门目标补上，否则预算仍按基线算，照旧误报超时。
    """
    peer = LongGatePeer(gate_target=600_000, window_s=0.6, hold_s=0.0)
    client = DpmClient(port=peer.port, timeout=0.1, retries=0, slow_hz=1_000_000, log=lambda _m: None)
    try:
        assert client.gate_target is None  # 还没读过状态
        client.start()  # 只发 START（协议里它不阻塞），不读状态
        t0 = time.monotonic()
        meas = client.wait_meas(timeout=0.2, poll_interval=0.01)
        elapsed = time.monotonic() - t0
    finally:
        client.close()
        peer.close()
    assert client.gate_target == 600_000  # 等待过程中学会了闸门目标
    assert meas.gate_cnt == 600_000
    assert elapsed >= 0.5  # 按学会后的预算等满了一个闸门窗口


def test_default_gate_keeps_short_timeout() -> None:
    """回归：默认闸门（2000 周期 = 40µs）不放大等待——该报超时还是要报，而且快。

    否则「长闸门不误报」会变成「所有等待都拖到最长」，源没有边沿时用户要白等好几秒。
    """
    peer = LongGatePeer(gate_target=P.DEFAULT_GATE_TARGET, window_s=30.0, hold_s=0.0)
    client = DpmClient(port=peer.port, timeout=0.1, retries=0, log=lambda _m: None)
    try:
        client.get_status()
        client.start()
        t0 = time.monotonic()
        with pytest.raises(P.DpmTimeoutError) as excinfo:
            client.wait_result(timeout=0.2, poll_interval=0.01)
        elapsed = time.monotonic() - t0
    finally:
        client.close()
        peer.close()
    assert 0.2 <= elapsed < 1.0
    assert "等待测量结果超时" in str(excinfo.value)


@pytest.mark.slow
def test_long_gate_measurement_does_not_time_out(sim_server) -> None:
    """端到端：`sim_server --gate 120000000`（2.4s 闸门）的测量正常读回，不报超时。

    修复前：等结果预算固定 2.0s < 2.4s 闸门窗口 → `wait_meas(timeout=2.0)` 必然抛
    `DpmTimeoutError`「等待测量结果超时」，而测量本身完全正常。
    """
    server = sim_server("--gate", str(LONG_GATE_TARGET))
    client = DpmClient(port=server.slave, timeout=0.5, retries=0)
    try:
        client.get_status()
        assert client.gate_wait_budget() > 2.4  # 预算已按闸门放大（> 旧的 2.0s 固定值）
        client.start()
        t0 = time.monotonic()
        meas = client.wait_meas(timeout=2.0, poll_interval=0.02)
        elapsed = time.monotonic() - t0
    finally:
        client.close()
    assert elapsed >= 2.3  # 闸门窗口 2.4s，真的等满了
    assert meas.gate_cnt >= LONG_GATE_TARGET
    assert meas.edge_cnt > 0


# ------------------------------------------------------------------ 通知帧分流的**独立**验证


class NotificationPeer:
    """用一个「外来」迷你对端（不是 dpm_host 的 sim_server）通过裸 pty 收发，
    确认 0xE1/0xE2 被当通知、0x81/0x82 只当响应（协议 v1.1）。
    """

    def __init__(self) -> None:
        self.master, slave = pty.openpty()
        self.port = os.ttyname(slave)
        self.requests: list[bytes] = []
        self.send_notifications = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._peer_loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        os.close(self.master)

    def _peer_loop(self) -> None:
        """极简对端：回 PING 响应，并主动发一条 STREAM_DATA(0xE1) 与 EVENT(0xE2)。"""
        parser = P.FrameParser()
        sent_notifications = False
        while not self._stop.is_set():
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


@pytest.fixture
def peer_client():
    """（外来对端, 客户端）：客户端 timeout=1.0，且不打印日志。"""
    peer = NotificationPeer()
    cli = DpmClient(port=peer.port, timeout=1.0, log=lambda _m: None)
    try:
        yield peer, cli
    finally:
        cli.close()
        peer.close()


def test_stream_and_event_are_dispatched_as_notifications(peer_client, monkeypatch) -> None:
    peer, client = peer_client
    peer.send_notifications = True  # 让对端在第一次 PING 后主动发两条通知
    streams: list[P.StreamData] = []
    events: list[P.Event] = []
    monkeypatch.setattr(client, "on_stream_data", streams.append)
    monkeypatch.setattr(client, "on_event", events.append)
    assert client.ping()
    deadline = time.monotonic() + 2.0
    while not (streams and events) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(streams) == 1
    assert streams[0].meas.seq == 7
    assert streams[0].lost_delta == 3
    assert streams[0].meas.freq_milli_hz == 25_000_000
    assert len(events) == 1
    assert (events[0].code, events[0].detail) == (1, 42)
    assert client.stats.notifications == 2
    # 通知帧不会污染请求/响应事务
    assert client.stats.unexpected == 0


def test_unknown_notification_code_is_ignored(peer_client) -> None:
    """0xE0..0xEF 里的未知通知码：忽略并计数，不影响事务。"""
    peer, client = peer_client
    client.ping()  # 先建立连接
    os.write(peer.master, P.encode_frame(0xE7, b"\x01\x02"))
    time.sleep(0.05)
    assert client.ping()
    assert client.stats.notifications >= 1
    assert client.stats.unexpected == 0


def test_notification_codes_are_not_confused_with_responses(peer_client) -> None:
    """0x81/0x82 只按响应处理：伪造一个 26 字节的 0x81 不会被当成 STREAM_DATA。"""
    peer, client = peer_client
    streams: list[P.StreamData] = []
    client.on_stream_data = streams.append
    client.ping()
    os.write(peer.master, P.encode_frame(0x81, b"\x00" * P.STREAM_DATA_SIZE))
    time.sleep(0.1)
    assert streams == []
    # 该帧字长不符（0x81 响应应为空），客户端只丢弃 + 计数，不抛异常
    assert client.ping()
    assert client.stats.unexpected >= 1
    assert client.stats.notifications == 0


# ------------------------------------------- 「源没有边沿」：ERR_BUSY 只表示「暂无结果」


class BusyResultPeer:
    """复现「切到没接信号的外部输入源」时 C 侧固件的响应：

    PL 停在 `busy = 1`、结果永不锁存 → `GET_RESULT` / `GET_MEAS` 等不到结果，回 `ERR_BUSY`
    （0xFF）。`GET_RESULT` 有 `valid` 字段，按协议 §3.0 本应回 `valid = 0`；这个对端刻意
    保留旧的 0xFF 行为，用来验证**上位机两种表达都当「还没有结果」继续轮询**，
    而不是把它当设备错误中止整段测量程序。

    回归背景：真机上曾报
    「设备错误：PS 返回错误响应 0xFF（ERR_BUSY），请求 0x05（GET_RESULT）」。
    """

    def __init__(self) -> None:
        self.master, slave = pty.openpty()
        self.port = os.ttyname(slave)
        self.counts: dict[int, int] = {}
        #: `GET_STATUS` 的 STATUS 字（测试可改成「有未读结果」等组合）
        self.status_word = self.STATUS_BUSY
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._peer_loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        os.close(self.master)

    #: `GET_STATUS`：busy=1（测量停在等边沿）、sigStatus=sourceReady（外部引脚恒 ready）
    STATUS_BUSY = 0x1
    STATUS_RESULT_VALID = 0x2
    SIGST_READY = 0x8

    def _peer_loop(self) -> None:
        parser = P.FrameParser()
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.master], [], [], 0.02)
            except OSError:  # pragma: no cover - pty 关闭
                return
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 256)
            except OSError:  # pragma: no cover
                return
            if not chunk:
                continue
            for ev in parser.feed(chunk):
                if not isinstance(ev, P.Frame):
                    continue
                cmd = int(ev.cmd)
                self.counts[cmd] = self.counts.get(cmd, 0) + 1
                if cmd == P.Cmd.PING:
                    reply = P.encode_frame(P.Cmd.PING.response)
                elif cmd == P.Cmd.GET_STATUS:
                    reply = P.encode_frame(
                        P.Cmd.GET_STATUS.response,
                        P.encode_status(P.Status(self.status_word, self.SIGST_READY, 2000, 0, 0)),
                    )
                elif cmd in (P.Cmd.GET_RESULT, P.Cmd.GET_MEAS):
                    reply = P.encode_frame(P.Cmd.ERR_BUSY)
                elif cmd == P.Cmd.SET_MODE:
                    reply = P.encode_frame(P.Cmd.SET_MODE.response, bytes([0]))
                else:
                    reply = P.encode_frame(cmd | 0x80)
                os.write(self.master, reply)


@pytest.fixture
def busy_peer_client():
    """（ERR_BUSY 对端, 客户端）：客户端 timeout=1.0、不打印日志。"""
    peer = BusyResultPeer()
    cli = DpmClient(port=peer.port, timeout=1.0, log=lambda _m: None)
    try:
        yield peer, cli
    finally:
        cli.close()
        peer.close()


def test_get_result_err_busy_is_polled_by_wait_result(busy_peer_client) -> None:
    """`wait_result`：`GET_RESULT` 回 ERR_BUSY 时继续轮询，最终报**超时**而不是设备错误。"""
    peer, client = busy_peer_client
    with pytest.raises(P.DpmTimeoutError) as excinfo:
        client.wait_result(timeout=0.3, poll_interval=0.02)
    # 必须是「超时」而不是 0xFF 设备错误（DpmTimeoutError.code == 0）
    assert excinfo.value.code == 0
    # 轮询了不止一次 GET_RESULT（0x05），说明 ERR_BUSY 被当「暂无结果」
    assert peer.counts.get(int(P.Cmd.GET_RESULT), 0) >= 2
    # 诊断要点名当前的输入源，用户才知道该去查外部引脚
    assert "外部引脚" in str(excinfo.value)


def test_get_result_err_busy_is_polled_by_wait_meas(busy_peer_client) -> None:
    """`wait_meas`：同一条 ERR_BUSY 在 GET_MEAS 与 GET_RESULT 两条路径上都不致命。"""
    peer, client = busy_peer_client
    assert client.start()  # START 成功，随后测量永不完成
    with pytest.raises(P.DpmTimeoutError) as excinfo:
        client.wait_meas(timeout=0.3, poll_interval=0.02)
    assert excinfo.value.code == 0
    assert peer.counts.get(int(P.Cmd.GET_RESULT), 0) >= 2
    assert "等待测量结果超时" in str(excinfo.value)


def test_no_result_diagnostic_reports_internal_source() -> None:
    """内部源诊断：`sigSelect=1` 且 `sourceReady=0` 时要点名「未使能/未锁定」。"""
    client = DpmClient(log=lambda _m: None)
    details = client._no_result_detail(P.Status(0x1, 0x0, 2000, 0, 0))
    assert "片内源未使能或 MMCM 未锁定" in details
    assert "片内信号源" in details
    # sourceReady=1 的内部源（enable|srcSelect|srcLocked|sourceReady = 0xF）：不该给误导
    details = client._no_result_detail(P.Status(0x1, 0xF, 2000, 0, 0))
    assert "片内源未使能或 MMCM 未锁定" not in details
    # 读不到 STATUS 时仍要给出兜底说明，不能抛异常
    assert "等待测量结果超时" in client._no_result_detail(None)


def test_cli_status_and_meas_survive_busy_get_meas(busy_peer_client, capsys) -> None:
    """CLI `status` / `meas`：结果槽里有未读旧结果、而 `GET_MEAS` 回 `ERR_BUSY` 时不许报设备错误。

    这是上面 `BusyResultPeer` 场景的 CLI 版：`--no-wait` 启动过一次测量、结果没人读走，
    再 `START` 一次却卡在没边的源上——`STATUS.resultValid` 仍为 1，`cli.py` 原先直接
    `client.get_meas()`，于是又把 `ERR_BUSY` 打成「设备错误」中止。
    """
    peer, client = busy_peer_client
    peer.status_word = BusyResultPeer.STATUS_BUSY | BusyResultPeer.STATUS_RESULT_VALID

    # `status` 是只读查询：读不到结果只说明原因，仍然算成功
    assert cli.cmd_status(argparse.Namespace(json=False), client) == 0
    captured = capsys.readouterr()
    assert "暂时读不到" in captured.out
    assert "设备错误" not in captured.err

    # `meas` 退化成「等一次新结果」：超时退出 + 诊断，而不是设备错误
    args = argparse.Namespace(json=False, no_start=False, result_timeout=0.3, expect_deg=None)
    assert cli.cmd_meas(args, client) == 1
    captured = capsys.readouterr()
    assert "测量未完成" in captured.err
    assert "ERR_BUSY" not in captured.err and "设备错误" not in captured.err

    # `--no-start`：不主动启动，也不该把「暂无结果」当设备错误
    args = argparse.Namespace(json=False, no_start=True, result_timeout=0.3, expect_deg=None)
    assert cli.cmd_meas(args, client) == 1
    assert "没有可用的新结果" in capsys.readouterr().err
