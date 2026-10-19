---
name: dpm-host-protocol
description: 'Use when: 修改 PC 侧上位机（host/dpm_host：protocol/client/cli/gui）、PS 侧串口协议实现（ps/）或验证脚本（spec/、ps/tools/），或需要判断"这条命令该由谁实现、字段单位是什么、怎么在无硬件时验证"。覆盖帧格式与 CRC、命令码与错误码、频率/相位定点换算、片内源参数换算、CLI/GUI 的用法与线程模型、pty 仿真后端与端到端验证入口。'
argument-hint: '要改的上位机/协议功能，或要复现的端到端验证'
---

# 串口协议与上位机（PS ↔ PC）

## 适用场景

- 改 `host/dpm_host/`（协议编解码、串口客户端、CLI、GUI）或 `ps/`（固件侧协议实现）。
- 加/改一条命令、字段或通知帧，需要两侧与文档同步。
- 没有硬件时要把整条链路（上位机 ↔ 服务端 ↔ PL 模型）跑通或复现验证结论。

## 权威来源（先读，别猜）

| 内容 | 位置 |
| --- | --- |
| 协议定稿（帧、命令表、字段单位、错误码） | `docs/ps-protocol.md` |
| 片内源换算与写入流程 | `docs/ps-protocol.md` §6；`ps/include/dpm_regs.h` |
| 验证判据与期望值模型 | `spec/gen_golden.py`、`spec/golden/vectors.json` |
| 相位可控性与网格结论 | `spec/drp_semantics.json`（当前方案 A 为可控） |
| 上位机用法与已知限制 | `host/README.md` |
| PS 侧构建、测试与上板 | `ps/README.md`、`docs/bringup.md` |

**三处实现必须逐位一致**：C（`ps/src/dpm_calc.c`、`dpm_sig.c`、`dpm_cmd.c`）、
Python（`host/dpm_host/mathx.py`、`sig.py`、`protocol.py`）、金标准（`spec/gen_golden.py`），
外加第三方对照 `spec/sim_server.py`。`spec/selftest_spec.py` 自动校验「文档 ↔ 头文件 ↔ 金标准」。

## 约定与踩过的坑

1. **字段单位**：`freqMilliHz` 承载的是**整数 Hz**（名字是历史遗留，25MHz → 25000000）；
   `phaseMilliDeg` 是毫度；`phaseField` 在方案 A 下是 MMCM 的 `DELAY_TIME`（相位码 >> 3）。
   改字段单位必须同步：协议文档、两侧实现、金标准、以及 `host/tests`/`ps/tests` 的断言。
2. **定点公式要用等价安全形式**（避免 64 位溢出）：
   `freqHz = (edge*f_slow)//gate`、`v = 360000*c + (360000*r)//gate`、
   `phase = (v*(f_slow//1000))//(f_dsp//1000)`。
3. **相位网格 = `45/D` 度**（1/8 VCO 周期，方案 A）：请求相位吸附到网格，`SET_SIG` 回显的是
   **可实现**相位。`D ≤ 126`（分频编码成 `HIGH_TIME + LOW_TIME` 各 6 位，127 会被截断）。
   **相位上界与 D 有关**：9 位相位码 `m = round(相位 × D / 45000) ≤ 511`
   ⟺ 可请求 `min(23017499/D, 359999)` 毫度（D=20/25/40/50 → 整个 0~360°；D=120 → 191.812°；
   D=125 → 184.139°）；越界请求回 `ERR_PARAM`（字段截断会让相位**回绕**，不是吸附）。
   `>180°` 的请求是合法的（相位计测无符号间隔），不要按老规则拒掉。两侧判据：
   C `dpm_mmcm_phase_code_fits` / Python `sig.phase_code_fits`；仿真后端必须用同一条
   （否则会出现「仿真接受、真机拒绝」的假象）；`DpmClient.scan` 遇到被拒的点会跳过并记日志。
4. **通知码**用 `0xE1/0xE2`：`0x81..0x8E` 是响应区间（值 = 请求码 `| 0x80`），`0xE0..0xEF`
   保留给 PS 主动上报，两者不重叠。新增响应码时先检查是否与通知码撞码。
5. **GUI 线程模型**：所有串口操作都在后台线程里串行执行，结果用 **Qt 信号**（PySide6）回主线程刷新；
   不要在工作线程里碰控件，也不要在主线程里做阻塞请求。
6. **CLI/GUI 的 DRP 字只是「预演」**：真正的换算在 PS 固件里做（`SET_SIG(freq, phase)`），
   上位机本地求解只用于显示 VCO/MULT/D 与量化提示。因此两侧公式必须一致，否则「显示」与「实测」会对不上。
7. **超时按闸门时间放大（长闸门不误报超时）**：一次测量的时长就是闸门窗口
   `T_gate = gateTarget / f_slow`（28 位闸门计数器 @50MHz ⇒ 最长 ≈5.37s），而真机固件还可能在
   `GET_RESULT`/`GET_MEAS` **命令内部**等一个闸门窗口（协议 §3.0，固件侧上限
   `DPM_WAIT_TIMEOUT_MS = 50ms + 4 × T_gate`，注意是**秒**的量纲、别再退回「µs 当 ms」的旧写法）。
   因此调用方给的超时只是下限：
   `DpmClient` 实际预算 ≥ `2×T_gate + 0.25s`（`gate_wait_budget()`；闸门目标由
   `GET_STATUS.gateTarget` / `SET_GATE` 回显缓存），单次请求超时只当「这一拍没问出结果」继续轮询。
   改超时相关代码时别退回固定值，否则 `gate 120000000`（2.4s）这种正常测量必报超时；
   回归入口：`host/tests/test_gate_timeout.py` 与 `test_client_over_pty.py` 的长闸门用例
   （pty 对端 + `sim_server --gate 120000000`）。GUI 的闸门上限就是 28 位计数器对应的 5.37s，
   不再压到 2s。

## 无硬件验证（pty 仿真后端）

```bash
# 终端 A：把 PL 模型接到一条 pty 上（模拟 PS 侧固件）
uv run python -m dpm_host.sim_server --pty --pty-link /tmp/dpm_pty
# 终端 B：CLI / GUI 连上去
uv run python -m dpm_host --port /tmp/dpm_pty ping
uv run python -m dpm_host --port /tmp/dpm_pty scan --freqs 8,25 --phases 0,45 --out /tmp/scan.csv
uv run python -m dpm_host --port /tmp/dpm_pty gui
```

- 仿真后端 `host/dpm_host/sim_fpga.py` 建模「寄存器语义 + 测量模型 + 假 MMCM」；
  DRP 段在模型里是平坦偏移 **`0x100 + off`**（对应真实的另一段 AXI 基地址 `0x43C1_0000`）。
- `--phase-model static` 用于模拟「相位不可控」的旧 bitstream，验证上位机的提示路径；
  当前方案 A 下用默认 `auto` 即可。
- GUI 改动后跑 `uv run python host/tools/gui_smoke.py [--scan]`：自动起 pty、跑连接/测量/切源/扫描，
  并用 `QWidget.grab()` 截真实窗口（无显示环境加 `QT_QPA_PLATFORM=offscreen`）。

## 端到端验证入口（真串口）

```bash
# C 服务端（与固件同一份 dpm_cmd.c）经 pty：80 点扫描 + 补充用例
uv run python spec/validate_sweep.py --server "./ps/build/host_main --device {device}"
uv run python spec/verify_extras.py  --server "./ps/build/host_main --device {device}"
# 离线验收演练（80 点扫描，含流上报与长闸门有界等待）
bash ps/tools/onboard_accept_sim.sh
# 上板（JTAG 冷启动 + 验收 + 扫描）
xsdb -quiet ps/tools/jtag_boot.tcl
uv run python ps/tools/onboard_accept.py --port /dev/ttyUSB0 --no-boot
```

判据与容限见 `dpm-measurement-model`；报告必须写清「跑了哪个后端（C 验证台 / 独立 sim_server /
真实板子）」，三者证明力不同：**仿真证明协议与换算，只有上板能证明真实 MMCM/布线与精度**。

## 交付前

按 `dpm-pre-delivery-check`：`uv run pytest -q`（全量 + 格式检查）、重建 C 验证台跑 `ctest`、
改了协议/换算再跑上面的端到端与 `uv run python spec/selftest_spec.py`。
