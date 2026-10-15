// 片内信号源 ↔ 测量通路的**联调**测试激励（Vivado xsim）
//
// 与 `tb_digital_phase_meter_top.sv` 的区别：那份激励用**理想信号源**驱动 dpm_top 的
// srcSig1/srcSig2（只覆盖测量通路）；本激励把**真实的 clk_wiz_src IP（MMCM + DRP）**接上去，
// 由测试激励通过 AXI4-Lite 写 DRP 来设定片内源的频率（相位在当前 bitstream 下不可改，
// 见 docs/verify-drp.md），从而在没有开发板的情况下验证：
//
//   1. 真实 MMCM 输出的频率确实是 `VCO / D`（验证 SET_SIG 的频率换算与 DRP 分频字编码）；
//   2. 测量通路的测频结果与「设定频率」一致（用 edgeCnt/gateCnt 反算，判据见
//      .agents/skills/dpm-measurement-model）；
//   3. 片内源在两路输出之间**固有的 90° 相位差**被正确测出（
//      `phaseCnt/edgeCnt` 应等于整数个 DSP 周期）；
//   4. 7 个冻结频点全部验证（频点表见 docs/ps-protocol.md 第 6.3 节）。
//
// 时钟：50MHz 参考 → clk_wiz_0 产生 400MHz dspClock（与上板配置一致）；clk_wiz_src 的
// 参考也是 50MHz。MMCM 锁定后 Safe Clock Startup 会放行输出，因此复位后要等足够长时间。
//
// 运行：sim/run_handoff_tb.sh
//
// 判定：每个频点打印 `HANDOFF_MEAS`，结束时打印 `HANDOFF_DONE pass=<n> fail=<m>`；
// 有任何 fail 时以 `$fatal` 结束。

`timescale 1ns/1ps
`default_nettype none

module tb_handoff_src_measure;

    // ---------------- 参数 ----------------
    localparam real CLOCK_PERIOD = 20.0;        // 50MHz 参考
    localparam real DSP_CLOCK_PERIOD = 2.5;     // 400MHz（与上板一致）
    localparam real F_SLOW = 50_000_000.0;
    localparam real F_DSP = 400_000_000.0;
    localparam int unsigned GATE_TARGET = 2000; // 40µs 闸门
    // 片内源：VCO = 50MHz × MULT，输出 = VCO / D
    localparam int unsigned MULT = 20;          // VCO = 1000MHz
    localparam int unsigned DIVCLK = 1;

    // DRP 偏移（实测，见 docs/ps-protocol.md 6.1）
    localparam logic [31:0] DRP_CLKFBOUT = 32'h200;
    localparam logic [31:0] DRP_CLKOUT0 = 32'h208;
    localparam logic [31:0] DRP_CLKOUT0_PHASE = 32'h20C;
    localparam logic [31:0] DRP_CLKOUT1 = 32'h214;
    localparam logic [31:0] DRP_CLKOUT1_PHASE = 32'h218;
    localparam logic [31:0] DRP_LOAD = 32'h25C;
    localparam logic [31:0] DRP_STATUS = 32'h004;

    // dpm_top 寄存器偏移
    localparam logic [31:0] REG_CTRL = 32'h00;
    localparam logic [31:0] REG_STATUS = 32'h04;
    localparam logic [31:0] REG_GATE_TARGET = 32'h08;
    localparam logic [31:0] REG_RESULT_GATE = 32'h0c;
    localparam logic [31:0] REG_RESULT_EDGE = 32'h10;
    localparam logic [31:0] REG_RESULT_PHASE = 32'h14;
    localparam logic [31:0] REG_SIG_CTRL = 32'h2c;
    localparam logic [31:0] REG_SIG_STATUS = 32'h30;

    // 冻结频点表：{目标频率 Hz, D}
    localparam int unsigned N_POINTS = 7;
    int unsigned point_freq [0:N_POINTS-1] = '{5_000_000, 6_250_000, 8_000_000,
                                               10_000_000, 12_500_000, 20_000_000, 25_000_000};
    int unsigned point_vco [0:N_POINTS-1]  = '{600_000_000, 750_000_000, 1_000_000_000,
                                               1_000_000_000, 1_000_000_000, 1_000_000_000,
                                               1_000_000_000};
    int unsigned point_mult [0:N_POINTS-1] = '{12, 15, 20, 20, 20, 20, 20};
    int unsigned point_div [0:N_POINTS-1]  = '{120, 120, 125, 100, 80, 50, 40};

    int pass_count = 0;
    int fail_count = 0;

    // ---------------- 时钟与复位 ----------------
    logic clock = 1'b0;
    logic dsp_clock = 1'b0;
    logic resetn = 1'b0;
    always #(CLOCK_PERIOD / 2.0) clock = ~clock;

    // ---------------- clk_wiz_0：50MHz → 400MHz DSP 域时钟 ----------------
    logic clk_wiz_locked;
    dpm_system_clk_wiz_0_0 dsp_clk_gen (
        .clk_in1(clock),
        .reset(1'b0),
        .clk_out1(dsp_clock),
        .locked(clk_wiz_locked)
    );

    // ---------------- clk_wiz_src：片内信号源（MMCM + DRP） ----------------
    logic [10:0] src_awaddr;
    logic src_awvalid, src_awready;
    logic [31:0] src_wdata;
    logic [3:0] src_wstrb;
    logic src_wvalid, src_wready;
    logic [1:0] src_bresp;
    logic src_bvalid, src_bready;
    logic [10:0] src_araddr;
    logic src_arvalid, src_arready;
    logic [31:0] src_rdata;
    logic [1:0] src_rresp;
    logic src_rvalid, src_rready;
    logic src_clk_out1, src_clk_out2, src_locked;

    dpm_system_clk_wiz_src_0 src_gen (
        .s_axi_aclk(clock),
        .s_axi_aresetn(resetn),
        .s_axi_awaddr(src_awaddr),
        .s_axi_awvalid(src_awvalid),
        .s_axi_awready(src_awready),
        .s_axi_wdata(src_wdata),
        .s_axi_wstrb(src_wstrb),
        .s_axi_wvalid(src_wvalid),
        .s_axi_wready(src_wready),
        .s_axi_bresp(src_bresp),
        .s_axi_bvalid(src_bvalid),
        .s_axi_bready(src_bready),
        .s_axi_araddr(src_araddr),
        .s_axi_arvalid(src_arvalid),
        .s_axi_arready(src_arready),
        .s_axi_rdata(src_rdata),
        .s_axi_rresp(src_rresp),
        .s_axi_rvalid(src_rvalid),
        .s_axi_rready(src_rready),
        .clk_out1(src_clk_out1),
        .clk_out2(src_clk_out2),
        .locked(src_locked),
        .clk_in1(clock)
    );

    // ---------------- dpm_top（测量 IP） ----------------
    logic [31:0] dpm_awaddr;
    logic dpm_awvalid, dpm_awready;
    logic [31:0] dpm_wdata;
    logic [3:0] dpm_wstrb;
    logic dpm_wvalid, dpm_wready;
    logic [1:0] dpm_bresp;
    logic dpm_bvalid, dpm_bready;
    logic [31:0] dpm_araddr;
    logic dpm_arvalid, dpm_arready;
    logic [31:0] dpm_rdata;
    logic [1:0] dpm_rresp;
    logic dpm_rvalid, dpm_rready;
    logic dpm_interrupt;

    DigitalPhaseMeterTop dut (
        .clock(clock),
        .resetn(resetn),
        .dspClock(dsp_clock),
        .sig1(1'b0),                 // 外部输入未使用
        .sig2(1'b0),
        .srcSig1(src_clk_out1),      // 真实 MMCM 输出直接作为被测信号
        .srcSig2(src_clk_out2),
        .srcLocked(src_locked),
        .s_axi_lite_awaddr(dpm_awaddr),
        .s_axi_lite_awprot(3'b0),
        .s_axi_lite_awvalid(dpm_awvalid),
        .s_axi_lite_awready(dpm_awready),
        .s_axi_lite_wdata(dpm_wdata),
        .s_axi_lite_wstrb(dpm_wstrb),
        .s_axi_lite_wvalid(dpm_wvalid),
        .s_axi_lite_wready(dpm_wready),
        .s_axi_lite_bresp(dpm_bresp),
        .s_axi_lite_bvalid(dpm_bvalid),
        .s_axi_lite_bready(dpm_bready),
        .s_axi_lite_araddr(dpm_araddr),
        .s_axi_lite_arprot(3'b0),
        .s_axi_lite_arvalid(dpm_arvalid),
        .s_axi_lite_arready(dpm_arready),
        .s_axi_lite_rdata(dpm_rdata),
        .s_axi_lite_rresp(dpm_rresp),
        .s_axi_lite_rvalid(dpm_rvalid),
        .s_axi_lite_rready(dpm_rready),
        .interrupt(dpm_interrupt)
    );

    // ---------------- AXI4-Lite BFM（下降沿驱动，上升沿采样） ----------------
    task automatic axi_init();
        dpm_awaddr = 0; dpm_awvalid = 0; dpm_wdata = 0; dpm_wstrb = 0; dpm_wvalid = 0;
        dpm_bready = 0; dpm_araddr = 0; dpm_arvalid = 0; dpm_rready = 0;
        src_awaddr = 0; src_awvalid = 0; src_wdata = 0; src_wstrb = 0; src_wvalid = 0;
        src_bready = 0; src_araddr = 0; src_arvalid = 0; src_rready = 0;
    endtask

    task automatic dpm_write(input logic [31:0] addr, input logic [31:0] data);
        @(negedge clock);
        dpm_awaddr = addr; dpm_awvalid = 1'b1;
        dpm_wdata = data; dpm_wstrb = 4'hf; dpm_wvalid = 1'b1;
        dpm_bready = 1'b1;
        do @(posedge clock); while (!(dpm_awvalid && dpm_awready));
        dpm_awvalid = 1'b0;
        do @(posedge clock); while (!(dpm_wvalid && dpm_wready));
        dpm_wvalid = 1'b0;
        do @(posedge clock); while (!dpm_bvalid);
        @(negedge clock);
        dpm_bready = 1'b0;
    endtask

    task automatic dpm_read(input logic [31:0] addr, output logic [31:0] data);
        @(negedge clock);
        dpm_araddr = addr; dpm_arvalid = 1'b1; dpm_rready = 1'b1;
        do @(posedge clock); while (!(dpm_arvalid && dpm_arready));
        dpm_arvalid = 1'b0;
        do @(posedge clock); while (!dpm_rvalid);
        data = dpm_rdata;
        @(negedge clock);
        dpm_rready = 1'b0;
    endtask

    task automatic src_write(input logic [31:0] addr, input logic [31:0] data);
        @(negedge clock);
        src_awaddr = addr[10:0]; src_awvalid = 1'b1;
        src_wdata = data; src_wstrb = 4'hf; src_wvalid = 1'b1;
        src_bready = 1'b1;
        do @(posedge clock); while (!(src_awvalid && src_awready));
        src_awvalid = 1'b0;
        do @(posedge clock); while (!(src_wvalid && src_wready));
        src_wvalid = 1'b0;
        do @(posedge clock); while (!src_bvalid);
        @(negedge clock);
        src_bready = 1'b0;
    endtask

    task automatic src_read(input logic [31:0] addr, output logic [31:0] data);
        @(negedge clock);
        src_araddr = addr[10:0]; src_arvalid = 1'b1; src_rready = 1'b1;
        do @(posedge clock); while (!(src_arvalid && src_arready));
        src_arvalid = 1'b0;
        do @(posedge clock); while (!src_rvalid);
        data = src_rdata;
        @(negedge clock);
        src_rready = 1'b0;
    endtask

    // ---------------- 测量辅助 ----------------
    task automatic wait_not_busy(input int unsigned max_reads);
        logic [31:0] status;
        int unsigned i;
        for (i = 0; i < max_reads; i++) begin
            dpm_read(REG_STATUS, status);
            if (!status[0]) return;
            // 每两次轮询之间推进 1µs 仿真时间：闸门最长 400µs，如果每次都立刻重读，
            // 一次测量要 2 万多次 AXI 事务（xsim 的墙钟时间会爆掉）。
            // 用 #1µs 让「仿真时间」推进，事务次数降到 ~400 次。
            #1000.0;
        end
        $display("HANDOFF_ERROR 测量超时（STATUS.busy 一直为 1）");
        $fatal(1, "measurement timeout");
    endtask

    task automatic run_measurement(input logic [31:0] gate_target_in,
                                   output logic [31:0] gate_cnt, output logic [31:0] edge_cnt,
                                   output logic [31:0] phase_cnt);
        logic [31:0] status;
        dpm_write(REG_GATE_TARGET, gate_target_in);
        // CTRL：保持中断使能 + 启动
        dpm_write(REG_CTRL, 32'h3);
        wait_not_busy(200000);
        dpm_read(REG_RESULT_GATE, gate_cnt);
        dpm_read(REG_RESULT_EDGE, edge_cnt);
        dpm_read(REG_RESULT_PHASE, phase_cnt);
        // 读走结果（清 resultValid 与中断挂起）
        dpm_write(32'h20, 32'h3);
        dpm_read(REG_STATUS, status);
    endtask

    // 配置片内源：写 CLKFBOUT / 两个分频字 / 相位字，再 Load 并等锁定
    // 打印关键阶段的仿真时刻，便于从日志判断卡在哪一步（xsim 跑 MMCM 模型很慢）
    task automatic mark(input string what);
        $display("HANDOFF_MARK %0t %s", $time, what);
    endtask

    task automatic program_source(input int unsigned mult, input int unsigned div,
                                  input logic [31:0] phase_mdeg);
        logic [31:0] status;
        int unsigned i;
        src_write(DRP_CLKFBOUT, (mult << 8) | (DIVCLK & 32'hff));
        src_write(DRP_CLKOUT0, div & 32'hff);
        src_write(DRP_CLKOUT1, div & 32'hff);
        src_write(DRP_CLKOUT0_PHASE, 32'd0);
        src_write(DRP_CLKOUT1_PHASE, phase_mdeg);
        src_write(DRP_LOAD, 32'h7);
        // 等 clk_wiz 内部锁定（重配置约 9µs，保守等 60µs）
        status = 32'h0;
        for (i = 0; i < 200; i++) begin
            src_read(DRP_STATUS, status);
            if (status[0]) break;
            #1_000.0;    // 1µs/次，最多等 200µs（实测重配置约 9µs）
        end
        if (!status[0]) begin
            $display("HANDOFF_ERROR 重配置后 MMCM 未锁定（DRP_STATUS=0x%08x）", status);
            $fatal(1, "MMCM relock timeout");
        end
    endtask

    // ---------------- 主流程 ----------------
    int unsigned p;
    logic [31:0] gate_cnt, edge_cnt, phase_cnt, sig_status, version;
    real f_meas, f_err_rel, phase_deg, phase_step_deg;
    int unsigned cycles;
    real cycles_exact;

    initial begin : main
        axi_init();
        resetn = 1'b0;
        repeat (20) @(negedge clock);
        resetn = 1'b1;
        repeat (200) @(negedge clock);
        // 注意：dspClock 在 MMCM 锁定前没有时钟（clk_wiz 的 Safe Clock Startup 门控），
        // 因此下面的等待以主时钟 50MHz 计。

        // 等 clk_wiz_0（DSP 时钟）锁定；clk_wiz_src 由 program_source 触发
        // MMCM 行为模型在 xsim 里锁定很慢：实测 dspClock 约 1.59ms、片内源约 1.97ms
        // （Safe Clock Startup 还要在 locked 之后数 8 拍），因此窗口给到 5ms。
        begin : wait_dsp_lock
            int unsigned waited;
            waited = 0;
            while (!clk_wiz_locked && waited < 5_000_000) begin
                #1_000.0;
                waited = waited + 1000;
            end
            if (!clk_wiz_locked) begin
                $display("HANDOFF_ERROR dspClock 的 MMCM 未在 %0d ns 内锁定", waited);
                $fatal(1, "dsp clock not locked");
            end
            $display("HANDOFF_INFO dspClock 在 %0d ns 后锁定", waited);
        end

        mark("reset done, read VERSION");
        dpm_read(32'h24, version);
        $display("HANDOFF_INFO version=0x%08x（期望 0x44504d02）", version);
        if (version !== 32'h4450_4d02) begin
            $display("HANDOFF_ERROR 版本号不符");
            $fatal(1, "bad version");
        end

        // 选中片内源（enable | srcSelect）
        dpm_write(REG_SIG_CTRL, 32'h3);
        dpm_read(REG_SIG_STATUS, sig_status);
        $display("HANDOFF_INFO SIG_STATUS=0x%08x (expect bit3 sourceReady=1)", sig_status);
        mark("SIG_CTRL written, source selected");

        mark("start sweep (7 points)");
        for (p = 0; p < N_POINTS; p++) begin
            program_source(point_mult[p], point_div[p], 32'd90000);
            #2000.0;    // 等 Safe Clock Startup 放行
            mark($sformatf("point %0d clock stable", p));

            mark($sformatf("point %0d reconfigured, measuring", p));
            begin
                // 闸门自适应：取 ≥150 个参考沿对应的低速周期数，保证 edgeCnt 足够大
                // （±1 个沿的相对误差 ~0.7%），同时把仿真时间控制在几百微秒内。
                automatic int unsigned gate_use;
                gate_use = (point_freq[p] / 100_000) + 1;      // ≈ 周期(µs) × 1e5 → 低速周期数
                if (gate_use < 2000) gate_use = 2000;
                mark($sformatf("point %0d gate=%0d", p, gate_use));
                run_measurement(gate_use, gate_cnt, edge_cnt, phase_cnt);
            end

            // ---- 频率判据：f_meas = edgeCnt / (gateCnt / F_SLOW) ----
            f_meas = (edge_cnt * F_SLOW) / gate_cnt;
            f_err_rel = (f_meas - point_freq[p]) / point_freq[p];
            if (f_err_rel < 0.0) f_err_rel = -f_err_rel;

            // ---- 相位判据：phaseCnt/edgeCnt 应为整数个 DSP 周期 ----
            cycles = (edge_cnt == 0) ? 0 : (phase_cnt / edge_cnt);
            cycles_exact = (edge_cnt == 0) ? 0.0 : (phase_cnt * 1.0) / edge_cnt;
            phase_deg = (edge_cnt == 0) ? 0.0 : (cycles * 360.0 * point_freq[p]) / F_DSP;
            phase_step_deg = (360.0 * point_freq[p]) / F_DSP;
            // 90° 在 DSP 网格上的期望：round(90/360 * T_sig / T_dsp) 个周期
            begin
                int unsigned exp_cycles;
                exp_cycles = (2 * point_freq[p] + F_DSP) / (2 * F_DSP);   // round(f_sig/(2*f_dsp)*4)/... 见下
                // 90° = T_sig/4，周期数 = round(f_sig/4 * (1/f_dsp) * ...) 直接用整数推导：
                // exp_cycles = round((90/360) * (F_DSP/point_freq[p]) 的倒数) = round(F_DSP/(4*f_sig))
                exp_cycles = (F_DSP + 2 * point_freq[p]) / (4 * point_freq[p]);
                if (cycles_exact < exp_cycles - 1.0 || cycles_exact > exp_cycles + 1.0) begin
                    $display("HANDOFF_MEAS point=%0d f_nom=%0d f_meas=%0f rel_err=%0.5f gate=%0d edge=%0d phase=%0d cycles=%0f exp_cycles=%0d result=FAIL",
                             p, point_freq[p], f_meas, f_err_rel, gate_cnt, edge_cnt, phase_cnt,
                             cycles_exact, exp_cycles);
                    fail_count++;
                end else begin
                    $display("HANDOFF_MEAS point=%0d f_nom=%0d f_meas=%0f rel_err=%0.5f gate=%0d edge=%0d phase=%0d cycles=%0f exp_cycles=%0d result=PASS",
                             p, point_freq[p], f_meas, f_err_rel, gate_cnt, edge_cnt, phase_cnt,
                             cycles_exact, exp_cycles);
                    pass_count++;
                end
            end

            // 频率相对误差：容限 ±1.5%（窗口边界 ±1 个沿 + 测量模型的取整）
            if (f_err_rel > 0.015) begin
                $display("HANDOFF_ERROR point=%0d 频率相对误差 %0.5f 超容限", p, f_err_rel);
                fail_count++;
            end
            // phaseCnt 必须是 edgeCnt 的整数倍（量化下界）
            if (edge_cnt != 0 && (phase_cnt % edge_cnt) != 0) begin
                $display("HANDOFF_ERROR point=%0d phaseCnt=%0d 不是 edgeCnt=%0d 的整数倍",
                         p, phase_cnt, edge_cnt);
                fail_count++;
            end
        end

        $display("HANDOFF_DONE pass=%0d fail=%0d", pass_count, fail_count);
        if (fail_count != 0) $fatal(1, "handoff test failed");
        $display("==== 联调测试通过：真实 MMCM 片内源 → 测量通路 ====");
        $finish;
    end

    // 看门狗：MMCM 锁定时序 + 7 个频点（每个 ~40µs 闸门 + ~100µs 重配置）留足余量。
    // 注意 xsim 跑 MMCM 行为模型很慢（实测约 100 秒 CPU / 20ms 仿真时间），因此不要
    // 用「仿真时间」当性能预算；这里给 400ms 是为了覆盖 MMCM 锁定的最坏情况。
    initial begin : watchdog
        #400_000_000;    // 400ms 仿真时间
        $display("HANDOFF_ERROR 看门狗超时（%0t）", $time);
        $fatal(1, "watchdog timeout");
    end

    // 进度监视：任何阶段卡住时都能从日志看出卡在哪一步
    initial begin : progress
        $display("HANDOFF_INFO %0t 开始：等 dspClock 锁定", $time);
        wait (clk_wiz_locked === 1'b1);
        $display("HANDOFF_INFO %0t dspClock 已锁定", $time);
        wait (src_locked === 1'b1);
        $display("HANDOFF_INFO %0t 片内源 MMCM 已锁定（复位后的初值）", $time);
    end

endmodule

`default_nettype wire
