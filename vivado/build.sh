#!/usr/bin/bash
# 一条命令跑完 Vivado 全流程：IP 打包 → block design → 综合实现/bitstream/XSA。
#
# 任一步失败立即退出（set -e）；各步骤的日志在仓库根目录的 vivado*.log 之外，还需要看
# build/vivado/bd_proj/dpm_bd.runs/*/runme.log 与 build/vivado/timing_summary.rpt。
# 注意：同一工程不能并发跑两个 Vivado（会互相覆盖 run 的步骤参数）。
set -e

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

echo "== [1/4] 打包测量 IP =="
vivado -mode batch -source "$script_dir/package_ip.tcl"
echo "== [2/4] 打包 DRP 控制器 IP =="
vivado -mode batch -source "$script_dir/package_src_ip.tcl"
echo "== [3/4] 创建 block design =="
vivado -mode batch -source "$script_dir/create_bd.tcl"
echo "== [4/4] 综合、实现、bitstream、XSA =="
vivado -mode batch -source "$script_dir/build_bitstream.tcl"
echo "== VIVADO_FLOW_DONE =="
