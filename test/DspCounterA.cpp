// DspCounter（ASIC 实现）单元测试。
//
// 被测对象是 build/chisel/DspCounterA/ 生成的模块（模块名仍是 DspCounter），按 cfg.platform
// 选择分段流水线计数器：32 位按 asicDspCounterPipeline（当前 16）拆段，每段
// ceil(32 / 16) = 2 位，段间进位寄存。输出不带对齐延迟链：计数期间只有最低段逐拍准确，
// 高段要等进位逐级传来。
//
// 模块级端口与 FPGA 版相同，端口包装、激励与断言全部在 test/DspCounter.cppm 里，本文件提供
// 平台差异参数。ASIC 版的 `io_value` 是各段的**原始**寄存器：没有输出对齐延迟链，段间进位
// 要传播 `asicDspCounterPipeline - 1` 拍，所以撤销使能后要等 15 拍再读最终值（相位计也按
// 同样的延迟读取）。`io_clear` 接在计数寄存器的**异步复位端**（sky130 `dfrtp` 的
// RESET_B），clear 置起后同拍清零。
// 生成 SV 里声明的隐式 `reset` 端口不参与 DspCounter 的任何逻辑（内部计数器由 `io_clear`
// 复位），公共逻辑因此不驱动它，也不把 `reset` 当作复位源。
#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
import DspCounterCommon;
#include <DspCounterAVerilator.h>
#include <verilator_bwd.hpp>

namespace
{
    using dut_t = DspCounterAVerilator;

    /// 撤销使能后等段间进位传播完所需的拍数：`cfg.asicDspCounterPipeline - 1`，当前 16 段 → 15 拍。
    /// 该值由 src/Utils.scala 的 Config 固定；若流水线级数改了，这里要同步更新
    /// （等多了不影响结果，等少了会读到还没进完位的中间值）。
    constexpr std::uint64_t settle_cycles{15};

    /// ASIC 的 clear 接在计数寄存器的异步复位端，置起后同拍清零。
    constexpr std::uint64_t clear_edges{0};

    /// 最低段的位掩码：每段 `ceil(32 / 16) = 2` 位，计数期间只有这一段逐拍准确。
    constexpr std::uint64_t block_mask{(1zu << 2zu) - 1zu};
}  // namespace

TEST_SUITE("DspCounterA")
{
    TEST_CASE("counts immediately, settles after enable drops, clear is async")
    { dsp_counter_test::run_enable_clear<dut_t>(settle_cycles, clear_edges, block_mask); }

    TEST_CASE("counts past the 8-bit and 16-bit boundaries")
    { dsp_counter_test::run_cross_boundary<dut_t>(settle_cycles, clear_edges); }

    TEST_CASE("carry keeps propagating after enable drops") { dsp_counter_test::run_carry_after_disable<dut_t>(settle_cycles); }
}
