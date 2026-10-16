#!/usr/bin/env python3
"""GUI 冒烟 + 截图：把 PySide6 界面接到一条 pty 上的仿真服务端，跑一遍主要操作并截图。

用法（在仓库根目录）：

    uv run python host/tools/gui_smoke.py                        # 自动起 sim_server，截图到 build/gui_smoke/
    uv run python host/tools/gui_smoke.py --port /dev/ttyUSB0    # 接真实板子（或已有的 pty）
    QT_QPA_PLATFORM=offscreen uv run python host/tools/gui_smoke.py --outdir /tmp/gui

产出：

* `gui.png`：主窗口截图（`QWidget.grab()`，不依赖截图工具与窗口系统）；
* `gui_stdout.txt`：每一步的实测状态（连接信息、频率/相位读数、结果表行数、扫描结果）。

退出码 0 = 全部步骤通过。**这不是单元测试**（单元测试在 `host/tests/`），而是「界面真的能用」
的可复现证据：它把 CLI 之外的那条 GUI 通路完整跑一遍，其中两项按硬件条件换算的行为是本次新增的：

* **相位上界逐频点刷新**：25MHz（D=40）放开到 359.999° 并真的设到 225°（>180°）测出来；
  8MHz（D=125）收窄到 184.139°；
* **闸门时间按秒设置**：界面输入 40µs，状态里读回 `gateTarget=2000` 个低速周期。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parents[1]
REPO = HOST_DIR.parent
sys.path.insert(0, str(HOST_DIR))

from dpm_host import gui as G  # noqa: E402
from dpm_host import protocol as P  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def start_sim_server(link: Path) -> subprocess.Popen[bytes]:
    """起一个 pty 仿真服务端（与 `python -m dpm_host.sim_server --pty` 相同）。

    带上 `--ext-disable`（外部输入无信号）：后面「切到外部输入源再测量」那一步要复现
    「源没有边沿 → 测量永不完成」的正常输入状态，界面上必须报超时 + 输入源诊断，
    而不是报设备错误（真机上曾报 `ERR_BUSY` / `GET_RESULT`）。
    """
    link.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "dpm_host.sim_server", "--pty", "--pty-link", str(link), "--freq", "25e6", "--phase", "90", "--ext-disable"],
        cwd=str(HOST_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        if link.exists():
            return proc
        time.sleep(0.05)
    proc.terminate()
    raise RuntimeError("仿真服务端没有建出 pty 软链接")


class Smoke:
    """一个极简的步骤驱动器：把 GUI 的操作排成队，用 QTimer 逐拍推进。"""

    def __init__(self, app: QApplication, window: G.DpmGuiApp, out) -> None:
        self.app = app
        self.win = window
        self.out = out
        self.steps: list[tuple[str, float, object]] = []
        self.failures: list[str] = []

    def add(self, name: str, delay: float, action: object) -> None:
        self.steps.append((name, delay, action))

    def log(self, message: str) -> None:
        print(message)
        self.out.write(message + "\n")
        self.out.flush()

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.log(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)

    def run(self) -> int:
        queue = list(self.steps)

        def step() -> None:
            if not queue:
                self.win.close()
                self.app.quit()
                return
            name, delay, action = queue.pop(0)

            def do_action() -> None:
                self.app.processEvents()
                try:
                    action()
                except Exception as exc:  # 步骤异常要显式失败，不要静默
                    self.check(name, False, f"异常 {type(exc).__name__}: {exc}")
                QTimer.singleShot(50, step)

            # 先等 `delay` 再执行动作：连接、测量、扫描都需要时间落地
            QTimer.singleShot(int(delay * 1000), do_action)

        QTimer.singleShot(0, step)
        self.app.exec()
        return 1 if self.failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DPM GUI 冒烟 + 截图")
    ap.add_argument("--port", default=None, help="串口或 pty 路径；不给则自动起 pty 仿真服务端")
    ap.add_argument("--outdir", default=str(REPO / "build" / "gui_smoke"), help="截图与日志输出目录")
    ap.add_argument("--scan", action="store_true", help="额外跑一次小扫描（频点 × 相位）")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    sim: subprocess.Popen[bytes] | None = None
    port = args.port
    if not port:
        link = Path("/tmp/dpm_gui_smoke_pty")
        sim = start_sim_server(link)
        port = str(link)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = G.DpmGuiApp(port=port, timeout=0.5)
    window.show()

    log_path = outdir / "gui_stdout.txt"
    with log_path.open("w", encoding="utf-8") as out:
        smoke = Smoke(app, window, out)

        def snapshot(name: str) -> None:
            window.grab().save(str(outdir / f"{name}.png"))

        smoke.add(
            "启动并自动连接",
            3.0,
            lambda: smoke.check(
                "启动并自动连接（PING + IDENTIFY）",
                window.client is not None and window.conn_label.text().startswith("已连接"),
                window.conn_label.text(),
            ),
        )
        smoke.add(
            "状态刷新",
            0.8,
            lambda: smoke.check(
                "状态已刷新（status/sigStatus 非空）",
                window.status_label.text().startswith("status=0x"),
                window.status_label.text(),
            ),
        )
        smoke.add("单次测量", 1.5, window.after_single)
        smoke.add(
            "检查单次测量读数",
            0.5,
            lambda: smoke.check(
                "单次测量：频率与相位已显示在界面上",
                window.freq_label.text() not in ("--", "") and window.phase_label.text() not in ("--", ""),
                f"{window.freq_label.text()} / {window.phase_label.text()}；{window.phase_extra.text()}",
            ),
        )
        smoke.add(
            "设置片内源 8MHz/22.5°",
            0.3,
            lambda: (
                window.freq_edit.setText("8MHz"),
                window.phase_spin.setValue(22.5),
                window.after_set_sig(),
            ),
        )
        smoke.add("单次测量（8MHz/22.5°）", 1.5, window.after_single)
        smoke.add(
            "检查 22.5° 读数",
            0.5,
            lambda: smoke.check(
                "细调相位生效（8MHz 请求 22.5° → 显示 ≈21.6°）",
                abs(float(window.phase_label.text().replace("°", "")) - 21.602) < 3.0,
                window.phase_label.text(),
            ),
        )
        smoke.add(
            "相位上界随频点刷新（8MHz）",
            0.3,
            lambda: smoke.check(
                "8MHz（D=125）相位上界收窄到 184.139°",
                abs(window.phase_spin.maximum() - 184.139) < 1e-3,
                f"max={window.phase_spin.maximum():.3f}°；{window.phase_limit_label.text()}",
            ),
        )
        smoke.add(
            "相位上界随频点刷新（25MHz）",
            0.3,
            lambda: (
                window.freq_edit.setText("25MHz"),
                smoke.check(
                    "25MHz（D=40）相位上界放开到 359.999°（>180° 可设）",
                    abs(window.phase_spin.maximum() - 359.999) < 1e-3,
                    f"max={window.phase_spin.maximum():.3f}°；{window.phase_limit_label.text()}",
                ),
            ),
        )
        smoke.add(
            "设置片内源 25MHz/225°（>180°）",
            0.3,
            lambda: (
                window.phase_spin.setValue(225.0),
                window.after_set_sig(),
            ),
        )
        smoke.add("单次测量（25MHz/225°）", 1.5, window.after_single)
        smoke.add(
            "检查 225° 读数",
            0.5,
            lambda: smoke.check(
                ">180° 相位可设可测（25MHz 请求 225° → 显示 ≈225°）",
                abs(float(window.phase_label.text().replace("°", "")) - 225.0) < 3.0,
                window.phase_label.text(),
            ),
        )
        smoke.add("切输入源", 0.8, lambda: (window.radio_ext.setChecked(True), window.after_set_source()))
        # 外部输入无信号（服务端 `--ext-disable`）：PL 停在 busy、结果永不锁存。
        # 这里复现用户报的那条链路：切到外部源 → 测量 → 必须给出「超时 + 输入源诊断」，
        # 不能是「设备错误：PS 返回错误响应 0xFF（ERR_BUSY），请求 0x05（GET_RESULT）」。
        smoke.add("外部无信号时单次测量", 1.0, window.after_single)
        smoke.add(
            "检查外部源超时诊断",
            3.0,
            lambda: smoke.check(
                "外部无信号：报超时并指出外部引脚，不是 ERR_BUSY 设备错误",
                "单次测量失败" in window.log_view.toPlainText()
                and "等待测量结果超时" in window.log_view.toPlainText()
                and "外部引脚" in window.log_view.toPlainText()
                and "ERR_BUSY" not in window.log_view.toPlainText(),
                window.log_view.toPlainText().strip().splitlines()[-1] if window.log_view.toPlainText().strip() else "（日志为空）",
            ),
        )
        smoke.add("切回片内源", 0.8, lambda: (window.radio_int.setChecked(True), window.after_set_source()))
        smoke.add(
            "闸门设置（按秒：40µs）",
            0.8,
            lambda: (
                window.gate_spin.setValue(4e-5),
                window.after_set_gate(),
            ),
        )
        smoke.add(
            "检查闸门换算",
            0.8,
            lambda: smoke.check(
                "闸门按秒下发（40µs → 2000 低速周期 @50MHz）",
                "gateTarget=2000" in window.status_label.text(),
                f"{window.gate_hint.text()}；{window.status_label.text()}",
            ),
        )
        smoke.add(
            "结果表",
            0.5,
            lambda: smoke.check(
                "结果表有行（最近一条在最上面）",
                window.table.rowCount() >= 1,
                f"rows={window.table.rowCount()}",
            ),
        )

        if args.scan:
            smoke.add(
                "扫描",
                0.3,
                lambda: (
                    # 8MHz（D=125）的相位上界只有 184.139° → 225° 那个点会被界面跳过（2×3-1=5 行）
                    window.scan_freqs_edit.setText("8,25"),
                    window.scan_phases_edit.setText("0,90,225"),
                    window.scan_settle_spin.setValue(0.05),
                    window.after_scan(),
                ),
            )
            smoke.add(
                "等扫描完成",
                8.0,
                lambda: smoke.check(
                    "扫描完成：6 个点里 8MHz/225° 越界被跳过，其余 5 点有结果",
                    len(window.last_rows) == 5,
                    f"rows={len(window.last_rows)}，表 {window.table.rowCount()} 行",
                ),
            )
            smoke.add(
                "导出 CSV",
                0.5,
                lambda: (
                    G.scan_rows_to_csv(window.last_rows, str(outdir / "scan.csv")),
                    smoke.check("CSV 可以导出（直接调用同一函数）", (outdir / "scan.csv").exists(), str(outdir / "scan.csv")),
                ),
            )
        # 长闸门（2.4s @50MHz）：等结果预算必须跟着放大（2×2.4+0.25 ≈ 5.05s），
        # 否则旧的固定 2.0s 预算会把一次**正常**测量报成「等待测量结果超时」。
        # 放在扫描之后：闸门被改成 2.4s 后每个扫描点也要跑满一个窗口，那会拖长扫描步骤。
        long_gate: dict[str, int] = {}

        def start_long_gate_measure() -> None:
            long_gate["mark"] = len(window.log_view.toPlainText())
            window.after_single()

        def check_long_gate_measure() -> None:
            tail = window.log_view.toPlainText()[long_gate.get("mark", 0) :]
            lines = [line for line in tail.strip().splitlines() if line.strip()]
            smoke.check(
                "长闸门 2.4s：等待预算自动放大，测量正常完成（不误报超时）",
                "单次测量完成" in tail and "等待测量结果超时" not in tail,
                "；".join(lines[-2:]) if lines else "（日志为空）",
            )

        smoke.add(
            "闸门设置（长闸门：2.4s）",
            0.8,
            lambda: (
                window.gate_spin.setValue(2.4),
                window.after_set_gate(),
            ),
        )
        smoke.add("单次测量（2.4s 闸门）", 1.0, start_long_gate_measure)
        smoke.add(
            "检查长闸门测量",
            4.5,  # 闸门窗口 2.4s + 余量：旧的 2.0s 预算会在这之前报超时
            check_long_gate_measure,
        )
        smoke.add("截图", 0.5, lambda: snapshot("gui"))
        smoke.add("截图（扫描后）", 0.3, lambda: snapshot("gui_final"))

        status = smoke.run()

    if sim is not None:
        sim.terminate()
        try:
            sim.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            sim.kill()

    print(f"\n截图与日志：{outdir}")
    print("GUI 冒烟：" + ("PASS" if status == 0 else "FAIL"))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
