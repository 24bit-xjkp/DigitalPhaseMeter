"""Tk 图形界面：`python3 -m dpm_host gui --port <串口|pty>`。

界面元素（对应任务书要求）：

- 连接/断开、波特率与连接状态；
- 状态灯：busy / locked（MMCM 锁定）/ lost（结果丢失）；
- 频率显示与**相位差 0~360°**（数字 + 仪表盘弧 + 0~360° 条形）；
- 单次测量 / 连续测量按钮，CLEAR 与统计复位；
- 闸门目标设置；
- 输入源单选（外部 / 片内）；
- 片内源频率与相位输入、设置按钮与扫描按钮（频点 × 相位列表 → 结果表 → CSV 导出）；
- 结果表（最近 N 条：序号 / 频率 / 相位 / gate / edge / phaseCnt）与 CSV 导出；
- 日志区。

线程模型：串口操作全部在后台工作线程里执行（`DpmClient` 自身线程安全），结果通过队列回到
Tk 主线程刷新界面，因此界面不会因为超时而卡死。相位字段是否可控由
`spec/drp_semantics.json` 的 `phase_controllable` 决定（缺省 True，实测前按「待确认」提示）。
"""

from __future__ import annotations

import argparse
import math
import queue
import sys
import threading
import time
from typing import Callable, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import mathx as M
from . import protocol as P
from . import sig as S
from .client import DpmClient, scan_rows_to_csv

__all__ = ["DpmGuiApp", "main"]

#: 结果表最多保留的行数。
MAX_ROWS = 200
#: 状态灯颜色。
COLOR_ON = "#2ecc71"
COLOR_OFF = "#555555"
COLOR_WARN = "#e67e22"
COLOR_BAD = "#e74c3c"
COLOR_BG = "#1e1e1e"
COLOR_FG = "#eeeeee"
COLOR_PANEL = "#2b2b2b"
COLOR_GAUGE = "#3498db"


class DpmGuiApp:
    """主窗口（组合而不是继承 `tk.Tk`，便于测试时复用组件）。"""

    def __init__(self, root: tk.Tk, *, port: str | None = None, baud: int = P.DEFAULT_BAUD, timeout: float = 0.5) -> None:
        self.root = root
        self.root.title("DigitalPhaseMeter 上位机")
        self.root.geometry("1020x760")
        self.root.configure(bg=COLOR_BG)

        self.client: DpmClient | None = None
        self.jobs: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self.events: queue.Queue[Callable[[], None]] = queue.Queue()
        self.worker = threading.Thread(target=self._worker_loop, name="dpm-gui-worker", daemon=True)
        self.worker.start()

        self.continuous = False
        self.phase_milli_deg = 0
        self.semantics = S.current_semantics()
        self.last_rows: list[dict] = []

        self._build_widgets()
        self._set_connected(False)
        if port:
            self.port_var.set(port)
            self.after_connect()
        self.root.after(50, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------ 界面搭建

    def _build_widgets(self) -> None:
        pad = {"padx": 6, "pady": 3}
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:  # pragma: no cover
            pass
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_FG)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("TLabelframe", background=COLOR_BG, foreground=COLOR_FG)
        style.configure("TLabelframe.Label", background=COLOR_BG, foreground="#9ad0f5")
        style.configure("TCheckbutton", background=COLOR_BG, foreground=COLOR_FG)
        style.configure("TRadiobutton", background=COLOR_BG, foreground=COLOR_FG)
        style.configure("TButton", padding=4)
        style.configure("Treeview", background="#252525", fieldbackground="#252525", foreground=COLOR_FG)
        style.configure("Treeview.Heading", background="#333333", foreground=COLOR_FG)

        # ---- 连接栏 ----
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="串口:").pack(side="left")
        self.port_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.port_var, width=22).pack(side="left", padx=4)
        ttk.Label(top, text="波特率:").pack(side="left")
        self.baud_var = tk.StringVar(value=str(P.DEFAULT_BAUD))
        ttk.Entry(top, textvariable=self.baud_var, width=8).pack(side="left", padx=4)
        ttk.Label(top, text="超时(s):").pack(side="left")
        self.timeout_var = tk.StringVar(value="0.5")
        ttk.Entry(top, textvariable=self.timeout_var, width=5).pack(side="left", padx=4)
        self.connect_btn = ttk.Button(top, text="连接", command=self.after_connect)
        self.connect_btn.pack(side="left", padx=4)
        self.disconnect_btn = ttk.Button(top, text="断开", command=self.after_disconnect)
        self.disconnect_btn.pack(side="left", padx=4)
        self.conn_label = ttk.Label(top, text="未连接")
        self.conn_label.pack(side="left", padx=8)

        # ---- 状态灯 ----
        lights = ttk.LabelFrame(self.root, text="状态")
        lights.pack(fill="x", **pad)
        self.light_canvases: dict[str, tk.Canvas] = {}
        for key, text in (("busy", "busy"), ("locked", "MMCM 锁定"), ("lost", "结果丢失"), ("ready", "sourceReady")):
            box = ttk.Frame(lights)
            box.pack(side="left", padx=10, pady=4)
            canvas = tk.Canvas(box, width=18, height=18, bg=COLOR_BG, highlightthickness=0)
            canvas.create_oval(2, 2, 16, 16, fill=COLOR_OFF, outline="#888888", tags="lamp")
            canvas.pack(side="left")
            ttk.Label(box, text=text).pack(side="left", padx=3)
            self.light_canvases[key] = canvas
        self.status_label = ttk.Label(lights, text="status=-  sigStatus=-  gateTarget=-  lostCount=-  seq=-")
        self.status_label.pack(side="left", padx=12)

        # ---- 频率 / 相位显示 ----
        disp = ttk.Frame(self.root)
        disp.pack(fill="x", **pad)
        freq_box = ttk.LabelFrame(disp, text="频率")
        freq_box.pack(side="left", fill="both", expand=True, padx=4)
        self.freq_label = ttk.Label(freq_box, text="--", font=("DejaVu Sans Mono", 22, "bold"))
        self.freq_label.pack(padx=12, pady=6)
        self.freq_raw_label = ttk.Label(freq_box, text="freqMilliHz = -")
        self.freq_raw_label.pack(padx=12, pady=(0, 6))

        phase_box = ttk.LabelFrame(disp, text="相位差（0~360°）")
        phase_box.pack(side="left", fill="both", expand=True, padx=4)
        self.phase_label = ttk.Label(phase_box, text="--", font=("DejaVu Sans Mono", 22, "bold"))
        self.phase_label.pack(padx=12, pady=2)
        self.phase_bar = tk.Canvas(phase_box, width=380, height=26, bg=COLOR_PANEL, highlightthickness=0)
        self.phase_bar.pack(padx=12, pady=2)
        self.gauge = tk.Canvas(phase_box, width=150, height=110, bg=COLOR_PANEL, highlightthickness=0)
        self.gauge.pack(padx=12, pady=2)
        self._draw_phase(0)
        self.phase_extra = ttk.Label(phase_box, text="phaseCnt=-  edgeCnt=-  phaseCnt/edgeCnt=-")
        self.phase_extra.pack(padx=12, pady=(0, 4))

        # ---- 操作按钮 ----
        ops = ttk.LabelFrame(self.root, text="测量控制")
        ops.pack(fill="x", **pad)
        self.single_btn = ttk.Button(ops, text="单次测量", command=self.after_single)
        self.single_btn.pack(side="left", padx=4, pady=4)
        self.cont_btn = ttk.Button(ops, text="连续测量：开", command=self.after_toggle_continuous)
        self.cont_btn.pack(side="left", padx=4, pady=4)
        self.clear_btn = ttk.Button(ops, text="CLEAR", command=lambda: self.submit(self._do_clear))
        self.clear_btn.pack(side="left", padx=4, pady=4)
        self.reset_btn = ttk.Button(ops, text="复位统计", command=lambda: self.submit(self._do_reset))
        self.reset_btn.pack(side="left", padx=4, pady=4)
        ttk.Label(ops, text="闸门目标:").pack(side="left", padx=(16, 2))
        self.gate_var = tk.StringVar(value=str(P.DEFAULT_GATE_TARGET))
        ttk.Entry(ops, textvariable=self.gate_var, width=8).pack(side="left")
        ttk.Button(ops, text="应用闸门", command=lambda: self.submit(self._do_set_gate)).pack(side="left", padx=4)

        # ---- 输入源 ----
        src = ttk.LabelFrame(self.root, text="输入源")
        src.pack(fill="x", **pad)
        self.source_var = tk.StringVar(value="int")
        ttk.Radiobutton(src, text="外部引脚 (sig1/sig2)", value="ext", variable=self.source_var,
                        command=self.after_set_source).pack(side="left", padx=6)
        ttk.Radiobutton(src, text="片内信号源 (MMCM)", value="int", variable=self.source_var,
                        command=self.after_set_source).pack(side="left", padx=6)

        # ---- 片内源设置 ----
        src2 = ttk.LabelFrame(self.root, text="片内信号源（频率 / 相位差）")
        src2.pack(fill="x", **pad)
        ttk.Label(src2, text="频率:").pack(side="left", padx=(6, 2))
        self.sig_freq_var = tk.StringVar(value="25e6")
        ttk.Entry(src2, textvariable=self.sig_freq_var, width=12).pack(side="left")
        ttk.Label(src2, text="相位差(度):").pack(side="left", padx=(10, 2))
        self.sig_phase_var = tk.StringVar(value="90")
        self.sig_phase_entry = ttk.Entry(src2, textvariable=self.sig_phase_var, width=8)
        self.sig_phase_entry.pack(side="left")
        self.sig_set_btn = ttk.Button(src2, text="设置", command=lambda: self.submit(self._do_set_sig))
        self.sig_set_btn.pack(side="left", padx=6)
        self.scan_freqs_var = tk.StringVar(value="5e6,6.25e6,8e6,10e6,12.5e6,20e6,25e6")
        self.scan_phases_var = tk.StringVar(value="0,22.5,45,67.5,90,112.5,135,157.5")
        ttk.Label(src2, text="扫描频点:").pack(side="left", padx=(14, 2))
        ttk.Entry(src2, textvariable=self.scan_freqs_var, width=30).pack(side="left")
        ttk.Label(src2, text="相位:").pack(side="left", padx=(6, 2))
        ttk.Entry(src2, textvariable=self.scan_phases_var, width=26).pack(side="left")
        self.scan_btn = ttk.Button(src2, text="扫描", command=self.after_scan)
        self.scan_btn.pack(side="left", padx=6)
        if self.semantics.phase_controllable:
            notice = f"相位字语义：{self.semantics.describe()}"
            color = "#f1c40f"
        else:
            notice = (
                "⚠ 当前 bitstream 相位固定 "
                f"{self.semantics.phase_static_milli_deg / 1000:.3f}°（DRP 相位字未接入）；"
                "要在线扫描相位需重建 IP/BD（docs/ps-protocol.md 6.2）"
            )
            color = COLOR_BAD
        self.semantics_label = ttk.Label(self.root, text=notice, foreground=color)
        self.semantics_label.pack(fill="x", padx=10)

        # ---- 结果表 ----
        table_box = ttk.LabelFrame(self.root, text="结果表（最近 {} 条）".format(MAX_ROWS))
        table_box.pack(fill="both", expand=True, **pad)
        columns = ("seq", "freq", "phase", "gate", "edge", "phaseCnt", "cycles")
        headings = {
            "seq": "序号",
            "freq": "频率",
            "phase": "相位差",
            "gate": "gate",
            "edge": "edge",
            "phaseCnt": "phaseCnt",
            "cycles": "DSP周期",
        }
        widths = {"seq": 60, "freq": 130, "phase": 100, "gate": 70, "edge": 80, "phaseCnt": 100, "cycles": 90}
        self.tree = ttk.Treeview(table_box, columns=columns, show="headings", height=7)
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="center")
        self.tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        scroll = ttk.Scrollbar(table_box, orient="vertical", command=self.tree.yview)
        scroll.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        btns = ttk.Frame(table_box)
        btns.pack(side="left", fill="y", padx=4)
        ttk.Button(btns, text="导出 CSV", command=self.after_export_csv).pack(fill="x", pady=2)
        ttk.Button(btns, text="清空表", command=self.after_clear_table).pack(fill="x", pady=2)
        ttk.Button(btns, text="读一次状态", command=lambda: self.submit(self._do_status)).pack(fill="x", pady=2)

        # ---- 日志 ----
        log_box = ttk.LabelFrame(self.root, text="日志")
        log_box.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_box, height=7, bg="#181818", fg="#d0d0d0", insertbackground="#d0d0d0")
        self.log_text.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        log_scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="left", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self._apply_phase_controllable()

    # ------------------------------------------------------------ 线程与队列

    def _worker_loop(self) -> None:  # pragma: no cover - 线程体
        while True:
            job = self.jobs.get()
            if job is None:
                break
            try:
                job()
            except Exception as exc:  # 任何异常都要回到日志区，不能让工作线程死掉
                self.post(lambda e=exc: self.log(f"操作失败：{type(e).__name__}: {e}"))

    def submit(self, job: Callable[[], None]) -> None:
        """把任务丢给工作线程（串口操作都在这里执行）。"""
        self.jobs.put(job)

    def post(self, callback: Callable[[], None]) -> None:
        """把界面更新排到 Tk 主线程。"""
        self.events.put(callback)

    def _drain_events(self) -> None:
        while True:
            try:
                callback = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as exc:  # pragma: no cover
                print(f"[gui] 界面更新失败：{exc!r}", file=sys.stderr)
        self.root.after(50, self._drain_events)

    def log(self, message: str) -> None:
        """往日志区追加一行（带时间戳）。"""
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")

    # ------------------------------------------------------------ 连接

    def after_connect(self) -> None:
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("缺少串口", "请先填写串口或 pty 路径")
            return
        try:
            baud = int(self.baud_var.get())
            timeout = float(self.timeout_var.get())
        except ValueError:
            messagebox.showerror("参数错误", "波特率/超时必须是数字")
            return
        self.submit(lambda: self._do_connect(port, baud, timeout))

    def _do_connect(self, port: str, baud: int, timeout: float) -> None:
        client = DpmClient(port=port, baud=baud, timeout=timeout)
        try:
            client.ping()
        except Exception as exc:
            client.close()
            self.post(lambda e=exc: (self.log(f"连接失败：{type(e).__name__}: {e}"),
                                     messagebox.showerror("连接失败", f"{type(e).__name__}: {e}")))
            return
        self.client = client
        info = client.identify()
        self.post(lambda: self._after_connected(port, info))

    def _after_connected(self, port: str, info: P.Identify) -> None:
        self.conn_label.configure(text=f"已连接 {port}", foreground=COLOR_ON)
        self._set_connected(True)
        self.log(f"已连接 {port}：ver=0x{info.ver:08X}（{info.ver_text}）"
                 f" dsp={info.dsp_clk_hz / 1e6:g}MHz slow={info.slow_clk_hz / 1e6:g}MHz flags=0x{info.flags:04X}")
        self.submit(self._do_status)

    def after_disconnect(self) -> None:
        self.continuous = False
        self.post(lambda: self.cont_btn.configure(text="连续测量：开"))
        client, self.client = self.client, None
        if client is not None:
            client.close()
        self.post(lambda: self._after_disconnected())

    def _after_disconnected(self) -> None:
        self.conn_label.configure(text="未连接", foreground=COLOR_FG)
        self._set_connected(False)
        self.log("已断开")

    def _set_connected(self, connected: bool) -> None:
        state = "normal" if connected else "disabled"
        for widget in (self.single_btn, self.cont_btn, self.clear_btn, self.reset_btn, self.scan_btn, self.sig_set_btn):
            widget.configure(state=state)
        if not connected:
            for canvas in self.light_canvases.values():
                canvas.itemconfigure("lamp", fill=COLOR_OFF)
            self.status_label.configure(text="status=-  sigStatus=-  gateTarget=-  lostCount=-  seq=-")
            self.freq_label.configure(text="--")
            self.phase_label.configure(text="--")
            self._draw_phase(0)

    def on_close(self) -> None:
        self.continuous = False
        if self.client is not None:
            self.client.close()
            self.client = None
        self.jobs.put(None)
        self.root.destroy()

    # ------------------------------------------------------------ 各操作

    def _require_client(self) -> DpmClient | None:
        if self.client is None:
            self.post(lambda: self.log("尚未连接"))
            return None
        return self.client

    def _do_status(self) -> None:
        client = self._require_client()
        if client is None:
            return
        st = client.get_status()
        self.post(lambda: self._show_status(st))
        if st.result_valid:
            self._do_read_meas(client)

    def _show_status(self, st: P.Status) -> None:
        self._lamp("busy", st.busy, COLOR_WARN)
        self._lamp("locked", st.src_locked, COLOR_ON)
        self._lamp("lost", st.lost, COLOR_BAD)
        self._lamp("ready", st.source_ready, COLOR_ON)
        self.status_label.configure(
            text=(
                f"status=0x{st.status:08X} [{st.status_text}]  "
                f"sigStatus=0x{st.sig_status:08X} [{st.sig_status_text}]  "
                f"gateTarget={st.gate_target}  lostCount={st.lost_count}  seq={st.seq}"
            )
        )
        self.source_var.set("int" if st.sig_select else "ext")
        if st.gate_target and not self.gate_var.get().strip():
            self.gate_var.set(str(st.gate_target))

    def _lamp(self, key: str, on: bool, color: str) -> None:
        self.light_canvases[key].itemconfigure("lamp", fill=color if on else COLOR_OFF)

    def _do_read_meas(self, client: DpmClient) -> None:
        meas = client.try_get_meas()
        if meas is None:
            return
        self.post(lambda: self._show_meas(meas))

    def _show_meas(self, meas: P.Meas) -> None:
        client = self.client
        slow = client.slow_hz if client else M.SLOW_HZ_DEFAULT
        dsp = client.dsp_hz if client else M.DSP_HZ_DEFAULT
        calc = M.meas_calc(meas.gate_cnt, meas.edge_cnt, meas.phase_cnt, slow, dsp)
        # 频率/相位一律用计数本地换算：wire 的 freqMilliHz 是 u32，>4.295MHz 会溢出
        self.freq_label.configure(text=M.format_freq_hz(calc.freq_hz))
        self.freq_raw_label.configure(
            text=f"freqHz = {calc.freq_hz}  （周期 {M.signal_period_100ps(calc.freq_hz) / 1e4:.6g} µs）"
        )
        self.phase_milli_deg = M.normalize_phase_milli_deg(calc.phase_milli_deg)
        self.phase_label.configure(text=M.format_milli_deg(self.phase_milli_deg))
        self._draw_phase(self.phase_milli_deg / 1000.0)
        cycles = M.phase_cycles_float(meas.phase_cnt, meas.edge_cnt)
        step = M.phase_quant_milli_deg(calc.freq_hz, dsp)
        self.phase_extra.configure(
            text=(
                f"phaseCnt={meas.phase_cnt}  edgeCnt={meas.edge_cnt}  "
                f"phaseCnt/edgeCnt={cycles:.6f} DSP周期（量化步长 {step / 1000:.3f}°）"
            )
        )
        self._add_row(meas, cycles)

    def _add_row(self, meas: P.Meas, cycles: float) -> None:
        values = (
            meas.seq,
            M.format_freq_hz(meas.freq_milli_hz),
            M.format_milli_deg(meas.phase_milli_deg),
            meas.gate_cnt,
            meas.edge_cnt,
            meas.phase_cnt,
            f"{cycles:.4f}",
        )
        self.tree.insert("", 0, values=values)
        children = self.tree.get_children()
        for item in children[MAX_ROWS:]:
            self.tree.delete(item)

    def _draw_phase(self, phase_deg: float) -> None:
        """画 0~360° 条形与仪表盘弧。"""
        bar = self.phase_bar
        bar.delete("all")
        width = int(bar.winfo_reqwidth())
        height = int(bar.winfo_reqheight())
        bar.create_rectangle(0, 0, width, height, fill=COLOR_PANEL, outline="")
        filled = max(0.0, min(1.0, phase_deg / 360.0)) * (width - 2)
        bar.create_rectangle(1, 1, 1 + filled, height - 1, fill=COLOR_GAUGE, outline="")
        for tick in range(0, 361, 90):
            x = 1 + (tick / 360.0) * (width - 2)
            bar.create_line(x, 0, x, 6, fill="#aaaaaa")
            bar.create_text(x, height - 7, text=str(tick), fill="#cccccc", font=("DejaVu Sans", 7))

        gauge = self.gauge
        gauge.delete("all")
        cx, cy, r = 75, 60, 45
        gauge.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#666666", width=2)
        # 弧：0° 在正上方，顺时针增长
        gauge.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-max(0.01, min(359.99, phase_deg)),
                         style="arc", outline=COLOR_GAUGE, width=6)
        angle = math.radians(90 - phase_deg)
        needle_x = cx + (r - 8) * math.cos(angle)
        needle_y = cy - (r - 8) * math.sin(angle)
        gauge.create_line(cx, cy, needle_x, needle_y, fill="#f39c12", width=2)
        gauge.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill="#f39c12", outline="")
        gauge.create_text(cx, cy + r + 12, text=f"{phase_deg:7.3f}°", fill=COLOR_FG, font=("DejaVu Sans Mono", 9))

    def after_single(self) -> None:
        self.submit(self._do_single)

    def _do_single(self) -> None:
        client = self._require_client()
        if client is None:
            return
        client.set_mode(P.MODE_SINGLE)
        client.start()
        self.post(lambda: self.log("已启动单次测量，等待结果……"))
        try:
            client.wait_result(timeout=2.0)
        except Exception as exc:
            self.post(lambda e=exc: self.log(f"等待结果失败：{e}（源没有边沿时测量永不完成）"))
            return
        self._do_status()

    def after_toggle_continuous(self) -> None:
        self.continuous = not self.continuous
        self.cont_btn.configure(text="连续测量：关" if self.continuous else "连续测量：开")
        if self.continuous:
            self.submit(self._continuous_loop)
        else:
            self.log("连续测量已停止")

    def _continuous_loop(self) -> None:
        """连续采样：每次读走后 PS 会自动重启（continuous 模式）。"""
        client = self._require_client()
        if client is None:
            return
        client.set_mode(P.MODE_CONTINUOUS)
        client.start()
        self.post(lambda: self.log("进入连续测量"))
        while self.continuous and self.client is client:
            try:
                client.wait_result(timeout=1.0)
            except Exception as exc:
                self.post(lambda e=exc: self.log(f"连续测量停止：{e}"))
                break
            self._do_read_meas(client)
            self._do_status()
        try:
            client.set_mode(P.MODE_SINGLE)
        except Exception:  # pragma: no cover - 断开竞态
            pass
        self.post(lambda: self.log("连续测量已退出"))

    def _do_clear(self) -> None:
        client = self._require_client()
        if client is None:
            return
        client.clear()
        self.post(lambda: self.log("已发送 CLEAR"))
        self._do_status()

    def _do_reset(self) -> None:
        client = self._require_client()
        if client is None:
            return
        client.reset_counters()
        self.post(lambda: self.log("已发送 RESET_COUNTERS"))
        self._do_status()

    def _do_set_gate(self) -> None:
        client = self._require_client()
        if client is None:
            return
        try:
            value = int(self.gate_var.get().strip())
        except ValueError:
            self.post(lambda: self.log("闸门目标必须是整数"))
            return
        got = client.set_gate(value)
        self.post(lambda: self.log(f"gateTarget = {got}（{got / client.slow_hz * 1e6:.1f} µs）"))

    def after_set_source(self) -> None:
        self.submit(self._do_set_source)

    def _do_set_source(self) -> None:
        client = self._require_client()
        if client is None:
            return
        source = P.SOURCE_INTERNAL if self.source_var.get() == "int" else P.SOURCE_EXTERNAL
        client.set_source(source)
        client.wait_source_ready(timeout=2.0)
        self.post(lambda: self.log(f"输入源切换为 {P.SOURCE_NAMES[source]}，sourceReady 已就绪"))
        self._do_status()

    def _do_set_sig(self) -> None:
        client = self._require_client()
        if client is None:
            return
        from .cli import parse_deg, parse_freq

        try:
            freq_hz = parse_freq(self.sig_freq_var.get())
            phase_mdeg = int(round(parse_deg(self.sig_phase_var.get()) * 1000))
        except ValueError:
            self.post(lambda: self.log("频率/相位格式不对（频率支持 25e6 或 25MHz）"))
            return
        sol = S.solve_sig(freq_hz, phase_mdeg, client.slow_hz, self.semantics)
        self.post(lambda: self.log(f"求解：{sol.describe()}"))
        for note in sol.notes:
            self.post(lambda n=note: self.log(f"提示：{n}"))
        if not sol.ok:
            self.post(lambda: self.log("该频率在 MMCM 约束内无解，未下发"))
            return
        if not self.semantics.phase_controllable:
            self.post(lambda: self.log(
                f"⚠ 当前 bitstream 相位固定 {self.semantics.phase_static_milli_deg / 1000:.3f}°："
                "相位字照写但不会生效（docs/ps-protocol.md 6.2）"
            ))
        client.wait_idle(timeout=2.0)
        info = client.set_sig(freq_hz, phase_mdeg)
        honored = abs(info.phase_milli_deg - phase_mdeg) <= 1000
        self.post(lambda: self.log(
            f"SET_SIG → f={info.freq_hz / 1e6:g}MHz 相位={info.phase_deg:g}° "
            f"VCO={info.vco_hz / 1e6:g}MHz D={info.div}"
            + ("" if honored else "（⚠ 设备未按请求改变相位）")
        ))
        self._do_status()

    def after_scan(self) -> None:
        if self.client is None:
            self.log("尚未连接")
            return
        self.scan_btn.configure(state="disabled")
        self.submit(self._do_scan)

    def _do_scan(self) -> None:
        from .cli import parse_deg, parse_freq, parse_list

        client = self._require_client()
        if client is None:
            return
        try:
            freqs = parse_list(self.scan_freqs_var.get(), parse_freq)
            phases = parse_list(self.scan_phases_var.get(), parse_deg)
        except ValueError as exc:
            self.post(lambda e=exc: self.log(f"扫描参数错误：{e}"))
            self.post(lambda: self.scan_btn.configure(state="normal"))
            return
        self.post(lambda: self.log(f"开始扫描：{len(freqs)} 频点 × {len(phases)} 相位"))
        rows: list[dict] = []

        def on_row(row: dict) -> None:
            rows.append(row)
            self.post(lambda r=row: self.log(
                f"  {r['freq_nominal'] / 1e6:g}MHz {r['phase_nominal']:g}° → "
                f"{M.format_freq_milli_hz(r['f_meas_mHz'])} {M.format_milli_deg(r['phase_meas_mdeg'])} "
                f"err={r['err_deg']:+.4f}°"
            ))

        try:
            client.scan(freqs, phases, gate_target=None, source=P.SOURCE_INTERNAL,
                        result_timeout=2.0, on_row=on_row)
        except Exception as exc:
            self.post(lambda e=exc: self.log(f"扫描中断：{type(e).__name__}: {e}"))
        self.last_rows = rows
        self.post(lambda: self.scan_btn.configure(state="normal"))
        self.post(lambda: self.log(f"扫描结束：{len(rows)} 行（可用「导出 CSV」保存）"))
        self._do_status()

    def after_export_csv(self) -> None:
        rows = self.last_rows or self._rows_from_table()
        if not rows:
            messagebox.showinfo("没有数据", "结果表为空，先测几条或跑一次扫描")
            return
        path = filedialog.asksaveasfilename(
            title="导出 CSV", defaultextension=".csv", filetypes=[("CSV", "*.csv"), ("全部文件", "*.*")]
        )
        if not path:
            return
        try:
            scan_rows_to_csv(rows, path)
            self.log(f"已导出 {len(rows)} 行到 {path}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))

    def _rows_from_table(self) -> list[dict]:
        rows: list[dict] = []
        for item in reversed(self.tree.get_children()):
            values = self.tree.item(item, "values")
            rows.append(
                {
                    "freq_nominal": "",
                    "phase_nominal": "",
                    "f_meas_mHz": values[1],
                    "phase_meas_mdeg": values[2],
                    "gateCnt": values[3],
                    "edgeCnt": values[4],
                    "phaseCnt": values[5],
                    "phaseCycles": values[6],
                    "err_deg": "",
                }
            )
        return rows

    def after_clear_table(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.last_rows = []
        self.log("结果表已清空")

    def _apply_phase_controllable(self) -> None:
        """相位字段不可控时把相位输入置灰并提示（Lead 第 6 条）。"""
        if self.semantics.phase_controllable:
            return
        try:
            self.sig_phase_entry.configure(state="disabled")
        except tk.TclError:  # pragma: no cover
            return
        self.semantics_label.configure(
            text=(
                f"⚠ 当前 bitstream 相位固定 {self.semantics.phase_static_milli_deg / 1000:.3f}°"
                "（DRP 相位字未接入），相位输入已置灰；要在线扫描相位需重建 IP/BD"
                f"（依据 {self.semantics.source}）"
            ),
            foreground=COLOR_BAD,
        )


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python3 -m dpm_host gui", description="DPM Tk 图形界面")
    ap.add_argument("--port", default=None, help="串口或 pty 路径（给出则启动后自动连接）")
    ap.add_argument("--baud", type=int, default=P.DEFAULT_BAUD, help="波特率")
    ap.add_argument("--timeout", type=float, default=0.5, help="单次请求超时（秒）")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    """GUI 入口（`python3 -m dpm_host gui [--port ...]`）。"""
    args = build_arg_parser().parse_args(argv)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法启动 Tk（检查 DISPLAY）：{exc}", file=sys.stderr)
        return 1
    app = DpmGuiApp(root, port=args.port, baud=args.baud, timeout=args.timeout)
    try:
        root.mainloop()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        if app.client is not None:
            app.client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
