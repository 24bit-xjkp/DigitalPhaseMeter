#include <array>
#include <cmath>
#include <optional>
#include <span>

#include <verilator_fwd.hpp>
#include "doctest_macros.hpp"
import verilator_utils.full;
#include <ControllerVerilator.h>
#include <verilator_bwd.hpp>

// Controller 跨两个时钟域：控制 FSM 与闸门计数器运行在低速的 clock 域（50MHz），
// PhaseMeter 运行在高速的 dspClock 域（测试中取 250MHz）。测试把 Controller 当作测量仪表，
// 用 verilator_utils 的近似比较（approx）校验测量结果与理论值的偏差是否落在容限内，
// 而不逐周期复现内部时序。
//
// 测量语义（对应 PhaseMeter 的实现）：
//   - gateCnt 是测量窗口内低速时钟的周期数：窗口开始时清零、每个低速周期加一。
//     控制器在闸门计数达到目标值后才发出停止请求，停止响应返回并锁存负载还会经过
//     若干低速周期，因此实测值比目标值略大，容限按固定的小偏移给出；
//   - edgeCnt 是测量窗口内参考信号 extSig_sig1 的上升沿数。窗口长度约等于 gateCnt 个低速
//     时钟周期，理论值为 gateCnt * T_clock / T_sig1；窗口边界与被测频率不整除时会
//     多算或少算一个沿，容限取 ±2；
//   - phaseCnt 在参考信号上升沿使能、在被测信号上升沿停止，每个参考周期内累计
//     一个“参考沿到被测沿”的高速时钟周期数，因此理论值为 offset / T_dspClock 乘以
//     参考沿数。每个周期的计数只能取整数，容限取每个参考周期 1 个高速时钟周期，
//     覆盖参考沿与被测沿落在高速时钟网格上的量化；被测相位偏移小于半个信号周期
//     时该映射保持一一对应，因此相位扫描限制在该范围内。
//
// 输入侧：Controller 只接收一条已经选定的被测信号 io.sig（外部引脚输入与片内信号源
// 的使能门控、二选一在上级的主时钟域完成，见 SignalInputMux）。本测试关心的是这条
// 信号*不*与主时钟对齐时测量是否仍然正确——测试里直接按「周期 + 相位」自由运行地
// 驱动它，因此覆盖的正是测量核心的异步采样链与跨时钟域测量窗口。
//
// 为让量化误差可忽略、测量误差在长闸门时间内相互抵消，闸门时间取 2000 个低速
// 周期（约 40us，覆盖数十到上千个信号周期），并用 doctest 的 GENERATE 扫描多个
// 信号频率与相位偏移。

namespace
{
    using namespace verilator_utils;
    using dut_t = ControllerVerilator;
    using dut_context_t = dut_context<dut_t, VerilatedFstC, false>;

    /// 高速时钟周期，250MHz。时间精度为 ns，周期必须取整数纳秒的偶数，半周期才能精确表示：
    /// 5_ns 这类奇数周期的半周期 2.5ns 会被截断成 2ns（实际变成 4ns）
    constexpr auto dsp_period{4_ns};
    /// 低速时钟周期，50MHz。两个时钟周期之比为整数，相位计测量的是高速时钟域内的
    /// 参考沿到被测沿间隔，与两域周期比无关
    constexpr auto clock_period{20_ns};
    /// 目标闸门计数，对应约 40us 的闸门时间
    constexpr std::uint64_t target_gate{2000zu};

    /// 闸门计数容限：相位计要等到被测信号的下一个上升沿才能给出停止响应，因此测量
    /// 窗口最多比目标闸门时间多一个被测信号周期，换算成低速时钟周期就是 T_sig2 /
    /// T_clock 再留一个周期的余量
    [[nodiscard]] verilator_utils::approx<std::uint64_t> gate_cnt_tolerance(std::uint64_t sig2_period_fs)
    {
        const auto extra_slow_cycles{
            static_cast<std::uint64_t>(std::lround(static_cast<double>(sig2_period_fs) / static_cast<double>(clock_period.rep)))};
        return {extra_slow_cycles + 1zu, 0.0};
    }

    /// 边沿计数容限：窗口边界上可能多算或少算一个沿
    constexpr verilator_utils::approx<std::uint64_t> edge_cnt_tolerance{2zu, 0.0};
    /// 相位计数容限：每个参考周期允许的高速时钟周期误差，覆盖参考沿与被测沿落在
    /// 高速时钟网格上的采样量化
    constexpr std::uint64_t phase_cnt_per_edge_tolerance{1zu};

    /// DUT 端口包装：Controller 有低速 clock 与高速 dspClock 两个时钟输入。
    /// DSP 域的复位由 Controller 内部的 ResetSynchronizer 从 reset 同步得到（异步置位、
    /// 与 dspClock 同步释放），所以测试只驱动 reset；释放后 DSP 域还要等 reset_depth 拍
    /// 才退出复位，测试的等待时间要覆盖这段延迟。
    struct port_t
    {
        bit_slice<CData> clock;
        bit_slice<CData> reset;
        bit_slice<CData> dsp_clock;
        bit_slice<CData> io_sig_sig1;
        bit_slice<CData> io_sig_sig2;
        bit_slice<CData> io_in_valid;
        vector_slice<IData> io_in_bits_target_gate;
        bit_slice<CData> io_out_valid;
        vector_slice<IData> io_out_bits_gate_cnt;
        vector_slice<IData> io_out_bits_edge_cnt;
        vector_slice<IData> io_out_bits_phase_cnt;

        explicit port_t(dut_t& dut) :
            clock{dut.clock}, reset{dut.reset}, dsp_clock{dut.dspClock, boolean}, io_sig_sig1{dut.io_sig_sig1, boolean},
            io_sig_sig2{dut.io_sig_sig2, boolean}, io_in_valid{dut.io_in_valid, boolean},
            io_in_bits_target_gate{dut.io_in_bits_targetGate, 28zu, dec_unsigned}, io_out_valid{dut.io_out_valid, boolean},
            io_out_bits_gate_cnt{dut.io_out_bits_gateCnt, 28zu, dec_unsigned},
            io_out_bits_edge_cnt{dut.io_out_bits_edgeCnt, 32zu, dec_unsigned},
            io_out_bits_phase_cnt{dut.io_out_bits_phaseCnt, 32zu, dec_unsigned}
        {
        }
    };

    /// 一次测量的激励参数
    struct stimulus_t
    {
        /// 参考信号 extSig_sig1 的周期与高电平时间
        femtosecond_t sig1_period{400_ns};
        femtosecond_t sig1_high{200_ns};
        /// 被测信号 extSig_sig2 的周期与高电平时间
        femtosecond_t sig2_period{400_ns};
        femtosecond_t sig2_high{200_ns};
        /// 被测信号上升沿相对参考信号上升沿的相位偏移
        femtosecond_t phase_offset{200_ns};
        /// 目标闸门计数
        std::uint64_t gate_cnt{target_gate};
        /// 启动后经过多少个低速时钟周期再改写目标闸门计数：用于验证 Controller 在启动的那
        /// 一拍就把 targetGate 锁存进自己的寄存器（测量期间改写不影响本次测量）。为空表示
        /// 测量期间不改写
        std::optional<std::uint64_t> gate_change_after_cycles{};
        /// 改写后的目标闸门计数，仅在 gate_change_after_cycles 有值时使用
        std::uint64_t gate_change_value{};
    };

    /// 一次测量的输出负载
    struct result_t
    {
        /// 闸门计数，单位为低速时钟周期
        std::uint64_t gate_cnt{};
        /// 参考信号上升沿数
        std::uint64_t edge_cnt{};
        /// 相位计数，单位为高速时钟周期
        std::uint64_t phase_cnt{};
        /// out.valid 为高的低速时钟周期数
        std::size_t valid_cycles{};
    };

    /// 测量任务的结果：是否观察到输出，以及输出负载
    struct measurement_t
    {
        bool observed{};
        result_t result{};
    };

    /**
     * @brief 生成被测信号激励，发起一次测量并读取结果
     *
     * 两个信号都是自由运行的周期信号：相位计在进入等待停止状态之后仍需要 extSig_sig2 的
     * 上升沿，因此不能用一次性脉冲。信号只在高速时钟下降沿更新，此时两个时钟均为
     * 低电平，每个高速时钟上升沿采样到的电平都是确定的，不会与低速域握手冲突。
     *
     * @param port DUT 端口
     * @param option 激励参数
     * @return 测量任务
     */
    [[nodiscard]] task<measurement_t> measure_once(port_t& port, const stimulus_t& option)
    {
        constexpr std::uint64_t low{0};
        port.io_sig_sig1 = low;
        port.io_sig_sig2 = low;
        port.io_in_valid = low;
        port.io_in_bits_target_gate = option.gate_cnt;

        measurement_t measurement{};
        // 信号按“周期 + 相位”自由运行，每个高速时钟下降沿更新电平，使每个高速时钟
        // 上升沿采样到的电平完全确定；时间统一换算到飞秒后用整数运算
        const auto signal_level{[](std::uint64_t now, std::uint64_t phase, std::uint64_t period, std::uint64_t high) {
            const auto phase_in_period{phase % period};
            const auto position{(now % period + period - phase_in_period) % period};
            return static_cast<std::uint64_t>(position < high);
        }};
        const auto sig1_period{static_cast<std::uint64_t>(option.sig1_period.rep)};
        const auto sig1_high{static_cast<std::uint64_t>(option.sig1_high.rep)};
        const auto sig2_period{static_cast<std::uint64_t>(option.sig2_period.rep)};
        const auto sig2_high{static_cast<std::uint64_t>(option.sig2_high.rep)};
        const auto sig2_phase{static_cast<std::uint64_t>(option.phase_offset.rep)};

        co_await wait_reset_finish(port.reset);
        // 对齐到高速时钟下降沿后再发起请求；同时记下启动时刻，作为「启动后经过多少个低速
        // 周期再改写 targetGate」的基准——targetGate 在启动的那一拍就被 Controller 锁存
        co_await wait_stimulate(port.dsp_clock);
        // 时间精度为 1ns，换算到飞秒需要乘以 1e6
        const auto start_fs{static_cast<std::uint64_t>(co_await time_in_time_precision()) * 1'000'000zu};
        port.io_in_valid = 1;
        co_await wait_stimulate(port.dsp_clock);

        bool gate_changed{false};
        // 信号自由运行，直到控制器给出测量结果
        while(static_cast<std::uint64_t>(port.io_out_valid) == 0u)
        {
            const auto now{static_cast<std::uint64_t>(co_await time_in_time_precision()) * 1'000'000zu};
            // 按需在启动后的指定时刻改写目标闸门计数（此时测量仍在进行中）：输入不必在测量
            // 期间保持稳定，本次测量仍应当按启动拍锁存的目标值完成
            if(option.gate_change_after_cycles.has_value() && !gate_changed)
            {
                const auto change_at{start_fs + *option.gate_change_after_cycles * static_cast<std::uint64_t>(clock_period.rep)};
                if(now >= change_at)
                {
                    port.io_in_bits_target_gate = option.gate_change_value;
                    gate_changed = true;
                }
            }
            port.io_sig_sig1 = signal_level(now, 0zu, sig1_period, sig1_high);
            port.io_sig_sig2 = signal_level(now, sig2_phase, sig2_period, sig2_high);
            co_await wait_stimulate(port.dsp_clock);
        }

        measurement.observed = true;
        measurement.result.gate_cnt = static_cast<std::uint64_t>(port.io_out_bits_gate_cnt);
        measurement.result.edge_cnt = static_cast<std::uint64_t>(port.io_out_bits_edge_cnt);
        measurement.result.phase_cnt = static_cast<std::uint64_t>(port.io_out_bits_phase_cnt);

        // 撤销请求并统计输出有效的低速周期数：先统计当前周期，再等待后续周期
        port.io_in_valid = 0;
        ++measurement.result.valid_cycles;
        co_await wait_verify(port.clock);
        while(static_cast<std::uint64_t>(port.io_out_valid) != 0u)
        {
            ++measurement.result.valid_cycles;
            co_await wait_verify(port.clock);
        }
        co_return measurement;
    }

    /// 用同一组激励连续执行若干次测量并结束仿真
    [[nodiscard]] task<void> run_measurements(port_t& port, const stimulus_t& option, std::span<measurement_t> measurements)
    {
        for(auto&& measurement: measurements) { measurement = co_await measure_once(port, option); }
        co_await eval_finish();
    }

    /// 依次用不同的目标闸门计数执行测量并结束仿真
    [[nodiscard]] task<void> run_gate_sweep(port_t& port,
                                            stimulus_t option,
                                            std::span<const std::uint64_t> gate_cnts,
                                            std::span<measurement_t> measurements)
    {
        for(auto&& [gate_cnt, measurement]: std::views::zip(gate_cnts, measurements))
        {
            option.gate_cnt = gate_cnt;
            measurement = co_await measure_once(port, option);
        }
        co_await eval_finish();
    }

    /**
     * @brief 校验测量结果与理论值的偏差在容限内
     *
     * @param result 测量结果
     * @param option 激励参数
     */
    void check_common(const result_t& result)
    {
        CAPTURE(result.gate_cnt);
        CAPTURE(result.edge_cnt);
        CAPTURE(result.phase_cnt);
        CAPTURE(result.valid_cycles);

        // 输出只应保持有效一个低速时钟周期
        CHECK_EQ(result.valid_cycles, 1zu);
    }

    /**
     * @brief 校验闸门计数与边沿计数
     *
     * @param result 测量结果
     * @param option 激励参数
     */
    void check_window(const result_t& result, const stimulus_t& option)
    {
        check_common(result);

        // 闸门计数是测量窗口内的低速时钟周期数，比目标值多出停止握手与负载锁存的周期
        CHECK_EQ(result.gate_cnt, gate_cnt_tolerance(option.sig2_period.rep)(option.gate_cnt));

        // 窗口长度为目标闸门数乘以低速时钟周期，理论边沿数由此换算；窗口边界与被测
        // 频率不整除时会多算或少算一个沿
        const auto window_fs{static_cast<double>(option.gate_cnt) * static_cast<double>(clock_period.rep)};
        const auto expected_edge_cnt{
            static_cast<std::uint64_t>(std::lround(window_fs / static_cast<double>(option.sig1_period.rep)))};
        CHECK_EQ(result.edge_cnt, edge_cnt_tolerance(expected_edge_cnt));
    }

    /**
     * @brief 校验相位计数与相位偏移对应的高速时钟周期数一致
     *
     * @param result 测量结果
     * @param option 激励参数
     * @note 被测相位偏移应当小于被测信号周期的一半，此时每个参考周期内的相位计数
     * 与偏移一一对应
     */
    void check_phase(const result_t& result, const stimulus_t& option)
    {
        check_common(result);
        CAPTURE(static_cast<double>(result.phase_cnt) / static_cast<double>(result.edge_cnt));

        // 相位计数在每个参考周期内累计一个“参考沿到被测沿”的高速时钟周期数，因此
        // 理论值为相位偏移对应的高速时钟周期数乘以参考沿数；每个周期的计数只能取
        // 整数，容限取每个参考周期 1 个高速时钟周期以覆盖该量化
        const auto offset_cycles{static_cast<double>(option.phase_offset.rep) / static_cast<double>(dsp_period.rep)};
        const auto theory_phase_cnt{
            static_cast<std::uint64_t>(std::lround(offset_cycles * static_cast<double>(result.edge_cnt)))};
        const auto phase_cnt_tolerance{
            verilator_utils::approx{phase_cnt_per_edge_tolerance * result.edge_cnt, 0.0}
        };
        CAPTURE(theory_phase_cnt);
        CHECK_EQ(result.phase_cnt, phase_cnt_tolerance(theory_phase_cnt));

        // 相位偏移恰好是整数个高速时钟周期时，每个参考周期的计数必然是同一个整数
        // （检测沿在时间上相隔 x 个周期，`ceil(a + x) - ceil(a) == x`），因此 phaseCnt 与
        // 理论值**逐位相等**。这条精确断言专门抓「每个周期固定少算/多算 1 个周期」这类
        // 系统性偏差：上面的 ±1 周期容限恰好把它覆盖掉（本项目实际踩过一次——相位计数器
        // 的清零信号少延迟一拍，所有读数系统性偏小 1 个 DSP 周期，单测全绿）。
        const auto offset_cycle_round{static_cast<double>(std::lround(offset_cycles))};
        constexpr verilator_utils::approx offset_cycle_tolerance{1e-9, 0};
        if(offset_cycle_tolerance(offset_cycles) == offset_cycle_round) { CHECK_EQ(result.phase_cnt, theory_phase_cnt); }
    }

    constexpr dut_context_option ctx_opt{.time_precision = verilator_time_unit::ns};
}  // namespace

TEST_SUITE("Controller")
{
    TEST_CASE("reset holds the controller idle and produces no result")
    {
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("Controller.reset");
        port_t port{ctx.dut()};

        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));

        constexpr static stimulus_t option{
            .sig1_period = 400_ns,
            .sig1_high = 200_ns,
            .sig2_period = 400_ns,
            .sig2_high = 200_ns,
            .gate_cnt = 100zu,
        };

        std::optional<measurement_t> after_reset{};
        const auto do_verify{[&] -> task<void> {
            port.io_sig_sig1 = 0;
            port.io_sig_sig2 = 0;
            port.io_in_valid = 0;
            port.io_in_bits_target_gate = 4zu;

            // 复位期间不应产生测量结果，输出负载也不应被更新
            for(auto cycle{0zu}; cycle != 20zu; ++cycle)
            {
                co_await verify_at(port.clock, [&] { CHECK_EQ(port.io_out_valid, 0u); });
            }

            // 撤销复位后仍应保持空闲，直到收到测量请求（DSP 域的复位由 reset 同步释放）
            port.reset = 0;
            for(auto cycle{0zu}; cycle != 20zu; ++cycle)
            {
                co_await verify_at(port.clock, [&] { CHECK_EQ(port.io_out_valid, 0u); });
            }

            // 复位之后控制器必须能正常工作：完成一次测量
            after_reset = co_await measure_once(port, option);
            co_await eval_finish();
        }};
        port.reset = 1;
        ctx.add_task(do_verify());

        ctx.loop_until_finish(10_ms);

        REQUIRE(after_reset.has_value());
        // REQUIRE失败会终止当前用例，因此此处访问是安全的
        const auto& result{after_reset.value()};  // NOLINT(bugprone-unchecked-optional-access)
        REQUIRE(result.observed);
        CHECK_EQ(result.result.gate_cnt, gate_cnt_tolerance(option.sig2_period.rep)(option.gate_cnt));
    }

    TEST_CASE("the gate count matches the target gate regardless of the signal periods")
    {
        const auto sig1_period_ns{GENERATE(100zu, 200zu, 400zu, 1000zu, 2000zu)};
        const auto sig2_period_ns{GENERATE(100zu, 200zu, 400zu, 1000zu, 2000zu)};
        const stimulus_t option{
            .sig1_period = femtosecond_t{static_cast<double>(sig1_period_ns) * 1e6},
            .sig1_high = femtosecond_t{static_cast<double>(sig1_period_ns) * 5e5},
            .sig2_period = femtosecond_t{static_cast<double>(sig2_period_ns) * 1e6},
            .sig2_high = femtosecond_t{static_cast<double>(sig2_period_ns) * 5e5},
        };
        CAPTURE(sig1_period_ns);
        CAPTURE(sig2_period_ns);

        dut_context_t ctx{ctx_opt};
        ctx.set_base_name(std::format("Controller.gate.{}_{}", sig1_period_ns, sig2_period_ns));
        port_t port{ctx.dut()};

        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_reset(port.reset, port.clock));

        std::array<measurement_t, 1zu> measurements{};
        ctx.add_task(run_measurements(port, option, measurements));

        ctx.loop_until_finish(5_ms);

        REQUIRE(measurements[0].observed);
        check_window(measurements[0].result, option);
    }

    TEST_CASE("the phase count matches the phase offset between the two signals")
    {
        // 被测相位偏移必须小于半个信号周期，否则“参考沿到被测沿”的映射不再一一对应
        const auto sig_period_ns{GENERATE(400zu, 1000zu)};
        const auto offset_ns{GENERATE(50zu, 200zu)};
        const stimulus_t option{
            .sig1_period = femtosecond_t{static_cast<double>(sig_period_ns) * 1e6},
            .sig1_high = femtosecond_t{static_cast<double>(sig_period_ns) * 5e5},
            .sig2_period = femtosecond_t{static_cast<double>(sig_period_ns) * 1e6},
            .sig2_high = femtosecond_t{static_cast<double>(sig_period_ns) * 5e5},
            .phase_offset = femtosecond_t{static_cast<double>(offset_ns) * 1e6},
        };
        CAPTURE(sig_period_ns);
        CAPTURE(offset_ns);

        dut_context_t ctx{ctx_opt};
        ctx.set_base_name(std::format("Controller.phase.{}_{}", sig_period_ns, offset_ns));
        port_t port{ctx.dut()};

        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_reset(port.reset, port.clock));

        std::array<measurement_t, 1zu> measurements{};
        ctx.add_task(run_measurements(port, option, measurements));

        ctx.loop_until_finish(5_ms);

        REQUIRE(measurements[0].observed);
        check_phase(measurements[0].result, option);
        check_window(measurements[0].result, option);
    }

    TEST_CASE("the measurement window follows io.in.bits.targetGate")
    {
        constexpr std::array gate_cnts{200zu, 500zu, 2000zu};
        const stimulus_t option{
            .sig1_period = 400_ns,
            .sig1_high = 200_ns,
            .sig2_period = 400_ns,
            .sig2_high = 200_ns,
        };
        CAPTURE(gate_cnts[0]);
        CAPTURE(gate_cnts[1]);
        CAPTURE(gate_cnts[2]);

        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("Controller.target_gate");
        port_t port{ctx.dut()};

        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_reset(port.reset, port.clock));

        std::array<measurement_t, gate_cnts.size()> measurements{};
        ctx.add_task(run_gate_sweep(port, option, gate_cnts, measurements));

        ctx.loop_until_finish(20_ms);

        // 每次测量都要跟随输入的目标闸门计数，且窗口内的参考沿数随之变化
        for(auto&& [gate_cnt, measurement]: std::views::zip(gate_cnts, measurements))
        {
            REQUIRE(measurement.observed);
            auto expected{option};
            expected.gate_cnt = gate_cnt;
            check_window(measurement.result, expected);
        }
        CHECK_LT(measurements[0].result.gate_cnt, measurements[1].result.gate_cnt);
        CHECK_LT(measurements[1].result.gate_cnt, measurements[2].result.gate_cnt);
    }

    TEST_CASE("consecutive measurements report the same result")
    {
        const stimulus_t option{
            .sig1_period = 400_ns,
            .sig1_high = 200_ns,
            .sig2_period = 400_ns,
            .sig2_high = 200_ns,
        };
        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("Controller.consecutive");
        port_t port{ctx.dut()};

        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_reset(port.reset, port.clock));

        std::array<measurement_t, 3zu> measurements{};
        ctx.add_task(run_measurements(port, option, measurements));

        ctx.loop_until_finish(20_ms);

        // 同一组激励下每次测量的结果应当一致，且与理论值相符
        for(const auto& measurement: measurements)
        {
            REQUIRE(measurement.observed);
            check_window(measurement.result, option);
            check_phase(measurement.result, option);
        }
        for(auto index{1zu}; index != measurements.size(); ++index)
        {
            CHECK_EQ(measurements[index].result.gate_cnt, measurements[0].result.gate_cnt);
            CHECK_EQ(measurements[index].result.edge_cnt, measurements[0].result.edge_cnt);
            CHECK_EQ(measurements[index].result.phase_cnt, measurements[0].result.phase_cnt);
        }
    }

    TEST_CASE("the measurement tracks an asynchronous input that is not aligned to the system clocks")
    {
        // 实际电路里被测信号来自外部引脚或 PL 内 MMCM，与 50MHz 主时钟没有固定的频率/相位关系。
        // 这里用一组与两个时钟都不对齐的参数（周期 640ns 不是低速周期 20ns 的整数倍）驱动 io.sig，
        // 覆盖的就是「异步被测信号 → 测量核心采样链」这条通路本身。
        constexpr femtosecond_t sig_period{640_ns};
        constexpr femtosecond_t offset{160_ns};
        const stimulus_t option{
            .sig1_period = sig_period,
            .sig1_high = sig_period / 2zu,
            .sig2_period = sig_period,
            .sig2_high = sig_period / 2zu,
            .phase_offset = offset,
        };

        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("Controller.async_input");
        port_t port{ctx.dut()};

        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_reset(port.reset, port.clock));

        std::array<measurement_t, 2zu> measurements{};
        ctx.add_task(run_measurements(port, option, measurements));

        ctx.loop_until_finish(20_ms);

        // 两次连续测量都要与理论值一致：周期 640ns、相位差 160ns（= 90°，即 40 个
        // 250MHz 测试时钟周期；测试使用的 DSP 时钟与芯片实际频率无关）
        for(const auto& measurement: measurements)
        {
            REQUIRE(measurement.observed);
            check_window(measurement.result, option);
            check_phase(measurement.result, option);
        }
    }

    TEST_CASE("a fast sig2 still completes the stop handshake")
    {
        // P1 的端到端回归：sig2 周期取 2 个慢时钟周期（测试时基下 10ns；换到芯片时基
        // 相当于 50MHz 慢时钟下的 100MHz 片内源频点）。sWaitStop 期间响应握手电平
        // 每个 sig2 上升沿都会翻转（旧实现），慢域每拍正好采样到偶数次翻转、电平恒定，
        // 停止响应送不到慢域，测量会一直停在 busy。修复后每个停止请求只翻转一次，
        // 即使 sig2 远快于慢时钟也能完成。
        constexpr femtosecond_t sig2_period{10_ns};
        const stimulus_t option{
            .sig1_period = 400_ns,
            .sig1_high = 200_ns,
            .sig2_period = sig2_period,
            .sig2_high = sig2_period / 2zu,
            // 短闸门即可：本用例只关心停止握手能否完成，不追求测量精度
            .gate_cnt = 200zu,
        };

        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("Controller.fast_sig2");
        port_t port{ctx.dut()};

        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_reset(port.reset, port.clock));

        std::array<measurement_t, 3zu> measurements{};
        ctx.add_task(run_measurements(port, option, measurements));

        ctx.loop_until_finish(20_ms);

        for(const auto& measurement: measurements)
        {
            REQUIRE(measurement.observed);
            check_common(measurement.result);
            // 停止请求之后还要等下一个 sig2 沿、并把响应同步回慢域，才锁存 gateCnt，
            // 因此实测闸门比目标值多几个慢周期（本用例留 8 个周期的上界）
            CHECK_GE(measurement.result.gate_cnt, option.gate_cnt);
            CHECK_LE(measurement.result.gate_cnt, option.gate_cnt + 8zu);
            // 窗口内的参考沿数仍然由实际窗口长度决定（窗口边界允许 ±2 个沿）
            const auto expected_edge_cnt{static_cast<std::uint64_t>(
                std::lround(static_cast<double>(measurement.result.gate_cnt) * static_cast<double>(clock_period.rep) /
                            static_cast<double>(option.sig1_period.rep)))};
            CHECK_EQ(measurement.result.edge_cnt, edge_cnt_tolerance(expected_edge_cnt));
        }
    }

    TEST_CASE("the target gate is latched when the measurement starts")
    {
        // 启动后等 20 个低速时钟周期（400ns，此时 FSM 早已进入 measuring）再把
        // io.in.bits.targetGate 从大目标值改成 8：Controller 在启动的那一拍就把目标值锁存进
        // 自己的寄存器，本次测量必须按大目标值完成，测量中的改写不影响本次测量。
        constexpr std::uint64_t changed_gate{8zu};
        const stimulus_t option{
            .sig1_period = 400_ns,
            .sig1_high = 200_ns,
            .sig2_period = 400_ns,
            .sig2_high = 200_ns,
            .gate_change_after_cycles = 20zu,
            .gate_change_value = changed_gate,
        };

        dut_context_t ctx{ctx_opt};
        ctx.set_base_name("Controller.target_gate_latch");
        port_t port{ctx.dut()};

        ctx.add_task(generate_clock(port.clock, clock_period));
        ctx.add_task(generate_clock(port.dsp_clock, dsp_period));
        ctx.add_task(generate_reset(port.reset, port.clock));

        std::array<measurement_t, 1zu> measurements{};
        ctx.add_task(run_measurements(port, option, measurements));

        ctx.loop_until_finish(5_ms);

        REQUIRE(measurements[0].observed);
        // 测量期间输入确实被改成了小值（否则下面的判据没有区分度）
        CHECK_EQ(static_cast<std::uint64_t>(port.io_in_bits_target_gate), changed_gate);
        // 闸门计数仍按启动时锁存的大目标值完成（gate_cnt_tolerance 覆盖停止握手带来的偏差），
        // 而不是按测量中改写后的小值
        CHECK_EQ(measurements[0].result.gate_cnt, gate_cnt_tolerance(option.sig2_period.rep)(option.gate_cnt));
        CHECK_GT(measurements[0].result.gate_cnt, changed_gate);
        // 窗口内的参考沿数随之与目标闸门时间一致（而不是与小值一致）
        check_window(measurements[0].result, option);
    }
}
