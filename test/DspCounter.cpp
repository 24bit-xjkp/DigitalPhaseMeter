// DspCounter（FPGA 实现）单元测试。
//
// 被测模块是 450MHz 测量域里的 32 位使能计数器（DSP48E1 累加器实现，见 src/DspCounter.scala），
// 在 PhaseMeter 里分别累计 edgeCnt 与 phaseCnt。模块级语义（使能计数、使能保持、clear 优先、
// 跨 8/16 位边界连续计数）与 ASIC 分段流水线版共用 test/DspCounter.cppm 的逻辑（端口包装、
// 激励与断言都在那边），本文件只提供 FPGA 的 Verilator DUT 类型与 0 拍输出延迟。
#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
import DspCounterCommon;
#include <DspCounterVerilator.h>
#include <verilator_bwd.hpp>

namespace
{
    using dut_t = DspCounterVerilator;
    /// 单级累加器没有分段，整数值逐拍就是当前计数，掩码取满 32 位。
    constexpr std::uint64_t block_mask{0xFFFF'FFFFzu};
}  // namespace

TEST_SUITE("DspCounter")
{
    TEST_CASE("enable counts, clear dominates enable")
    {
        // FPGA 版的计数输出是寄存器直接输出，没有额外延迟；clear 是同步清零，
        // 在下一个上升沿生效
        dsp_counter_test::run_enable_clear<dut_t>(0, 1, block_mask);
    }

    TEST_CASE("counts past the 16-bit boundary") { dsp_counter_test::run_cross_boundary<dut_t>(0, 1); }

    TEST_CASE("carry keeps propagating after enable drops") { dsp_counter_test::run_carry_after_disable<dut_t>(0); }
}
