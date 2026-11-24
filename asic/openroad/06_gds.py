#!/usr/bin/env python3
"""用 KLayout 把 OpenROAD 的 routed DEF 与 sky130 单元 GDS 合并成最终版图。

OpenROAD 26Q4 的二进制不带 write_gds，因此最后一步用 KLayout 的 LEF/DEF 读取器：
DEF 里的标准单元实例从 PDK 的 ``cells/*/*.gds`` 取版图，布线几何与 via 由 DEF/LEF
生成。DEF 的 LEF 层名会显式映射回 sky130 的 GDS 层号，避免走线落到 LEF 顺序的
回退层。只作为交付用的版图视图；DRC / LVS 签核不在本流程范围内。

同时生成与 GDS 同名的 ``.lyp`` 图层配置文件，供 KLayout 查看时显示层名与可见性；
颜色不在文件里指定，由 KLayout 使用默认调色板分配。

用法示例：
    python3 asic/openroad/06_gds.py \
        --def build/asic/openroad/final.def \
        --output build/asic/openroad/dpm_asic.gds \
        --tech-lef build/asic/sky130_fd_sc_hd.tlef \
        --cells-lef build/asic/sky130_fd_sc_hd_cells.lef \
        --cells-dir /home/luo/skywater-pdk/.../cells
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import klayout.db as kdb
except ImportError:  # pragma: no cover - 运行环境没有 KLayout Python 包时
    print(
        "[gds] 警告：没有找到 klayout Python 包（klayout.db），跳过 GDS 导出",
        file=sys.stderr,
    )
    raise SystemExit(2)

# LEF 层名 -> sky130 GDS 的 (layer, datatype)。
# KLayout 的 LEF/DEF 读取器在没有 layer map 时按 LEF 中的层顺序回退到 1/0、3/0、5/0 ...
# 生成 DEF 几何，和单元 GDS 里的 65/20、66/20、67/20、68/20 ... 完全分开，看起来就像
# “只有单元、没有连线”。这里显式映射布线层与 cut 层，保证 DEF 的走线/通过孔落回
# 工艺层。单元 GDS 自身的几何不受这张表影响。
SKY130_LEF_TO_GDS = {
    "li1": (67, 20),
    "mcon": (67, 44),
    "met1": (68, 20),
    "via": (68, 44),
    "met2": (69, 20),
    "via2": (69, 44),
    "met3": (70, 20),
    "via3": (70, 44),
    "met4": (71, 20),
    "via4": (71, 44),
    "met5": (72, 20),
}

# 查看用的图层配置：(layer, datatype) -> (名称, 默认显示)。
# 层号/名称与 sky130 PDK 的 docs/rules/gds_layers.csv 对应。颜色不在 .lyp 里指定，
# 由 KLayout 按默认调色板自动分配；默认隐藏 implant、pin/label、areaid 与单元边界，
# 避免全芯片视图过密。
DISPLAY_LAYERS = {
    (64, 20): ("nwell", True),
    (64, 5): ("nwell.label", False),
    (64, 16): ("nwell.pin", False),
    (64, 59): ("pwell.label", False),
    (65, 20): ("diff", True),
    (65, 44): ("tap", True),
    (66, 20): ("poly", True),
    (66, 44): ("licon1", True),
    (66, 15): ("poly.short", False),
    (67, 20): ("li1", True),
    (67, 44): ("mcon", True),
    (67, 5): ("li1.label", False),
    (67, 16): ("li1.pin", False),
    (68, 20): ("met1", True),
    (68, 44): ("via", True),
    (68, 5): ("met1.label", False),
    (68, 16): ("met1.pin", False),
    (69, 20): ("met2", True),
    (69, 44): ("via2", True),
    (70, 20): ("met3", True),
    (70, 44): ("via3", True),
    (71, 20): ("met4", True),
    (71, 44): ("via4", True),
    (72, 20): ("met5", True),
    (78, 44): ("hvtp", False),
    (93, 44): ("nsdm", False),
    (94, 20): ("psdm", False),
    (95, 20): ("npc", False),
    (81, 4): ("areaid.sc", False),
    (81, 23): ("areaid.de", False),
    (83, 44): ("text", False),
    (122, 16): ("pwell.pin", False),
    (236, 0): ("cell.boundary", False),
}


def write_lyp(path: Path, present_layers: set[tuple[int, int]]) -> None:
    """生成 KLayout 图层配置文件；只写层名/可见性，颜色交给 KLayout 默认调色板。"""
    root = ET.Element("layer-properties")
    known: set[tuple[int, int]] = set()

    def add_entry(name: str, layer: int, datatype: int, visible: bool) -> None:
        properties = ET.SubElement(root, "properties")
        ET.SubElement(properties, "visible").text = "true" if visible else "false"
        ET.SubElement(properties, "name").text = name
        ET.SubElement(properties, "source").text = f"{layer}/{datatype}@1"

    for key in sorted(DISPLAY_LAYERS):
        if key not in present_layers:
            continue
        known.add(key)
        name, visible = DISPLAY_LAYERS[key]
        add_entry(name, key[0], key[1], visible)

    # 以后 PDK 若新增未登记的层，仍然给一个默认隐藏的条目，避免查看时出现无名层。
    for layer, datatype in sorted(present_layers - known):
        add_entry(f"{layer}/{datatype}", layer, datatype, False)

    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space=" ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--def", dest="def_file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--lyp", help="输出的 KLayout 图层配置（默认与 GDS 同名的 .lyp）")
    parser.add_argument("--tech-lef", required=True)
    parser.add_argument("--cells-lef", required=True)
    parser.add_argument("--cells-dir", required=True)
    args = parser.parse_args()

    for path in (args.def_file, args.tech_lef, args.cells_lef, args.cells_dir):
        if not os.path.exists(path):
            print(f"[gds] 错误：{path} 不存在", file=sys.stderr)
            return 1

    cell_gds = sorted(glob.glob(os.path.join(args.cells_dir, "*", "*.gds")))
    if not cell_gds:
        print(f"[gds] 错误：{args.cells_dir} 下没有单元 GDS", file=sys.stderr)
        return 1

    options = kdb.LoadLayoutOptions()
    # macro 几何来自 macro_layout_files，LEF 里找不到 MACRO 是预期情况，
    # 关掉 KLayout 的这类告警（真正影响几何的错误仍会以异常/空版图暴露）
    options.warn_level = 0
    config = options.lefdef_config
    config.lef_files = [args.tech_lef, args.cells_lef]
    # 2 = 不用 LEF 几何生成宏，改用 macro_layout_files 里 GDS 的真实版图
    config.macro_resolution_mode = 2
    config.macro_layout_files = cell_gds
    config.dbu = 0.001
    config.read_lef_with_def = False

    # 把 DEF 的布线/cut 层映射到 sky130 的真实 GDS 层号（见文件头说明）
    config.layer_map.clear()
    for lef_name, (layer, datatype) in SKY130_LEF_TO_GDS.items():
        config.layer_map.map(f"{lef_name}: {layer}/{datatype}")

    # 交付版图只保留工艺几何：不生成 DEF pin/label、LEF obstruction/blockage/fill
    # 以及单元外框等辅助图层（单元 GDS 里的 label 仍原样保留）。
    config.produce_cell_outlines = False
    config.produce_pins = False
    config.produce_labels = False
    config.produce_lef_pins = False
    config.produce_lef_labels = False
    config.produce_obstructions = False
    config.produce_blockages = False
    config.produce_fills = False

    layout = kdb.Layout()
    layout.read(args.def_file, options)
    top = layout.top_cell()
    if top is None:
        print("[gds] 错误：读取 DEF 后没有顶层单元", file=sys.stderr)
        return 1
    layout.write(args.output)

    # 以写出的文件为准建立 .lyp：DEF 读取器可能在内存里留下空的 pin/label 层，
    # 这些层不会被写进 GDS，不该出现在查看配置里。
    written = kdb.Layout()
    written.read(args.output)
    present_layers = {(written.get_info(li).layer, written.get_info(li).datatype) for li in written.layer_indexes()}
    lyp_path = Path(args.lyp) if args.lyp else Path(args.output).with_suffix(".lyp")
    write_lyp(lyp_path, present_layers)

    bbox = top.bbox()
    print(
        f"[gds] 顶层单元 {top.name}，bbox {bbox.width() * layout.dbu:.3f} x "
        f"{bbox.height() * layout.dbu:.3f} um，"
        f"{layout.cells()} 个单元，输出 {args.output} "
        f"({os.path.getsize(args.output) / 1e6:.1f} MB) 和 {lyp_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
