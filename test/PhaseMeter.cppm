module;
// PhaseMeter 公共测试逻辑（FPGA / ASIC 两个实现共用）。
//
// 本模块**不包含任何 Verilator 生成头**，只导出与具体 DUT 无关的部分：
//   - port_t：端口包装。两个实现的端口完全相同，因此结构体本身不是模板，只有构造函数
//     需要 DUT 类型（由实参推导）；
//   - 观测 helper 与各用例的激励/断言：普通协程，只操作 port_t，与 DUT 类型无关，
//     因此只编译一次；
//   - run_<用例>：唯一还需要 DUT 类型的地方（dut_context 是模板），只负责构造上下文、
//     注册时钟与激励任务、跑到结束并汇总/打印结果。
// 具体测试（test/PhaseMeter.cpp / test/PhaseMeterA.cpp）只提供 DUT 类型。
//
// 两个实现的差别只在 response 的发送时机：ASIC 的响应比 FPGA 版晚
// `asicDspCounterPipeline - 1` 拍（给分段计数器的段间进位传播留时间），现有用例的观测窗口
// （400 / 2000 个高速周期）远大于这段延迟，因此同一套断言仍然成立。
//
// doctest 断言宏在这里展开（模块内部使用），因此公共模块的消费者仍需 import
// verilator_utils.full 以获得 doctest 实体。
#include <array>
#include <cstddef>
#include <cstdint>
#include <doctest_macros.hpp>
export module PhaseMeterCommon;
import verilator_utils.full;

// 模块内部使用：下面不再写 verilator_utils:: 前缀（using 指令不导出，不影响消费者）。
using namespace verilator_utils;

export namespace phase_meter_test
{
    /// 高速测量时钟周期（测试时基取整数纳秒以便精确激励；实际芯片为 450MHz）
    constexpr inline auto dsp_period{4_ns};
    /// 被测信号周期与相位差：两路同频、相位差半个周期
    constexpr inline auto sig_period{400_ns};
    constexpr inline auto sig_phase{200_ns};
    /// 停止响应的观测窗口：2000 个高速周期（8us）。一次停止握手只有几个周期，
    /// 而一个 sig2 周期是 400ns；历史上的缺陷会在窗口内对每个 sig2 上升沿翻转一次
    /// （窗口内约 20 次）。该窗口对 ASIC 的 `asicDspCounterPipeline - 1` 拍延迟同样足够。
    constexpr inline std::uint64_t observe_cycles{2000zu};
    /// 每轮完整握手的轮数：第二轮验证「每次启动都会重新武装发送逻辑」
    constexpr inline std::size_t rounds{2zu};
    /// 上下文选项：时间精度取 ns，所有等待时长都是整数纳秒。
    constexpr inline dut_context_option ctx_opt{.time_precision = verilator_time_unit::ns};

    /// DUT 端口包装。
    ///
    /// 字段与 DUT 类型无关，所以这里不是模板；只有构造函数需要具体 DUT 类型。
    struct port_t
    {
        bit_slice<verilator::CData> clock;
        bit_slice<verilator::CData> reset;
        bit_slice<verilator::CData> io_sig_sig1;
        bit_slice<verilator::CData> io_sig_sig2;
        bit_slice<verilator::CData> io_request;
        bit_slice<verilator::CData> io_response_ready;
        bit_slice<verilator::CData> io_force_stop;
        bit_slice<verilator::CData> io_response_valid;
        vector_slice<verilator::IData> io_response_bits_edge_cnt;
        vector_slice<verilator::IData> io_response_bits_phase_cnt;

        template <typename dut_t>
        explicit port_t(dut_t& dut) :
            clock{dut.clock}, reset{dut.reset}, io_sig_sig1{dut.io_sig_sig1, boolean}, io_sig_sig2{dut.io_sig_sig2, boolean},
            io_request{dut.io_request, boolean}, io_response_ready{dut.io_response_ready, boolean},
            io_force_stop{dut.io_forceStop, boolean}, io_response_valid{dut.io_response_valid, boolean},
            io_response_bits_edge_cnt{dut.io_response_bits_edgeCnt, 32zu, dec_unsigned},
            io_response_bits_phase_cnt{dut.io_response_bits_phaseCnt, 32zu, dec_unsigned}
        {
        }
    };

    /// 在接下来 `cycles` 个高速周期内统计 response.valid 的翻转次数
    [[nodiscard]] task<std::uint64_t> count_response_toggles(const port_t& port, std::uint64_t cycles)
    {
        std::uint64_t count{0};
        auto previous{static_cast<std::uint64_t>(port.io_response_valid)};
        for(auto cycle{0zu}; cycle != static_cast<std::size_t>(cycles); ++cycle)
        {
            co_await wait_verify(port.clock);
            const auto current{static_cast<std::uint64_t>(port.io_response_valid)};
            if(current != previous) { ++count; }
            previous = current;
        }
        co_return count;
    }

    /// 空转若干高速周期
    [[nodiscard]] task<void> wait_cycles(const port_t& port, std::size_t cycles)
    {
        for(auto cycle{0zu}; cycle != cycles; ++cycle) { co_await wait_verify(port.clock); }
        co_return;
    }

    /// 停止握手用例的观测结果。断言在协程内即时检查，这里留给用例入口在仿真结束后
    /// 再汇总一次，避免协程提前退出时只看得到第一处失败。
    struct stop_response_result
    {
        std::array<std::uint64_t, rounds> start_toggles{};
        std::array<std::uint64_t, rounds> stop_toggles{};
    };

    /// 停止握手缺陷回归：每个请求（启动 / 停止）只产生一次 response.valid 电平翻转。
    ///
    /// PhaseMeter 通过「电平翻转」把 response.valid 送回慢域，慢域用同步链检测翻转沿。
    /// 这个协议要求**每个事件只翻转一次**——如果 sWaitStop 期间对每个 sig2 上升沿都翻转
    /// 一次，当 sig2 频率接近/超过慢时钟的一半时，慢域每拍采样到的电平可能保持不变，
    /// 停止响应送不到慢域，控制器就永远停在等待停止响应的状态（STATUS.busy 卡死）。
    [[nodiscard]] task<void> stop_response(port_t& port, stop_response_result& result)
    {
        port.io_request = 0;
        port.io_response_ready = 0;
        // forceStop 在本用例里始终无效；显式驱动，避免依赖端口的初始值
        port.io_force_stop = 0;
        co_await wait_reset_finish(port.reset);

        for(auto round{0zu}; round != rounds; ++round)
        {
            // 启动请求：慢域发送端翻转一次电平（PhaseMeter 在 sIdle 收到一个边沿）
            co_await wait_stimulate(port.clock);
            port.io_request = 1;

            // 启动响应只应翻转一次（启动握手本身不是本次缺陷所在，这里顺带守住）。
            // ASIC 版的响应晚 `asicDspCounterPipeline - 1` 拍，但 400 个周期的窗口足够覆盖。
            result.start_toggles[round] = co_await count_response_toggles(port, 400zu);
            CHECK_EQ(result.start_toggles[round], 1zu);

            // 慢域收到启动响应后的应答（PhaseMeter 在 sMeasuring 期间忽略它）
            co_await wait_stimulate(port.clock);
            port.io_response_ready = 1;
            co_await wait_cycles(port, 100zu);

            // 停止请求：再翻转一次电平
            co_await wait_stimulate(port.clock);
            port.io_request = 0;

            // 故意不应答停止响应，观察一个长窗口：每个停止请求只应产生一次翻转。
            // 修复前这里会对每个 sig2 上升沿再翻转一次（窗口内约 20 次）。
            result.stop_toggles[round] = co_await count_response_toggles(port, observe_cycles);
            CHECK_EQ(result.stop_toggles[round], 1zu);

            // 完成本轮停止握手（慢域应答）
            co_await wait_stimulate(port.clock);
            port.io_response_ready = 0;
            co_await wait_cycles(port, 100zu);
        }

        co_await eval_finish();
    }

    /// forceStop 中止测量：计数器被清零、不产生停止应答，并且可以从 idle 重新发起测量。
    ///
    /// @param clear_latency clear 在计数器输出上完全排空所需的延迟拍数：计数器没有输出对齐
    ///                      延迟链，ASIC 又是异步清零，因此两个实现都传 0；参数保留是为了
    ///                      给这类平台差异留出可调余量。
    [[nodiscard]] task<void> force_stop(port_t& port, std::uint64_t clear_latency)
    {
        port.io_request = 0;
        port.io_response_ready = 0;
        port.io_force_stop = 0;
        co_await wait_reset_finish(port.reset);

        // 启动一次测量并应答启动握手
        co_await wait_stimulate(port.clock);
        port.io_request = 1;
        CHECK_EQ(co_await count_response_toggles(port, 400zu), 1zu);
        co_await wait_stimulate(port.clock);
        port.io_response_ready = 1;

        // 让计数器累计一段时间：相位差 200ns = 50 个高速周期，两个计数器都应非零。
        // ASIC 版的停止响应晚 `asicDspCounterPipeline - 1` 拍，200 拍后仍远大于非零门槛。
        co_await wait_cycles(port, 200zu);
        CHECK_GT(static_cast<std::uint64_t>(port.io_response_bits_edge_cnt), 0zu);
        CHECK_GT(static_cast<std::uint64_t>(port.io_response_bits_phase_cnt), 0zu);

        // 强制停止：接收端看到 forceStop 电平翻转后回到 idle，计数器被清零。
        // 与停止请求不同，这里不依赖被测信号的上升沿，也不产生停止应答。
        // 清零经 1 拍 clearCnt（ASIC 是异步复位，清零同拍生效）；再留出 forceStop 跨域
        // 同步与 clear_latency 排空时间，避免把仍显示旧值的输出误判为清零失败。
        port.io_force_stop = 1;
        co_await wait_cycles(port, clear_latency + 8zu);
        CHECK_EQ(static_cast<std::uint64_t>(port.io_response_bits_edge_cnt), 0zu);
        CHECK_EQ(static_cast<std::uint64_t>(port.io_response_bits_phase_cnt), 0zu);

        // 被中止的测量不会产生停止应答；idle 期间计数器保持清零
        CHECK_EQ(co_await count_response_toggles(port, 400zu), 0zu);
        CHECK_EQ(static_cast<std::uint64_t>(port.io_response_bits_edge_cnt), 0zu);

        // 从 idle 重新发起一次完整测量：启动应答 → 停止请求 → 停止应答
        port.io_response_ready = 0;
        co_await wait_stimulate(port.clock);
        port.io_request = 0;
        CHECK_EQ(co_await count_response_toggles(port, 400zu), 1zu);
        co_await wait_stimulate(port.clock);
        port.io_response_ready = 1;
        co_await wait_cycles(port, 100zu);

        co_await wait_stimulate(port.clock);
        port.io_request = 1;
        CHECK_EQ(co_await count_response_toggles(port, 400zu), 1zu);
        CHECK_GT(static_cast<std::uint64_t>(port.io_response_bits_edge_cnt), 0zu);
        CHECK_GT(static_cast<std::uint64_t>(port.io_response_bits_phase_cnt), 0zu);

        co_await wait_stimulate(port.clock);
        port.io_response_ready = 0;
        co_await wait_cycles(port, 100zu);
        co_await eval_finish();
    }

    /// 用例入口：停止握手每个请求只翻转一次。
    ///
    /// `dut_context` 是模板，因此这里是本模块唯一还需要 DUT 类型的地方。
    template <typename dut_t>
    void run_stop_response_once()
    {
        dut_context<dut_t, verilator::VerilatedFstC, false> ctx{ctx_opt};
        ctx.set_base_name("PhaseMeter.stop_response_once");
        port_t port{ctx.dut()};

        ctx.add_task(generate_clock(port.clock, dsp_period));
        // 被测信号自由运行：两路同频、相位差半个周期
        ctx.add_task(generate_clock(port.io_sig_sig1, sig_period));
        ctx.add_task(generate_clock(port.io_sig_sig2, sig_period, sig_phase));
        ctx.add_task(generate_reset(port.reset, port.clock));

        stop_response_result result{};
        ctx.add_task(stop_response(port, result));
        ctx.loop_until_finish(10_ms);

        // 协程内的断言失败不会终止用例，这里再汇总一次，避免只看第一处
        for(auto round{0zu}; round != rounds; ++round)
        {
            CAPTURE(round);
            CHECK_EQ(result.start_toggles[round], 1zu);
            CHECK_EQ(result.stop_toggles[round], 1zu);
        }
        MESSAGE(ctx.stats());
    }

    /// 用例入口：forceStop 中止测量、清计数器并允许重新测量。
    ///
    /// `clear_latency` 是 clear 在计数器输出上完全排空所需的延迟拍数（FPGA 0，ASIC 7）。
    template <typename dut_t>
    void run_force_stop(std::uint64_t clear_latency)
    {
        dut_context<dut_t, verilator::VerilatedFstC, false> ctx{ctx_opt};
        ctx.set_base_name("PhaseMeter.force_stop");
        port_t port{ctx.dut()};

        ctx.add_task(generate_clock(port.clock, dsp_period));
        ctx.add_task(generate_clock(port.io_sig_sig1, sig_period));
        ctx.add_task(generate_clock(port.io_sig_sig2, sig_period, sig_phase));
        ctx.add_task(generate_reset(port.reset, port.clock));

        ctx.add_task(force_stop(port, clear_latency));
        ctx.loop_until_finish(10_ms);
        MESSAGE(ctx.stats());
    }
}  // namespace phase_meter_test
