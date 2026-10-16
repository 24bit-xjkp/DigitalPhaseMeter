"""PySide6 图形界面：`uv run python -m dpm_host gui --port <串口|pty>`。

界面元素（对应任务书要求）：

- 连接/断开、波特率与连接状态（给出 `--port` 时启动后自动连接并 `IDENTIFY`）；
- 状态灯：busy / MMCM 锁定（locked）/ 结果丢失（lost）/ sourceReady；
- 频率显示（大字号 + `freqHz` 原值与周期）与**相位差 0~360°**（数字 + 仪表盘弧 + 0~360° 条形）；
- 单次测量 / 连续测量开关，CLEAR 与统计复位；
- **闸门时间**按**秒**设置（下发时按低速时钟换算成 `SET_GATE` 的周期数，见 :func:`gate_seconds_to_cycles`）；
- 输入源单选（外部引脚 / 片内信号源）；
- 片内源频率与相位输入 + 「设置」；扫描频点/相位列表 + 「扫描」（频点 × 相位 → 结果表 → CSV 导出）；
- 结果表（最近 N 条：序号 / 频率 / 相位 / gate / edge / phaseCnt / DSP 周期）+ 导出 CSV + 清空表；
- 日志区（带时间戳，含求解出的 VCO/MULT/D、相位量化提示、错误信息）。

线程模型：所有串口操作都在一个后台 `QThread` 里**串行**执行，结果通过 Qt 信号（跨线程队列连接）
回到界面线程刷新控件，因此界面不会因为请求超时而卡死，也不需要在工作线程里碰控件。

相位可控性来自 `spec/drp_semantics.json`（方案 A 下为 true）；不可控时（旧 bitstream）相位输入会
置灰并给出提示，而不是假装能改。

**相位上界逐频点不同**：>180° 的相位差是合法请求（相位计测的是无符号间隔），但 MMCM 的相位码
只有 9 位（`DELAY_TIME` 6 位 + `PHASE_MUX` 3 位），可请求上界是 `min(23017499/D, 359999)` 毫度
（见 `docs/ps-protocol.md` §6.2 与 :func:`sig.phase_max_milli_deg`）：25MHz（D=40）能到
359.999°，10MHz（D=100）到 230.174°，8MHz（D=125）只到 184.139°。因此频率输入框每次变化都会
用 :func:`freq_phase_limit_milli_deg` 重算相位输入的上界（而不是写死 180°）。
"""

from __future__ import annotations

import argparse
import math
import queue
import sys
import threading
import time
from collections.abc import Callable, Sequence

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import mathx as M
from . import protocol as P
from . import sig as S
from .cli import parse_freq  # 频率写法（`25MHz` / `25e6` / `25M`）与 CLI 共用一套，见 host/README.md 第 4 节
from .client import (
    GATE_TIMEOUT_FACTOR,
    GATE_TIMEOUT_SLACK_S,
    DpmClient,
    gate_wait_budget_seconds,
    scan_rows_to_csv,
)

__all__ = [
    "DpmGuiApp",
    "main",
    "format_seconds",
    "parse_freq_hz",
    "gate_seconds_to_cycles",
    "gate_cycles_to_seconds",
    "freq_phase_limit_milli_deg",
]

#: 结果表最多保留的行数。
MAX_ROWS = 200
#: 相位输入的满量程（度）：0° 与 360° 同义，满量程不接收（协议字段是毫度 0..359999）。
PHASE_MAX_DEG = 359.999
#: 闸门时间（秒）输入范围：下限 1µs，上限 = RTL/协议的闸门计数上界。
#:
#: RTL 的闸门计数器是 28 位（`GATE_TARGET_MAX = 2²⁸-1`），50MHz 下最长 5.3687s。
#: 旧界面把上限压在 2s，理由是「闸门时间长到几十秒时用户等不到结果」——那正是固定超时
#: （等结果 2.0s）造成的假超时；现在等待预算按闸门时间自动放大（`client.gate_wait_budget`），
#: 因此放开到真实上界，长闸门换来的是更高的频率分辨率。
GATE_MIN_SECONDS = 1e-6
GATE_MAX_SECONDS = M.GATE_TARGET_MAX / float(M.SLOW_HZ_DEFAULT)
#: 闸门计数（低速周期）的合法范围，与协议 `SET_GATE` / RTL 的 28 位闸门计数器一致。
GATE_MIN_CYCLES = 1
GATE_MAX_CYCLES = M.GATE_TARGET_MAX
#: 相位仪表盘的最小尺寸（逻辑像素）。
GAUGE_SIZE = 150
#: 相位条形的高度。
BAR_HEIGHT = 36
#: 调色板（深色主题）。
COLOR_FG = "#eeeeee"
COLOR_PANEL = "#2b2b2b"
COLOR_ON = "#2ecc71"
COLOR_OFF = "#555555"
COLOR_WARN = "#e67e22"
COLOR_BAD = "#e74c3c"
COLOR_GAUGE = "#3498db"
COLOR_INFO = "#9ad0f5"


# --------------------------------------------------------------- 单位换算
#
# 界面上的两个「单位」都在这里换算，避免 Qt 控件里散落魔数：
#
# * 闸门时间：界面按**秒**输入，协议 `SET_GATE` 的字段是**低速时钟周期数**
#   （`gateCnt = gateTarget + ceil(T_sig/T_slow)`，见 docs/ps-protocol.md 第 5 节）；
# * 相位上界：逐频点不同的 MMCM 9 位相位码上界（见模块 docstring）。


def format_seconds(seconds: float) -> str:
    """把秒格式化成人类可读的 µs / ms / s（日志与提示标签用）。"""
    value = float(seconds)
    if abs(value) >= 1.0:
        return f"{value:.6g} s"
    if abs(value) >= 1e-3:
        return f"{value * 1e3:.6g} ms"
    return f"{value * 1e6:.6g} µs"


def parse_freq_hz(text: str) -> int:
    """解析频率输入框的文本 → 整数 Hz（写法与 CLI 的 `--freq` 共用 :func:`cli.parse_freq`）。

    `25MHz` / `25e6` / `25M` / `25000000` / `25 kHz` 都接受。这里把 `float('inf')`、
    `float('nan')`（`OverflowError`）与空串这类输入统一成 `ValueError`，让 Qt 槽只兜一种异常。
    """
    try:
        return parse_freq(str(text))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"无法识别的频率写法：{text!r}") from exc


def gate_seconds_to_cycles(seconds: float, slow_hz: int) -> int:
    """闸门时间（秒）→ 低速周期数（= `SET_GATE` 字段值）。

    换算只差一个低速时钟比例：`gateTarget = round(seconds × f_slow)`。结果夹到
    `1..1e8`（协议 u32 字段与旧界面同界），保证不会下发 0 周期这种无意义的闸门。
    """
    hz = int(slow_hz)
    if hz <= 0:
        raise ValueError(f"低速时钟必须为正数，实际 {slow_hz}")
    cycles = int(round(float(seconds) * hz))
    return max(GATE_MIN_CYCLES, min(GATE_MAX_CYCLES, cycles))


def gate_cycles_to_seconds(cycles: int, slow_hz: int) -> float:
    """低速周期数 → 闸门时间（秒）；用于把 `STATUS.gateTarget` 回显到秒输入框。"""
    hz = int(slow_hz)
    if hz <= 0:
        raise ValueError(f"低速时钟必须为正数，实际 {slow_hz}")
    return int(cycles) / float(hz)


def freq_phase_limit_milli_deg(
    freq_hz: int,
    slow_hz: int = M.SLOW_HZ_DEFAULT,
    semantics: S.DrpSemantics | None = None,
) -> tuple[int, int]:
    """某频点在 MMCM 约束下的**可请求**相位上界（毫度）与输出分频 `D`。

    返回 ``(上界毫度, D)``；该频点在 MMCM 约束内无解时返回 ``(0, 0)``。

    上界由 9 位相位码决定（`sig.phase_max_milli_deg`），而 `D` 由频率的冻结求解规则给出
    （`sig.solve_sig`，与 PS 固件/金标准同一套），因此**上界逐频点不同**：
    25MHz（D=40）→ 359999（整个 0~360°）、10MHz（D=100）→ 230174、
    8MHz（D=125）→ 184139（只能略超 180°）。相位不影响 `D`，所以这里按请求相位 0° 求解。
    """
    sol = S.solve_sig(int(freq_hz), 0, int(slow_hz), semantics)
    if not sol.ok or sol.div <= 0:
        return 0, 0
    return S.phase_max_milli_deg(sol.div), int(sol.div)


class _Worker(QThread):
    """串口工作线程：串行执行投递进来的任务（任务自己负责把结果用信号发回界面线程）。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jobs: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._stop = threading.Event()

    def submit(self, job: Callable[[], None]) -> None:
        """把任务排进队列（可从界面线程调用）。"""
        self._jobs.put(job)

    def stop(self) -> None:
        """请求退出：排一个哨兵让 `run()` 的循环结束。"""
        self._stop.set()
        self._jobs.put(None)

    def run(self) -> None:  # pragma: no cover - 线程体
        while not self._stop.is_set():
            job = self._jobs.get()
            if job is None:
                break
            try:
                job()
            except Exception as exc:  # 任务内部应自己兜住异常，这里只做兜底
                print(f"[dpm-gui-worker] 任务异常：{exc!r}", file=sys.stderr)


class LampWidget(QWidget):
    """状态灯：圆点 + 标题（自绘，不依赖外部图标资源）。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._color = QColor(COLOR_OFF)
        self.setFixedSize(104, 30)

    def set_on(self, on: bool, color: str = COLOR_ON) -> None:
        """亮/灭（`color` 是亮起时的颜色）。"""
        self._color = QColor(color if on else COLOR_OFF)
        self.update()

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt 命名约定
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(self._color))
        painter.setPen(QPen(QColor("#000000"), 1))
        painter.drawEllipse(4, 6, 18, 18)
        painter.setPen(QPen(QColor(COLOR_FG)))
        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        painter.drawText(28, 21, self._title)
        painter.end()


class PhaseGauge(QWidget):
    """0~360° 仪表盘：圆弧 + 刻度 + 指针 + 中心读数。0° 在正上方，顺时针增长。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._phase_deg = 0.0
        self.setMinimumSize(GAUGE_SIZE, GAUGE_SIZE)

    def set_phase(self, phase_deg: float) -> None:
        self._phase_deg = float(phase_deg) % 360.0
        self.update()

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt 命名约定
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = float(min(self.width(), self.height()))
        cx, cy = self.width() / 2.0, self.height() / 2.0
        radius = side / 2.0 - 14.0
        painter.setPen(QPen(QColor("#666666"), 2))
        painter.setBrush(QBrush(QColor(COLOR_PANEL)))
        painter.drawEllipse(int(cx - radius), int(cy - radius), int(2 * radius), int(2 * radius))

        # 弧：Qt 的角度单位是 1/16 度、逆时针为正，因此顺时针绘制要取负
        painter.setPen(QPen(QColor(COLOR_GAUGE), 6))
        painter.drawArc(
            int(cx - radius),
            int(cy - radius),
            int(2 * radius),
            int(2 * radius),
            int(90 * 16),
            int(-max(0.01, min(359.99, self._phase_deg)) * 16),
        )

        painter.setPen(QPen(QColor("#aaaaaa"), 1))
        for tick in range(0, 361, 45):
            angle = math.radians(90 - tick)
            x1 = cx + (radius - 10) * math.cos(angle)
            y1 = cy - (radius - 10) * math.sin(angle)
            x2 = cx + radius * math.cos(angle)
            y2 = cy - radius * math.sin(angle)
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))

        angle = math.radians(90 - self._phase_deg)
        painter.setPen(QPen(QColor(COLOR_BAD), 3))
        painter.drawLine(int(cx), int(cy), int(cx + (radius - 16) * math.cos(angle)), int(cy - (radius - 16) * math.sin(angle)))
        painter.setBrush(QBrush(QColor(COLOR_FG)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(int(cx - 3), int(cy - 3), 6, 6)

        painter.setPen(QPen(QColor(COLOR_FG)))
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(0, int(cy + radius - 16), self.width(), 16, int(Qt.AlignmentFlag.AlignHCenter), f"{self._phase_deg:.3f}°")
        painter.end()


class PhaseBar(QWidget):
    """0~360° 条形指示（带 0/90/180/270/360 刻度）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._phase_deg = 0.0
        self.setMinimumHeight(BAR_HEIGHT)

    def set_phase(self, phase_deg: float) -> None:
        self._phase_deg = float(phase_deg) % 360.0
        self.update()

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt 命名约定
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        bar_h = h - 12
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(COLOR_PANEL)))
        painter.drawRect(0, 0, w, bar_h)
        painter.setBrush(QBrush(QColor(COLOR_GAUGE)))
        painter.drawRect(1, 1, int((self._phase_deg / 360.0) * max(1, w - 2)), max(1, bar_h - 2))
        painter.setPen(QPen(QColor("#aaaaaa"), 1))
        font = QFont()
        font.setPointSize(7)
        painter.setFont(font)
        for tick in range(0, 361, 90):
            x = int((tick / 360.0) * (w - 2))
            painter.drawLine(x, bar_h, x, bar_h + 4)
            painter.drawText(max(0, min(w - 24, x - 8)), bar_h + 4, 24, 12, int(Qt.AlignmentFlag.AlignHCenter), str(tick))
        painter.end()


class DpmGuiApp(QMainWindow):
    """主窗口：串口操作投给 `_Worker`，结果经信号回界面线程。"""

    # ---- 工作线程 → 界面线程的信号 ----
    sig_log = Signal(str)
    sig_conn_state = Signal(bool, str, object)  # connected, text, Identify | None
    sig_status = Signal(object)  # P.Status
    sig_meas = Signal(object, object)  # P.Meas, M.Meas
    sig_scan_done = Signal(list, str)  # rows, message
    sig_scan_progress = Signal(int, int)  # done, total
    sig_error = Signal(str, str)  # title, message
    sig_busy = Signal(bool)

    def __init__(self, *, port: str | None = None, baud: int = P.DEFAULT_BAUD, timeout: float = 0.5) -> None:
        super().__init__()
        self.setWindowTitle("DigitalPhaseMeter 上位机（PySide6）")
        self.resize(1180, 860)

        self.client: DpmClient | None = None
        self.semantics = S.current_semantics()
        self.last_rows: list[dict] = []
        self._continuous = False
        self._scan_cancel = threading.Event()

        self.worker = _Worker(self)
        self.worker.start()

        self._build_ui()
        self._connect_signals()
        self._apply_connected(False, "未连接", None)
        self._refresh_semantics_note()

        if port:
            self.port_edit.setText(port)
            self.after_connect()

    # ------------------------------------------------------------ 界面搭建

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        root.addWidget(self._build_conn_bar())
        middle = QHBoxLayout()
        middle.addWidget(self._build_left_column(), 1)
        middle.addWidget(self._build_right_column(), 1)
        root.addLayout(middle, 1)
        root.addWidget(self._build_table_group(), 2)
        root.addWidget(self._build_log_group(), 1)
        self.statusBar().showMessage("就绪")

    def _build_conn_bar(self) -> QWidget:
        box = QGroupBox("连接")
        lay = QHBoxLayout(box)
        lay.addWidget(QLabel("串口:"))
        self.port_edit = QLineEdit()
        self.port_edit.setPlaceholderText("/dev/ttyUSB0 或 /dev/pts/N")
        self.port_edit.setMinimumWidth(200)
        lay.addWidget(self.port_edit)
        lay.addWidget(QLabel("波特率:"))
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(9600, 3_000_000)
        self.baud_spin.setValue(int(P.DEFAULT_BAUD))
        lay.addWidget(self.baud_spin)
        lay.addWidget(QLabel("超时(s):"))
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.05, 10.0)
        self.timeout_spin.setSingleStep(0.1)
        self.timeout_spin.setValue(0.5)
        lay.addWidget(self.timeout_spin)
        self.connect_btn = QPushButton("连接")
        self.disconnect_btn = QPushButton("断开")
        lay.addWidget(self.connect_btn)
        lay.addWidget(self.disconnect_btn)
        self.conn_label = QLabel("未连接")
        self.conn_label.setStyleSheet(f"color: {COLOR_FG}; font-weight: bold;")
        lay.addWidget(self.conn_label)
        lay.addStretch(1)
        return box

    def _build_left_column(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)

        status_box = QGroupBox("状态")
        slay = QVBoxLayout(status_box)
        lamps = QHBoxLayout()
        self.lamp_busy = LampWidget("busy")
        self.lamp_locked = LampWidget("locked")
        self.lamp_lost = LampWidget("lost")
        self.lamp_ready = LampWidget("ready")
        for lamp in (self.lamp_busy, self.lamp_locked, self.lamp_lost, self.lamp_ready):
            lamps.addWidget(lamp)
        lamps.addStretch(1)
        slay.addLayout(lamps)
        self.status_label = QLabel("status=-  sigStatus=-  gateTarget=-  lostCount=-  seq=-")
        self.status_label.setWordWrap(True)
        self.status_label.setFont(QFont("monospace", 9))
        self.status_label.setStyleSheet(f"color: {COLOR_INFO};")
        slay.addWidget(self.status_label)
        lay.addWidget(status_box)

        sig_box = QGroupBox("片内信号源（MMCM）")
        form = QFormLayout(sig_box)
        self.freq_edit = QLineEdit("25MHz")
        self.freq_edit.setToolTip("写法：25MHz / 25e6 / 25M / 25000000（Hz）；与 CLI 的 --freq 一致")
        self.phase_spin = QDoubleSpinBox()
        # 上界不是固定 180°：它由频点的 D 决定（9 位相位码），在 _refresh_phase_limit() 里刷新。
        self.phase_spin.setRange(0.0, PHASE_MAX_DEG)
        self.phase_spin.setDecimals(3)
        self.phase_spin.setSingleStep(1.0)
        self.phase_spin.setValue(90.0)
        self.phase_spin.setSuffix(" °")
        form.addRow("频率:", self.freq_edit)
        form.addRow("相位差:", self.phase_spin)
        self.phase_limit_label = QLabel("相位上界：-")
        self.phase_limit_label.setWordWrap(True)
        self.phase_limit_label.setStyleSheet(f"color: {COLOR_INFO};")
        form.addRow(self.phase_limit_label)
        self.sig_set_btn = QPushButton("设置片内源")
        form.addRow("", self.sig_set_btn)
        self.semantics_label = QLabel("")
        self.semantics_label.setWordWrap(True)
        self.semantics_label.setStyleSheet(f"color: {COLOR_INFO};")
        form.addRow(self.semantics_label)
        lay.addWidget(sig_box)

        src_box = QGroupBox("输入源")
        sl = QHBoxLayout(src_box)
        self.radio_ext = QRadioButton("外部引脚")
        self.radio_int = QRadioButton("片内信号源")
        self.int_group = QButtonGroup(self)
        self.int_group.addButton(self.radio_ext)
        self.int_group.addButton(self.radio_int)
        self.radio_int.setChecked(True)
        sl.addWidget(self.radio_ext)
        sl.addWidget(self.radio_int)
        self.src_apply_btn = QPushButton("应用")
        sl.addWidget(self.src_apply_btn)
        sl.addStretch(1)
        lay.addWidget(src_box)

        ctl_box = QGroupBox("测量控制")
        grid = QGridLayout(ctl_box)
        self.single_btn = QPushButton("单次测量")
        self.cont_btn = QPushButton("连续测量：关")
        self.clear_btn = QPushButton("CLEAR")
        self.reset_btn = QPushButton("复位统计")
        self.gate_spin = QDoubleSpinBox()
        # 闸门时间按**秒**输入：6 位小数（1µs）比测量需要的粒度还细（50MHz 下 1 个低速周期 = 20ns），
        # 下发时按低速时钟换算成周期数（见 gate_seconds_to_cycles）。
        self.gate_spin.setRange(GATE_MIN_SECONDS, GATE_MAX_SECONDS)
        self.gate_spin.setDecimals(6)
        self.gate_spin.setSingleStep(1e-5)
        self.gate_spin.setValue(gate_cycles_to_seconds(P.DEFAULT_GATE_TARGET, M.SLOW_HZ_DEFAULT))
        self.gate_spin.setSuffix(" s")
        self.gate_spin.setToolTip("闸门时间（秒）：= 低速周期数 / 低速时钟；下发时换算成 SET_GATE 的周期数")
        self.gate_hint = QLabel("")
        self.gate_hint.setWordWrap(True)
        self.gate_hint.setStyleSheet(f"color: {COLOR_INFO};")
        self.gate_btn = QPushButton("设置闸门")
        grid.addWidget(self.single_btn, 0, 0)
        grid.addWidget(self.cont_btn, 0, 1)
        grid.addWidget(self.clear_btn, 1, 0)
        grid.addWidget(self.reset_btn, 1, 1)
        grid.addWidget(QLabel("闸门时间:"), 2, 0)
        grid.addWidget(self.gate_spin, 2, 1)
        grid.addWidget(self.gate_hint, 3, 0, 1, 2)
        grid.addWidget(self.gate_btn, 4, 0, 1, 2)
        lay.addWidget(ctl_box)
        lay.addStretch(1)
        return panel

    def _build_right_column(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)

        meas_box = QGroupBox("测量结果")
        mlay = QVBoxLayout(meas_box)
        self.freq_label = QLabel("--")
        big = QFont()
        big.setPointSize(26)
        big.setBold(True)
        self.freq_label.setFont(big)
        self.freq_raw_label = QLabel("freqHz = -")
        self.freq_raw_label.setStyleSheet(f"color: {COLOR_INFO};")
        mlay.addWidget(self.freq_label)
        mlay.addWidget(self.freq_raw_label)

        gauges = QHBoxLayout()
        self.gauge = PhaseGauge()
        gauges.addWidget(self.gauge)
        right = QVBoxLayout()
        self.phase_label = QLabel("--")
        phase_font = QFont()
        phase_font.setPointSize(20)
        phase_font.setBold(True)
        self.phase_label.setFont(phase_font)
        self.phase_extra = QLabel("phaseCnt=-  edgeCnt=-")
        self.phase_extra.setWordWrap(True)
        self.phase_extra.setStyleSheet(f"color: {COLOR_INFO};")
        right.addWidget(self.phase_label)
        right.addWidget(self.phase_extra)
        right.addStretch(1)
        gauges.addLayout(right, 1)
        mlay.addLayout(gauges)
        self.phase_bar = PhaseBar()
        mlay.addWidget(self.phase_bar)
        lay.addWidget(meas_box)

        scan_box = QGroupBox("多频点 × 多相位扫描")
        sform = QFormLayout(scan_box)
        self.scan_freqs_edit = QLineEdit("5,6.25,8,10,12.5,20,25")
        self.scan_phases_edit = QLineEdit("0,45,90,135")
        self.scan_settle_spin = QDoubleSpinBox()
        self.scan_settle_spin.setRange(0.0, 5.0)
        self.scan_settle_spin.setSingleStep(0.05)
        self.scan_settle_spin.setValue(0.1)
        self.scan_settle_spin.setSuffix(" s")
        sform.addRow("频点(MHz):", self.scan_freqs_edit)
        sform.addRow("相位(°):", self.scan_phases_edit)
        sform.addRow("每点稳定:", self.scan_settle_spin)
        self.scan_btn = QPushButton("开始扫描")
        sform.addRow("", self.scan_btn)
        self.scan_progress = QProgressBar()
        self.scan_progress.setValue(0)
        sform.addRow(self.scan_progress)
        lay.addWidget(scan_box)
        lay.addStretch(1)
        return panel

    def _build_table_group(self) -> QWidget:
        box = QGroupBox(f"结果表（最近 {MAX_ROWS} 条）")
        lay = QVBoxLayout(box)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["序号", "频率", "相位差", "gateCnt", "edgeCnt", "phaseCnt", "DSP周期"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self.table)
        btns = QHBoxLayout()
        self.export_btn = QPushButton("导出 CSV")
        self.clear_table_btn = QPushButton("清空表")
        btns.addWidget(self.export_btn)
        btns.addWidget(self.clear_table_btn)
        btns.addStretch(1)
        lay.addLayout(btns)
        return box

    def _build_log_group(self) -> QWidget:
        box = QGroupBox("日志")
        lay = QVBoxLayout(box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(QFont("monospace", 9))
        lay.addWidget(self.log_view)
        return box

    def _connect_signals(self) -> None:
        self.connect_btn.clicked.connect(self.after_connect)
        self.disconnect_btn.clicked.connect(self.after_disconnect)
        self.single_btn.clicked.connect(self.after_single)
        self.cont_btn.clicked.connect(self.after_continuous_toggle)
        self.clear_btn.clicked.connect(self.after_clear)
        self.reset_btn.clicked.connect(self.after_reset_counters)
        self.gate_btn.clicked.connect(self.after_set_gate)
        self.src_apply_btn.clicked.connect(self.after_set_source)
        self.sig_set_btn.clicked.connect(self.after_set_sig)
        self.scan_btn.clicked.connect(self.after_scan)
        self.export_btn.clicked.connect(self.after_export)
        self.clear_table_btn.clicked.connect(self.after_clear_table)
        # 单位换算的联动：频率变了 → 相位上界跟着变（逐频点不同）；闸门秒值变了 → 提示周期数
        self.freq_edit.textChanged.connect(self._refresh_phase_limit)
        self.gate_spin.valueChanged.connect(self._refresh_gate_hint)

        self.sig_log.connect(self.log)
        self.sig_conn_state.connect(self._apply_connected)
        self.sig_status.connect(self._show_status)
        self.sig_meas.connect(self._show_meas)
        self.sig_scan_done.connect(self._on_scan_done)
        self.sig_scan_progress.connect(self._on_scan_progress)
        self.sig_error.connect(self._show_error)
        self.sig_busy.connect(self._set_busy)

    # ------------------------------------------------------------ 小工具

    @Slot(str)
    def log(self, message: str) -> None:
        """追加一行带时间戳的日志（界面线程）。"""
        self.log_view.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {message}")

    def submit(self, job: Callable[[], None]) -> None:
        """把任务投给工作线程。"""
        self.worker.submit(job)

    @Slot(bool)
    def _set_busy(self, busy: bool) -> None:
        self.statusBar().showMessage("忙…" if busy else "就绪")

    @Slot(str, str)
    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def _require_client(self) -> DpmClient | None:
        if self.client is None:
            self.sig_log.emit("尚未连接")
            return None
        return self.client

    def _slow_hz(self) -> int:
        """当前低速时钟频率（未连接/未 `IDENTIFY` 时用默认 50MHz）。

        闸门秒值换算与相位上界求解都要用它；`DpmClient` 的 `IDENTIFY` 会覆盖默认值。
        """
        if self.client is not None and self.client.slow_hz:
            return int(self.client.slow_hz)
        return M.SLOW_HZ_DEFAULT

    def _refresh_semantics_note(self) -> None:
        """按 `spec/drp_semantics.json` 的结论刷新片内源提示与相位输入可用性。"""
        sem = self.semantics
        if sem.phase_controllable:
            self.phase_spin.setEnabled(True)
            self.semantics_label.setText(f"相位可在线设定：网格 {S.PHASE_GRID_MDEG_NUM}/D 毫度（1/8 VCO 周期 = 45°/D）；来源 {sem.source}")
        else:
            self.phase_spin.setEnabled(False)
            self.semantics_label.setText(
                f"⚠ 当前 bitstream 相位固定 {sem.phase_static_milli_deg / 1000:.3f}°，相位输入已置灰；来源 {sem.source}"
            )
        self._refresh_phase_limit()

    @Slot(str)
    def _refresh_phase_limit(self, _text: str = "") -> None:
        """按频率输入框刷新相位输入的上界与说明（上界逐频点不同，见模块 docstring）。

        相位不影响 MMCM 的输出分频 `D`，所以用请求相位 0° 求解即可。频率写法还没写完
        （如 `2`、`25MH` 打了一半）时只更新提示，不动上界，免得输入过程中上界乱跳。
        """
        if not self.semantics.phase_controllable:
            self.phase_limit_label.setText("相位不可控：相位输入已置灰")
            return
        try:
            freq_hz = parse_freq_hz(self.freq_edit.text())
        except ValueError:
            self.phase_limit_label.setText("相位上界：频率写好后自动刷新")
            return
        limit_mdeg, div = freq_phase_limit_milli_deg(freq_hz, self._slow_hz(), self.semantics)
        if div <= 0:
            self.phase_limit_label.setText(f"⚠ {freq_hz / 1e6:g}MHz 在 MMCM 约束内无解，相位上界：-")
            return
        grid = S.mmcm_phase_grid_milli_deg(div)
        limit_deg = limit_mdeg / 1000.0
        new_max = min(PHASE_MAX_DEG, limit_deg)
        old_value = float(self.phase_spin.value())
        self.phase_spin.setMaximum(new_max)
        if float(self.phase_spin.value()) < old_value - 1e-9:
            # setMaximum() 会把超出的当前值夹住：这不是用户改的，要写一条日志说明原因
            new_value = float(self.phase_spin.value())
            self.log(f"相位输入按 MMCM 上界收窄：{old_value:.3f}° → {new_value:.3f}°（{freq_hz / 1e6:g}MHz 的 D={div}）")
        if new_max >= PHASE_MAX_DEG:
            note = f"相位上界 {PHASE_MAX_DEG:.3f}°（整个 0~360° 都可请求）"
        else:
            # D ≤ 126 时上界恒 > 180°（23017499/126 = 182.678°），所以这里只说「到不了 360°」
            note = f"相位上界 {new_max:.3f}°（>180° 可请求，但不到 360°）"
        self.phase_limit_label.setText(f"{note}；D={div}，网格 {float(grid) / 1000:.4f}°")

    @Slot(float)
    def _refresh_gate_hint(self, _value: float = 0.0) -> None:
        """把秒输入框换算成低速周期数显示出来（下发的就是这个周期数），并给出等结果的等待预算。

        等待预算 = `2×闸门 + 0.25s`（`DpmClient.gate_wait_budget`）：长闸门下一次测量本来
        就要跑一个闸门窗口，界面上先说清要等多久，用户才不会把它当成卡死。
        """
        slow = self._slow_hz()
        cycles = gate_seconds_to_cycles(self.gate_spin.value(), slow)
        resolution_hz = slow / cycles
        budget = gate_wait_budget_seconds(cycles, slow)
        self.gate_hint.setText(
            f"= {cycles} 低速周期 @ {slow / 1e6:g}MHz（频率分辨率 ≈ {resolution_hz / 1000:.4g} kHz"
        )

    # ------------------------------------------------------------ 连接

    def after_connect(self) -> None:
        """连接串口：PING + IDENTIFY（在后台线程里做）。"""
        port = self.port_edit.text().strip()
        if not port:
            self._show_error("参数错误", "请填写串口路径（如 /dev/ttyUSB0 或 /dev/pts/3）")
            return
        baud = int(self.baud_spin.value())
        timeout = float(self.timeout_spin.value())
        self.sig_busy.emit(True)
        self.sig_log.emit(f"正在连接 {port}（{baud} baud）…")

        def job() -> None:
            client = DpmClient(port=port, baud=baud, timeout=timeout)
            try:
                client.ping()
                info = client.identify()
            except Exception as exc:
                client.close()
                self.sig_log.emit(f"连接失败：{type(exc).__name__}: {exc}")
                self.sig_error.emit("连接失败", f"{type(exc).__name__}: {exc}")
                self.sig_busy.emit(False)
                return
            self.client = client
            self.sig_conn_state.emit(True, f"已连接 {port}", info)
            self.sig_log.emit(
                f"已连接 {port}：ver=0x{info.ver:08X}（{info.ver_text}）"
                f" dsp={info.dsp_clk_hz / 1e6:g}MHz slow={info.slow_clk_hz / 1e6:g}MHz"
                f" flags=0x{info.flags:04X}（相位可控={'是' if info.phase_controllable else '否'}）"
            )
            self.sig_busy.emit(False)
            self._job_status()

        self.submit(job)

    def after_disconnect(self) -> None:
        """断开串口（并停掉连续测量）。"""
        self._continuous = False

        def job() -> None:
            client, self.client = self.client, None
            if client is not None:
                client.close()
            self.sig_conn_state.emit(False, "未连接", None)
            self.sig_log.emit("已断开")

        self.submit(job)

    @Slot(bool, str, object)
    def _apply_connected(self, connected: bool, text: str, info: object) -> None:
        color = COLOR_ON if connected else COLOR_FG
        self.conn_label.setText(text)
        self.conn_label.setStyleSheet(f"color: {color}; font-weight: bold;")
        # 连接/断开都会改变低速时钟（`IDENTIFY` 才知道真实值），闸门秒值换算与相位上界都要跟着刷新
        self._refresh_phase_limit()
        self._refresh_gate_hint()
        for widget in (
            self.single_btn,
            self.cont_btn,
            self.clear_btn,
            self.reset_btn,
            self.scan_btn,
            self.sig_set_btn,
            self.src_apply_btn,
            self.gate_btn,
        ):
            widget.setEnabled(connected)
        if not connected:
            for lamp in (self.lamp_busy, self.lamp_locked, self.lamp_lost, self.lamp_ready):
                lamp.set_on(False)
            self.status_label.setText("status=-  sigStatus=-  gateTarget=-  lostCount=-  seq=-")
            self.freq_label.setText("--")
            self.freq_raw_label.setText("freqHz = -")
            self.phase_label.setText("--")
            self.phase_extra.setText("phaseCnt=-  edgeCnt=-")
            self.gauge.set_phase(0.0)
            self.phase_bar.set_phase(0.0)

    # ------------------------------------------------------------ 状态 / 结果

    def _job_status(self) -> None:
        """读一次状态；若有未读结果则顺带读走（在工作线程里执行）。"""

        def job() -> None:
            client = self._require_client()
            if client is None:
                return
            try:
                st = client.get_status()
            except Exception as exc:
                self.sig_log.emit(f"读状态失败：{type(exc).__name__}: {exc}")
                return
            self.sig_status.emit(st)
            if not st.result_valid:
                return
            try:
                meas = client.try_get_meas()
            except Exception as exc:
                self.sig_log.emit(f"读结果失败：{type(exc).__name__}: {exc}")
                return
            if meas is None:
                return
            slow = client.slow_hz or M.SLOW_HZ_DEFAULT
            dsp = client.dsp_hz or M.DSP_HZ_DEFAULT
            self.sig_meas.emit(meas, M.meas_calc(meas.gate_cnt, meas.edge_cnt, meas.phase_cnt, slow, dsp))

        self.submit(job)

    @Slot(object)
    def _show_status(self, st: P.Status) -> None:
        self.lamp_busy.set_on(bool(st.busy), COLOR_WARN)
        self.lamp_locked.set_on(bool(st.src_locked))
        self.lamp_lost.set_on(bool(st.lost), COLOR_BAD)
        self.lamp_ready.set_on(bool(st.source_ready))
        self.status_label.setText(
            f"status=0x{st.status:08X} [{st.status_text}]  "
            f"sigStatus=0x{st.sig_status:08X} [{st.sig_status_text}]  "
            f"gateTarget={st.gate_target}  lostCount={st.lost_count}  seq={st.seq}"
        )
        self.radio_int.setChecked(bool(st.sig_select))
        self.radio_ext.setChecked(not bool(st.sig_select))
        if st.gate_target and not self.gate_spin.hasFocus():
            # 秒输入框回显状态里的周期数（正常情况下就是刚下发的那次换算，回到同一个秒值）
            self.gate_spin.setValue(gate_cycles_to_seconds(st.gate_target, self._slow_hz()))

    @Slot(object, object)
    def _show_meas(self, meas: P.Meas, calc: M.Meas) -> None:
        # 频率/相位一律用计数本地换算：wire 的 freqMilliHz 是 u32，>4.295MHz 会溢出
        self.freq_label.setText(M.format_freq_hz(calc.freq_hz))
        period_us = M.signal_period_100ps(calc.freq_hz) / 1e4
        self.freq_raw_label.setText(f"freqHz = {calc.freq_hz}  （周期 {period_us:.6g} µs）")
        phase_milli = M.normalize_phase_milli_deg(calc.phase_milli_deg)
        self.phase_label.setText(M.format_milli_deg(phase_milli))
        self.gauge.set_phase(phase_milli / 1000.0)
        self.phase_bar.set_phase(phase_milli / 1000.0)
        cycles = M.phase_cycles_float(meas.phase_cnt, meas.edge_cnt)
        dsp = self.client.dsp_hz if self.client else M.DSP_HZ_DEFAULT
        step = M.phase_quant_milli_deg(calc.freq_hz, dsp)
        self.phase_extra.setText(
            f"phaseCnt={meas.phase_cnt}  edgeCnt={meas.edge_cnt}  " f"phaseCnt/edgeCnt={cycles:.6f} DSP周期（量化步长 {step / 1000:.3f}°）"
        )
        self._add_row(meas, cycles)

    def _add_row(self, meas: P.Meas, cycles: float) -> None:
        values = [
            str(meas.seq),
            M.format_freq_hz(meas.freq_milli_hz),
            M.format_milli_deg(meas.phase_milli_deg),
            str(meas.gate_cnt),
            str(meas.edge_cnt),
            str(meas.phase_cnt),
            f"{cycles:.4f}",
        ]
        self.table.insertRow(0)
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            self.table.setItem(0, col, item)
        while self.table.rowCount() > MAX_ROWS:
            self.table.removeRow(self.table.rowCount() - 1)

    # ------------------------------------------------------------ 测量操作

    def after_single(self) -> None:
        """单次测量：启动一次并等结果。"""
        client = self._require_client()
        if client is None:
            return
        self.sig_busy.emit(True)

        def job() -> None:
            try:
                client.set_mode(P.MODE_SINGLE)
                result, meas, calc = client.measure_once(timeout=max(2.0, client.timeout * 4))
            except Exception as exc:
                self.sig_log.emit(f"单次测量失败：{type(exc).__name__}: {exc}")
                self.sig_busy.emit(False)
                return
            self.sig_meas.emit(meas, calc)
            self.sig_log.emit(
                f"单次测量完成：seq={result.seq} gateCnt={result.gate_cnt} edgeCnt={result.edge_cnt} "
                f"phaseCnt={result.phase_cnt} → {calc.freq_hz / 1e6:.6f}MHz / {calc.phase_milli_deg / 1000:.3f}°"
            )
            self.sig_busy.emit(False)
            self._job_status()

        self.submit(job)

    def after_continuous_toggle(self) -> None:
        """连续测量开/关（工作线程里循环等结果，直到关闭或出错）。"""
        client = self._require_client()
        if client is None:
            return
        if self._continuous:
            self._continuous = False
            self.cont_btn.setText("连续测量：关")
            self.log("连续测量已停止")
            return
        self._continuous = True
        self.cont_btn.setText("连续测量：开")

        def job() -> None:
            try:
                client.set_mode(P.MODE_CONTINUOUS)
                client.start()
            except Exception as exc:
                self.sig_log.emit(f"连续测量启动失败：{type(exc).__name__}: {exc}")
                self._continuous = False
                self.cont_btn.setText("连续测量：关")
                return
            self.sig_log.emit("连续测量已启动")
            while self._continuous and self.client is client:
                try:
                    result = client.wait_result(timeout=1.0)
                    # `GET_MEAS` 可能回 `ERR_BUSY`（对端暂无**新**结果可给，例如刚读走一条、
                    # 下一次测量还没结束）。那不是设备错误，不该中断连续测量：用刚读到的
                    # `GET_RESULT` 计数本地换算，界面照常刷新这一条结果。
                    meas = client.try_get_meas()
                except Exception as exc:
                    self.sig_log.emit(f"连续测量中断：{type(exc).__name__}: {exc}")
                    break
                calc = M.meas_calc(result.gate_cnt, result.edge_cnt, result.phase_cnt, client.slow_hz, client.dsp_hz)
                if meas is None:
                    meas = P.Meas(
                        seq=result.seq,
                        gate_cnt=result.gate_cnt,
                        edge_cnt=result.edge_cnt,
                        phase_cnt=result.phase_cnt,
                        freq_milli_hz=calc.freq_hz,
                        phase_milli_deg=calc.phase_milli_deg,
                    )
                self.sig_meas.emit(meas, calc)
                time.sleep(0.005)
            try:
                client.set_mode(P.MODE_SINGLE)
            except Exception:  # pragma: no cover - 断开时的收尾
                pass
            self._continuous = False
            self.cont_btn.setText("连续测量：关")
            self.sig_log.emit("连续测量已结束")

        self.submit(job)

    def after_clear(self) -> None:
        """CLEAR：复位结果区与标志。"""
        client = self._require_client()
        if client is None:
            return

        def job() -> None:
            try:
                client.clear()
            except Exception as exc:
                self.sig_log.emit(f"CLEAR 失败：{type(exc).__name__}: {exc}")
                return
            self.sig_log.emit("已发送 CLEAR")
            self._job_status()

        self.submit(job)

    def after_reset_counters(self) -> None:
        """RESET_COUNTERS：复位统计计数。"""
        client = self._require_client()
        if client is None:
            return

        def job() -> None:
            try:
                client.reset_counters()
            except Exception as exc:
                self.sig_log.emit(f"复位统计失败：{type(exc).__name__}: {exc}")
                return
            self.sig_log.emit("已发送 RESET_COUNTERS")
            self._job_status()

        self.submit(job)

    def after_set_gate(self) -> None:
        """设置闸门目标：界面按**秒**输入，这里换算成低速周期数（`SET_GATE` 的字段单位）下发。"""
        client = self._require_client()
        if client is None:
            return
        seconds = float(self.gate_spin.value())
        slow = client.slow_hz or M.SLOW_HZ_DEFAULT
        value = gate_seconds_to_cycles(seconds, slow)
        if seconds * slow > GATE_MAX_CYCLES:
            self.log(
                f"闸门时间 {format_seconds(seconds)} 超过 {GATE_MAX_CYCLES} 个低速周期的上界，"
                f"按 {format_seconds(gate_cycles_to_seconds(GATE_MAX_CYCLES, slow))} 下发"
            )

        def job() -> None:
            try:
                got = client.set_gate(value)
            except Exception as exc:
                self.sig_log.emit(f"设置闸门失败：{type(exc).__name__}: {exc}")
                return
            actual = format_seconds(got / slow)
            budget = gate_wait_budget_seconds(got, slow)
            self.sig_log.emit(
                f"闸门时间 {format_seconds(seconds)} → gateTarget = {got} 低速周期"
                f"（实际 {actual} @ {slow / 1e6:g}MHz，等结果预算 ≈ {format_seconds(budget)}）"
            )
            self._job_status()

        self.submit(job)

    def after_set_source(self) -> None:
        """切换输入源（外部引脚 / 片内信号源）。"""
        client = self._require_client()
        if client is None:
            return
        source = P.SOURCE_INTERNAL if self.radio_int.isChecked() else P.SOURCE_EXTERNAL

        def job() -> None:
            try:
                client.set_source(source)
                client.wait_source_ready(timeout=2.0)
            except Exception as exc:
                self.sig_log.emit(f"切换输入源失败：{type(exc).__name__}: {exc}")
                return
            self.sig_log.emit(f"输入源 = {'片内信号源' if source == P.SOURCE_INTERNAL else '外部引脚'}")
            self._job_status()

        self.submit(job)

    def after_set_sig(self) -> None:
        """设置片内源频率与相位差：先本地求解（打印 VCO/MULT/D 与量化提示）再写。

        相位 >180° 是合法请求，但不能超过该频点 `D` 给出的 9 位相位码上界；越界会被 MMCM
        的字段截断（相位回绕）并让协议回 `ERR_PARAM`，所以这里先判掉并说明上界。
        """
        client = self._require_client()
        if client is None:
            return
        try:
            freq_hz = parse_freq_hz(self.freq_edit.text())
        except ValueError:
            self._show_error("参数错误", f"频率写法无法识别：{self.freq_edit.text()!r}（例：25MHz / 25e6 / 25M / 25000000）")
            return
        phase_mdeg = int(round(self.phase_spin.value() * 1000))
        semantics = self.semantics

        def job() -> None:
            sol = S.solve_sig(freq_hz, phase_mdeg, client.slow_hz or S.F_IN_HZ, semantics)
            if not sol.ok:
                self.sig_log.emit(f"求解失败：{sol.describe()}")
                self.sig_error.emit("设置片内源失败", sol.describe())
                return
            if not S.phase_code_fits(phase_mdeg, sol.div):
                # 判据与 PS 固件（`dpm_cmd.c` 的 `dpm_mmcm_phase_code_fits`）和仿真后端同一条：
                # 与相位是否可控无关——越界的请求服务端一律回 ERR_PARAM。
                limit_mdeg = S.phase_max_milli_deg(sol.div)
                message = (
                    f"相位 {phase_mdeg / 1000:.3f}° 超出该频点的 MMCM 上界 {limit_mdeg / 1000:.3f}°"
                    f"（D={sol.div}，9 位相位码 m ≤ {S.PHASE_CODE_MAX}；越界不是吸附而是字段截断回绕）"
                )
                self.sig_log.emit("设置片内源失败：" + message)
                self.sig_error.emit(
                    "设置片内源失败",
                    message + "\n\n请降低相位，或换一个 D 更小的频点（上界 = min(23017499/D, 359.999) 毫度）。",
                )
                return
            self.sig_log.emit("求解：" + sol.describe())
            for note in sol.notes:
                self.sig_log.emit("  " + note)
            try:
                client.wait_idle(timeout=2.0)
                info = client.set_sig(freq_hz, phase_mdeg)
            except Exception as exc:
                self.sig_log.emit(f"设置片内源失败：{type(exc).__name__}: {exc}")
                self.sig_error.emit("设置片内源失败", f"{type(exc).__name__}: {exc}")
                return
            self.sig_log.emit(
                f"片内源已设置：f={info.freq_hz} Hz 相位={info.phase_milli_deg / 1000:.3f}° "
                f"VCO={info.vco_hz / 1e6:g}MHz D={info.div} phaseField(DELAY_TIME)={info.phase_field}"
            )
            self._job_status()

        self.submit(job)

    # ------------------------------------------------------------ 扫描

    @staticmethod
    def _parse_scan_list(text: str, *, freq: bool) -> list[float]:
        """解析逗号/分号分隔的列表，返回**毫度（相位）或 Hz（频点）**。

        频点可以写 `8`（= 8MHz）、`8MHz`、`8e6`、`8000000`（≥100kHz 按 Hz 处理）；
        相位可以写 `45`、`45deg`、`45°`。列表面板上的标签写的是 MHz / °，
        因此纯数字默认按 MHz / 度解释——**不要**把 `8e6` 再乘 1e6（那是 8THz）。
        """
        out: list[float] = []
        for raw in text.replace(";", ",").split(","):
            token = raw.strip()
            if not token:
                continue
            low = token.lower().replace("°", "deg")
            try:
                if freq:
                    if low.endswith("mhz"):
                        value_hz = float(low[: -len("mhz")]) * 1e6
                    elif low.endswith("ghz"):
                        value_hz = float(low[: -len("ghz")]) * 1e9
                    elif low.endswith("khz"):
                        value_hz = float(low[: -len("khz")]) * 1e3
                    elif low.endswith("hz"):
                        value_hz = float(low[: -len("hz")])
                    else:
                        plain = float(low)
                        value_hz = plain if plain >= 1e5 else plain * 1e6  # 纯数字按 MHz
                    out.append(value_hz)
                else:
                    degrees = float(low[: -len("deg")] if low.endswith("deg") else low)
                    out.append(degrees)
            except ValueError as exc:
                raise ValueError(f"无法识别的{'频点' if freq else '相位'}：{raw!r}") from exc
        return out

    def after_scan(self) -> None:
        """多频点 × 多相位扫描（工作线程里逐点设置 + 测量，进度回传界面）。"""
        client = self._require_client()
        if client is None:
            return
        try:
            freqs_mhz = self._parse_scan_list(self.scan_freqs_edit.text(), freq=True)
            phases_deg = self._parse_scan_list(self.scan_phases_edit.text(), freq=False)
        except ValueError as exc:
            self._show_error("参数错误", str(exc))
            return
        if not freqs_mhz or not phases_deg:
            self._show_error("参数错误", "频点与相位列表都不能为空")
            return
        freqs = [int(round(f)) for f in freqs_mhz]  # 已是 Hz
        settle = float(self.scan_settle_spin.value())
        total = len(freqs) * len(phases_deg)
        self.scan_progress.setRange(0, total)
        self.scan_progress.setValue(0)
        self._scan_cancel.clear()

        def job() -> None:
            rows: list[dict] = []
            done = 0
            try:
                client.set_source(P.SOURCE_INTERNAL)
                client.wait_source_ready(timeout=2.0)
            except Exception as exc:
                self.sig_log.emit(f"扫描前切换片内源失败：{type(exc).__name__}: {exc}")
                self.sig_scan_done.emit([], "扫描未开始")
                return
            for freq in freqs:
                # 该频点的相位上界（9 位相位码，逐频点不同）：越界的点直接跳过，
                # 与 DpmClient.scan / CLI 扫描计划一致——它是「该点不可实现」，不是链路故障。
                limit_mdeg, div = freq_phase_limit_milli_deg(freq, client.slow_hz or S.F_IN_HZ, self.semantics)
                if div <= 0:
                    self.sig_log.emit(f"{freq / 1e6:g}MHz 在 MMCM 约束内无解，整列跳过")
                for phase_deg in phases_deg:
                    if self._scan_cancel.is_set():
                        self.sig_scan_done.emit(rows, "扫描已取消")
                        return
                    phase_mdeg = int(round(phase_deg * 1000))
                    if div > 0 and not S.phase_code_fits(phase_mdeg, div):
                        # 判据与 PS 固件、仿真后端同一条：越界请求服务端一律回 ERR_PARAM（与相位是否可控无关）
                        limit_deg = limit_mdeg / 1000.0
                        self.sig_log.emit(f"跳过 {freq / 1e6:g}MHz/{phase_deg:g}°：超出该频点相位上界 {limit_deg:.3f}°（D={div}）")
                        done += 1
                        self.sig_scan_progress.emit(done, total)
                        continue
                    try:
                        info = client.set_sig(freq, phase_mdeg)
                        if settle:
                            time.sleep(settle)
                        _, meas, calc = client.measure_once(timeout=5.0)
                    except Exception as exc:
                        self.sig_log.emit(f"{freq / 1e6:g}MHz/{phase_deg:g}° 失败：{type(exc).__name__}: {exc}")
                        rows.append(
                            {
                                "freq_nominal": freq,
                                "phase_nominal": phase_deg,
                                "freq_meas_mHz": None,
                                "phase_meas_mdeg": None,
                                "err_deg": None,
                                "gateCnt": None,
                                "edgeCnt": None,
                                "phaseCnt": None,
                                "phase_controllable": self.semantics.phase_controllable,
                            }
                        )
                        done += 1
                        self.sig_scan_progress.emit(done, total)
                        continue
                    step_mdeg = M.phase_quant_milli_deg(calc.freq_hz, client.dsp_hz) or 0
                    exp_mdeg = info.phase_milli_deg
                    err_deg = ((calc.phase_milli_deg - exp_mdeg + 180_000) % 360_000 - 180_000) / 1000.0
                    rows.append(
                        {
                            "freq_nominal": freq,
                            "phase_nominal": phase_deg,
                            "freq_meas_mHz": calc.freq_hz,
                            "phase_meas_mdeg": calc.phase_milli_deg,
                            "expected_mdeg": exp_mdeg,
                            "quantum_mdeg": step_mdeg,
                            "err_deg": err_deg,
                            "err_over_quantum": (abs(err_deg) * 1000.0 / step_mdeg) if step_mdeg else None,
                            "gateCnt": meas.gate_cnt,
                            "edgeCnt": meas.edge_cnt,
                            "phaseCnt": meas.phase_cnt,
                            "phaseCycles": M.phase_cycles_float(meas.phase_cnt, meas.edge_cnt),
                            "phase_controllable": self.semantics.phase_controllable,
                        }
                    )
                    self.sig_meas.emit(meas, calc)
                    done += 1
                    self.sig_scan_progress.emit(done, total)
            self.sig_scan_done.emit(rows, f"扫描完成：{done}/{total} 点")

        self.submit(job)

    @Slot(int, int)
    def _on_scan_progress(self, done: int, total: int) -> None:
        self.scan_progress.setRange(0, max(1, total))
        self.scan_progress.setValue(done)

    @Slot(list, str)
    def _on_scan_done(self, rows: list[dict], message: str) -> None:
        self.last_rows = list(rows)
        self.log(f"{message}（本次 {len(rows)} 行；可导出 CSV）")
        errors = [abs(r["err_deg"]) for r in rows if r.get("err_deg") is not None]
        if errors:
            self.log(f"扫描相位误差：最大 {max(errors):.3f}°，平均 {sum(errors) / len(errors):.3f}°")

    # ------------------------------------------------------------ 导出 / 清空

    def after_export(self) -> None:
        """把最近一次扫描结果导出成 CSV。"""
        if not self.last_rows:
            self._show_error("没有数据", "还没有扫描结果；先做一次扫描")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出扫描 CSV", "dpm_scan.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            scan_rows_to_csv(self.last_rows, path)
        except Exception as exc:
            self._show_error("导出失败", f"{type(exc).__name__}: {exc}")
            return
        self.log(f"CSV 已写入 {path}（{len(self.last_rows)} 行）")

    def after_clear_table(self) -> None:
        """清空结果表与导出的行缓存。"""
        self.table.setRowCount(0)
        self.last_rows = []
        self.log("结果表已清空")

    # ------------------------------------------------------------ 生命周期

    def closeEvent(self, event: object) -> None:  # noqa: N802 - Qt 命名约定
        self._continuous = False
        self._scan_cancel.set()
        client, self.client = self.client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - 关闭失败不阻塞退出
                pass
        self.worker.stop()
        self.worker.wait(2000)
        super().closeEvent(event)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python3 -m dpm_host gui", description="DPM PySide6 图形界面")
    ap.add_argument("--port", default=None, help="串口或 pty 路径（给出则启动后自动连接）")
    ap.add_argument("--baud", type=int, default=P.DEFAULT_BAUD, help="波特率")
    ap.add_argument("--timeout", type=float, default=0.5, help="单次请求超时（秒）")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    """GUI 入口（`uv run python -m dpm_host gui [--port ...]`）。"""
    args = build_arg_parser().parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = DpmGuiApp(port=args.port, baud=args.baud, timeout=args.timeout)
    window.show()
    try:
        return int(app.exec())
    except KeyboardInterrupt:  # pragma: no cover - 终端里 Ctrl-C
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
