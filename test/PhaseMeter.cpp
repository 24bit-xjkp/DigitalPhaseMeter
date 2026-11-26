// PhaseMeter（FPGA 实现）单元测试。
//
// 测试逻辑（停止握手只翻转一次、forceStop 中止测量）与 ASIC 版共用 test/PhaseMeter.cppm；
// 本文件只提供 FPGA 的 Verilator DUT 类型。ASIC 版的 response 晚
// `asicDspCounterPipeline - 1` 拍，观测窗口足够大，因此同一套断言对两个实现都成立。
#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
import PhaseMeterCommon;
#include <PhaseMeterVerilator.h>
#include <verilator_bwd.hpp>

namespace
{
    using dut_t = PhaseMeterVerilator;
}  // namespace

TEST_SUITE("PhaseMeter")
{
    TEST_CASE("a stop request toggles response.valid exactly once") { phase_meter_test::run_stop_response_once<dut_t>(); }

    TEST_CASE("forceStop aborts the measurement, clears the counters and allows a new measurement")
    { phase_meter_test::run_force_stop<dut_t>(0); }
}
