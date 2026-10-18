"""DigitalPhaseMeter PC 侧上位机（Python）。

本包实现 `docs/ps-protocol.md` 定义的 PS ↔ PC 串口协议的 PC 侧全部逻辑：

- :mod:`dpm_host.protocol`  帧编解码、CRC-16/MODBUS、流式解析器与各命令的 PAYLOAD 编解码；
- :mod:`dpm_host.client`    串口客户端（超时/重试/线程安全/通知帧回调）；
- :mod:`dpm_host.mathx`     频率与相位的定点换算（与 `ps/include/dpm_calc.h` 同一契约）；
- :mod:`dpm_host.sig`       片内信号源（MMCM）求解与 DRP 配置字；
- :mod:`dpm_host.sim_fpga`  寄存器级 + 测量级 PL 模型（无硬件时验证上位机）；
- :mod:`dpm_host.sim_server` 把 PL 模型接到串口（含 `--pty` 虚拟串口模式）上，模拟 PS 侧固件；
- :mod:`dpm_host.cli`       `python3 -m dpm_host <子命令>` 命令行界面；
- :mod:`dpm_host.gui`       PySide6 图形界面。

所有文档与代码注释使用中文。默认时钟为低速 50MHz、高速（DSP）450MHz。
"""

from __future__ import annotations

__version__ = "0.1.0"

#: 协议版本（与 `docs/ps-protocol.md` 对齐，仅用于本包自述）。
PROTOCOL_VERSION = "1.0"

__all__ = ["__version__", "PROTOCOL_VERSION"]
