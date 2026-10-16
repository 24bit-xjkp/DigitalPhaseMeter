#!/usr/bin/env bash
#
#
# 【已作废·仅作历史记录】本脚本依赖已被方案 A 取代的 `clk_wiz_src` IP（已从 create_bd.tcl 删除），
# 现在**跑不起来**；产出见 docs/verify-drp.md 与 docs/verify-report.md §3。
# 当前方案的联调见 sim/run_handoff_tb.sh。
# 用 Vivado 自带 xsim 跑 clk_wiz_src（MMCM + 动态重配置）的 DRP 位域语义实测激励
# sim/tb_clk_wiz_src_drp.sv。
#
# 被测对象是 BD 生成目录里的 clk_wiz IP 本体（不是替代模型），参考时钟与 AXI 时钟都是
# 50MHz。激励通过 AXI4-Lite 写 DRP 寄存器文件（0x200 起）、写 Load（0x25C）触发重配置，
# 然后用 1ps 精度的时间戳实测 clk_out1 / clk_out2 的周期与相位差。
#
# 两种源码模式（同一份 IP 的两种等价来源，用于交叉验证）：
#   --rtl      （默认）编译 BD 生成目录里的 RTL 源：VHDL（axi_clk_config / clk_wiz_drp /
#              ipif / proc_common）+ Verilog（顶层 / clk_wiz / mmcm_pll_drp），混合语言。
#   --netlist  编译同一 IP 的功能仿真网表 *_sim_netlist.v（纯 Verilog 门级网表，
#              用 LUT/FF/MMCME2_ADV 原语，自带 glbl）。
#
# 产物与留档：
#   build/xsim_drp/<mode>/xsim_drp.log  本次运行的完整原始日志（stdout 全量）
#   spec/golden/drp_meas_<mode>.txt     各模式原始输出副本（随交付物留档；
#                                       用 .txt 而不是 .log，因为仓库 .gitignore 有 *.log）
#
# 用法：
#   sim/run_drp_tb.sh                 # RTL 模式
#   sim/run_drp_tb.sh --netlist       # 网表模式
#   sim/run_drp_tb.sh --clean         # 先清掉 build/xsim_drp
#   sim/run_drp_tb.sh --ip-dir <dir>  # 指定生成的 IP 目录
#
# 环境变量：
#   XILINX_VIVADO  Vivado 安装路径，默认 /home/luo/Vivado/2025.2/Vivado

set -euo pipefail

readonly ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly VIVADO_ROOT="${XILINX_VIVADO:-/home/luo/Vivado/2025.2/Vivado}"
readonly DEFAULT_IP_DIR="${ROOT_DIR}/build/vivado/bd_proj/dpm_bd.gen/sources_1/bd/dpm_system/ip/dpm_system_clk_wiz_src_0"
readonly TB_NAME="tb_clk_wiz_src_drp"
readonly SIM_ROOT="${ROOT_DIR}/build/xsim_drp"
readonly RAW_LOG_DIR="${ROOT_DIR}/spec/golden"

readonly XVLOG="${VIVADO_ROOT}/bin/xvlog"
readonly XELAB="${VIVADO_ROOT}/bin/xelab"
readonly XSIM="${VIVADO_ROOT}/bin/xsim"
readonly XVHDL="${VIVADO_ROOT}/bin/xvhdl"
readonly GLBL="${VIVADO_ROOT}/data/verilog/src/glbl.v"
# mmcm_pll_drp.v 里 `include "mmcm_pll_drp_func_7s_mmcm.vh"，源文件在 IP 安装目录里
readonly CLK_WIZ_INC="${VIVADO_ROOT}/data/ip/xilinx/clk_wiz_v6_0"

mode="rtl"
ip_dir="${DEFAULT_IP_DIR}"

for arg in "$@"; do
    case "${arg}" in
    --rtl) mode="rtl" ;;
    --netlist) mode="netlist" ;;
    --clean) rm -rf "${SIM_ROOT}" ;;
    --ip-dir)
        shift
        ip_dir="$1"
        ;;
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

for tool in "${XVLOG}" "${XELAB}" "${XSIM}" "${XVHDL}"; do
    if [[ ! -x "${tool}" ]]; then
        echo "找不到 ${tool}，请设置 XILINX_VIVADO 指向 Vivado 安装目录" >&2
        exit 1
    fi
done

if [[ ! -d "${ip_dir}" ]]; then
    echo "找不到生成的 clk_wiz IP 目录：${ip_dir}" >&2
    echo "先跑 vivado/create_bd.tcl 生成 BD，或用 --ip-dir 指定" >&2
    exit 1
fi

readonly RUN_DIR="${SIM_ROOT}/${mode}"
readonly SNAPSHOT="${TB_NAME}_${mode}_sim"
readonly LOG="${RUN_DIR}/xsim_drp.log"

rm -rf "${RUN_DIR}"
mkdir -p "${RUN_DIR}"
cd "${RUN_DIR}"

echo "== 模式：${mode}；IP 目录：${ip_dir} =="
echo "== 运行目录：${RUN_DIR} =="

if [[ "${mode}" == "rtl" ]]; then
    # VHDL 源：先包（package），再实体；顺序即依赖顺序
    vhdl_files=(
        "${ip_dir}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_proc_common_pkg.vhd"
        "${ip_dir}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_conv_funs_pkg.vhd"
        "${ip_dir}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_family_support.vhd"
        "${ip_dir}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_family.vhd"
        "${ip_dir}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_pselect_f.vhd"
        "${ip_dir}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_soft_reset.vhd"
        "${ip_dir}/proc_common_v3_00_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_ipif_pkg.vhd"
        "${ip_dir}/axi_lite_ipif_v1_01_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_address_decoder.vhd"
        "${ip_dir}/axi_lite_ipif_v1_01_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_slave_attachment.vhd"
        "${ip_dir}/axi_lite_ipif_v1_01_a/hdl/src/vhdl/dpm_system_clk_wiz_src_0_axi_lite_ipif.vhd"
        "${ip_dir}/dpm_system_clk_wiz_src_0_clk_wiz_drp.vhd"
        "${ip_dir}/dpm_system_clk_wiz_src_0_axi_clk_config.vhd"
    )
    for f in "${vhdl_files[@]}"; do
        if [[ ! -f "${f}" ]]; then
            echo "缺少 VHDL 源：${f}" >&2
            exit 1
        fi
    done
    echo "== 编译 VHDL 源（${#vhdl_files[@]} 个文件）=="
    "${XVHDL}" --work work "${vhdl_files[@]}"

    echo "== 编译 Verilog 源（顶层 + clk_wiz + mmcm_pll_drp + glbl）=="
    "${XVLOG}" --work work -i "${CLK_WIZ_INC}" \
        "${ip_dir}/dpm_system_clk_wiz_src_0_clk_wiz.v" \
        "${ip_dir}/dpm_system_clk_wiz_src_0_mmcm_pll_drp.v" \
        "${ip_dir}/dpm_system_clk_wiz_src_0.v" \
        "${GLBL}"

    echo "== 编译测试激励 =="
    "${XVLOG}" --sv --work work "${ROOT_DIR}/sim/${TB_NAME}.sv"

    echo "== 细化 =="
    # MMCME2_ADV/BUFG 等原语取自 unisims_ver 预编译库。
    # 注意：不能再加 -L unisim —— VHDL 版 unisim 里同名实体的 generic 是 boolean，
    # 而生成的 clk_wiz.v 按字符串传参（"FALSE"/"TRUE"），xelab 会报
    # [VRFC 10-1537] value ... is out of target constraint range false to true。
    # 本 IP 的 VHDL 侧只有 `use UNISIM.VCOMPONENTS.ALL` 声明，没有例化任何原语。
    "${XELAB}" --debug typical --timescale 1ns/1ps "${TB_NAME}" glbl -s "${SNAPSHOT}" \
        -L unisims_ver
else
    netlist="${ip_dir}/dpm_system_clk_wiz_src_0_sim_netlist.v"
    if [[ ! -f "${netlist}" ]]; then
        echo "缺少功能仿真网表：${netlist}" >&2
        exit 1
    fi
    echo "== 编译功能仿真网表（自带 glbl）=="
    "${XVLOG}" --work work "${netlist}"

    echo "== 编译测试激励 =="
    "${XVLOG}" --sv --work work "${ROOT_DIR}/sim/${TB_NAME}.sv"

    echo "== 细化 =="
    # 网表里的原语（LUT*/FDRE/MMCME2_ADV/BUFG*）来自 unisims_ver 预编译库
    "${XELAB}" --debug typical --timescale 1ns/1ps "${TB_NAME}" glbl -s "${SNAPSHOT}" \
        -L unisims_ver
fi

echo "== 运行 =="
set +e
"${XSIM}" "${SNAPSHOT}" -runall 2>&1 | tee "${LOG}"
xsim_status=${PIPESTATUS[0]}
set -e

# 原始输出按模式留档（两种模式的结果应完全一致，见 docs/verify-drp.md）。
# 用 .txt 后缀：仓库 .gitignore 有 *.log，留档件要能随交付物一起提交。
mkdir -p "${RAW_LOG_DIR}"
RAW_LOG_COPY="${RAW_LOG_DIR}/drp_meas_${mode}.txt"
cp "${LOG}" "${RAW_LOG_COPY}"

meas_lines=$(grep -c '^DRP_MEAS ' "${LOG}" || true)
if grep -q '^DRP_TB_DONE ' "${LOG}" && [[ "${meas_lines}" -gt 0 ]]; then
    echo "== 仿真完成：DRP_MEAS 行数 ${meas_lines}；原始日志 ${LOG}；留档副本 ${RAW_LOG_COPY} =="
else
    echo "== 仿真失败（xsim 退出码 ${xsim_status}，DRP_MEAS 行数 ${meas_lines}），详见 ${LOG} ==" >&2
    grep -nE 'ERROR|Error|FATAL|\$fatal|超时' "${LOG}" | head -20 >&2 || true
    exit 1
fi
