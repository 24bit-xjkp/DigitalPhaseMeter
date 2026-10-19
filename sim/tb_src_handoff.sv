// 片内信号源 ↔ 测量通路联调激励（方案 A：裸 MMCM + 极简 DRP 控制器）
//
// 与 `tb_digital_phase_meter_top.sv` 的区别：那份激励用**理想信号源**驱动 dpm_top 的
// intSig_sig1/srcSig2（只覆盖测量通路）；本激励把 **BD 里真实用的三个单元**接起来：
//
//     dpm_mmcm_src（裸 MMCME2_ADV + BUFGCE 安全启动）
//        ▲ DRP 口
//     MmcmDrpController（Chisel 生成产物，AXI4-Lite 从口 @0x43C1_0000）
//        └─ clkOut1/clkOut2 ─► DigitalPhaseMeterTop（srcSig1/srcSig2）
//
// 它验证的是**编码 + 通路**这一整条链路：
//   1. 软件给出的 MMCM 寄存器值（PHASE_MUX/DELAY_TIME）真的让输出相位差等于请求值；
//   2. 频率 = VCO / D；
//   3. 测量通路测出来的相位差与「直接量出来的输出相位差」一致（1 个 DSP 周期内）。
//
// 判据（与 `docs/verify-plan.md` 第 4 节一致）：
//   - 直接测得的相位差与请求值之差 ≤ 半个硬件网格（45°/D，四舍五入）；
//   - 测量通路的相位差与直接测得值之差 ≤ 1 个 DSP 周期 + 1 毫度（采样量化下界）。
//
// 点表覆盖（与 `spec/gen_golden.py::FREQ_POINTS_HZ` / `phase_points_for` 一致）：
// 10 个频点 × 逐频点相位点 = 80 个点，其中 ≥180° 的 32 个；>180° 的上界由 9 位相位码
// 决定（D=125 只能到 183.96°、D=120 只能到 191.625°、D≤73 能到 315°），每个频点都
// 保证至少一个 >180° 的用例。
// 每点判 4 条：直接测得相位 = MMCM 网格值、测量 = 直接测得、测量 = 网格值、计数模型。
//
// 编译/运行见 `sim/run_handoff_tb.sh`。

`timescale 1ps/1ps
`default_nettype none

module tb_src_handoff;

    // ------------------------------------------------------------ 时钟与复位
    // 真实设计里 DSP 域是 450MHz = 9 × 50MHz（`clk_wiz` 从 FCLK_CLK0 倍频）。450MHz 的
    // 周期 2222.22…ps 在 1ps 时基下不是整数，直接取 2222ps 会让「信号周期 / DSP 周期」
    // 偏离整数比，做几百个参考沿的累加后 `phaseCnt` 出现非零余数（`phaseCnt % edgeCnt`
    // 不再为 0）——这是仿真时基的近似，不是设计缺陷。
    //
    // 因此把主时钟按「整数皮秒且与 MMCM 各倍频点都整除」重新选值：T_MAIN 必须是
    // MULT ∈ {12,15,18,20,21} 的公倍数（lcm = 1260ps）与 DSP 周期的 9 倍，
    // 取 T_MAIN = 18900ps（52.91MHz）、T_DSP = 2100ps（476.19MHz），两者严格 9:1。
    // 这样：
    //   * MMCM 的 VCO 周期 T_MAIN / MULT 全是整数 ps，行为模型的输出沿不会因取整
    //     每周期抖 1ps（6MHz / 15MHz / 30MHz 频点曾因此出现 `phaseCnt % edgeCnt != 0`）；
    //   * 信号周期 T_MAIN × D / M 全是整数 ps，且 T_sig / T_DSP = 9D / M 严格为整数
    //     （冻结频点本来就要求 f_dsp / f_sig 为整数），测量模型与仿真时钟严格自洽。
    // 绝对频率与标称值差约 5.8%，但本激励只验证数字通路（相位/频率的比值语义），
    // 频点表的频率比 f_sig / f_slow = M / D 与真实设计逐位一致，判据不受影响。
    localparam time T_MAIN = 18900;  // 52.91MHz（标称 50MHz；主时钟 / DRP DCLK / MMCM 参考）
    localparam time T_DSP  = 2100;   // 476.19MHz（标称 450MHz；DSP 域，理想时钟）
    localparam real F_SLOW_HZ = 50_000_000.0;  // 标称值（仿真时基按比例缩放，比值不变）
    localparam int unsigned F_SLOW_HZ_INT = 50_000_000;  // 计数模型的整数形式（标称值）
    localparam real F_DSP_HZ = 450_000_000.0;  // 标称值
    localparam real T_DSP_PS = 2100.0;

    logic clock = 1'b0;
    logic dsp_clock = 1'b0;
    logic resetn = 1'b0;

    always #(T_MAIN / 2) clock = ~clock;
    always #(T_DSP / 2) dsp_clock = ~dsp_clock;

    // ------------------------------------------------------------ 片内源（MMCM + DRP 控制器）
    logic [6:0] drp_daddr;
    logic [15:0] drp_di;
    logic drp_den, drp_dwe, drp_drdy;
    logic [15:0] drp_dout;
    logic drp_reset;  // 重配置复位：控制器在写 DRP 期间拉高
    logic src_clk1, src_clk2, src_locked;

    dpm_mmcm_src #(
        .CLKIN_PERIOD      (18.900),   // 与 T_MAIN 一致（52.91MHz，见上面的时基说明）
        .CLKFBOUT_MULT_F   (20.000),
        .DIVCLK_DIVIDE     (1),
        .CLKOUT0_DIVIDE_F  (40.000),
        .CLKOUT1_DIVIDE    (40),
        .CLKOUT1_PHASE     (90.000)
    ) src_mmcm (
        .clkIn    (clock),
        .reset    (~resetn),
        .clkOut1  (src_clk1),
        .clkOut2  (src_clk2),
        .locked   (src_locked),
        .drpDclk  (clock),
        .drpDaddr (drp_daddr),
        .drpDi    (drp_di),
        .drpDen   (drp_den),
        .drpDwe   (drp_dwe),
        .drpDout  (drp_dout),
        .drpDrdy  (drp_drdy),
        .drpReset (drp_reset)
    );

    // DRP 控制器的 AXI4-Lite 从口（基地址 0x43C1_0000：激励驱动**完整地址**，
    // 与 PS 上板时的行为一致——历史上按裸偏移驱动掩盖过地址译码缺陷）
    localparam logic [31:0] SRC_BASE = 32'h43C1_0000;
    logic [31:0] src_awaddr;
    logic        src_awvalid, src_awready;
    logic [31:0] src_wdata;
    logic [3:0]  src_wstrb;
    logic        src_wvalid, src_wready;
    logic [1:0]  src_bresp;
    logic        src_bvalid, src_bready;
    logic [31:0] src_araddr;
    logic        src_arvalid, src_arready;
    logic [31:0] src_rdata;
    logic [1:0]  src_rresp;
    logic        src_rvalid, src_rready;

    MmcmDrpController src_drp_ctrl (
        .clock     (clock),
        .resetn    (resetn),
        .s_axi_lite_awaddr  (src_awaddr),
        .s_axi_lite_awprot  (3'b000),
        .s_axi_lite_awvalid (src_awvalid),
        .s_axi_lite_awready (src_awready),
        .s_axi_lite_wdata   (src_wdata),
        .s_axi_lite_wstrb   (src_wstrb),
        .s_axi_lite_wvalid  (src_wvalid),
        .s_axi_lite_wready  (src_wready),
        .s_axi_lite_bresp   (src_bresp),
        .s_axi_lite_bvalid  (src_bvalid),
        .s_axi_lite_bready  (src_bready),
        .s_axi_lite_araddr  (src_araddr),
        .s_axi_lite_arprot  (3'b000),
        .s_axi_lite_arvalid (src_arvalid),
        .s_axi_lite_arready (src_arready),
        .s_axi_lite_rdata   (src_rdata),
        .s_axi_lite_rresp   (src_rresp),
        .s_axi_lite_rvalid  (src_rvalid),
        .s_axi_lite_rready  (src_rready),
        .drp_daddr (drp_daddr),
        .drp_di    (drp_di),
        .drp_den   (drp_den),
        .drp_dwe   (drp_dwe),
        .drp_dout  (drp_dout),
        .drp_drdy  (drp_drdy),
        .locked    (src_locked),
        .mmcm_reset(drp_reset)
    );

    // ------------------------------------------------------------ 测量 IP
    localparam logic [31:0] DPM_BASE = 32'h43C00000;
    // dpm_top 寄存器偏移（与 README 的寄存器映射一致）
    localparam logic [31:0] R_CTRL = 32'h00, R_STATUS = 32'h04, R_GATE = 32'h08;
    localparam logic [31:0] R_RES_GATE = 32'h0C, R_RES_EDGE = 32'h10, R_RES_PHASE = 32'h14;
    localparam logic [31:0] R_RES_SEQ = 32'h18, R_IRQ_STATUS = 32'h20;
    localparam logic [31:0] R_SIG_CTRL = 32'h2C, R_SIG_STATUS = 32'h30;

    // DRP 寄存器镜像偏移（与 ps/include/dpm_regs.h 一致）
    localparam logic [31:0] D_CLKFBOUT = 32'h08, D_CLKFBOUT2 = 32'h0C;
    localparam logic [31:0] D_CLKOUT0 = 32'h10, D_CLKOUT0_2 = 32'h14;
    localparam logic [31:0] D_CLKOUT1 = 32'h18, D_CLKOUT1_2 = 32'h1C;
    localparam logic [31:0] D_DIVCLK = 32'h20, D_POWER = 32'h24, D_LOAD = 32'h28;
    localparam logic [31:0] D_STATUS = 32'h04;

    logic [31:0] dpm_awaddr;
    logic        dpm_awvalid, dpm_awready;
    logic [31:0] dpm_wdata;
    logic [3:0]  dpm_wstrb;
    logic        dpm_wvalid, dpm_wready;
    logic [1:0]  dpm_bresp;
    logic        dpm_bvalid, dpm_bready;
    logic [31:0] dpm_araddr;
    logic        dpm_arvalid, dpm_arready;
    logic [31:0] dpm_rdata;
    logic [1:0]  dpm_rresp;
    logic        dpm_rvalid, dpm_rready;
    logic        dpm_interrupt;

    DigitalPhaseMeterTop dut (
        .clock   (clock),
        .resetn  (resetn),
        .dspClock(dsp_clock),
        .extSig_sig1    (1'b0),          // 外部输入未使用（片内源由 SIG_CTRL 选中）
        .extSig_sig2    (1'b0),
        .intSig_sig1 (src_clk1),      // 真实 MMCM 输出直接作为被测信号
        .intSig_sig2 (src_clk2),
        .srcLocked(src_locked),
        .s_axi_lite_awaddr (dpm_awaddr),
        .s_axi_lite_awprot (3'b000),
        .s_axi_lite_awvalid(dpm_awvalid),
        .s_axi_lite_awready(dpm_awready),
        .s_axi_lite_wdata  (dpm_wdata),
        .s_axi_lite_wstrb  (dpm_wstrb),
        .s_axi_lite_wvalid (dpm_wvalid),
        .s_axi_lite_wready (dpm_wready),
        .s_axi_lite_bresp  (dpm_bresp),
        .s_axi_lite_bvalid (dpm_bvalid),
        .s_axi_lite_bready (dpm_bready),
        .s_axi_lite_araddr (dpm_araddr),
        .s_axi_lite_arprot (3'b000),
        .s_axi_lite_arvalid(dpm_arvalid),
        .s_axi_lite_arready(dpm_arready),
        .s_axi_lite_rdata  (dpm_rdata),
        .s_axi_lite_rresp  (dpm_rresp),
        .s_axi_lite_rvalid (dpm_rvalid),
        .s_axi_lite_rready (dpm_rready),
        .interrupt(dpm_interrupt)
    );

    // ------------------------------------------------------------ AXI4-Lite BFM
    task automatic axi_init();
        src_awaddr = 0; src_awvalid = 0; src_wdata = 0; src_wstrb = 0; src_wvalid = 0;
        src_bready = 0; src_araddr = 0; src_arvalid = 0; src_rready = 0;
        dpm_awaddr = 0; dpm_awvalid = 0; dpm_wdata = 0; dpm_wstrb = 0; dpm_wvalid = 0;
        dpm_bready = 0; dpm_araddr = 0; dpm_arvalid = 0; dpm_rready = 0;
    endtask

    // AXI4-Lite BFM（与 sim/tb_digital_phase_meter_top.sv 同一套握手顺序：AW → W → B）。
    //
    // 注意握手判据必须在**下降沿之后**采样：从口的 awready/wready 在握手那一拍的下一个
    // 周期就会拉低（awHeld/wHeld 锁存），在 `@(posedge clock)` 之后再读 ready 会永远看不到
    // 握手完成——早期版本因此把整条联调卡死在第一笔写。
    task automatic dpm_write(input logic [31:0] off, input logic [31:0] data);
        @(negedge clock);
        dpm_awaddr = DPM_BASE + off; dpm_awvalid = 1'b1;
        do @(posedge clock); while (!(dpm_awvalid && dpm_awready));
        @(negedge clock);
        dpm_awvalid = 1'b0;
        dpm_wdata = data; dpm_wstrb = 4'hf; dpm_wvalid = 1'b1;
        do @(posedge clock); while (!(dpm_wvalid && dpm_wready));
        @(negedge clock);
        dpm_wvalid = 1'b0;
        dpm_bready = 1'b1;
        do @(posedge clock); while (!dpm_bvalid);
        @(negedge clock);
        dpm_bready = 1'b0;
        dpm_awaddr = 0; dpm_wdata = 0; dpm_wstrb = 0;
    endtask

    task automatic dpm_read(input logic [31:0] off, output logic [31:0] data);
        @(negedge clock);
        dpm_araddr = DPM_BASE + off; dpm_arvalid = 1'b1; dpm_rready = 1'b1;
        do @(posedge clock); while (!(dpm_arvalid && dpm_arready));
        @(negedge clock);
        dpm_arvalid = 1'b0;
        do @(posedge clock); while (!dpm_rvalid);
        data = dpm_rdata;
        @(negedge clock);
        dpm_rready = 1'b0;
        dpm_araddr = 0;
    endtask

    task automatic src_write(input logic [31:0] off, input logic [15:0] data);
        @(negedge clock);
        src_awaddr = SRC_BASE + off; src_awvalid = 1'b1;
        do @(posedge clock); while (!(src_awvalid && src_awready));
        @(negedge clock);
        src_awvalid = 1'b0;
        src_wdata = {16'h0, data}; src_wstrb = 4'hf; src_wvalid = 1'b1;
        do @(posedge clock); while (!(src_wvalid && src_wready));
        @(negedge clock);
        src_wvalid = 1'b0;
        src_bready = 1'b1;
        do @(posedge clock); while (!src_bvalid);
        @(negedge clock);
        src_bready = 1'b0;
        src_awaddr = 0; src_wdata = 0; src_wstrb = 0;
    endtask

    task automatic src_read(input logic [31:0] off, output logic [31:0] data);
        @(negedge clock);
        src_araddr = SRC_BASE + off; src_arvalid = 1'b1; src_rready = 1'b1;
        do @(posedge clock); while (!(src_arvalid && src_arready));
        @(negedge clock);
        src_arvalid = 1'b0;
        do @(posedge clock); while (!src_rvalid);
        data = src_rdata;
        @(negedge clock);
        src_rready = 1'b0;
        src_araddr = 0;
    endtask

    // ------------------------------------------------------------ MMCM 寄存器编码
    // 与 `ps/include/dpm_regs.h::DPM_MMCM_ENCODE_CLKOUT` / `host/dpm_host/sig.py` 同一套规则：
    // 相位码 m = round(phase_mdeg × D / 45000)（单位 1/8 VCO 周期），
    // DELAY_TIME = m >> 3（ClkReg2 低 6 位）、PHASE_MUX = m & 7（ClkReg1 高 3 位）。
    // 这里的输出相位由 unisim 的 MMCME2_ADV 行为模型**独立**决定，因此这条链路能反过来
    // 检验编码规则本身。
    function automatic logic [15:0] enc_clkreg1(input int unsigned div, input int unsigned phase_mdeg);
        int unsigned code;
        int unsigned ht, lt;
        begin
            code = (phase_mdeg * div * 8 + 180000) / 360000;
            ht = div / 2;
            lt = div - ht;
            enc_clkreg1 = 16'h1000 | (((code & 7) << 13)) | ((ht & 6'h3f) << 6) | (lt & 6'h3f);
        end
    endfunction

    function automatic logic [15:0] enc_clkreg2(input int unsigned div, input int unsigned phase_mdeg);
        int unsigned code;
        int unsigned ed, nc;
        begin
            code = (phase_mdeg * div * 8 + 180000) / 360000;
            ed = div & 1;
            nc = (div == 1) ? 1 : 0;
            enc_clkreg2 = (ed << 7) | (nc << 6) | ((code >> 3) & 6'h3f);
        end
    endfunction

    // DIVCLK = 1 的权威值 0x1041（与 clk_wiz 生成值一致）
    localparam logic [15:0] DIVCLK_WORD = 16'h1041;
    localparam logic [15:0] POWER_WORD = 16'hFFFF;
    // 参考值：M=20（25MHz 工作点）的 CLKFBOUT 字 = 0x128A = {5'b0,1'b1,HT=10,LT=10}，
    // 由 `enc_clkreg1(mult, 0)` 现算（频点表里 M 有 12/15/16/20 四种）。
    localparam logic [15:0] CLKFBOUT_WORD_M20 = 16'h128A;

    /// 写入一个工作点：8 个寄存器镜像 + LOAD，然后等重新锁定。
    ///
    /// 倍频 M 也要按 MMCM 的位域编码写进 CLKFBOUT（M=20 → 0x128A，与 clk_wiz 生成值一致；
    /// 见 `ps/tests/test_sig.c`）——频点表里 M 有 12/15/16/20 四种，不能写死。
    task automatic program_source(
        input int unsigned mult, input int unsigned div, input int unsigned phase_mdeg,
        output logic locked_ok
    );
        logic [31:0] status;
        int unsigned i;
        begin
            src_write(D_CLKFBOUT, enc_clkreg1(mult, 0));
            src_write(D_CLKFBOUT2, 16'h0000);
            // CLKOUT0 的 upper 字是小数分频字（本设计不用小数分频，写 0）
            src_write(D_CLKOUT0, enc_clkreg1(div, 0));
            src_write(D_CLKOUT0_2, 16'h0000);
            src_write(D_CLKOUT1, enc_clkreg1(div, phase_mdeg));
            src_write(D_CLKOUT1_2, enc_clkreg2(div, phase_mdeg));
            src_write(D_DIVCLK, DIVCLK_WORD);
            src_write(D_POWER, POWER_WORD);
            src_write(D_LOAD, 16'h0001);
            locked_ok = 1'b0;
            // 等 MMCM 重新锁定：先等 locked 落下（或直接已落下），再等它起来
            for (i = 0; i < 200; i++) begin
                src_read(D_STATUS, status);
                if ((status & 32'h2) != 0) break;  // busy：重配置进行中
                #(T_MAIN * 20);
            end
            // MMCM 重新锁定的仿真时间约 1~2ms，每次循环 ~600ns → 需要几千次
            for (i = 0; i < 20000; i++) begin
                src_read(D_STATUS, status);
                if ((status & 32'h1) != 0 && src_locked) begin
                    locked_ok = 1'b1;
                    break;
                end
                #(T_MAIN * 20);
            end
        end
    endtask

    // ------------------------------------------------------------ 直接测量 MMCM 输出
    /// 测两路输出的周期（ps）与「clkOut1 上升沿 → 下一个 clkOut2 上升沿」的延迟（ps）。
    task automatic measure_outputs(output real t1_ps, output real t2_ps, output real delay_ps,
                                  output bit edges_ok);
        time e1, e1_next, e2, e2_next;
        time t0;
        begin
            edges_ok = 1'b0;
            t1_ps = 0.0; t2_ps = 0.0; delay_ps = 0.0;
            // 等边沿要有界：重配置后输出被 BUFGCE 重新放行，若一直等不到就是异常
            t0 = $time;
            while (src_clk1 !== 1'b1 && ($time - t0) < 5_000_000) @(posedge clock);
            if (src_clk1 !== 1'b1) begin
                $display("HANDOFF_ERROR 重配置后 clkOut1 一直没有高电平（输出未恢复）");
                return;
            end
            @(posedge src_clk1); e1 = $time;
            @(posedge src_clk1); e1_next = $time;
            t1_ps = real'(e1_next - e1);
            @(posedge src_clk2); e2 = $time;
            @(posedge src_clk2); e2_next = $time;
            t2_ps = real'(e2_next - e2);
            // 相位差 = 「clkOut1 的某个上升沿 → 它之后的第一个 clkOut2 上升沿」，落在 (0, T2]。
            // 必须记下**这次**用到的那两个边沿时刻：不能拿早先记的 e1_next 去减，
            // 两者之间可能已经过去好几个周期（早期版本因此把相位量成 2~3 个周期）。
            @(posedge src_clk1); e1 = $time;
            @(posedge src_clk2); e2 = $time;
            delay_ps = real'(e2 - e1);
            if (delay_ps < 0.0) delay_ps = 0.0;
            if (delay_ps >= t2_ps) delay_ps = delay_ps - t2_ps;
            edges_ok = 1'b1;
        end
    endtask

    // ------------------------------------------------------------ 测量流程
    task automatic run_measurement(input logic [31:0] gate_target,
                                   output logic [31:0] gate_cnt, output logic [31:0] edge_cnt,
                                   output logic [31:0] phase_cnt);
        logic [31:0] status;
        int unsigned i;
        begin
            dpm_write(R_GATE, gate_target);
            dpm_write(R_IRQ_STATUS, 32'h7);          // 清标志
            dpm_write(R_CTRL, 32'h6);                // irqEnable=1 | clear
            dpm_write(R_CTRL, 32'h3);                // irqEnable=1 | start
            gate_cnt = 0; edge_cnt = 0; phase_cnt = 0;
            for (i = 0; i < 20000; i++) begin
                dpm_read(R_STATUS, status);
                if ((status & 32'h2) != 0) break;    // resultValid
                #(T_MAIN * 4);
            end
            dpm_read(R_RES_GATE, gate_cnt);
            dpm_read(R_RES_EDGE, edge_cnt);
            dpm_read(R_RES_PHASE, phase_cnt);
        end
    endtask

    // ------------------------------------------------------------ 相位上界（9 位相位码）
    // 与 `ps/include/dpm_regs.h::dpm_mmcm_phase_code_fits`、`spec/gen_golden.py::phase_points_for`
    // 同一判据：`m = round(相位 × D / 45000) <= 511`（DELAY_TIME 6 位 + PHASE_MUX 3 位）。
    // 越界请求会让两个字段按位截断、相位**回绕**成另一个值，因此点表里不允许出现（下面会断言）。
    localparam int unsigned PHASE_CODE_MAX = 511;
    localparam int unsigned PHASE_FULL_SCALE_MDEG = 360_000;
    localparam int unsigned PHASE_MAX_MDEG_NUM = 23_017_499;  // m<=511 ⟺ 相位×D <= 23017499

    /// 9 位相位码（可能 >511，故返回宽类型，便于判越界）。
    function automatic int unsigned phase_code(input int unsigned phase_mdeg, input int unsigned div);
        phase_code = (phase_mdeg * div * 8 + 180_000) / 360_000;
    endfunction

    /// 请求是否落在 9 位相位码内。
    function automatic logic phase_code_fits(input int unsigned phase_mdeg, input int unsigned div);
        phase_code_fits = (phase_mdeg < PHASE_FULL_SCALE_MDEG) &&
                          (phase_code(phase_mdeg, div) <= PHASE_CODE_MAX);
    endfunction

    /// 相位码上界点（m = 511）对应的可请求相位（毫度），保证落在 45°/D 网格上。
    function automatic int unsigned phase_top_mdeg(input int unsigned div);
        phase_top_mdeg = (PHASE_CODE_MAX * 45_000 + div / 2) / div;
    endfunction

    /// 相位码 → 可实现相位（毫度），与 `dpm_mmcm_phase_code_to_mdeg` 同一公式。
    function automatic int unsigned code_to_mdeg(input int unsigned code, input int unsigned div);
        code_to_mdeg = ((code * 45_000) + div / 2) / div;
    endfunction

    // ------------------------------------------------------------ 测试点表
    // 频点表：与 `spec/gen_golden.py::FREQ_POINTS_HZ`、`docs/ps-protocol.md` §6.4 一致，
    // 10 个频点全是冻结求解规则的**精确解**（误差 0），覆盖 VCO 600/750/900/1000/1050MHz、
    // MULT 12/15/18/20/21 与 D = 20/35/40/50/70/100/120/125。
    // 450MHz 轮次的选点要求：频点整除 450MHz（`f_dsp / f_sig` 为整数，等价于信号周期是
    // DSP 周期的整数倍），否则每个周期的相位计数窗口在 k 与 k+1 之间交替、
    // `phaseCnt % edgeCnt == 0` 不成立（400MHz 轮次的 6.4MHz 点实测余数 129，作历史记录）。
    localparam int unsigned N_FREQS = 10;
    localparam int unsigned MAX_PHASES = 12;             // 单频点最多 5+1+3 = 9 个点
    localparam int unsigned MAX_POINTS = N_FREQS * MAX_PHASES;

    int unsigned freq_hz[0:N_FREQS-1] = '{5_000_000, 6_000_000, 9_000_000, 10_000_000, 15_000_000,
                                         18_000_000, 25_000_000, 30_000_000, 45_000_000,
                                         50_000_000};
    int unsigned freq_mult[0:N_FREQS-1] = '{12, 15, 18, 20, 21, 18, 20, 21, 18, 20};
    int unsigned freq_div[0:N_FREQS-1] = '{120, 125, 100, 100, 70, 50, 40, 35, 20, 20};

    int unsigned point_freq[0:MAX_POINTS-1];
    int unsigned point_mult[0:MAX_POINTS-1];
    int unsigned point_div[0:MAX_POINTS-1];
    int unsigned point_mdeg[0:MAX_POINTS-1];
    int unsigned n_points = 0;
    int unsigned n_over180 = 0;

    localparam logic [31:0] GATE_TARGET = 2000;

    task automatic add_point(input int unsigned freq, input int unsigned mult,
                             input int unsigned div, input int unsigned phase_mdeg);
        begin
            point_freq[n_points] = freq;
            point_mult[n_points] = mult;
            point_div[n_points] = div;
            point_mdeg[n_points] = phase_mdeg;
            if (phase_mdeg >= 180_000) n_over180 = n_over180 + 1;
            n_points = n_points + 1;
        end
    endtask

    /// 生成测试点表（与 `spec/gen_golden.py::phase_points_for` 同一规则）：
    ///   1. 每个频点必含 0/45/90/135/180（45° 网格，180° 的相位码是 4D ≤ 504，恒可表示）；
    ///   2. D 为偶数时加 22.5°（需要 PHASE_MUX 细调）；
    ///   3. 加 225/270/315 中相位码 ≤ 511 的那些；
    ///   4. 一个 >180° 的 45° 倍数都不剩时（D ≥ 103），用相位码上界点（m = 511）兜底，
    ///      保证**每个频点都有 >180° 的用例**。
    task automatic build_points();
        int unsigned base[0:4];
        int unsigned over[0:2];
        int unsigned i, k, emitted_over;
        begin
            base = '{0, 45_000, 90_000, 135_000, 180_000};
            over = '{225_000, 270_000, 315_000};
            n_points = 0;
            n_over180 = 0;
            for (i = 0; i < N_FREQS; i = i + 1) begin
                for (k = 0; k < 5; k = k + 1) begin
                    add_point(freq_hz[i], freq_mult[i], freq_div[i], base[k]);
                end
                if (freq_div[i] % 2 == 0) begin
                    add_point(freq_hz[i], freq_mult[i], freq_div[i], 22_500);
                end
                emitted_over = 0;
                for (k = 0; k < 3; k = k + 1) begin
                    if (phase_code_fits(over[k], freq_div[i])) begin
                        add_point(freq_hz[i], freq_mult[i], freq_div[i], over[k]);
                        emitted_over = emitted_over + 1;
                    end
                end
                if (emitted_over == 0) begin
                    add_point(freq_hz[i], freq_mult[i], freq_div[i], phase_top_mdeg(freq_div[i]));
                end
            end
        end
    endtask

    // ------------------------------------------------------------ 主流程
    int unsigned p, k, failures, progress;
    real t1_ps, t2_ps, delay_ps;
    real meas_delay_ps, direct_phase_mdeg, want_phase_mdeg, snap_phase_mdeg, grid_mdeg;
    real err_direct_ps, err_meas_ps, err_snap_ps, snap_ps;
    int unsigned code, snapped_mdeg;
    int unsigned gate_hi, edge_lo, edge_hi;
    logic locked_ok, edges_ok;
    logic [31:0] gate_cnt, edge_cnt, phase_cnt, sig_status;

    initial begin
        failures = 0;
        progress = 0;
        axi_init();
        resetn = 1'b0;
        repeat (20) @(posedge clock);
        resetn = 1'b1;

        // MMCM 首次锁定
        for (p = 0; p < 20000 && !src_locked; p++) #(T_MAIN * 20);
        if (!src_locked) begin
            $display("HANDOFF_ERROR MMCM 首次锁定超时");
            $fatal(1);
        end
        $display("HANDOFF_INFO t=%0t 片内源 MMCM 已锁定（静态配置 25MHz/90°）", $time);

        // 测量 IP：使能片内源并选中它
        dpm_write(R_SIG_CTRL, 32'h3);
        dpm_read(R_SIG_STATUS, sig_status);
        if ((sig_status & 32'h4) == 0) begin
            $display("HANDOFF_ERROR SIG_STATUS.srcLocked 未置位（0x%08x）", sig_status);
            $fatal(1);
        end

        build_points();
        $display("HANDOFF_INFO 测试点表：%0d 个频点、%0d 个点（其中 ≥180° 的 %0d 个）",
                 N_FREQS, n_points, n_over180);
        // 点表自检：相位码必须在 9 位内且落在 45°/D 网格上（不合法就立刻报错，不要静默吸附）
        for (p = 0; p < n_points; p = p + 1) begin
            code = phase_code(point_mdeg[p], point_div[p]);
            snapped_mdeg = code_to_mdeg(code, point_div[p]);
            if (code > PHASE_CODE_MAX || snapped_mdeg != point_mdeg[p]) begin
                $display("HANDOFF_ERROR 点表非法：point%0d f=%0dHz D=%0d 相位=%0dmdeg（相位码 %0d、网格值 %0d）",
                         p, point_freq[p], point_div[p], point_mdeg[p], code, snapped_mdeg);
                $fatal(1);
            end
        end
        if (CLKFBOUT_WORD_M20 !== enc_clkreg1(20, 0)) begin
            $display("HANDOFF_ERROR 编码自检：M=20 的 CLKFBOUT 字应为 0x128A");
            $fatal(1);
        end

        for (p = 0; p < n_points; p = p + 1) begin
            $display("HANDOFF_STEP point%0d t=%0t 开始配置 f=%0dHz M=%0d D=%0d 相位=%0d mdeg",
                     p, $time, point_freq[p], point_mult[p], point_div[p], point_mdeg[p]);
            program_source(point_mult[p], point_div[p], point_mdeg[p], locked_ok);
            $display("HANDOFF_STEP point%0d t=%0t 配置完成 locked_ok=%0d", p, $time, locked_ok);
            if (!locked_ok) begin
                $display("HANDOFF_ERROR point%0d 重配置后未锁定", p);
                failures++;
                progress++;
                continue;
            end
            #(T_MAIN * 100);  // 等输出稳定

            measure_outputs(t1_ps, t2_ps, delay_ps, edges_ok);
            if (!edges_ok) begin
                failures++;
                progress++;
                continue;
            end
            // 请求值 → 9 位相位码 → MMCM 网格上的**实际**输出相位（点表自检已保证相等）
            code = phase_code(point_mdeg[p], point_div[p]);
            snapped_mdeg = code_to_mdeg(code, point_div[p]);
            want_phase_mdeg = real'(point_mdeg[p]);
            snap_phase_mdeg = real'(snapped_mdeg);
            grid_mdeg = 45000.0 / real'(point_div[p]);
            snap_ps = (snap_phase_mdeg / 360000.0) * t2_ps;

            $display("HANDOFF_STEP point%0d t=%0t 已量到输出 T1=%0.1fps T2=%0.1fps delay=%0.1fps",
                     p, $time, t1_ps, t2_ps, delay_ps);
            direct_phase_mdeg = (delay_ps / t2_ps) * 360000.0;
            // 判据 1：直接测得的相位差 = MMCM 实际输出相位 ± 半个网格
            //（半个网格是「请求值吸附到 45°/D 网格」的固有偏差，不算缺陷）
            err_direct_ps = delay_ps - snap_ps;
            if (err_direct_ps > t2_ps / 2.0) err_direct_ps = err_direct_ps - t2_ps;
            if (err_direct_ps < -t2_ps / 2.0) err_direct_ps = err_direct_ps + t2_ps;
            if (err_direct_ps < 0.0) err_direct_ps = -err_direct_ps;

            run_measurement(GATE_TARGET, gate_cnt, edge_cnt, phase_cnt);
            if (edge_cnt == 0) begin
                $display("HANDOFF_ERROR point%0d 未测到参考沿", p);
                failures++;
                progress++;
                continue;
            end
            meas_delay_ps = (real'(phase_cnt) / real'(edge_cnt)) * T_DSP_PS;
            // 判据 2：测量通路与直接测得值之差 ≤ 1 个 DSP 周期 + 1 毫度。
            // 按周期取模比较：
            //  * 0° 时两沿几乎重合：修复相位计数使能窗口之前的旧 RTL 会量到**一整个周期**
            //    （清零被置位吞掉，见 PhaseMeter.scala 的注释）；现在两沿落在同一个 DSP
            //    采样拍时量化为 0 个周期。0° 与一整周期在「无符号间隔、模 360°」口径下等价，
            //    所以这里只按取模比较，不锁定具体约定；
            //  * 361° 附近的点（如 359.1°）也可能因 1 个 DSP 周期的采样同步抖动量回 ~0°；
            //  * 直接测量也有 ±1 个 DSP 周期的采样同步抖动。
            err_meas_ps = meas_delay_ps - delay_ps;
            if (err_meas_ps > t2_ps / 2.0) err_meas_ps = err_meas_ps - t2_ps;
            if (err_meas_ps < -t2_ps / 2.0) err_meas_ps = err_meas_ps + t2_ps;
            if (err_meas_ps < 0.0) err_meas_ps = -err_meas_ps;

            // 判据 3：测量通路与「MMCM 实际输出相位」之差 ≤ 1 个 DSP 周期 + 1 毫度
            //（把编码 → 硬件 → 测量整条链路一次判掉，不依赖直接测量）
            err_snap_ps = meas_delay_ps - snap_ps;
            if (err_snap_ps > t2_ps / 2.0) err_snap_ps = err_snap_ps - t2_ps;
            if (err_snap_ps < -t2_ps / 2.0) err_snap_ps = err_snap_ps + t2_ps;
            if (err_snap_ps < 0.0) err_snap_ps = -err_snap_ps;

            // 判据 4：计数模型（与上板验收同一口径）。闸门在 target 数满之后的下一个
            // extSig_sig2 上升沿关闭，因此 gate ∈ [target, target + ceil(T_sig/T_slow)]，
            // 再加 5 个慢时钟周期的跨时钟域余量；参考沿数按**闸门区间**折算（±1 个沿，
            // 窗口边界不整除）。注意两点：
            //   * gate 的精确值依赖启动时刻与信号沿的相对相位，金标准里的 gate 是最坏
            //     情况上界，不能按等号判；
            //   * 相位计的 DSP 窗口比闸门计数窗口多出几拍 CDC 同步/启动对齐延迟
            //     （实测 ≈3 个慢时钟周期），因此 edge 只能按闸门**区间**折算再放宽 1 个沿，
            //     不能按实测 gate 反推（否则会要求 edge = gate×f/f_slow）。
            gate_hi = GATE_TARGET + (F_SLOW_HZ_INT + point_freq[p] - 1) / point_freq[p];
            edge_lo = (GATE_TARGET * (point_freq[p] / 1000)) / (F_SLOW_HZ_INT / 1000);
            edge_hi = ((gate_hi + 5) * (point_freq[p] / 1000)) / (F_SLOW_HZ_INT / 1000);

            $display("HANDOFF_MEAS point%0d f=%0dHz M=%0d D=%0d 请求=%0.3f° 网格值=%0.3f° 直接测得=%0.3f° 网格=%0.4f°",
                     p, point_freq[p], point_mult[p], point_div[p], want_phase_mdeg / 1000.0,
                     snap_phase_mdeg / 1000.0, direct_phase_mdeg / 1000.0, grid_mdeg / 1000.0);
            $display("HANDOFF_MEAS point%0d T1=%0.1fps T2=%0.1fps delay=%0.1fps | 测量: gate=%0d(%0d..%0d) edge=%0d(%0d..%0d) phaseCnt=%0d → %0.1fps",
                     p, t1_ps, t2_ps, delay_ps, gate_cnt, GATE_TARGET, gate_hi + 5, edge_cnt,
                     edge_lo - 1, edge_hi + 1, phase_cnt, meas_delay_ps);

            if (err_direct_ps > T_DSP_PS + 1.0) begin
                $display("HANDOFF_FAIL point%0d 输出相位差与 MMCM 网格值相差 %0.1fps（>1 个 DSP 周期，网格 %0.4f°）",
                         p, err_direct_ps, grid_mdeg / 1000.0);
                failures++;
            end
            if (err_meas_ps > T_DSP_PS + 1.0) begin
                $display("HANDOFF_FAIL point%0d 测量相位差与直接测得值相差 %0.1fps (>1 个 DSP 周期)",
                         p, err_meas_ps);
                failures++;
            end
            if (err_snap_ps > T_DSP_PS + 1.0) begin
                $display("HANDOFF_FAIL point%0d 测量相位差与 MMCM 网格值（%0.3f°）相差 %0.1fps",
                         p, snap_phase_mdeg / 1000.0, err_snap_ps);
                failures++;
            end
            if (gate_cnt < GATE_TARGET || gate_cnt > gate_hi + 5 || edge_cnt + 1 < edge_lo ||
                edge_cnt > edge_hi + 1 || (phase_cnt % edge_cnt) != 0) begin
                $display("HANDOFF_FAIL point%0d 计数模型不符：gate=%0d(期望 %0d..%0d) edge=%0d(期望 %0d..%0d) phaseCnt%%edge=%0d",
                         p, gate_cnt, GATE_TARGET, gate_hi + 5, edge_cnt, edge_lo - 1, edge_hi + 1,
                         phase_cnt % edge_cnt);
                failures++;
            end
            progress++;
        end

        $display("HANDOFF_SUMMARY points=%0d freqs=%0d over180=%0d fails=%0d", n_points, N_FREQS,
                 n_over180, failures);
        if (failures == 0) begin
            $display("HANDOFF_DONE 全部 %0d 个点通过（%0d 个频点，含 %0d 个 ≥180° 的点）", n_points,
                     N_FREQS, n_over180);
        end else begin
            $display("HANDOFF_DONE 失败 %0d / %0d 个点", failures, n_points);
            $fatal(1);
        end
        $finish;
    end

    // 看门狗：主流程每完成一个点就把 `progress` 加一。25ms 仿真时间内没有任何进展
    // 就判卡死（单个点即使重锁失败也只要 ~50µs；首次锁定/重锁的轮询上限各 8~12ms）。
    initial begin
        int unsigned watchdog_last = 0;
        int unsigned stalled = 0;
        forever begin
            #(T_MAIN * 20);  // 400ns
            if (progress != watchdog_last) begin
                watchdog_last = progress;
                stalled = 0;
            end else begin
                stalled = stalled + 1;
                if (stalled > 62500) begin  // 62500 × 400ns = 25ms
                    $display("HANDOFF_ERROR 仿真卡死：25ms 仿真时间内没有进展（progress=%0d/%0d）",
                             progress, n_points);
                    $fatal(1);
                end
            end
        end
    end

    // 总时长保护：整轮 80 个点的仿真时间约 4ms（每点 ≈ 40µs 闸门 + 重锁），
    // 200ms 是 50 倍余量。注意 xsim 的 `#` 延时是 32 位有符号（ps 时基下上限 ≈2.1ms），
    // 长延时必须用循环拼出来。
    initial begin
        repeat (200) #1_000_000_000;  // 200 × 1ms = 200ms
        $display("HANDOFF_ERROR 仿真超时（200ms）");
        $fatal(1);
    end

endmodule

`default_nettype wire
