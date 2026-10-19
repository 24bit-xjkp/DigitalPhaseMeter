/** @file dpm_cmd.c
 *  @brief 命令处理核心：分发、参数校验、单次/连续测量状态机、流上报、通知帧。
 *
 * 本文件**不接触板级外设**：所有 PL 访问经 `dpm_io_t`，因此 PS 侧与宿主机验证台
 * 共用同一份实现（见 `ps/include/dpm_cmd.h`）。
 *
 * 与 PL 的交互要点（`src/main/scala/DigitalPhaseMeter/ControlRegs.scala`）：
 *   - 每次写 CTRL 都要带上当前 irqEnable（b1 是普通读写位）；
 *   - 结果读走后必须写 IRQ_STATUS = PENDING | RESULT_READ，否则下一次结果被记为丢失；
 *   - 片内源配置（SIG_CTRL）写入后，写通道可能被拉低 ready，配置要轮询 SIG_STATUS 确认；
 *   - START 在 busy 期间被忽略，软件侧对应 ERR_BUSY。
 */

#include "dpm_cmd.h"
#include "dpm_regs.h"

/* ------------------------------------------------------------- 默认参数（§7） */

#define DPM_DEFAULT_GATE_TARGET 2000u
#define DPM_DEFAULT_MODE DPM_MODE_SINGLE
#define DPM_DEFAULT_SOURCE DPM_SRC_INTERNAL
#define DPM_DEFAULT_STREAM_INTERVAL_MS 100u

/* ------------------------------------------------------------- 小工具 */

/** 无 libc：自己清零。 */
static void ctx_zero(dpm_cmd_ctx_t* c)
{
    uint8_t* p = (uint8_t*)c;
    uint32_t i;
    for(i = 0u; i < (uint32_t)sizeof(*c); i++) { p[i] = 0u; }
}

static void put_u8(uint8_t* p, uint8_t v) { p[0] = v; }

/** 组一条响应（成功路径）并累加命令计数。 */
static uint32_t cmd_reply(dpm_cmd_ctx_t* c, uint8_t cmd, const uint8_t* payload, size_t len, uint8_t* out, uint32_t out_cap)
{
    size_t n = dpm_frame_encode(out, (size_t)out_cap, DPM_CMD_RESP(cmd), payload, len);
    if(n != 0u) { c->cmd_count++; }
    return (uint32_t)n;
}

/** 组一条错误响应（PAYLOAD 为空）。 */
static uint32_t cmd_reply_err(dpm_cmd_ctx_t* c, uint8_t code, uint8_t* out, uint32_t out_cap)
{
    if(code == DPM_RESP_ERR_PARAM) { c->err_param_count++; }
    else
    {
        c->err_busy_count++;
    }
    return (uint32_t)dpm_frame_encode(out, (size_t)out_cap, code, 0, 0u);
}

/** 回读的 `phaseField`：MMCM ClkReg2 的 DELAY_TIME 字段（整数 VCO 周期，0..63）。
 *
 * 方案 A 下相位可控（PS 直接编码 MMCM 寄存器），本字段回报实际写入的 DELAY_TIME；
 * 完整相位（含 PHASE_MUX 的 1/8 VCO 周期细调）看响应里的 `phaseMilliDeg`。
 * 旧 bitstream（`DPM_DRP_PHASE_WRITABLE = 0`）下按协议定稿一律回 0。 */
static uint8_t phase_field_out(uint16_t field)
{
#if DPM_DRP_PHASE_WRITABLE
    return (uint8_t)field;
#else
    (void)field;
    return 0u;
#endif
}

/* ------------------------------------------------------------- 初始化 */

void dpm_cmd_init(dpm_cmd_ctx_t* c, const dpm_io_t* io, void* io_ctx, const dpm_clocks_t* clk)
{
    if(c == 0 || io == 0) { return; }
    ctx_zero(c);
    c->io = io;
    c->io_ctx = io_ctx;
    if(clk != 0) { c->clk = *clk; }
    else
    {
        c->clk.slow_hz = 50000000u;
        c->clk.dsp_hz = 450000000u;
    }
    c->gate_target = DPM_DEFAULT_GATE_TARGET;
    c->mode = DPM_DEFAULT_MODE;
    c->source = DPM_DEFAULT_SOURCE;
    c->irq_enable = 1u;
    c->stream_enable = 0u;
    c->stream_interval_ms = DPM_DEFAULT_STREAM_INTERVAL_MS;

    /* 先写不涉及配置快照的寄存器；SIG_CTRL 放最后（它会引发跨域配置握手）。 */
    dpm_reg_wr(io, io_ctx, DPM_REG_GATE_TARGET, DPM_DEFAULT_GATE_TARGET);
    dpm_reg_wr(io, io_ctx, DPM_REG_CTRL, DPM_CTRL_IRQ_ENABLE);
    dpm_reg_wr(io, io_ctx, DPM_REG_CTRL, DPM_CTRL_IRQ_ENABLE | DPM_CTRL_CLEAR);

    /* 默认选片内源；未锁定时只置 select（enable 保持 0 → sourceReady = 0）。 */
    if((dpm_reg_rd(io, io_ctx, DPM_REG_SIG_STATUS) & DPM_SIGST_LOCKED) != 0u)
    {
        dpm_reg_wr(io, io_ctx, DPM_REG_SIG_CTRL, DPM_SIG_ENABLE | DPM_SIG_SELECT);
    }
    else
    {
        dpm_reg_wr(io, io_ctx, DPM_REG_SIG_CTRL, DPM_SIG_SELECT);
    }
    c->lost_report = dpm_reg_rd(io, io_ctx, DPM_REG_LOST_COUNT);
}

/* ------------------------------------------------------------- PL 读包装 */

uint32_t dpm_cmd_status(const dpm_cmd_ctx_t* c) { return dpm_reg_rd(c->io, c->io_ctx, DPM_REG_STATUS); }

uint32_t dpm_cmd_sig_status(const dpm_cmd_ctx_t* c) { return dpm_reg_rd(c->io, c->io_ctx, DPM_REG_SIG_STATUS); }

uint32_t dpm_cmd_drp_status(const dpm_cmd_ctx_t* c)
{
    /* 状态寄存器在 DRP 基址 + 0x004（不是「字 31 / 0x27C」，那里恒为 0）。
     * 见 `dpm_regs.h` 的 DPM_DRP_STATUS_OFF。 */
    return dpm_drp_rd(c->io, c->io_ctx, DPM_DRP_STATUS_OFF);
}

int dpm_cmd_source_ready(const dpm_cmd_ctx_t* c) { return (dpm_cmd_sig_status(c) & DPM_SIGST_READY) != 0u ? 1 : 0; }

/* ------------------------------------------------------------- 测量状态机 */

int dpm_cmd_start(dpm_cmd_ctx_t* c, uint32_t now_ms)
{
    uint32_t st;
    uint32_t ctrl;
    uint32_t st2;

    if(c == 0 || c->io == 0) { return 3; }
    st = dpm_cmd_status(c);
    if((st & DPM_STATUS_BUSY) != 0u)
    {
        c->started = 1u;
        return 2; /* PL 会忽略 busy 期间的 start */
    }
    if(dpm_cmd_source_ready(c) == 0) { return 1; /* 源不可用（片内源未使能/未锁定） */ }
    /* 在写 CTRL 之前记录结果槽的状态：PL 只保留最新一条结果，若启动前就有一个
     * 没被读走的旧结果，就必须用 RESULT_SEQ 变化来区分新旧（否则 GET_MEAS 会拿到旧值）。 */
    c->valid_before = ((st & DPM_STATUS_RESULT_VALID) != 0u) ? 1u : 0u;
    c->seq_before = dpm_reg_rd(c->io, c->io_ctx, DPM_REG_RESULT_SEQ);

    ctrl = DPM_CTRL_START;
    if(c->irq_enable != 0u) { ctrl |= DPM_CTRL_IRQ_ENABLE; }
    dpm_reg_wr(c->io, c->io_ctx, DPM_REG_CTRL, ctrl);
    /* 确认 start 生效：写通道可能被配置快照挂起；也可能测量已经跑完。 */
    st2 = dpm_cmd_status(c);
    if((st2 & (DPM_STATUS_BUSY | DPM_STATUS_RESULT_VALID)) == 0u) { return 3; }
    c->started = 1u;
    c->start_ms = now_ms;
    c->awaiting = 1u;
    return 0;
}

int dpm_cmd_fetch(dpm_cmd_ctx_t* c)
{
    uint32_t st;
    uint32_t lost_now;
    uint32_t delta = 0u;

    if(c == 0 || c->io == 0) { return 0; }
    st = dpm_cmd_status(c);
    if((st & DPM_STATUS_BUSY) != 0u) { return 0; /* 测量进行中，结果还没锁存 */ }
    if((st & DPM_STATUS_RESULT_VALID) == 0u) { return 0; }
    if(c->awaiting != 0u && c->valid_before != 0u && dpm_reg_rd(c->io, c->io_ctx, DPM_REG_RESULT_SEQ) == c->seq_before)
    {
        /* 启动前就有一个未读结果、且序号没变：这是旧结果，继续等本次测量 */
        return 0;
    }
    /* 4 个结果寄存器顺序读回（一次结果只有 4 个字，不需要突发） */
    c->last.gate_cnt = dpm_reg_rd(c->io, c->io_ctx, DPM_REG_RESULT_GATE);
    c->last.edge_cnt = dpm_reg_rd(c->io, c->io_ctx, DPM_REG_RESULT_EDGE);
    c->last.phase_cnt = dpm_reg_rd(c->io, c->io_ctx, DPM_REG_RESULT_PHASE);
    c->last.seq = dpm_reg_rd(c->io, c->io_ctx, DPM_REG_RESULT_SEQ);
    /* 结果已读走：必须写 b1，否则 PL 会把下一次结果记为丢失 */
    dpm_reg_wr(c->io, c->io_ctx, DPM_REG_IRQ_STATUS, DPM_IRQ_PENDING | DPM_IRQ_RESULT_READ);

    dpm_meas_calc(&c->last, &c->clk, &c->last_meas);
    c->have_result = 1u;
    c->seq_seen = c->last.seq;
    c->started = 0u;
    c->awaiting = 0u;

    /* 丢失计数（PL 在结果被覆盖时 +1；CLEAR/RESET 会让它回卷，按回卷处理） */
    lost_now = dpm_reg_rd(c->io, c->io_ctx, DPM_REG_LOST_COUNT);
    if(lost_now >= c->lost_report) { delta = lost_now - c->lost_report; }
    c->lost_report = lost_now;
    if(delta != 0u)
    {
        c->lost_pending += delta;
        c->lost_total += delta;
        /* 通知帧与流上报同开关：只有上位机显式打开流模式才主动上报 */
        if(c->stream_enable != 0u && c->emit != 0)
        {
            uint8_t pl[5];
            uint8_t frame[DPM_FRAME_MAX];
            dpm_event_t ev;
            ev.code = DPM_EVENTCODE_LOST;
            ev.detail = delta;
            size_t fn;
            put_u8(pl, ev.code);
            dpm_wr_u32(&pl[1], ev.detail);
            fn = dpm_frame_encode(frame, sizeof(frame), DPM_EVT_EVENT, pl, 5u);
            if(fn != 0u) { (void)c->emit(c->emit_user, frame, (uint32_t)fn); }
        }
    }
    return 1;
}

/** 等待 PL 空闲（STATUS.busy = 0）。返回 0 = 已空闲，1 = 超时。
 *
 * 用于「必须在 idle 时做」的命令：DRP 重配置（SET_SIG）与结果区清零（CLEAR）。
 * PL 的 CTRL.clear 在 busy 期间本来就是挂起的，这里等它落地再回报成功。 */
static int wait_idle(dpm_cmd_ctx_t* c, uint32_t timeout_ms, uint32_t poll_limit)
{
    uint32_t i;
    uint32_t t0 = 0u;
    uint8_t have_clock = 0u;

    if((dpm_cmd_status(c) & DPM_STATUS_BUSY) == 0u) { return 0; }
    if(c->io->now_ms != 0)
    {
        t0 = c->io->now_ms(c->io_ctx);
        have_clock = 1u;
    }
    for(i = 0u; i < poll_limit; i++)
    {
        if((dpm_cmd_status(c) & DPM_STATUS_BUSY) == 0u) { return 0; }
        if(have_clock != 0u && timeout_ms != 0u && (c->io->now_ms(c->io_ctx) - t0) >= timeout_ms) { return 1; }
        if(c->io->delay_ms != 0) { c->io->delay_ms(c->io_ctx, 0u); }
    }
    return 1;
}

int dpm_cmd_wait_result(dpm_cmd_ctx_t* c, uint32_t timeout_ms, uint32_t poll_limit)
{
    uint32_t i;
    uint32_t t0 = 0u;
    uint8_t have_clock;

    if(c == 0 || c->io == 0) { return 1; }
    if(dpm_cmd_fetch(c) != 0) { return 0; }
    if(c->io->now_ms != 0)
    {
        t0 = c->io->now_ms(c->io_ctx);
        have_clock = 1u;
    }
    else
    {
        have_clock = 0u;
    }

    for(i = 0u; i < poll_limit; i++)
    {
        uint32_t st;
        if(dpm_cmd_fetch(c) != 0) { return 0; }
        st = dpm_cmd_status(c);
        if((st & DPM_STATUS_BUSY) == 0u && c->started == 0u)
        {
            /* 测量已结束但没有新结果：再确认一次，仍无则报超时 */
            if(dpm_cmd_fetch(c) != 0) { return 0; }
            return 1;
        }
        if(have_clock != 0u && timeout_ms != 0u)
        {
            uint32_t now = c->io->now_ms(c->io_ctx);
            if((now - t0) >= timeout_ms) { return 1; }
        }
        if(c->io->delay_ms != 0) { c->io->delay_ms(c->io_ctx, 0u); /* 让出总线/时间片（允许为空实现） */ }
    }
    return dpm_cmd_fetch(c) != 0 ? 0 : 1;
}

/** 若正在测量或有未读结果则等一次；否则沿用缓存结果。返回 0 = 可以取用缓存。 */
static int ensure_result(dpm_cmd_ctx_t* c, uint32_t now_ms)
{
    uint32_t st = dpm_cmd_status(c);
    if((st & (DPM_STATUS_BUSY | DPM_STATUS_RESULT_VALID)) != 0u || c->started != 0u)
    {
        return dpm_cmd_wait_result(c, DPM_WAIT_TIMEOUT_MS(c->gate_target, c->clk.slow_hz), DPM_WAIT_POLL_LIMIT(c->gate_target));
    }
    (void)now_ms;
    return 0;
}

/* ------------------------------------------------------------- 周期任务 */

void dpm_cmd_tick(dpm_cmd_ctx_t* c, uint32_t now_ms)
{
    uint32_t i;
    uint32_t st;

    if(c == 0 || c->io == 0) { return; }

    /* 1) 取走已完成的结果。
     *
     * 只在**连续模式**或**流上报开启**时自动取走：单次模式下把结果留在 PL 里，
     * 上位机轮询 STATUS 能看到 resultValid = 1，再用 GET_RESULT/GET_MEAS 取走
     * （与真实 PL「锁存那一拍置 resultValid，软件读走才清」的时序一致）。 */
    if(c->mode == DPM_MODE_CONTINUOUS || c->stream_enable != 0u)
    {
        for(i = 0u; i < 4u; i++)
        {
            if(dpm_cmd_fetch(c) == 0) { break; }
            if(c->mode == DPM_MODE_CONTINUOUS) { (void)dpm_cmd_start(c, now_ms); }
        }
    }
    else
    {
        i = 0u;
    }

    /* 2) MMCM 失锁→恢复事件（只在流模式下主动上报） */
    st = dpm_cmd_sig_status(c);
    if((st & DPM_SIGST_LOCKED) != 0u)
    {
        if(c->locked_known != 0u && c->locked_last == 0u)
        {
            uint8_t pl[5];
            uint8_t frame[DPM_FRAME_MAX];
            size_t fn;
            c->relock_count++;
            if(c->stream_enable != 0u && c->emit != 0)
            {
                put_u8(pl, DPM_EVENTCODE_RELOCK);
                dpm_wr_u32(&pl[1], c->relock_count);
                fn = dpm_frame_encode(frame, sizeof(frame), DPM_EVT_EVENT, pl, 5u);
                if(fn != 0u) { (void)c->emit(c->emit_user, frame, (uint32_t)fn); }
            }
        }
        c->locked_last = 1u;
        c->locked_known = 1u;
    }
    else
    {
        c->locked_last = 0u;
    }

    /* 3) 流上报：按 intervalMs 节流 */
    if(c->stream_enable != 0u && c->have_result != 0u && c->emit != 0)
    {
        uint32_t interval = (c->stream_interval_ms != 0u) ? c->stream_interval_ms : 1u;
        if((now_ms - c->stream_ms) >= interval)
        {
            uint8_t pl[27];
            uint8_t frame[DPM_FRAME_MAX];
            uint32_t n = 0u;
            size_t fn;
            uint32_t lost_delta = c->lost_pending;
            c->stream_ms = now_ms;
            c->lost_pending = 0u;
            if(lost_delta > 0xFFFFu) { lost_delta = 0xFFFFu; }
            dpm_wr_u32(&pl[n], c->last.seq);
            n += 4u;
            dpm_wr_u32(&pl[n], c->last.gate_cnt);
            n += 4u;
            dpm_wr_u32(&pl[n], c->last.edge_cnt);
            n += 4u;
            dpm_wr_u32(&pl[n], c->last.phase_cnt);
            n += 4u;
            dpm_wr_u32(&pl[n], c->last_meas.freq_milli_hz);
            n += 4u;
            dpm_wr_u32(&pl[n], c->last_meas.phase_milli_deg);
            n += 4u;
            dpm_wr_u16(&pl[n], (uint16_t)lost_delta);
            n += 2u;
            /* 必须用编码函数返回的**帧总长**（= LEN + 4），不要自己算 n + 4：
             * LEN = payload + 1，少一个字节日志帧会被截断（CRC 高位丢失）。 */
            fn = dpm_frame_encode(frame, sizeof(frame), DPM_EVT_STREAM_DATA, pl, (size_t)n);
            if(fn != 0u) { (void)c->emit(c->emit_user, frame, (uint32_t)fn); }
        }
    }

    /* 4) 连续模式：没有测量在飞就补一次 */
    if(c->mode == DPM_MODE_CONTINUOUS)
    {
        st = dpm_cmd_status(c);
        if((st & DPM_STATUS_BUSY) == 0u && c->started == 0u) { (void)dpm_cmd_start(c, now_ms); }
    }
}

/* ------------------------------------------------------------- 命令处理 */

/** GET_SIG 用：从 MMCM 寄存器镜像反推当前工作点（无缓存时使用）。
 *
 * 方案 A 下 PL 侧的寄存器就是 **MMCM 的原始 16 位寄存器值**，因此反推要按
 * XAPP888 的位域解码（编码规则见 `dpm_regs.h` 的 `DPM_MMCM_ENCODE_CLKOUT`）：
 *
 *   ClkReg1 = {PHASE_MUX[15:13], 1'b1, HIGH_TIME[11:6], LOW_TIME[5:0]}
 *   ClkReg2 = {EDGE[7], NO_COUNT[6], DELAY_TIME[5:0]}
 *
 * 分频比 = HIGH_TIME + LOW_TIME（divide==1 时是 no_count 的特例，单独处理）。
 * 相位 = PHASE_MUX × 1/8 VCO 周期 + DELAY_TIME × 1 VCO 周期，换算见
 * `dpm_regs.h` 的 `dpm_mmcm_decode_phase_mdeg`（与正向编码互为逆运算）。
 */
static void sig_from_drp(const dpm_cmd_ctx_t* c, dpm_sig_t* out)
{
    uint32_t fb1 = dpm_drp_rd(c->io, c->io_ctx, DPM_DRP_OFF_CLKFBOUT);
    uint32_t fb2 = dpm_drp_rd(c->io, c->io_ctx, DPM_DRP_OFF_CLKFBOUT2);
    uint32_t c01 = dpm_drp_rd(c->io, c->io_ctx, DPM_DRP_OFF_CLKOUT0);
    uint32_t dv = dpm_drp_rd(c->io, c->io_ctx, DPM_DRP_OFF_DIVCLK);
    uint32_t c11 = dpm_drp_rd(c->io, c->io_ctx, DPM_DRP_OFF_CLKOUT1);
    uint32_t c12 = dpm_drp_rd(c->io, c->io_ctx, DPM_DRP_OFF_CLKOUT1_2);
    uint32_t mult, divclk, div;

    (void)fb2;
    /* divide==1 是特例（NO_COUNT 置位、HIGH/LOW_TIME 是占位的 1），用共享解码函数 */
    mult = dpm_mmcm_decode_divide(fb1);
    div = dpm_mmcm_decode_divide(c01);
    divclk = dpm_mmcm_decode_divclk(dv);
    if(mult == 0u || div == 0u || divclk == 0u) { return; }

    out->mult = (uint16_t)mult;
    out->divclk = (uint16_t)divclk;
    out->div = (uint16_t)div;
    out->vco_hz = (uint32_t)(((uint64_t)c->clk.slow_hz * (uint64_t)mult) / (uint64_t)divclk);
    out->freq_hz = (uint32_t)(((uint64_t)out->vco_hz + (uint64_t)div / 2u) / (uint64_t)div);
    out->phase_milli_deg = dpm_mmcm_decode_phase_mdeg(c11, c12, div);
    out->phase_field = (uint16_t)(c12 & 0x3Fu); /* DELAY_TIME */
    out->phase_step_ps = dpm_sig_phase_step_ps(out->vco_hz);
}

static uint32_t handle_identify(dpm_cmd_ctx_t* c, uint8_t* out, uint32_t cap)
{
    dpm_identify_t v;
    uint8_t pl[40];
    size_t n;

    v.ver = DPM_SW_VERSION;
    v.git_rev = DPM_GIT_REV;
    /* caps 与 flags 使用同一组位定义（协议 §3 定稿）：b0 片内源、b1 连续、b2 流、b3 中断。
     * 轮询实现（未编译中断路径）时 b3 = 0。 */
    v.caps = (uint32_t)DPM_FLAGS_VALUE;
    v.version_reg = dpm_reg_rd(c->io, c->io_ctx, DPM_REG_VERSION);
    v.caps_reg = dpm_reg_rd(c->io, c->io_ctx, DPM_REG_CAPS);
    v.dsp_clk_hz = c->clk.dsp_hz;
    v.slow_clk_hz = c->clk.slow_hz;
    v.flags = (uint16_t)DPM_FLAGS_VALUE;
#if !DPM_DRP_PHASE_WRITABLE
    /* b4 = phaseControllable：只会在旧 bitstream（DPM_DRP_PHASE_WRITABLE=0）下走到这里——
     * clk_out2 相位被静态 generic 固定为 90°，写相位字不生效（实测结论见
     * spec/drp_semantics.json），故本位保持 0。当前方案 A 下该宏为 1，走下面的 else 分支。 */
    v.flags &= (uint16_t)~DPM_FLAG_PHASE_CTRL;
#else
    v.flags |= (uint16_t)DPM_FLAG_PHASE_CTRL;
#endif
    n = dpm_enc_identify(pl, sizeof(pl), &v);
    if(n == 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap); }
    return cmd_reply(c, DPM_CMD_IDENTIFY, pl, n, out, cap);
}

static uint32_t handle_status(dpm_cmd_ctx_t* c, uint8_t* out, uint32_t cap)
{
    dpm_status_reply_t v;
    uint8_t pl[20];
    size_t n = 0u;

    v.status = dpm_cmd_status(c);
    v.sig_status = dpm_cmd_sig_status(c);
    v.gate_target = c->gate_target;
    v.lost_count = dpm_reg_rd(c->io, c->io_ctx, DPM_REG_LOST_COUNT);
    v.seq = dpm_reg_rd(c->io, c->io_ctx, DPM_REG_RESULT_SEQ);
    dpm_wr_u32(&pl[n], v.status);
    n += 4u;
    dpm_wr_u32(&pl[n], v.sig_status);
    n += 4u;
    dpm_wr_u32(&pl[n], v.gate_target);
    n += 4u;
    dpm_wr_u32(&pl[n], v.lost_count);
    n += 4u;
    dpm_wr_u32(&pl[n], v.seq);
    n += 4u;
    return cmd_reply(c, DPM_CMD_GET_STATUS, pl, n, out, cap);
}

static uint32_t handle_result(dpm_cmd_ctx_t* c, uint8_t* out, uint32_t cap, uint32_t now_ms)
{
    dpm_result_reply_t v;
    uint8_t pl[32];
    size_t n = 0u;
    int ready = (ensure_result(c, now_ms) == 0);

    /* 等不到结果**不是**「当前状态不允许该命令」，而是「选中的输入源没有边沿」这一正常
     * 输入状态（协议 §3.0：源没有边沿时测量永不完成）。协议同时规定 `GET_RESULT.valid`
     * 就是「结果是否就绪」的权威探针，所以这里必须回 `valid = 0`（计数全 0），
     * **不能回 ERR_BUSY**：
     *   - 回 ERR_BUSY 会让按协议写的上位机把它当致命错误中止整段测量程序
     *     （实测：切到没接信号的外部输入源后 START，GET_RESULT 回 0xFF，上位机报
     *     「设备错误：PS 返回错误响应 0xFF（ERR_BUSY），请求 0x05（GET_RESULT）」）；
     *   - 两个参照实现（`host/dpm_host/sim_server.py` 与 `spec/sim_server.py`）在无结果时
     *     也都是回 `valid = 0`，`ERR_BUSY` 只用于 SET_SOURCE/SET_SIG/START 这类状态受限命令。
     * 保留 `ensure_result` 的**有界**等待：`START` 之后紧接着一次 `GET_RESULT` 仍能在结果
     * 到来时一次读走（对同步式调用方方便），只是超时后表达方式改为 `valid = 0`。 */
    if(ready != 0)
    {
        v.valid = (c->have_result != 0u) ? 1u : 0u;
        v.r = c->last;
    }
    else
    {
        v.valid = 0u;
        v.r.seq = 0u;
        v.r.gate_cnt = 0u;
        v.r.edge_cnt = 0u;
        v.r.phase_cnt = 0u;
    }
    put_u8(&pl[n], v.valid);
    n += 1u;
    dpm_wr_u32(&pl[n], v.r.seq);
    n += 4u;
    dpm_wr_u32(&pl[n], v.r.gate_cnt);
    n += 4u;
    dpm_wr_u32(&pl[n], v.r.edge_cnt);
    n += 4u;
    dpm_wr_u32(&pl[n], v.r.phase_cnt);
    n += 4u;
    return cmd_reply(c, DPM_CMD_GET_RESULT, pl, n, out, cap);
}

static uint32_t handle_meas(dpm_cmd_ctx_t* c, uint8_t* out, uint32_t cap, uint32_t now_ms)
{
    dpm_meas_reply_t v;
    uint8_t pl[32];
    size_t n = 0u;

    if(ensure_result(c, now_ms) != 0) { return cmd_reply_err(c, DPM_RESP_ERR_BUSY, out, cap); }
    v.r = c->last;
    v.freq_milli_hz = c->last_meas.freq_milli_hz;
    v.phase_milli_deg = c->last_meas.phase_milli_deg;
    dpm_wr_u32(&pl[n], v.r.seq);
    n += 4u;
    dpm_wr_u32(&pl[n], v.r.gate_cnt);
    n += 4u;
    dpm_wr_u32(&pl[n], v.r.edge_cnt);
    n += 4u;
    dpm_wr_u32(&pl[n], v.r.phase_cnt);
    n += 4u;
    dpm_wr_u32(&pl[n], v.freq_milli_hz);
    n += 4u;
    dpm_wr_u32(&pl[n], v.phase_milli_deg);
    n += 4u;
    return cmd_reply(c, DPM_CMD_GET_MEAS, pl, n, out, cap);
}

static uint32_t handle_set_gate(dpm_cmd_ctx_t* c, const uint8_t* payload, uint8_t len, uint8_t* out, uint32_t cap)
{
    uint32_t gate;
    uint8_t pl[4];

    if(dpm_dec_set_gate(payload, len, &gate) == 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap); }
    if(gate == 0u || gate > DPM_GATE_TARGET_MAX) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap); }
    dpm_reg_wr(c->io, c->io_ctx, DPM_REG_GATE_TARGET, gate);
    c->gate_target = gate;
    dpm_wr_u32(pl, gate);
    return cmd_reply(c, DPM_CMD_SET_GATE, pl, 4u, out, cap);
}

static uint32_t handle_set_mode(dpm_cmd_ctx_t* c, const uint8_t* payload, uint8_t len, uint8_t* out, uint32_t cap)
{
    uint8_t mode;
    uint8_t pl[1];

    if(dpm_dec_set_mode(payload, len, &mode) == 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap); }
    if(mode != DPM_MODE_SINGLE && mode != DPM_MODE_CONTINUOUS) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap); }
    c->mode = mode;
    if(mode == DPM_MODE_CONTINUOUS)
    {
        /* 连续模式：命令返回后由 tick 立即补一次启动 */
        c->started = 0u;
    }
    put_u8(pl, mode);
    return cmd_reply(c, DPM_CMD_SET_MODE, pl, 1u, out, cap);
}

static uint32_t handle_set_source(dpm_cmd_ctx_t* c, const uint8_t* payload, uint8_t len, uint8_t* out, uint32_t cap)
{
    uint8_t src;
    uint8_t pl[4];
    uint32_t st;

    if(dpm_dec_set_source(payload, len, &src) == 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap); }
    if(src != DPM_SRC_EXTERNAL && src != DPM_SRC_INTERNAL) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap); }
    if(src == DPM_SRC_INTERNAL)
    {
        /* 未锁定就选片内源 → ERR_BUSY，且不改变硬件状态 */
        if((dpm_cmd_sig_status(c) & DPM_SIGST_LOCKED) == 0u) { return cmd_reply_err(c, DPM_RESP_ERR_BUSY, out, cap); }
        dpm_reg_wr(c->io, c->io_ctx, DPM_REG_SIG_CTRL, DPM_SIG_ENABLE | DPM_SIG_SELECT);
    }
    else
    {
        /* 外部引脚：内源使能清零（两路输出恒低，不再影响测量） */
        dpm_reg_wr(c->io, c->io_ctx, DPM_REG_SIG_CTRL, 0u);
    }
    c->source = src;
    st = dpm_cmd_sig_status(c); /* 回显 SIG_STATUS（同时确认配置快照已送出） */
    dpm_wr_u32(pl, st);
    return cmd_reply(c, DPM_CMD_SET_SOURCE, pl, 4u, out, cap);
}

static uint32_t handle_set_sig(dpm_cmd_ctx_t* c, const uint8_t* payload, uint8_t len, uint8_t* out, uint32_t cap)
{
    uint32_t freq;
    uint32_t phase;
    dpm_sig_t sig;
    dpm_sig_solve_t st;
    uint8_t pl[15];
    size_t n = 0u;
    int rc;

    if(dpm_dec_set_sig(payload, len, &freq, &phase) == 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap); }
    if(freq == 0u || phase >= DPM_MMCM_PHASE_FULL_SCALE_MDEG)
    {
        /* 相位请求的**协议范围**是 [0, 360000)：360° 与 0° 同义，满量程不接收。
         * 「能不能实现」由 9 位相位码决定且与 D 有关，要等求解出 D 后再判（见下）。 */
        return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap);
    }
    dpm_sig_solve(freq, phase, c->clk.slow_hz, &sig, &st);
    if(st.ok == 0 || st.error_milli_hz > DPM_SIG_FREQ_TOL_MILLI_HZ(freq))
    {
        return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap);
    }
    /* 相位上界 = 9 位相位码（与 D 有关，见 dpm_regs.h 的注 2）：
     * m > 511 时 MMCM 的 PHASE_MUX/DELAY_TIME 字段会截断，输出相位**回绕**而不是吸附，
     * 这类请求必须显式拒绝（旧固件一律拒 ≥180°，对 D ≤ 63 的频点过于保守）。 */
    if(dpm_mmcm_phase_code_fits(phase, sig.div) == 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap); }
    /* DRP 重配置必须在 idle 时做：先等当前测量结束（有界），超时才回 ERR_BUSY。
     * 上位机（spec/validate_sweep.py 的 run_point）在 SET_SIG 之前不一定等到空闲，
     * 因此这里等而不是直接拒绝。 */
    if(wait_idle(c, DPM_WAIT_TIMEOUT_MS(c->gate_target, c->clk.slow_hz), DPM_WAIT_POLL_LIMIT(c->gate_target)) != 0)
    {
        return cmd_reply_err(c, DPM_RESP_ERR_BUSY, out, cap);
    }
    rc = dpm_sig_apply(c, &sig);
    if(rc != 0) { return cmd_reply_err(c, DPM_RESP_ERR_BUSY, out, cap); }
    /* 改完片内源必须复位测量 FSM：DRP 一改，正在等 extSig_sig2 上升沿的窗口就再也等不到
     * （源被重新配置，边沿关系变了），表现为 STATUS.busy 恒 1、GET_RESULT 永远
     * ERR_BUSY。上板实测踩到过；这里显式写 CTRL 的 clear 位（与 CLEAR 命令同一路径）。 */
    dpm_reg_wr(c->io, c->io_ctx, DPM_REG_CTRL, (c->irq_enable != 0u ? DPM_CTRL_IRQ_ENABLE : 0u) | DPM_CTRL_CLEAR);
    /* 内源在用时要确认 sourceReady 已恢复（协议 §4） */
    if(c->source == DPM_SRC_INTERNAL && (dpm_cmd_sig_status(c) & DPM_SIGST_READY) == 0u)
    {
        return cmd_reply_err(c, DPM_RESP_ERR_BUSY, out, cap);
    }
    c->sig = sig;
    c->sig_valid = 1u;
    dpm_wr_u32(&pl[n], sig.freq_hz);
    n += 4u;
    dpm_wr_u32(&pl[n], sig.phase_milli_deg);
    n += 4u;
    dpm_wr_u32(&pl[n], sig.vco_hz);
    n += 4u;
    dpm_wr_u16(&pl[n], sig.div);
    n += 2u;
    put_u8(&pl[n], (uint8_t)phase_field_out(sig.phase_field));
    n += 1u;
    return cmd_reply(c, DPM_CMD_SET_SIG, pl, n, out, cap);
}

static uint32_t handle_get_sig(dpm_cmd_ctx_t* c, uint8_t* out, uint32_t cap)
{
    dpm_sig_t sig;
    uint8_t pl[15];
    size_t n = 0u;

    if(c->sig_valid != 0u) { sig = c->sig; }
    else
    {
        /* 未配置过：从 DRP 字反推（上电默认点或外部工具改过 DRP 的情形） */
        sig.mult = 0u;
        sig.divclk = 0u;
        sig.div = 0u;
        sig.vco_hz = 0u;
        sig.freq_hz = 0u;
        sig.phase_field = 0u;
        sig.phase_milli_deg = 0u;
        sig.phase_step_ps = 0u;
        sig_from_drp(c, &sig);
    }
    dpm_wr_u32(&pl[n], sig.freq_hz);
    n += 4u;
    dpm_wr_u32(&pl[n], sig.phase_milli_deg);
    n += 4u;
    dpm_wr_u32(&pl[n], sig.vco_hz);
    n += 4u;
    dpm_wr_u16(&pl[n], sig.div);
    n += 2u;
    put_u8(&pl[n], (uint8_t)phase_field_out(sig.phase_field));
    n += 1u;
    return cmd_reply(c, DPM_CMD_GET_SIG, pl, n, out, cap);
}

static uint32_t handle_clear(dpm_cmd_ctx_t* c, uint8_t* out, uint32_t cap)
{
    /* PL 的 CTRL.clear 在 busy 期间挂起（测量结束后生效），这里等它落地再回报成功。 */
    if(wait_idle(c, DPM_WAIT_TIMEOUT_MS(c->gate_target, c->clk.slow_hz), DPM_WAIT_POLL_LIMIT(c->gate_target)) != 0)
    {
        return cmd_reply_err(c, DPM_RESP_ERR_BUSY, out, cap);
    }
    dpm_reg_wr(c->io, c->io_ctx, DPM_REG_CTRL, (c->irq_enable != 0u ? DPM_CTRL_IRQ_ENABLE : 0u) | DPM_CTRL_CLEAR);
    c->last.gate_cnt = 0u;
    c->last.edge_cnt = 0u;
    c->last.phase_cnt = 0u;
    c->last.seq = 0u;
    c->last_meas.freq_milli_hz = 0u;
    c->last_meas.phase_milli_deg = 0u;
    c->have_result = 0u;
    c->lost_report = 0u;
    c->lost_pending = 0u;
    c->seq_seen = 0u;
    c->started = 0u;
    return cmd_reply(c, DPM_CMD_CLEAR, 0, 0u, out, cap);
}

static uint32_t handle_stream(dpm_cmd_ctx_t* c, const uint8_t* payload, uint8_t len, uint8_t* out, uint32_t cap)
{
    uint8_t enable;
    uint16_t interval;
    uint8_t pl[3];

    if(dpm_dec_stream(payload, len, &enable, &interval) == 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap); }
    if(enable > 1u || interval == 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, cap); }
    c->stream_enable = enable;
    c->stream_interval_ms = interval;
    c->stream_ms = (c->io->now_ms != 0) ? c->io->now_ms(c->io_ctx) : 0u;
    put_u8(pl, enable);
    dpm_wr_u16(&pl[1], interval);
    return cmd_reply(c, DPM_CMD_STREAM, pl, 3u, out, cap);
}

static uint32_t handle_reset_counters(dpm_cmd_ctx_t* c, uint8_t* out, uint32_t cap)
{
    /* 清 PL 的丢失计数（b2）并清中断挂起（b0）；不动 resultValid，避免丢结果。 */
    dpm_reg_wr(c->io, c->io_ctx, DPM_REG_IRQ_STATUS, DPM_IRQ_PENDING | DPM_IRQ_LOST_CLEAR);
    c->lost_report = 0u;
    c->lost_pending = 0u;
    c->lost_total = 0u;
    c->relock_count = 0u;
    return cmd_reply(c, DPM_CMD_RESET_COUNTERS, 0, 0u, out, cap);
}

uint32_t
    dpm_cmd_handle(dpm_cmd_ctx_t* c, uint8_t cmd, const uint8_t* payload, uint8_t payload_len, uint8_t* out, uint32_t out_cap)
{
    uint32_t now_ms;

    if(c == 0 || c->io == 0 || out == 0) { return 0u; }
    if(DPM_CMD_IS_REQUEST(cmd) == 0u) { return 0u; /* 响应/通知帧方向：忽略 */ }
    now_ms = (c->io->now_ms != 0) ? c->io->now_ms(c->io_ctx) : 0u;

    switch(cmd)
    {
        case DPM_CMD_PING:
            if(payload_len != 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, out_cap); }
            return cmd_reply(c, DPM_CMD_PING, 0, 0u, out, out_cap);
        case DPM_CMD_IDENTIFY:
            if(payload_len != 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, out_cap); }
            return handle_identify(c, out, out_cap);
        case DPM_CMD_GET_STATUS:
            if(payload_len != 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, out_cap); }
            return handle_status(c, out, out_cap);
        case DPM_CMD_START:
        {
            int rc;
            if(payload_len != 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, out_cap); }
            rc = dpm_cmd_start(c, now_ms);
            if(rc != 0) { return cmd_reply_err(c, DPM_RESP_ERR_BUSY, out, out_cap); }
            return cmd_reply(c, DPM_CMD_START, 0, 0u, out, out_cap);
        }
        case DPM_CMD_GET_RESULT:
            if(payload_len != 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, out_cap); }
            return handle_result(c, out, out_cap, now_ms);
        case DPM_CMD_GET_MEAS:
            if(payload_len != 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, out_cap); }
            return handle_meas(c, out, out_cap, now_ms);
        case DPM_CMD_SET_GATE: return handle_set_gate(c, payload, payload_len, out, out_cap);
        case DPM_CMD_SET_MODE: return handle_set_mode(c, payload, payload_len, out, out_cap);
        case DPM_CMD_SET_SOURCE: return handle_set_source(c, payload, payload_len, out, out_cap);
        case DPM_CMD_SET_SIG: return handle_set_sig(c, payload, payload_len, out, out_cap);
        case DPM_CMD_GET_SIG:
            if(payload_len != 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, out_cap); }
            return handle_get_sig(c, out, out_cap);
        case DPM_CMD_CLEAR:
            if(payload_len != 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, out_cap); }
            return handle_clear(c, out, out_cap);
        case DPM_CMD_STREAM: return handle_stream(c, payload, payload_len, out, out_cap);
        case DPM_CMD_RESET_COUNTERS:
            if(payload_len != 0u) { return cmd_reply_err(c, DPM_RESP_ERR_PARAM, out, out_cap); }
            return handle_reset_counters(c, out, out_cap);
        default: return 0u; /* 未知命令：丢弃，不回响应 */
    }
}

/* --------------------------------------------------------- 请求 PAYLOAD 解码 */

uint8_t dpm_dec_set_gate(const uint8_t* p, uint8_t len, uint32_t* gate_target)
{
    if(p == 0 || len != 4u) { return 0u; }
    *gate_target = dpm_rd_u32(p);
    return 1u;
}

uint8_t dpm_dec_set_mode(const uint8_t* p, uint8_t len, uint8_t* mode)
{
    if(p == 0 || len != 1u) { return 0u; }
    *mode = p[0];
    return 1u;
}

uint8_t dpm_dec_set_source(const uint8_t* p, uint8_t len, uint8_t* source)
{
    if(p == 0 || len != 1u) { return 0u; }
    *source = p[0];
    return 1u;
}

uint8_t dpm_dec_set_sig(const uint8_t* p, uint8_t len, uint32_t* freq_hz, uint32_t* phase_milli_deg)
{
    if(p == 0 || len != 8u) { return 0u; }
    *freq_hz = dpm_rd_u32(p);
    *phase_milli_deg = dpm_rd_u32(&p[4]);
    return 1u;
}

uint8_t dpm_dec_stream(const uint8_t* p, uint8_t len, uint8_t* enable, uint16_t* interval_ms)
{
    if(p == 0 || len != 3u) { return 0u; }
    *enable = p[0];
    *interval_ms = dpm_rd_u16(&p[1]);
    return 1u;
}

/* --------------------------------------------------------- 响应 PAYLOAD 编码 */

size_t dpm_enc_identify(uint8_t* out, size_t cap, const dpm_identify_t* v)
{
    size_t need = 4u * 7u + 2u;
    size_t n = 0u;

    if(out == 0 || v == 0 || cap < need) { return 0u; }
    dpm_wr_u32(&out[n], v->ver);
    n += 4u;
    dpm_wr_u32(&out[n], v->git_rev);
    n += 4u;
    dpm_wr_u32(&out[n], v->caps);
    n += 4u;
    dpm_wr_u32(&out[n], v->version_reg);
    n += 4u;
    dpm_wr_u32(&out[n], v->caps_reg);
    n += 4u;
    dpm_wr_u32(&out[n], v->dsp_clk_hz);
    n += 4u;
    dpm_wr_u32(&out[n], v->slow_clk_hz);
    n += 4u;
    dpm_wr_u16(&out[n], v->flags);
    n += 2u;
    return n;
}

size_t dpm_enc_status(uint8_t* out, size_t cap, const dpm_status_reply_t* v)
{
    size_t n = 0u;

    if(out == 0 || v == 0 || cap < 20u) { return 0u; }
    dpm_wr_u32(&out[n], v->status);
    n += 4u;
    dpm_wr_u32(&out[n], v->sig_status);
    n += 4u;
    dpm_wr_u32(&out[n], v->gate_target);
    n += 4u;
    dpm_wr_u32(&out[n], v->lost_count);
    n += 4u;
    dpm_wr_u32(&out[n], v->seq);
    n += 4u;
    return n;
}

size_t dpm_enc_result(uint8_t* out, size_t cap, const dpm_result_reply_t* v)
{
    size_t n = 0u;

    if(out == 0 || v == 0 || cap < 17u) { return 0u; }
    out[n++] = v->valid;
    dpm_wr_u32(&out[n], v->r.seq);
    n += 4u;
    dpm_wr_u32(&out[n], v->r.gate_cnt);
    n += 4u;
    dpm_wr_u32(&out[n], v->r.edge_cnt);
    n += 4u;
    dpm_wr_u32(&out[n], v->r.phase_cnt);
    n += 4u;
    return n;
}

size_t dpm_enc_meas(uint8_t* out, size_t cap, const dpm_meas_reply_t* v)
{
    size_t n = 0u;

    if(out == 0 || v == 0 || cap < 24u) { return 0u; }
    dpm_wr_u32(&out[n], v->r.seq);
    n += 4u;
    dpm_wr_u32(&out[n], v->r.gate_cnt);
    n += 4u;
    dpm_wr_u32(&out[n], v->r.edge_cnt);
    n += 4u;
    dpm_wr_u32(&out[n], v->r.phase_cnt);
    n += 4u;
    dpm_wr_u32(&out[n], v->freq_milli_hz);
    n += 4u;
    dpm_wr_u32(&out[n], v->phase_milli_deg);
    n += 4u;
    return n;
}

size_t dpm_enc_sig(uint8_t* out, size_t cap, const dpm_sig_reply_t* v)
{
    size_t n = 0u;

    if(out == 0 || v == 0 || cap < 15u) { return 0u; }
    dpm_wr_u32(&out[n], v->freq_hz);
    n += 4u;
    dpm_wr_u32(&out[n], v->phase_milli_deg);
    n += 4u;
    dpm_wr_u32(&out[n], v->vco_hz);
    n += 4u;
    dpm_wr_u16(&out[n], v->div);
    n += 2u;
    out[n++] = v->phase_field;
    return n;
}

size_t dpm_enc_stream(uint8_t* out, size_t cap, const dpm_stream_data_t* v)
{
    size_t n;

    if(out == 0 || v == 0 || cap < 26u) { return 0u; }
    n = dpm_enc_meas(out, cap, &v->meas);
    if(n == 0u) { return 0u; }
    dpm_wr_u16(&out[n], v->lost_delta);
    n += 2u;
    return n;
}

size_t dpm_enc_event(uint8_t* out, size_t cap, const dpm_event_t* v)
{
    if(out == 0 || v == 0 || cap < 5u) { return 0u; }
    out[0] = v->code;
    dpm_wr_u32(&out[1], v->detail);
    return 5u;
}
