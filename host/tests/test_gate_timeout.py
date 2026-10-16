"""长闸门下的超时预算：`DpmClient` 必须按闸门时间放大等待时间（不误报超时）。

背景（真机复现的问题）：一次测量的时长就是闸门窗口 `T_gate = gateCnt / f_slow ≈ gateTarget / f_slow`
（`docs/ps-protocol.md` 第 5 节）。上位机原来的固定超时是「单次请求 0.5s、等结果 2.0s」，闸门时间
一长（RTL 的 28 位闸门计数器在 50MHz 下最长 ≈5.37s）就会在**测量本身没问题**的情况下报
「等待测量结果超时」；真机 C 固件还会在 `GET_RESULT` / `GET_MEAS` 命令**内部**等结果
（`50 + 4×闸门时间` 的有界等待，见协议 §3.0），单次请求超时同样会被闸门拖爆。

修法：调用方给的超时只当**下限**，实际预算不小于 `2×闸门 + 0.25s`；闸门目标由 `GET_STATUS` /
`SET_GATE` 自动缓存。本文件是**纯单元**用例（不连串口/pty），真机语义（对端在命令内部阻塞、
结果按闸门窗口锁存）由 `host/tests/test_client_over_pty.py` 的长闸门用例覆盖。

运行：
    仓库根目录：`uv run pytest host/tests/test_gate_timeout.py -q`
    host 目录： `uv run pytest tests/test_gate_timeout.py -q`
"""

from __future__ import annotations

import argparse

import pytest
from dpm_host import cli
from dpm_host import mathx as M
from dpm_host import protocol as P
from dpm_host.client import (
    GATE_TIMEOUT_FACTOR,
    GATE_TIMEOUT_SLACK_S,
    DpmClient,
    gate_wait_budget_seconds,
)

SLOW_HZ = 50_000_000
#: 长闸门（2.4s @50MHz）：旧代码的 2.0s 等结果预算必然误报超时。
LONG_GATE = 120_000_000


# ------------------------------------------------------------------ 预算换算


def test_budget_is_zero_when_gate_unknown() -> None:
    """闸门目标未知/为 0 时不放大（沿用调用方给的超时），也绝不会算出负数。"""
    assert gate_wait_budget_seconds(None, SLOW_HZ) == 0.0
    assert gate_wait_budget_seconds(0, SLOW_HZ) == 0.0


def test_budget_keeps_default_gate_unchanged() -> None:
    """默认闸门（2000 周期 = 40µs）的预算 ≈ 0.25s，远小于原来的 2.0s 等结果超时。"""
    budget = gate_wait_budget_seconds(P.DEFAULT_GATE_TARGET, SLOW_HZ)
    assert budget == pytest.approx(GATE_TIMEOUT_FACTOR * 40e-6 + GATE_TIMEOUT_SLACK_S)
    assert budget < 0.5


def test_budget_over_long_gate() -> None:
    """长闸门的预算随闸门窗口线性放大：2.4s 闸门 → 5.05s，5.37s（28 位上界）→ ≈11.0s。"""
    assert gate_wait_budget_seconds(LONG_GATE, SLOW_HZ) == pytest.approx(5.05)
    # 28 位闸门计数器在 50MHz 下的最长闸门（RTL 上界）
    assert gate_wait_budget_seconds(M.GATE_TARGET_MAX, SLOW_HZ) == pytest.approx(10.9874, abs=1e-3)


def test_budget_zero_slow_clock_does_not_divide_by_zero() -> None:
    assert gate_wait_budget_seconds(2000, 0) == 0.0


# ------------------------------------------------------------------ 客户端语义


def test_effective_timeout_uses_base_as_floor() -> None:
    """调用方给的超时是下限：比预算长就用调用方的，比预算短就按预算放大。"""
    client = DpmClient(timeout=0.5)
    client.gate_target = LONG_GATE  # 等价于 GET_STATUS/SET_GATE 缓存下来的闸门目标

    assert client.gate_window_s() == pytest.approx(2.4)
    assert client.gate_wait_budget() == pytest.approx(5.05)
    # 基线更长 → 以基线为准（不缩短调用方要求的等待）
    assert client.effective_timeout(30.0) == pytest.approx(30.0)
    # 基线更短 → 放大到预算，长闸门不误报
    assert client.effective_timeout(2.0) == pytest.approx(5.05)
    assert client.effective_timeout(0.5) == pytest.approx(5.05)
    assert client.effective_timeout() == pytest.approx(5.05)  # 用 self.timeout=0.5 当基线
    # 诊断请求可以关掉放大（GET_STATUS 不会被闸门拖住）
    assert client.effective_timeout(0.2, gate_aware=False) == pytest.approx(0.2)


def test_effective_timeout_without_gate_target() -> None:
    """还没读到闸门目标时行为与改动前一致（不放大）。"""
    client = DpmClient(timeout=0.5)
    assert client.gate_window_s() is None
    assert client.gate_wait_budget() == 0.0
    assert client.effective_timeout(0.3) == pytest.approx(0.3)
    assert client.effective_timeout() == pytest.approx(0.5)


def test_get_status_caches_gate_target(monkeypatch) -> None:
    """`GET_STATUS` 顺带把 `gateTarget` 记进缓存（之后所有请求的预算按它算）。"""
    client = DpmClient(timeout=0.5)
    st = P.Status(status=0, sig_status=0, gate_target=LONG_GATE, lost_count=0, seq=0)
    frame = P.Frame(int(P.Cmd.GET_STATUS.response), P.encode_status(st))
    monkeypatch.setattr(client, "request", lambda *a, **k: frame)

    assert client.get_status().gate_target == LONG_GATE
    assert client.gate_target == LONG_GATE
    assert client.gate_wait_budget() == pytest.approx(5.05)


def test_get_status_keeps_cache_when_device_reports_zero(monkeypatch) -> None:
    """设备报 `gateTarget = 0`（未配置闸门）时不覆盖已有缓存，也不按 0 算预算。"""
    client = DpmClient(timeout=0.5)
    client.gate_target = LONG_GATE
    st = P.Status(status=0, sig_status=0, gate_target=0, lost_count=0, seq=0)
    frame = P.Frame(int(P.Cmd.GET_STATUS.response), P.encode_status(st))
    monkeypatch.setattr(client, "request", lambda *a, **k: frame)

    client.get_status()
    assert client.gate_target == LONG_GATE


def test_set_gate_caches_effective_value(monkeypatch) -> None:
    """`SET_GATE` 用**回显的生效值**更新缓存（对端可能钳位），之后的等待预算按它算。"""
    client = DpmClient(timeout=0.5)
    frame = P.Frame(int(P.Cmd.SET_GATE.response), P.encode_set_gate(LONG_GATE))
    monkeypatch.setattr(client, "request", lambda *a, **k: frame)

    assert client.set_gate(999_999_999) == LONG_GATE
    assert client.gate_target == LONG_GATE
    assert client.gate_wait_budget() == pytest.approx(5.05)


def test_ensure_gate_target_does_not_ask_again(monkeypatch) -> None:
    """闸门目标已知时不再多发 `GET_STATUS`（`monitor`/连续测量每个样本都会进 `wait_*`）。"""

    def boom(*_a, **_k):  # pragma: no cover - 调用了就是失败
        raise AssertionError("目标已知时不该再发请求")

    client = DpmClient(timeout=0.5)
    client.gate_target = P.DEFAULT_GATE_TARGET
    monkeypatch.setattr(client, "request", boom)
    client._ensure_gate_target()  # 已知 → 直接返回


def test_refresh_gate_target_survives_failure(monkeypatch) -> None:
    """`GET_STATUS` 失败时沿用缓存、不抛异常（等待预算退回调用方给的基线）。"""
    client = DpmClient(timeout=0.5)

    def boom(*_a, **_k):
        raise P.DpmTimeoutError(P.Cmd.GET_STATUS, 0.1)

    monkeypatch.setattr(client, "request", boom)
    assert client.refresh_gate_target() is None
    assert client.gate_target is None
    assert client.effective_timeout(0.3) == pytest.approx(0.3)  # 退回基线，不放大

    client.gate_target = LONG_GATE  # 有缓存时刷新失败也不丢缓存
    assert client.refresh_gate_target() == LONG_GATE
    assert client.gate_wait_budget() == pytest.approx(5.05)


def test_no_result_detail_mentions_gate_budget() -> None:
    """超时诊断要点明「闸门多长、等了多久」，否则用户只会看到一句没有信息量的超时。"""
    client = DpmClient(timeout=0.5)
    client.gate_target = LONG_GATE
    st = P.Status(status=int(P.STATUS_BUSY), sig_status=0, gate_target=LONG_GATE, lost_count=0, seq=0)
    detail = client._no_result_detail(st)
    assert "等待测量结果超时" in detail
    assert str(LONG_GATE) in detail
    assert "5.05" in detail  # 本次实际等待预算
    assert "外部引脚" in detail  # 输入源诊断仍然在


# ------------------------------------------------------------------ CLI 接线


class _FakeClient:
    """`gate` 子命令用到的客户端接口（只读 + SET_GATE 回显）。"""

    slow_hz = SLOW_HZ

    def __init__(self, effective: int) -> None:
        self.effective = int(effective)

    def set_gate(self, value: int) -> int:
        return self.effective

    def gate_window_s(self) -> float:
        return self.effective / self.slow_hz

    def gate_wait_budget(self) -> float:
        return gate_wait_budget_seconds(self.effective, self.slow_hz)


def test_cli_gate_prints_wait_budget(capsys) -> None:
    """`gate` 子命令把「生效闸门 + 等结果等待预算」一起打出来（长闸门下用户才知道要等多久）。"""
    args = argparse.Namespace(target=LONG_GATE, json=False)
    assert cli.cmd_gate(args, _FakeClient(LONG_GATE)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"gateTarget = {LONG_GATE}" in out
    assert "等待预算 = 5.05 s" in out
    assert "长闸门不会误报超时" in out


def test_cli_gate_json_reports_budget(capsys) -> None:
    """`--json` 输出里也带上预算，脚本消费时不必自己重算。"""
    args = argparse.Namespace(target=LONG_GATE, json=True)
    assert cli.cmd_gate(args, _FakeClient(LONG_GATE)) == cli.EXIT_OK
    payload = capsys.readouterr().out
    assert '"gateTarget": 120000000' in payload
    assert '"waitBudgetSeconds": 5.05' in payload
