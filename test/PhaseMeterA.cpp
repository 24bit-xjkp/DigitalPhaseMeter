// PhaseMeter（ASIC 实现）单元测试。
//
// 被测对象是 build/chisel/PhaseMeterA/ 生成的模块（模块名仍是 PhaseMeter）。ASIC 版把停止
// 响应的判定从 sWaitStop 起晚 `asicDspCounterPipeline - 1`（当前 15）拍（见
// src/PhaseMeter.scala 的 isWaitStop），给分段计数器的段间进位留出传播时间；响应本身也相应
// 晚这么多拍。现有用例只用「翻转次数」与「长窗口」做判据（400 / 2000 个高速周期），
// 不依赖这段延迟以内的绝对时序，因此直接复用 test/PhaseMeter.cppm 的 FPGA 测试逻辑。
// 计数器没有输出对齐延迟链，ASIC 的 clear 又是异步清零，所以 forceStop 后不需要额外排空
// 拍数，clear_latency 传 0。
//
// 若 ASIC 与 FPGA 版出现行为不兼容（例如窗口内翻转次数不同），应作为设计问题报告，
// 不要在测试里放宽断言或改 RTL 来掩盖。
#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
import PhaseMeterCommon;
#include <PhaseMeterAVerilator.h>
#include <verilator_bwd.hpp>

namespace
{
    using dut_t = PhaseMeterAVerilator;
}  // namespace

TEST_SUITE("PhaseMeterA")
{
    TEST_CASE("a stop request toggles response.valid exactly once") { phase_meter_test::run_stop_response_once<dut_t>(); }

    TEST_CASE("forceStop aborts the measurement, clears the counters and allows a new measurement")
    { phase_meter_test::run_force_stop<dut_t>(0); }
}
