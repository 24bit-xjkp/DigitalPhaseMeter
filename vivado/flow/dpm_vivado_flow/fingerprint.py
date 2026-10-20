"""逐 run 的输入指纹。

指纹只使用「真正影响该 run 结果」的输入：
- 综合：BD/IP 内容、顶层 XDC（只对 synth_1）、综合策略目标值、适用 hook 内容、工具/器件/schema。
- 实现：synth_1 指纹、实现策略、INIT/ROUTE hook、工具/器件/schema。
"""

from __future__ import annotations

from .hashing import compose
from .paths import PART
from .stamps import SCHEMA


def _tool(part: str, vivado: str) -> tuple[object, ...]:
    return (SCHEMA, part, vivado)


def synth_inputs(
    *,
    bd_structure: str,
    bd_args: str,
    xdc_hash: str | None,
    strategy_sig: str,
    hook_hash: str | None,
    vivado: str,
    part: str = PART,
) -> str:
    return compose(
        "synth",
        bd_structure,
        bd_args,
        xdc_hash or "",
        strategy_sig,
        hook_hash or "",
        _tool(part, vivado),
    )


def ip_synth_inputs(
    *,
    base: str,
    strategy_sig: str,
    hook_hash: str | None,
    vivado: str,
    part: str = PART,
) -> str:
    return compose(
        "ip_synth",
        base,
        strategy_sig,
        hook_hash or "",
        _tool(part, vivado),
    )


def impl_inputs(
    *,
    synth_fingerprint: str,
    strategy_sig: str,
    init_hook_hash: str,
    route_hook_hash: str,
    vivado: str,
    part: str = PART,
) -> str:
    return compose(
        "impl",
        synth_fingerprint,
        strategy_sig,
        init_hook_hash,
        route_hook_hash,
        _tool(part, vivado),
    )
