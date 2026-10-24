#!/usr/bin/env bash
#
# 用 xsim 跑「片内信号源 ↔ 测量通路」联调激励 sim/tb_src_handoff.sv（方案 A）。
#
# 被测 RTL（**与 BD 里实际用的同一份源**，不是复制品）：
#   · build/chisel/DigitalPhaseMeterTop/   —— 测量 IP（Chisel 生成产物）
#   · build/chisel/MmcmDrpController/      —— 极简 DRP 控制器（同一份产物也打包成 dpm_src_drp IP）
#   · vivado/rtl/dpm_mmcm_src.v            —— 裸 MMCME2_ADV + BUFGCE 安全启动
#   · unisims_ver 的 MMCME2_ADV            —— 厂商行为模型（相位/频率语义的真值来源）
#
# 它回答的问题是：「软件写的那套 MMCM 寄存器值，真的让输出相位差等于请求值吗？」
# 上板只能通过相位计间接观察；这里可以**直接量两路输出的周期与延迟**，再和测量通路的
# 读数对照，因此是上板前唯一能定位「编码错 / 通路错」的实验。
#
# 产物：
#   build/xsim_handoff/xsim_handoff.log     完整原始日志

#
# 用法：
#   sim/run_handoff_tb.sh                # 跑点表里的全部点（10 个频点 × 逐频点相位点 = 80 个点）
#   sim/run_handoff_tb.sh --clean        # 先清 build/xsim_handoff
#   sim/run_handoff_tb.sh --no-gen       # 不重新生成 Chisel RTL（跳过 sbtn）
#
# 整轮约 17s 墙钟（点表里每点 ≈ 40µs 闸门的仿真时间）。验收判据是日志里的
# `HANDOFF_SUMMARY points=80 ... fails=0` 且 HANDOFF_MEAS 行数 = 2×points。
#
# 环境变量：XILINX_VIVADO 默认 /home/luo/Vivado/2025.2/Vivado

set -euo pipefail

readonly ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly VIVADO_ROOT="${XILINX_VIVADO:-/home/luo/Vivado/2025.2/Vivado}"
readonly TOP_RTL_DIR="${ROOT_DIR}/build/chisel/DigitalPhaseMeterTop"
readonly DRP_RTL_DIR="${ROOT_DIR}/build/chisel/MmcmDrpController"
readonly MMCM_RTL="${ROOT_DIR}/vivado/rtl/dpm_mmcm_src.v"
readonly TB_NAME="tb_src_handoff"
readonly SIM_ROOT="${ROOT_DIR}/build/xsim_handoff"

readonly XVLOG="${VIVADO_ROOT}/bin/xvlog"
readonly XELAB="${VIVADO_ROOT}/bin/xelab"
readonly XSIM="${VIVADO_ROOT}/bin/xsim"
readonly GLBL="${VIVADO_ROOT}/data/verilog/src/glbl.v"

do_gen=1
for arg in "$@"; do
    case "${arg}" in
    --clean) rm -rf "${SIM_ROOT}" ;;
    --no-gen) do_gen=0 ;;
    -h | --help)
        sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "未知参数：${arg}" >&2
        exit 2
        ;;
    esac
done

for tool in "${XVLOG}" "${XELAB}" "${XSIM}"; do
    [[ -x "${tool}" ]] || {
        echo "找不到 ${tool}，请设置 XILINX_VIVADO" >&2
        exit 1
    }
done

if [[ ${do_gen} -eq 1 ]]; then
    # 与其它生成入口一致：默认每次都重新生成，避免源文件改过而 build/chisel/ 里
    # 还是旧产物（只判断目录是否存在会跳过生成，用旧 RTL 跑出来的结论不可信）。
    for entry in "EmitDigitalPhaseMeterTop:${TOP_RTL_DIR}" "EmitMmcmDrpController:${DRP_RTL_DIR}"; do
        main="${entry%%:*}"
        echo "== 生成 ${main} RTL =="
        (cd "${ROOT_DIR}" && sbtn -batch "runMain DigitalPhaseMeter.${main}")
    done
fi

for f in "${MMCM_RTL}" "${GLBL}"; do
    [[ -f "${f}" ]] || {
        echo "缺少源文件：${f}" >&2
        exit 1
    }
done
for d in "${TOP_RTL_DIR}" "${DRP_RTL_DIR}"; do
    [[ -f "${d}/filelist.f" ]] || {
        echo "缺少生成产物：${d}（先跑 sbtn）" >&2
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

gen_files=()
for d in "${TOP_RTL_DIR}" "${DRP_RTL_DIR}"; do
    while IFS= read -r line; do
        [[ -z "${line}" ]] && continue
        gen_files+=("${d}/${line}")
    done <"${d}/filelist.f"
done

echo "== 编译 RTL（dpm_top ${TOP_RTL_DIR##*/} + DRP 控制器 + 裸 MMCM + glbl，共 ${#gen_files[@]} 个生成文件）=="
"${XVLOG}" --sv --work work "${gen_files[@]}" "${MMCM_RTL}" "${GLBL}"

echo "== 编译测试激励 =="
"${XVLOG}" --sv --work work "${ROOT_DIR}/sim/${TB_NAME}.sv"

echo "== 细化 =="
# 不能加 -L unisim（VHDL 版 unisim 与 Verilog unisims_ver 冲突）；MMCME2_ADV 走 unisims_ver。
"${XELAB}" --debug typical --timescale 1ns/1ps "${TB_NAME}" glbl -s "${SNAPSHOT}" -L unisims_ver

echo "== 运行（xsim 跑 MMCM 行为模型很慢：锁一次约 1~2ms 仿真时间）=="
set +e
"${XSIM}" "${SNAPSHOT}" -runall 2>&1 | tee "${LOG}"
xsim_status=${PIPESTATUS[0]}
set -e

mkdir -p "$(dirname "${LOG}")"

meas_lines=$(grep -c '^HANDOFF_MEAS ' "${LOG}" || true)
summary=$(grep -m1 '^HANDOFF_SUMMARY ' "${LOG}" || true)
points=$(sed -n 's/.*points=\([0-9]*\).*/\1/p' <<<"${summary}")
over180=$(sed -n 's/.*over180=\([0-9]*\).*/\1/p' <<<"${summary}")
fails=$(sed -n 's/.*fails=\([0-9]*\).*/\1/p' <<<"${summary}")
# 每个点打印 2 行 HANDOFF_MEAS（相位行 + 计数行），因此行数必须等于 2×points；
# 再要求 SUMMARY 明确 fails=0 —— 只数「有没有输出」会把中途 $fatal 当成成功。
if grep -q '^HANDOFF_DONE ' "${LOG}" && [[ -n "${points}" ]] && [[ "${fails}" == "0" ]] &&
    [[ "${meas_lines}" -eq $((2 * points)) ]] && ! grep -q '^HANDOFF_FAIL' "${LOG}"; then
    echo "== 联调完成：${points} 个点（≥180° 的 ${over180} 个）、HANDOFF_MEAS ${meas_lines} 行；原始日志 ${LOG} =="
    grep -E '^HANDOFF_(INFO|SUMMARY|DONE)' "${LOG}"
    grep -c '^HANDOFF_MEAS ' "${LOG}" >/dev/null
else
    echo "== 仿真失败（xsim 退出码 ${xsim_status}；points=${points:-?} fails=${fails:-?} HANDOFF_MEAS 行数 ${meas_lines}），详见 ${LOG} ==" >&2
    grep -nE 'ERROR|Error|FATAL|\$fatal|HANDOFF_ERROR|HANDOFF_FAIL' "${LOG}" | head -20 >&2 || true
    exit 1
fi
