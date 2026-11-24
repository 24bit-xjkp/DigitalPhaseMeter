// DspCounter（ASIC 实现）单元测试。
//
// 被测对象是 build/chisel/DspCounterA/ 生成的模块（模块名仍是 DspCounter），按 cfg.platform
// 选择分段流水线计数器：32 位拆成 asicDspCounterPipeline（默认 8）个 4 位段，段间进位寄存。
// 代价是输出比实际计数晚 `asicDspCounterPipeline - 1 = 7` 拍，因此期望值是 `max(0, cycles - 7)`。
//
// 模块级端口与 FPGA 版相同，端口包装、激励与断言全部在 test/DspCounter.cppm 里，本文件只把
// 输出延迟参数从 0 换成 7。延迟链不再随 io.clear 复位，因此 clear 在输出上也要晚 7 拍才可见；
// PhaseMeter 的停止响应已按同样的 7 拍延迟读数，不依赖 clear 立即反映到输出。
// clear/复位语义按生成端口的实际情况：功能复位输入是同步清零
// `io_clear`；生成 SV 里声明的隐式 `reset` 端口不参与 DspCounter 的任何逻辑（内部计数器与
// 进位寄存器由 `io_clear` 复位，输出对齐链无复位），公共逻辑因此不驱动它，也不把 `reset`
// 当作复位源。
#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
import DspCounterCommon;
#include <DspCounterAVerilator.h>
#include <verilator_bwd.hpp>

namespace
{
    using dut_t = DspCounterAVerilator;

    /// ASIC 分段流水线计数器的输出延迟：`cfg.asicDspCounterPipeline - 1`，默认 8 段 → 7 拍。
    /// 该值由 src/Utils.scala 的 Config 固定；若流水线级数改了，这里要同步更新。
    constexpr std::uint64_t output_latency{7};
}  // namespace

TEST_SUITE("DspCounterA")
{
    TEST_CASE("output trails the count by asicDspCounterPipeline-1 cycles")
    { dsp_counter_test::run_enable_clear<dut_t>(output_latency, output_latency); }

    TEST_CASE("counts past the 8-bit and 16-bit boundaries")
    { dsp_counter_test::run_cross_boundary<dut_t>(output_latency, output_latency); }
}
