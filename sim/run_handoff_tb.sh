#!/usr/bin/env bash
#
# 用 xsim 跑「片内信号源 ↔ 测量通路」联调激励 sim/tb_handoff_src_measure.sv。
#
# 与 run_xsim.sh 的区别：那份激励用**理想信号源**驱动 dpm_top，本脚本把 BD 里的
# **真实 clk_wiz_src IP（MMCM + DRP）**接到 dpm_top 的 srcSig1/srcSig2 上，
# 由激励写 DRP 设定频率（当前 bitstream 下相位不可改，见 docs/verify-drp.md），
# 再用 dpm_top 自己的 AXI 寄存器口读回测量结果，验证「真实 MMCM 输出 → 测量通路」这条链路。
#
# 被测 RTL：build/chisel/DigitalPhaseMeterTop/（单一产物：复位同步是纯 RTL 的 ResetSynchronizer，
#     产物里没有 XPM 原语，xsim 与 Verilator 共用同一份）
#     · 若目录不存在，先跑：sbtn -batch "runMain DigitalPhaseMeter.EmitDigitalPhaseMeterTop"
# 片内源 IP：build/vivado/bd_proj/.../ip/dpm_system_clk_wiz_src_0（BD 生成物）
#     · 若目录不存在，先跑：vivado -mode batch -source vivado/create_bd.tcl
# DSP 域时钟：BD 里的 clk_wiz_0（复用同一份生成源，400MHz）
#
# 产物：
#   build/xsim_handoff/xsim_handoff.log     完整原始日志
#   spec/golden/handoff_meas.txt            留档副本（用 .txt：仓库 .gitignore 有 *.log）
#
# 用法：
#   sim/run_handoff_tb.sh                # 完整联调（7 个冻结频点）
#   sim/run_handoff_tb.sh --clean        # 先清 build/xsim_handoff
#   sim/run_handoff_tb.sh --no-gen       # 不重新生成 dpm_top RTL（跳过 sbtn）
#
# 环境变量：XILINX_VIVADO 默认 /home/luo/Vivado/2025.2/Vivado

set -euo pipefail

readonly ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly VIVADO_ROOT="${XILINX_VIVADO:-/home/luo/Vivado/2025.2/Vivado}"
readonly BD_GEN_DIR="${ROOT_DIR}/build/vivado/bd_proj/dpm_bd.gen/sources_1/bd/dpm_system/ip"
readonly SRC_IP_DIR="${BD_GEN_DIR}/dpm_system_clk_wiz_src_0"
readonly DSP_IP_DIR="${BD_GEN_DIR}/dpm_system_clk_wiz_0_0" # DSP 域时钟用的 clk_wiz（无 DRP）
readonly TOP_RTL_DIR="${ROOT_DIR}/build/chisel/DigitalPhaseMeterTop"
readonly TB_NAME="tb_handoff_src_measure"
readonly SIM_ROOT="${ROOT_DIR}/build/xsim_handoff"
readonly RAW_LOG_DIR="${ROOT_DIR}/spec/golden"

readonly XVLOG="${VIVADO_ROOT}/bin/xvlog"
readonly XVHDL="${VIVADO_ROOT}/bin/xvhdl"
readonly XELAB="${VIVADO_ROOT}/bin/xelab"
readonly XSIM="${VIVADO_ROOT}/bin/xsim"
readonly GLBL="${VIVADO_ROOT}/data/verilog/src/glbl.v"
readonly CLK_WIZ_INC="${VIVADO_ROOT}/data/ip/xilinx/clk_wiz_v6_0"

do_gen=1
for arg in "$@"; do
    case "${arg}" in
    --clean) rm -rf "${SIM_ROOT}" ;;
    --no-gen) do_gen=0 ;;
    -h | --help)
        sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "未知参数：${arg}" >&2
        exit 2
        ;;
    esac
done

for tool in "${XVLOG}" "${XVHDL}" "${XELAB}" "${XSIM}"; do
    [[ -x "${tool}" ]] || {
        echo "找不到 ${tool}，请设置 XILINX_VIVADO" >&2
        exit 1
    }
done

if [[ ${do_gen} -eq 1 && ! -d "${TOP_RTL_DIR}" ]]; then
    echo "== 生成 dpm_top RTL =="
    (cd "${ROOT_DIR}" && sbtn -batch "runMain DigitalPhaseMeter.EmitDigitalPhaseMeterTop")
fi

for d in "${TOP_RTL_DIR}" "${SRC_IP_DIR}" "${DSP_IP_DIR}"; do
    [[ -d "${d}" ]] || {
        echo "缺少目录：${d}（先跑 create_bd.tcl / sbtn）" >&2
        exit 1
    }
done

readonly RUN_DIR="${SIM_ROOT}/run"
readonly SNAPSHOT="${TB_NAME}_sim"
readonly LOG="${RUN_DIR}/xsim_handoff.log"
rm -rf "${RUN_DIR}"
mkdir -p "${RUN_DIR}"
cd "${RUN_DIR}"

echo "== 运行目录：${RUN_DIR} =="

# ---- 1) 片内源与 DSP 时钟两个 clk_wiz IP 的 VHDL 源 ----
# 两个 IP 的实体名不同（前缀不同），但内部包名也各带前缀，因此都能编进同一个 work 库。
vhdl_files=(
    "${SRC_IP_DIR}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_proc_common_pkg.vhd"
    "${SRC_IP_DIR}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_conv_funs_pkg.vhd"
    "${SRC_IP_DIR}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_family_support.vhd"
    "${SRC_IP_DIR}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_family.vhd"
    "${SRC_IP_DIR}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_pselect_f.vhd"
    "${SRC_IP_DIR}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_soft_reset.vhd"
    "${SRC_IP_DIR}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_ipif_pkg.vhd"
    "${SRC_IP_DIR}/axi_lite_ipif_v1_01_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_address_decoder.vhd"
    "${SRC_IP_DIR}/axi_lite_ipif_v1_01_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_slave_attachment.vhd"
    "${SRC_IP_DIR}/axi_lite_ipif_v1_01_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_axi_lite_ipif.vhd"
    "${SRC_IP_DIR}/dpm_system_clk_wiz_src_0_clk_wiz_drp.vhd"
    "${SRC_IP_DIR}/dpm_system_clk_wiz_src_0_axi_clk_config.vhd"
)
for f in "${vhdl_files[@]}"; do
    [[ -f "${f}" ]] || {
        echo "缺少 VHDL 源：${f}" >&2
        exit 1
    }
done
echo "== 编译片内源 clk_wiz 的 VHDL 源（${#vhdl_files[@]} 个；DSP 域 clk_wiz 无 DRP，纯 Verilog）=="
"${XVHDL}" --work work "${vhdl_files[@]}"

echo "== 编译 clk_wiz Verilog 源 + glbl =="
# glbl 是 unisim 原语（MMCM）仿真所需；生成的 dpm_top 已不含 XPM，不需要再编 xpm_cdc.sv
"${XVLOG}" --sv --work work -i "${CLK_WIZ_INC}" \
    "${SRC_IP_DIR}/dpm_system_clk_wiz_src_0_clk_wiz.v" \
    "${SRC_IP_DIR}/dpm_system_clk_wiz_src_0_mmcm_pll_drp.v" \
    "${SRC_IP_DIR}/dpm_system_clk_wiz_src_0.v" \
    "${DSP_IP_DIR}/dpm_system_clk_wiz_0_0_clk_wiz.v" \
    "${DSP_IP_DIR}/dpm_system_clk_wiz_0_0.v" \
    "${GLBL}"

echo "== 编译 dpm_top 生成产物 RTL（按 filelist.f 顺序）=="
top_files=()
while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    top_files+=("${TOP_RTL_DIR}/${line}")
done <"${TOP_RTL_DIR}/filelist.f"
"${XVLOG}" --sv --work work "${top_files[@]}"

echo "== 编译测试激励 =="
"${XVLOG}" --sv --work work "${ROOT_DIR}/sim/${TB_NAME}.sv"

echo "== 细化 =="
# 注意：不能加 -L unisim（VHDL 版 unisim 的 generic 类型与生成的 clk_wiz.v 字符串参数冲突）
"${XELAB}" --debug typical --timescale 1ns/1ps "${TB_NAME}" glbl -s "${SNAPSHOT}" \
    -L unisims_ver

echo "== 运行 =="
set +e
"${XSIM}" "${SNAPSHOT}" -runall 2>&1 | tee "${LOG}"
xsim_status=${PIPESTATUS[0]}
set -e

mkdir -p "${RAW_LOG_DIR}"
cp "${LOG}" "${RAW_LOG_DIR}/handoff_meas.txt"

meas_lines=$(grep -c '^HANDOFF_MEAS ' "${LOG}" || true)
if grep -q '^HANDOFF_DONE ' "${LOG}" && [[ "${meas_lines}" -gt 0 ]]; then
    echo "== 联调完成：HANDOFF_MEAS 行数 ${meas_lines}；原始日志 ${LOG}；留档 ${RAW_LOG_DIR}/handoff_meas.txt =="
    grep -E '^HANDOFF_(INFO|MEAS|DONE)' "${LOG}"
else
    echo "== 仿真失败（xsim 退出码 ${xsim_status}，HANDOFF_MEAS 行数 ${meas_lines}），详见 ${LOG} ==" >&2
    grep -nE 'ERROR|Error|FATAL|\$fatal|HANDOFF_ERROR' "${LOG}" | head -20 >&2 || true
    exit 1
fi
