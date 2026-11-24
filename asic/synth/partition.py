#!/usr/bin/env python3
"""把展平后的组合逻辑按时钟域拆开，交给 ABC 用各自的目标延迟映射。

背景：Yosys 的 ``abc`` 只有一个全局 ``-D`` 目标延迟。如果整个设计都按
dspClock 的 300MHz（3333 ps）映射，50MHz 的 clock 域也会被要求满足 3.333ns。
全局 ``-D`` 会把不需要跑那么快的 clock 域一起收紧。本脚本在
``dfflibmap`` 之后、ABC 之前接管：

1. 从 ``write_json`` 导出的网表里，沿时钟网络给每个触发器/ICG 找到所属时钟域；
2. 组合逻辑按"扇出能到达哪个时钟域的寄存器"分组。若一个组合单元同时到达
   多个域，归到周期最短（约束最紧）的域；
3. 生成一段 Yosys 脚本：用 ``setattr submod`` + ``submod -hidden`` 把每组
   组合逻辑移进子模块，对子模块分别执行 ``abc -D <该域周期>``，最后
   ``flatten`` 回单层网表。

这样每个域的组合逻辑只看到自己的目标延迟，跨域边界仍然是触发器到触发器的
路径（与原网表一致），不会引入额外的组合逻辑切分点。

调用方式见 ``asic/synth/synth.ys``。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import NoReturn

# 允许出现在 select 模式里的字符。Yosys 自动生成的单元名里可能有 $、:、.、/
# 等；* ? [ ] 会被当成通配符，\ 在 select 模式里可能被当成转义，一律提前拒绝，
# 避免选错单元。
BAD_PATTERN_CHARS = set("*?[]\\")


def output_pins(cell: dict) -> set[str]:
    """返回单元的输出引脚名。

    该阶段网表里只有两类单元：Yosys 内部门（``$_XXX_``，输出统一为 Y）和
    liberty 映射出的时序单元（DFF 的 Q/Q_N、ICG 的 GCLK）。liberty 里的
    组合单元还没有被实例化，因此不需要更通用的方向推断。
    """
    cell_type = cell["type"]
    if cell_type.startswith("$_"):
        return {"Y"}
    pins = set(cell["connections"])
    return {pin for pin in pins if pin == "Q" or pin.startswith("Q_") or pin == "GCLK"}


def is_comb(cell: dict) -> bool:
    return cell["type"].startswith("$_") and "Y" in cell["connections"]


def is_sequential(cell: dict) -> bool:
    return "CLK" in cell["connections"] or "CLK_N" in cell["connections"]


def is_icg(cell: dict) -> bool:
    return "GCLK" in cell["connections"]


def fail(message: str) -> NoReturn:
    print(f"partition.py: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="按时钟域拆分组合逻辑并生成分域 ABC 脚本")
    parser.add_argument("--json", required=True, help="Yosys write_json 导出的展平网表")
    parser.add_argument("--out", required=True, help="生成的 Yosys 脚本路径")
    parser.add_argument("--liberty", required=True, help="ABC 映射用的 liberty")
    parser.add_argument("--constr", required=True, help="ABC -constr 约束文件")
    parser.add_argument(
        "--clock",
        action="append",
        required=True,
        metavar="PORT=PERIOD_PS",
        help="时钟端口与目标周期（ps），可重复，例如 clock=20000 dspClock=3333",
    )
    args = parser.parse_args()

    clock_period: dict[str, int] = {}
    for item in args.clock:
        if "=" not in item:
            fail(f"--clock 需要 PORT=PERIOD_PS 形式，收到 {item!r}")
        port, period_text = item.split("=", 1)
        try:
            period = int(period_text)
        except ValueError:
            fail(f"--clock 的周期必须是整数 ps，收到 {period_text!r}")
        if period <= 0:
            fail(f"--clock 的周期必须为正，收到 {period}")
        if port in clock_period:
            fail(f"时钟端口 {port} 重复")
        clock_period[port] = period

    with open(args.json, encoding="utf-8") as handle:
        design = json.load(handle)

    modules = design["modules"]
    if len(modules) != 1:
        fail(f"期望展平后只有一个模块，实际有 {len(modules)} 个：{', '.join(modules)}")
    top_name, module = next(iter(modules.items()))
    cells: dict = module["cells"]
    ports: dict = module["ports"]

    # 时钟根：顶层时钟端口对应的 net bit -> 时钟域名。
    root_bit: dict[int, str] = {}
    for port, period in clock_period.items():
        if port not in ports:
            fail(f"顶层没有时钟端口 {port}")
        if ports[port]["direction"] != "input":
            fail(f"时钟端口 {port} 不是输入")
        for bit in ports[port]["bits"]:
            root_bit[bit] = port

    # 输出端口 bit 集合：只连到输出端口的组合逻辑归到最慢的域。
    output_bits: set[int] = set()
    for port in ports.values():
        if port["direction"] == "output":
            output_bits.update(port["bits"])

    # 每个 bit 的驱动者和使用者。
    driver: dict[int, tuple[str, str]] = {}
    for name, cell in cells.items():
        outs = output_pins(cell)
        for pin, bits in cell["connections"].items():
            if pin not in outs:
                continue
            for bit in bits:
                if bit in driver:
                    fail(f"net bit {bit} 有多个驱动：{driver[bit]} 和 {(name, pin)}")
                driver[bit] = (name, pin)
    consumers: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for name, cell in cells.items():
        outs = output_pins(cell)
        for pin, bits in cell["connections"].items():
            if pin in outs:
                continue
            for bit in bits:
                consumers[bit].append((name, pin))

    def clock_of_bit(bit: int, seen: set[int]) -> str | None:
        """沿时钟网络回溯到顶层时钟端口。

        ICG 的 GCLK 输出继承其 CLK 输入的域；时钟路径上若有组合缓冲/反相器，
        也继续往前找。
        """
        if bit in root_bit:
            return root_bit[bit]
        if bit in seen or bit not in driver:
            return None
        seen = seen | {bit}
        name, _pin = driver[bit]
        cell = cells[name]
        if is_icg(cell):
            return clock_of_bit(cell["connections"]["CLK"][0], seen)
        if is_sequential(cell):
            return None
        if is_comb(cell):
            for pin, bits in cell["connections"].items():
                if pin in output_pins(cell):
                    continue
                for source in bits:
                    domain = clock_of_bit(source, seen)
                    if domain is not None:
                        return domain
        return None

    # 时序单元（DFF/ICG）的时钟域。
    seq_domain: dict[str, str] = {}
    for name, cell in cells.items():
        if not is_sequential(cell):
            continue
        clk_pin = "CLK" if "CLK" in cell["connections"] else "CLK_N"
        domain = clock_of_bit(cell["connections"][clk_pin][0], set())
        if domain is None:
            fail(f"时序单元 {name} 的时钟无法追到顶层时钟端口")
        seq_domain[name] = domain

    # 组合单元的扇出能到达哪些域的寄存器（或输出端口）。带记忆化，避免重复
    # 遍历；正常网表是 DAG，出现环说明网表异常，直接报错。
    memo: dict[str, frozenset[str]] = {}
    visiting: set[str] = set()

    def sink_domains(name: str) -> frozenset[str]:
        if name in memo:
            return memo[name]
        if name in visiting:
            fail(f"组合逻辑出现环路，涉及单元 {name}")
        visiting.add(name)
        result: set[str] = set()
        cell = cells[name]
        outs = output_pins(cell)
        # 只看本单元的输出：沿扇出方向找到它最终驱动的寄存器/输出端口。
        for pin, bits in cell["connections"].items():
            if pin not in outs:
                continue
            for bit in bits:
                if bit in output_bits:
                    result.add("__output__")
                for consumer, _consumer_pin in consumers.get(bit, ()):
                    consumer_cell = cells[consumer]
                    if is_sequential(consumer_cell):
                        result.add(seq_domain[consumer])
                    elif is_comb(consumer_cell):
                        result |= sink_domains(consumer)
                    elif consumer_cell["type"] == "$scopeinfo":
                        continue
                    else:
                        fail(f"无法分类的单元 {consumer}（类型 {consumer_cell['type']}）")
        visiting.remove(name)
        memo[name] = frozenset(result)
        return memo[name]

    groups: dict[str, list[str]] = defaultdict(list)
    unknown: list[str] = []
    for name, cell in cells.items():
        if not is_comb(cell):
            if not is_sequential(cell) and cell["type"] != "$scopeinfo":
                unknown.append(f"{name}（类型 {cell['type']}）")
            continue
        domains = set(sink_domains(name))
        if domains - {"__output__"}:
            # 同时到达多个域时取周期最短的域，保证约束不会放松。
            domain = min(domains - {"__output__"}, key=lambda item: clock_period[item])
        elif "__output__" in domains:
            # 只驱动顶层输出：按最慢的域处理（输出延迟由 SDC 约束）。
            domain = max(clock_period, key=lambda item: clock_period[item])
        else:
            fail(f"组合单元 {name} 的扇出没有到达任何寄存器或输出端口")
        groups[domain].append(name)

    if unknown:
        fail("网表里存在无法分类的单元：" + "，".join(unknown))

    if not groups:
        fail("没有找到可映射的组合逻辑")

    for domain, names in groups.items():
        print(f"partition.py: {len(names)} 个组合单元 -> {domain}（{clock_period[domain]} ps）")

    # 生成 Yosys 脚本。单元名按 40 个一行拆分，避免单行过长。
    lines: list[str] = [
        "# 由 asic/synth/partition.py 生成，请勿手工修改。",
        "# 每个域的 submod 标签形如 dpm_<clock_port>，最终模块名为 <top>_dpm_<clock_port>。",
        "",
    ]
    for domain, names in groups.items():
        tag = f"dpm_{domain}"
        lines.append(f'# {domain}: {len(names)} cells, target {clock_period[domain]} ps')
        lines.append("select -none")
        for start in range(0, len(names), 40):
            chunk = names[start : start + 40]
            for name in chunk:
                if BAD_PATTERN_CHARS & set(name):
                    fail(f"单元名 {name!r} 含有 Yosys 通配符，无法安全地按名选择")
            lines.append("select -add " + " ".join(f"c:{name}" for name in chunk))
        lines.append(f'setattr -set submod "{tag}"')
        lines.append("")

    lines += [
        "# 把带 submod 属性的单元移进子模块（私有端口名，flatten 后恢复原名）。",
        "select -clear",
        f"select {top_name}",
        "submod -hidden",
        "",
    ]

    for domain, names in groups.items():
        tag = f"dpm_{domain}"
        lines += [
            f"# {domain} 域：目标 {clock_period[domain]} ps",
            "select -clear",
            # -assert-any 只断言存在、不改变当前选择；必须再显式 select 一次，
            # 否则 abc 会对整个设计（包括另一个域的子模块）套用同一个 -D。
            f"select -assert-any {top_name}_{tag}",
            f"select {top_name}_{tag}",
            f"abc -liberty {args.liberty} -constr {args.constr} -D {clock_period[domain]}",
            "",
        ]

    lines += [
        "# 合并回单层网表，供 OpenROAD 读取。",
        "select -clear",
        "flatten",
        "opt_clean",
        "select -clear",
        "",
    ]

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    print(f"partition.py: 已生成 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
