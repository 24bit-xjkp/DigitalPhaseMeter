#!/usr/bin/env bash
#
# 用 Vivado 自带 xsim 仿真数字相位计顶层 IP（DigitalPhaseMeterTop）
#
# 流程：
#   1. 用 sbtn 生成综合产物 SystemVerilog（--type=syn：复位同步改用 xpm_cdc_sync_rst），
#      产物在 build/chisel/DigitalPhaseMeterTop/syn/；
#   2. xvlog 编译 XPM 原语源码、生成的 RTL（按 filelist.f 顺序）与测试激励；
#   3. xelab 细化测试顶层；
#   4. xsim 运行，按测试激励打印的结果判定成败。
#
# 用法：
#   sim/run_xsim.sh               # 生成 RTL 后编译并运行
#   sim/run_xsim.sh --no-gen      # 跳过 sbtn 生成，直接用现有 RTL
#   sim/run_xsim.sh --gui         # 运行结束后用 xsim GUI 打开波形
#   sim/run_xsim.sh --clean       # 先清理上次的仿真目录
#
# 环境变量：
#   XILINX_VIVADO   Vivado 安装路径，默认为 /home/luo/Vivado/2025.2/Vivado

set -euo pipefail

readonly ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly VIVADO_ROOT="${XILINX_VIVADO:-/home/luo/Vivado/2025.2/Vivado}"
readonly RTL_DIR="${ROOT_DIR}/build/chisel/DigitalPhaseMeterTop/syn"
readonly SIM_DIR="${ROOT_DIR}/build/xsim"
readonly TB_FILE="tb_digital_phase_meter_top"
readonly SNAPSHOT="${TB_FILE}_sim"

readonly XVLOG="${VIVADO_ROOT}/bin/xvlog"
readonly XELAB="${VIVADO_ROOT}/bin/xelab"
readonly XSIM="${VIVADO_ROOT}/bin/xsim"
readonly XPM_CDC="${VIVADO_ROOT}/data/ip/xpm/xpm_cdc/hdl/xpm_cdc.sv"
# XPM 需要全局复位模块 glbl（Vivado 提供的仿真模型）
readonly GLBL="${VIVADO_ROOT}/data/verilog/src/glbl.v"

generate_rtl=true
open_gui=false

for arg in "$@"; do
    case "${arg}" in
    --no-gen) generate_rtl=false ;;
    --gui) open_gui=true ;;
    --clean) rm -rf "${SIM_DIR}" ;;
    -h | --help)
        sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "未知参数：${arg}" >&2
        exit 2
        ;;
    esac
done

for tool in "${XVLOG}" "${XELAB}" "${XSIM}"; do
    if [[ ! -x "${tool}" ]]; then
        echo "找不到 ${tool}，请设置 XILINX_VIVADO 指向 Vivado 安装目录" >&2
        exit 1
    fi
done

if [[ "${generate_rtl}" == true ]]; then
    echo "== 生成 SystemVerilog（--type=syn）=="
    (cd "${ROOT_DIR}" && sbtn -batch "runMain DigitalPhaseMeter.EmitDigitalPhaseMeterTop --type=syn")
fi

if [[ ! -f "${RTL_DIR}/filelist.f" ]]; then
    echo "找不到 ${RTL_DIR}/filelist.f，请先运行生成入口" >&2
    exit 1
fi

mkdir -p "${SIM_DIR}"
cd "${SIM_DIR}"

echo "== 编译 XPM 原语 =="
"${XVLOG}" --sv --work work "${XPM_CDC}" "${GLBL}"

echo "== 编译生成的 RTL =="
# filelist.f 中的路径相对于生成目录，这里补全成绝对路径后再交给 xvlog
awk -v dir="${RTL_DIR}/" '{ print dir $0 }' "${RTL_DIR}/filelist.f" >"${SIM_DIR}/rtl_filelist.f"
"${XVLOG}" --sv --work work -f "${SIM_DIR}/rtl_filelist.f"

echo "== 编译测试激励 =="
"${XVLOG}" --sv --work work "${ROOT_DIR}/sim/${TB_FILE}.sv"

echo "== 细化 =="
# 生成的 RTL 没有 timescale 指令，用 --timescale 指定默认值，否则 xsim 拒绝细化
"${XELAB}" --debug typical --timescale 1ns/1ps "${TB_FILE}" glbl -s "${SNAPSHOT}"

echo "== 运行 =="
set +e
"${XSIM}" "${SNAPSHOT}" -runall 2>&1 | tee xsim.log
xsim_status=${PIPESTATUS[0]}
set -e

if grep -q "==== 测试通过" xsim.log; then
    echo "== xsim 仿真通过 =="
    if [[ "${open_gui}" == true ]]; then
        exec "${XSIM}" "${SNAPSHOT}" -gui
    fi
else
    echo "== xsim 仿真失败（xsim 退出码 ${xsim_status}），详见 ${SIM_DIR}/xsim.log ==" >&2
    exit 1
fi
