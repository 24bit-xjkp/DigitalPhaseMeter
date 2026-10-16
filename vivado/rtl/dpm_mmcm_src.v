/** @file dpm_mmcm_src.v
 *  @brief 片内信号源用的裸 MMCME2_ADV 薄封装
 *
 * 「方案 A」的 PL 侧时钟资源。作用与原来的 `clk_wiz_src`（Clock Wizard）等价，
 * 但**不含任何重配置算术**：MMCM 的 DRP 口直接引到模块外部，由
 * `MmcmDrpController`（Chisel）把软件算好的 16 位寄存器值逐条写进去。
 *
 * ## 为什么需要这个文件
 *
 * Vivado 的 `create_bd_cell` **不接受 `-type primitive`**，而 `MMCME2_ADV` 是
 * unisim 原语、不在工程里（实测 `[filemgmt 56-587] Failed to resolve reference`），
 * 因此不能直接在 block design 里例化它。这里用一个薄 HDL 模块包一层，
 * `add_files` 后用 module reference 例化（与 `dpm-vivado-ip` skill 记录的做法一致）。
 *
 * ## 时钟结构（与厂商 clk_wiz 生成的一致）
 *
 * {{{
 *   clkIn ──► MMCME2_ADV ──┬─► CLKFBOUT ──► BUFG ──► CLKFBIN     （反馈，ZHOLD 补偿）
 *                          ├─► CLKOUT0  ──► BUFGCE ──► clkOut1   （参考输出）
 *                          └─► CLKOUT1  ──► BUFGCE ──► clkOut2   （移相输出）
 * }}}
 *
 * **Safe Clock Startup**：`locked` 经各自的 8 位移位寄存器（用同一 MMCM 输出经
 * `BUFH` 打拍）连续采样 8 次后才拉高对应的 `BUFGCE.CE`，因此 MMCM 锁定前输出
 * 不会放行。这与 clk_wiz 的 `USE_SAFE_CLOCK_STARTUP` 行为一致，也避免了
 * 「组合逻辑门控时钟」（UG949 点名的反模式）。
 *
 * ## DRP
 *
 * `drpDaddr`/`drpDi`/`drpDen`/`drpDwe`/`drpDout`/`drpDrdy` 直接对应
 * MMCME2_ADV 的同名端口。DCLK 由外部（`MmcmDrpController` 所在的主时钟域）驱动，
 * 这里把它接成 `drpDclk` 输入。
 *
 * `drpReset` 是**重配置复位**（高有效，由 `MmcmDrpController` 在写 DRP 期间拉高），
 * 与系统复位 `reset` 相或后接 MMCM 的 `RST`。MMCM 的 DRP 写要求器件处于复位状态，
 * 且相位延迟单元只在复位/重新锁定时重新初始化——不拉这个复位时实测「分频字生效、
 * 相位字不生效」。
 *
 * ## 注意
 *
 * 本模块只负责时钟产生与门控；**不改变任何 MMCM 配置**。复位后 MMCM 使用
 * `CLKFBOUT_MULT_F` / `CLKOUTx_DIVIDE` 等参数给出的静态配置；软件在初始化时
 * 通过 DRP 写入目标工作点。
 */

`timescale 1ns/1ps
`default_nettype none

module dpm_mmcm_src #(
    /// 参考时钟周期（ns），用于 MMCM 的抖动/带宽计算
    parameter real CLKIN_PERIOD = 20.000,
    /// VCO 倍频（CLKFBOUT_MULT_F）
    parameter real CLKFBOUT_MULT_F = 20.000,
    /// 输入分频（DIVCLK_DIVIDE）
    parameter integer DIVCLK_DIVIDE = 1,
    /// CLKOUT0 分频（clkOut1，参考输出）
    parameter real CLKOUT0_DIVIDE_F = 40.000,
    /// CLKOUT1 分频（clkOut2，移相输出）
    parameter integer CLKOUT1_DIVIDE = 40,
    /// CLKOUT1 静态相位（度）；运行期由 DRP 覆盖
    parameter real CLKOUT1_PHASE = 90.000
) (
    /// 参考时钟（BD 里接 FCLK_CLK0 = 50MHz）
    input  wire        clkIn,
    /// 高有效复位（BD 里接 proc_sys_reset 的 peripheral_reset）
    input  wire        reset,
    /// 参考输出（与 clkOut2 同频，相位差由 DRP 设定）
    output wire        clkOut1,
    /// 移相输出
    output wire        clkOut2,
    /// MMCM 锁定指示
    output wire        locked,
    // ---- 动态重配置口（接 MmcmDrpController）----
    input  wire        drpDclk,
    input  wire [6:0]  drpDaddr,
    input  wire [15:0] drpDi,
    input  wire        drpDen,
    input  wire        drpDwe,
    output wire [15:0] drpDout,
    output wire        drpDrdy,
    /// 重配置复位（高有效）：控制器在写 DRP 期间拉高，与系统复位相或后接 MMCM.RST
    input  wire        drpReset
);

    wire clkfbout;
    wire clkfbout_buf;
    wire clkout0_raw;
    wire clkout1_raw;
    wire locked_int;
    wire reset_high;
    wire psdone_unused;
    wire clkfbstopped_unused;
    wire clkinstopped_unused;
    wire clkout0b_unused;
    wire clkout1b_unused;
    wire clkout2_unused;
    wire clkout2b_unused;
    wire clkout3_unused;
    wire clkout3b_unused;
    wire clkout4_unused;
    wire clkout5_unused;
    wire clkout6_unused;
    wire clkfboutb_unused;

    // MMCM 的复位 = 系统复位 或 重配置复位（DRP 写期间必须让 MMCM 处于复位）
    assign reset_high = reset | drpReset;

    // Safe Clock Startup：每个输出各自 8 位移位寄存器，连续 8 拍锁定后才放行。
    // ASYNC_REG + KEEP 与厂商生成物一致（该寄存器由复位异步清零）。
    (* KEEP = "TRUE" *) (* ASYNC_REG = "TRUE" *) reg [7:0] seq_reg1 = 8'h00;
    (* KEEP = "TRUE" *) (* ASYNC_REG = "TRUE" *) reg [7:0] seq_reg2 = 8'h00;

    MMCME2_ADV #(
        .BANDWIDTH            ("OPTIMIZED"),
        .CLKOUT4_CASCADE      ("FALSE"),
        .COMPENSATION         ("ZHOLD"),
        .STARTUP_WAIT         ("FALSE"),
        .DIVCLK_DIVIDE        (DIVCLK_DIVIDE),
        .CLKFBOUT_MULT_F      (CLKFBOUT_MULT_F),
        .CLKFBOUT_PHASE       (0.000),
        .CLKFBOUT_USE_FINE_PS ("FALSE"),
        .CLKOUT0_DIVIDE_F     (CLKOUT0_DIVIDE_F),
        .CLKOUT0_PHASE        (0.000),
        .CLKOUT0_DUTY_CYCLE   (0.500),
        .CLKOUT0_USE_FINE_PS  ("FALSE"),
        .CLKOUT1_DIVIDE       (CLKOUT1_DIVIDE),
        .CLKOUT1_PHASE        (CLKOUT1_PHASE),
        .CLKOUT1_DUTY_CYCLE   (0.500),
        .CLKOUT1_USE_FINE_PS  ("FALSE"),
        .CLKIN1_PERIOD        (CLKIN_PERIOD)
    ) mmcm_adv_inst (
        .CLKFBOUT     (clkfbout),
        .CLKFBOUTB    (clkfboutb_unused),
        .CLKOUT0      (clkout0_raw),
        .CLKOUT0B     (clkout0b_unused),
        .CLKOUT1      (clkout1_raw),
        .CLKOUT1B     (clkout1b_unused),
        .CLKOUT2      (clkout2_unused),
        .CLKOUT2B     (clkout2b_unused),
        .CLKOUT3      (clkout3_unused),
        .CLKOUT3B     (clkout3b_unused),
        .CLKOUT4      (clkout4_unused),
        .CLKOUT5      (clkout5_unused),
        .CLKOUT6      (clkout6_unused),
        .CLKFBIN      (clkfbout_buf),
        .CLKIN1       (clkIn),
        .CLKIN2       (1'b0),
        .CLKINSEL     (1'b1),
        .DADDR        (drpDaddr),
        .DCLK         (drpDclk),
        .DEN          (drpDen),
        .DI           (drpDi),
        .DO           (drpDout),
        .DRDY         (drpDrdy),
        .DWE          (drpDwe),
        .PSCLK        (1'b0),
        .PSEN         (1'b0),
        .PSINCDEC     (1'b0),
        .PSDONE       (psdone_unused),
        .LOCKED       (locked_int),
        .CLKINSTOPPED (clkinstopped_unused),
        .CLKFBSTOPPED (clkfbstopped_unused),
        .PWRDWN       (1'b0),
        .RST          (reset_high)
    );

    assign locked = locked_int;

    // 反馈时钟缓冲
    BUFG clkf_buf (
        .O (clkfbout_buf),
        .I (clkfbout)
    );

    // ---- 输出 1（参考） ----
    BUFGCE clkout1_buf (
        .O  (clkOut1),
        .CE (seq_reg1[7]),
        .I  (clkout0_raw)
    );
    wire clkout1_en_clk;
    BUFH clkout1_buf_en (
        .O (clkout1_en_clk),
        .I (clkout0_raw)
    );
    always @(posedge clkout1_en_clk or posedge reset_high) begin
        if (reset_high == 1'b1) begin
            seq_reg1 <= 8'h00;
        end else begin
            seq_reg1 <= {seq_reg1[6:0], locked_int};
        end
    end

    // ---- 输出 2（移相） ----
    BUFGCE clkout2_buf (
        .O  (clkOut2),
        .CE (seq_reg2[7]),
        .I  (clkout1_raw)
    );
    wire clkout2_en_clk;
    BUFH clkout2_buf_en (
        .O (clkout2_en_clk),
        .I (clkout1_raw)
    );
    always @(posedge clkout2_en_clk or posedge reset_high) begin
        if (reset_high == 1'b1) begin
            seq_reg2 <= 8'h00;
        end else begin
            seq_reg2 <= {seq_reg2[6:0], locked_int};
        end
    end

endmodule

`default_nettype wire
