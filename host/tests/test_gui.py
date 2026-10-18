"""上位机 GUI 的两项单位换算与相位上界的单元测试（`host/dpm_host/gui.py`）。

覆盖两条**按硬件条件换算**的界面规则：

1. **闸门时间按秒设置**：界面输入秒，下发时换算成 `SET_GATE` 的低速周期数
   （`gate_seconds_to_cycles` / `gate_cycles_to_seconds`），`STATUS.gateTarget` 再回显成秒；
2. **相位上界逐频点不同**：>180° 是合法请求，但 MMCM 的 9 位相位码把它限制在
   `min(23017499/D, 359999)` 毫度（`freq_phase_limit_milli_deg`），因此 25MHz（D=40）能到
   359.999°、8MHz（D=125）只到 184.139°，界面按频点刷新相位输入框的上界。

纯函数用例不需要 Qt；控件用例用 `QT_QPA_PLATFORM=offscreen` 起一个离屏 `QApplication`
（Qt 平台插件不可用时 `pytest.skip`），因此无显示环境也能跑。真实窗口 + 串口的端到端证据由
`host/tools/gui_smoke.py` 提供（那是脚本而不是 pytest 用例）。

运行：
    仓库根目录：`uv run pytest host/tests/test_gui.py -q`
    host 目录： `uv run pytest tests/test_gui.py -q`
"""

from __future__ import annotations

import os
import time

import pytest
from dpm_host import gui as G
from dpm_host import mathx as M
from dpm_host import protocol as P
from dpm_host import sig as S

SLOW_HZ = 50_000_000


# ------------------------------------------------------------ 纯函数：闸门秒 ↔ 周期


def test_gate_seconds_to_cycles_matches_default_gate() -> None:
    """默认闸门 2000 个低速周期 = 40µs（`P.DEFAULT_GATE_TARGET` 与 50MHz 低速时钟）。"""
    cycles = G.gate_seconds_to_cycles(40e-6, SLOW_HZ)
    assert cycles == P.DEFAULT_GATE_TARGET == 2000
    assert G.gate_cycles_to_seconds(cycles, SLOW_HZ) == pytest.approx(40e-6)


def test_gate_seconds_to_cycles_round_trip() -> None:
    """秒 → 周期 → 秒 在 1µs 粒度上闭合（界面 6 位小数 = 1µs，50MHz 下 = 50 个周期）。"""
    for seconds in (1e-6, 40e-6, 1e-3, 0.5, 2.0):
        cycles = G.gate_seconds_to_cycles(seconds, SLOW_HZ)
        assert abs(G.gate_cycles_to_seconds(cycles, SLOW_HZ) - seconds) <= 1 / SLOW_HZ


def test_gate_cycles_clamped_to_protocol_range() -> None:
    """0 与超过闸门计数上界的请求都被夹回合法区间（协议/RTL 的 28 位闸门计数器）。"""
    assert G.gate_seconds_to_cycles(0.0, SLOW_HZ) == G.GATE_MIN_CYCLES
    assert G.gate_seconds_to_cycles(10.0, SLOW_HZ) == G.GATE_MAX_CYCLES
    assert G.GATE_MAX_CYCLES == M.GATE_TARGET_MAX
    assert G.GATE_MAX_SECONDS == pytest.approx(M.GATE_TARGET_MAX / SLOW_HZ)


def test_gate_max_seconds_covers_long_gate() -> None:
    """上限放开到 RTL 的真实上界（50MHz 下 ≈5.37s），长闸门在界面上能设进去。"""
    assert G.GATE_MAX_SECONDS > 5.0
    assert G.gate_seconds_to_cycles(5.0, SLOW_HZ) == 250_000_000
    assert G.gate_seconds_to_cycles(5.3, SLOW_HZ) == 265_000_000


def test_gate_conversion_uses_slow_clock() -> None:
    """换算跟低速时钟走：同一个秒值在 100MHz 下是两倍的周期数。"""
    assert G.gate_seconds_to_cycles(40e-6, 100_000_000) == 4000
    assert G.gate_cycles_to_seconds(4000, 100_000_000) == pytest.approx(40e-6)


def test_gate_conversion_rejects_bad_clock() -> None:
    for slow_hz in (0, -1):
        with pytest.raises(ValueError):
            G.gate_seconds_to_cycles(1e-3, slow_hz)
        with pytest.raises(ValueError):
            G.gate_cycles_to_seconds(2000, slow_hz)


def test_format_seconds_units() -> None:
    assert G.format_seconds(40e-6) == "40 µs"
    assert G.format_seconds(1e-3) == "1 ms"
    assert G.format_seconds(2.0) == "2 s"


# -------------------------------------------------------- 纯函数：频率写法解析


def test_parse_freq_hz_forms() -> None:
    """频率输入框与 CLI `--freq` 共用一套写法（`25MHz` / `25e6` / `25M` / `25 kHz`）。"""
    assert G.parse_freq_hz("25MHz") == 25_000_000
    assert G.parse_freq_hz("25e6") == 25_000_000
    assert G.parse_freq_hz("25M") == 25_000_000
    assert G.parse_freq_hz("25 kHz") == 25_000
    assert G.parse_freq_hz(" 25000000 ") == 25_000_000


@pytest.mark.parametrize("text", ["", "abc", "25MH", "inf", "nan"])
def test_parse_freq_hz_rejects_garbage(text: str) -> None:
    """识别不了的写法统一抛 `ValueError`（`inf`/`nan` 是 `OverflowError` 也必须被归一）。"""
    with pytest.raises(ValueError):
        G.parse_freq_hz(text)


# --------------------------------------------------- 纯函数：逐频点的相位上界


@pytest.mark.parametrize(
    ("freq_hz", "div", "limit_mdeg"),
    [
        # 与 `docs/ps-protocol.md` §6.2 的相位上界表一致（D 由冻结求解规则给出）
        (5_000_000, 120, 191_812),
        (6_250_000, 120, 191_812),
        (8_000_000, 125, 184_139),
        (10_000_000, 100, 230_174),
        (12_500_000, 80, 287_718),
        (20_000_000, 50, 359_999),
        (25_000_000, 40, 359_999),
        (50_000_000, 20, 359_999),
    ],
)
def test_freq_phase_limit_matches_protocol_table(freq_hz: int, div: int, limit_mdeg: int) -> None:
    """>180° 能不能设由频点的 D 决定，逐点与协议文档的上界表逐位一致。"""
    limit, got_div = G.freq_phase_limit_milli_deg(freq_hz, SLOW_HZ)
    assert (limit, got_div) == (limit_mdeg, div)
    # 上界本身必须是「可请求」的：相位码恰好落在 9 位字段内
    assert S.phase_code_fits(limit_mdeg - 1, div) or limit_mdeg == S.FULL_SCALE_MILLI_DEG - 1


def test_freq_phase_limit_allows_above_180_only_when_d_small() -> None:
    """25MHz（D=40）能设 225°；8MHz（D=125）不能——这正是界面要按频点刷新的原因。"""
    limit_25m, div_25m = G.freq_phase_limit_milli_deg(25_000_000, SLOW_HZ)
    assert div_25m == 40 and limit_25m > 225_000 and S.phase_code_fits(225_000, div_25m)

    limit_8m, div_8m = G.freq_phase_limit_milli_deg(8_000_000, SLOW_HZ)
    assert div_8m == 125 and limit_8m < 225_000 and not S.phase_code_fits(225_000, div_8m)
    # 8MHz 仍能略超 180°（184.139° 是 9 位相位码的上界，吸附后 183.96°）
    assert 180_000 < limit_8m < 190_000


def test_freq_phase_limit_no_solution_for_non_positive_freq() -> None:
    """非正频率在 MMCM 约束内无解 → `(0, 0)`（界面据此提示而不是显示假上界）。"""
    assert G.freq_phase_limit_milli_deg(0, SLOW_HZ) == (0, 0)
    assert G.freq_phase_limit_milli_deg(-1_000_000, SLOW_HZ) == (0, 0)


# ------------------------------------------------------- 控件：离屏 QApplication


@pytest.fixture(scope="module")
def window():
    """离屏 `DpmGuiApp`（不连接串口）：Qt 平台插件不可用时跳过控件用例。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qt = pytest.importorskip("PySide6.QtWidgets")
    try:
        app = qt.QApplication.instance() or qt.QApplication([])
    except Exception as exc:  # pragma: no cover - 无显示环境且 offscreen 插件缺失
        pytest.skip(f"Qt 平台插件不可用：{exc}")
    win = G.DpmGuiApp()
    yield win
    win.close()
    app.processEvents()


def test_window_phase_limit_follows_frequency(window) -> None:
    """频率输入框每次变化都刷新相位上界（默认 25MHz 放开，8MHz 收窄）。"""
    window.freq_edit.setText("25MHz")
    assert window.phase_spin.maximum() == pytest.approx(G.PHASE_MAX_DEG)
    assert "359.999" in window.phase_limit_label.text()

    window.freq_edit.setText("8MHz")
    assert window.phase_spin.maximum() == pytest.approx(184.139, abs=1e-3)
    assert "184.139" in window.phase_limit_label.text()

    window.freq_edit.setText("10MHz")
    assert window.phase_spin.maximum() == pytest.approx(230.174, abs=1e-3)


def test_window_phase_above_180_can_be_set(window) -> None:
    """25MHz 下 225° 可以输入（不再被 179.999° 卡住）；换到 8MHz 时按上界夹住并记日志。"""
    window.freq_edit.setText("25MHz")
    window.phase_spin.setValue(225.0)
    assert window.phase_spin.value() == pytest.approx(225.0)

    logged = len(window.log_view.toPlainText())
    window.freq_edit.setText("8MHz")
    assert window.phase_spin.value() == pytest.approx(184.139, abs=1e-3)
    assert "收窄" in window.log_view.toPlainText()[logged:]


def test_window_phase_limit_unknown_frequency_text(window) -> None:
    """频率文本写了一半（`25MH`）时不动上界，只更新提示，避免输入过程中上界乱跳。"""
    window.freq_edit.setText("25MHz")
    before = window.phase_spin.maximum()
    window.freq_edit.setText("25MH")
    assert window.phase_spin.maximum() == before
    assert "自动刷新" in window.phase_limit_label.text()
    window.freq_edit.setText("25MHz")  # 还原，避免影响后续用例


def test_window_phase_limit_no_solution(window) -> None:
    """非正频率在 MMCM 约束内无解：提示出来，而不是显示一个假上界。"""
    window.freq_edit.setText("0")
    assert "无解" in window.phase_limit_label.text()
    window.freq_edit.setText("25MHz")


def test_window_gate_is_seconds(window) -> None:
    """闸门输入框是秒（后缀 s），并实时显示等价周期数与等结果的等待预算。"""
    window.gate_spin.setValue(4e-5)
    assert window.gate_spin.suffix().strip() == "s"
    assert window.gate_spin.decimals() == 6
    assert "2000 低速周期" in window.gate_hint.text()
    # 默认闸门（40µs）的预算 ≈ 2×40µs + 0.25s，远小于界面的等结果基线
    assert "等待预算" in window.gate_hint.text()

    window.gate_spin.setValue(1e-3)
    assert "50000 低速周期" in window.gate_hint.text()

    # 长闸门：预算随闸门线性放大（5s 闸门 → ≈10.25s）
    window.gate_spin.setValue(5.0)
    assert "250000000 低速周期" in window.gate_hint.text()
    assert "10.25 s" in window.gate_hint.text()


def test_window_gate_echo_from_status(window) -> None:
    """`STATUS.gateTarget`（周期数）回显到秒输入框：20000 周期 → 400µs。"""
    window._show_status(P.Status(status=0, sig_status=0, gate_target=20_000, lost_count=0, seq=0))
    assert window.gate_spin.value() == pytest.approx(400e-6)
    assert "20000 低速周期" in window.gate_hint.text()


def test_window_gate_download_converts_to_cycles(window) -> None:
    """「设置闸门」下发的是换算后的低速周期数（工作线程里调用 `client.set_gate`）。"""

    class _FakeClient:
        slow_hz = SLOW_HZ
        dsp_hz = 450_000_000

        def __init__(self) -> None:
            self.gates: list[int] = []

        def set_gate(self, value: int) -> int:
            self.gates.append(int(value))
            return int(value)

        def get_status(self):  # 状态回读失败不影响本用例的断言
            raise RuntimeError("测试桩不提供状态")

    fake = _FakeClient()
    window.client = fake
    try:
        window.gate_spin.setValue(4e-5)  # 40µs
        window.after_set_gate()
        deadline = time.time() + 5.0
        while not fake.gates and time.time() < deadline:
            time.sleep(0.01)
        assert fake.gates == [2000]
    finally:
        window.client = None
