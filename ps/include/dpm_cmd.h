/** @file dpm_cmd.h
 *  @brief 命令处理核心（可移植、无 IO 依赖）的上下文、API 与 PAYLOAD 结构。
 *
 * `dpm_cmd.c` 是 PS 侧与宿主机验证台**共用的同一份**命令状态机：
 * 它只通过 `dpm_io_t` 访问 PL 寄存器、通过 `emit` 回调送通知帧，不 include 任何
 * libc 头、不使用浮点与可变参数，因此两侧都能编译。
 *
 * 职责边界：
 *   - 帧层（CRC、收帧状态机、小端工具）在 `dpm_proto.c`（头文件由 Lead 冻结）；
 *   - 命令分发、参数校验、测量状态机、流上报在本文件/`dpm_cmd.c`；
 *   - 板级细节（UART、GIC、时钟）在 `dpm_uart.c` / `dpm_irq.c` / `dpm_regs_io.c`。
 */

#ifndef DPM_CMD_H
#define DPM_CMD_H

#include <stddef.h>
#include <stdint.h>

#include "dpm_calc.h"
#include "dpm_io.h"
#include "dpm_proto.h"

/* --------------------------------------------------------------- 版本与能力 */

/** PS 侧软件版本：0xMMmmpppp（当前 1.0.0）。 */
#define DPM_SW_VERSION 0x00010000u

/** 编译期注入的 git 版本（`ps/build.sh` 会尝试用 `git rev-parse` 填 32 位短哈希）。 */
#ifndef DPM_GIT_REV
#define DPM_GIT_REV 0u
#endif

/** IDENTIFY 的 caps（PS 侧能力位，与 PL 的 CAPS 寄存器无关）。 */
#define DPM_CAPS_PS_SIG_SOURCE (1u << 0) /**< 支持片内信号源与 DRP 配置 */
#define DPM_CAPS_PS_CONTINUOUS (1u << 1) /**< 支持连续模式 */
#define DPM_CAPS_PS_STREAM (1u << 2)     /**< 支持流上报 */
#define DPM_CAPS_PS_IRQ (1u << 3)        /**< 编译了中断路径 */
#define DPM_CAPS_PS_DIV64 (1u << 4)      /**< 自带 64 位除法（freestanding） */
#define DPM_CAPS_PS_SELFTEST (1u << 5)   /**< 由宿主机验证台构建（仿真后端） */
#define DPM_CAPS_PS_PHASE_CTRL (1u << 6) /**< 片内源相位可在线改变（方案 A 下为 1） */

/** IDENTIFY 的 flags 的 b4：片内源相位是否可控。
 *
 * 当前实现（方案 A）：PS 直接写 MMCM 的 16 位寄存器镜像（PHASE_MUX/DELAY_TIME），
 * 相位在线可改写，因此 b4 = 1。与 `dpm_regs.h` 的 `DPM_DRP_PHASE_WRITABLE` 保持一致
 * （该宏为 1 时本位置位）。 */
#define DPM_FLAG_PHASE_CTRL (1u << 4)

/** IDENTIFY 的 flags（协议 §3.1 的 b0..b3，必须与实际能力一致）。 */
#if defined(DPM_HOST_SELFTEST)
#define DPM_FLAGS_VALUE (DPM_FLAG_HAS_SIG_SOURCE | DPM_FLAG_CONTINUOUS | DPM_FLAG_STREAM)
#elif defined(DPM_USE_IRQ) && (DPM_USE_IRQ == 1)
#define DPM_FLAGS_VALUE \
    (DPM_FLAG_HAS_SIG_SOURCE | DPM_FLAG_CONTINUOUS | DPM_FLAG_STREAM | DPM_FLAG_IRQ)
#else
#define DPM_FLAGS_VALUE (DPM_FLAG_HAS_SIG_SOURCE | DPM_FLAG_CONTINUOUS | DPM_FLAG_STREAM)
#endif

/* --------------------------------------------------------------- 超时/轮询 */

/** 等待测量结果时的毫秒上限：`50ms + 4 × 闸门时间（秒）`。
 *
 * 闸门窗口 `T_gate = gateTarget / slow_hz`（协议第 5 节）就是一次测量的时长；RTL 的闸门计数器
 * 是 28 位，50MHz 下最长 ≈5.37s，因此这条上限最多 ≈21.5s——与闸门同量级、有界。
 *
 * **量纲约定**：上限 = `50ms + 4 × 闸门时间（秒）`。闸门时间的微秒数值不能当毫秒用，
 * 否则长闸门下等待上限会放大 1000 倍（协议 §3.0 的有界等待约定）。
 *
 * 中间量用 u64（`4000 × gateTarget` 最大 ≈1.07e12），结果必然落在 u32 内。
 * `slow_hz == 0` 时按 1 处理，避免除零（那种配置本身非法，只需不崩）。
 */
#define DPM_WAIT_TIMEOUT_MS(gate_target, slow_hz)                                          \
    ((uint32_t)(50ull + (4000ull * (uint64_t)(gate_target)) /                              \
                             (((slow_hz) != 0u) ? (uint64_t)(slow_hz) : 1ull)))

/** 无毫秒时间源时的兜底轮询次数上限（每次轮询至少含 1 次 PL 寄存器读）。
 *
 * 量纲与上面不同：这是**慢时钟周期**意义上的界，不是时间界——一次轮询至少要读一拍寄存器，
 * 因此循环本身至少要花 `4 × gateTarget` 个慢周期 ≈ 4 个闸门窗口。只有在 `io->now_ms == 0`
 * 的集成里才会用到它；提前返回（回 `valid = 0`）是良性的：上位机会继续轮询，PL 里的测量照跑。
 */
#define DPM_WAIT_POLL_LIMIT(gate_target) (4u * (gate_target) + 200000u)

/** SET_SIG 的频率容差：0.1% + 1Hz（冻结频点误差为 0，非精确解一律 ERR_PARAM）。 */
#define DPM_SIG_FREQ_TOL_MILLI_HZ(freq_hz) ((freq_hz) / 1000u + 1000u)

/* ----------------------------------------------------------- PAYLOAD 结构 */

/** IDENTIFY（0x02）响应。 */
typedef struct {
    uint32_t ver;         /**< 软件版本 0xMMmmpppp */
    uint32_t git_rev;     /**< 编译期注入的 git 版本 */
    uint32_t caps;        /**< DPM_CAPS_PS_* */
    uint32_t version_reg; /**< PL VERSION 寄存器回读 */
    uint32_t caps_reg;    /**< PL CAPS 寄存器回读 */
    uint32_t dsp_clk_hz;  /**< 高速（DSP）时钟频率 */
    uint32_t slow_clk_hz; /**< 低速（闸门）时钟频率 */
    uint16_t flags;       /**< DPM_FLAG_* */
} dpm_identify_t;

/** GET_STATUS（0x03）响应。 */
typedef struct {
    uint32_t status;      /**< PL STATUS 原值 */
    uint32_t sig_status;  /**< PL SIG_STATUS 原值 */
    uint32_t gate_target; /**< 当前目标闸门计数 */
    uint32_t lost_count;  /**< PL LOST_COUNT 原值 */
    uint32_t seq;         /**< PL RESULT_SEQ 原值 */
} dpm_status_reply_t;

/** GET_RESULT（0x05）响应。 */
typedef struct {
    uint8_t valid;         /**< 1 = 有可用结果 */
    dpm_result_t r;        /**< seq / gate_cnt / edge_cnt / phase_cnt */
} dpm_result_reply_t;

/** GET_MEAS（0x06）响应，也是 STREAM_DATA（0x81）的前半部分。 */
typedef struct {
    dpm_result_t r;           /**< 原始观测量 */
    uint32_t freq_milli_hz;   /**< 定点频率。**单位是 Hz**（字段名沿用线上名 freqMilliHz，
                               * 但真正的毫赫兹会溢出 u32，见 docs/ps-protocol.md 第 5 节） */
    uint32_t phase_milli_deg; /**< 定点相位，1e-3 度 */
} dpm_meas_reply_t;

/** SET_SIG/GET_SIG 的工作点回显（协议 §3 的 5 个字段）。 */
typedef struct {
    uint32_t freq_hz;         /**< 实际输出频率 */
    uint32_t phase_milli_deg; /**< 实际相位差（已吸附到字段网格） */
    uint32_t vco_hz;          /**< VCO 频率 */
    uint16_t div;             /**< CLKOUT 分频 D */
    uint8_t phase_field;      /**< 写入 DRP 的相位字段 */
} dpm_sig_reply_t;

/** STREAM_DATA（0x81）通知。 */
typedef struct {
    dpm_meas_reply_t meas;
    uint16_t lost_delta; /**< 自上次上报以来的新增丢失数（饱和到 0xFFFF） */
} dpm_stream_data_t;

/** EVENT（0x82）通知。 */
typedef struct {
    uint8_t code;    /**< DPM_EVENTCODE_* */
    uint32_t detail; /**< 事件细节（计数或状态字） */
} dpm_event_t;

/* ----------------------------------------------------------- 服务器上下文 */

/** 串口命令服务器的全部状态（无动态内存，可整块放在 BSS）。 */
typedef struct {
    /* 依赖注入 */
    const dpm_io_t *io; /**< 寄存器 IO 实现 */
    void *io_ctx;       /**< IO 上下文（真实 MMIO 为 0，仿真后端为模型指针） */
    dpm_clocks_t clk;   /**< 低速/高速时钟频率 */

    /* 协议可见配置 */
    uint32_t gate_target;       /**< 目标闸门计数 */
    uint8_t mode;               /**< DPM_MODE_* */
    uint8_t source;             /**< DPM_SRC_* */
    uint8_t irq_enable;         /**< CTRL.b1 的软件侧镜像 */
    uint8_t stream_enable;      /**< STREAM.enable */
    uint16_t stream_interval_ms;/**< STREAM.intervalMs */

    /* 片内信号源工作点 */
    dpm_sig_t sig;     /**< 最近一次成功配置的工作点 */
    uint8_t sig_valid; /**< 1 = 已成功配置过 DRP */

    /* 最近一次结果 */
    dpm_result_t last;    /**< 原始观测量 */
    dpm_meas_t last_meas; /**< 定点换算结果 */
    uint8_t have_result;  /**< 1 = 至少拿到过一次结果（GET_RESULT.valid） */

    /* 丢失与事件统计 */
    uint32_t lost_report;  /**< 上次上报时的 LOST_COUNT 基准 */
    uint32_t lost_pending; /**< 尚未上报的新增丢失数 */
    uint32_t lost_total;   /**< 累计新增丢失数 */
    uint32_t seq_seen;     /**< 最近一次读到的 RESULT_SEQ */
    uint8_t locked_last;   /**< 上次观察到的 MMCM locked */
    uint8_t locked_known;  /**< 是否见过 locked = 1 */
    uint32_t relock_count; /**< 「失锁并恢复」事件计数 */

    /* 时间与调度 */
    uint32_t start_ms;  /**< 最近一次 START 的时刻 */
    uint32_t stream_ms; /**< 上次流上报时刻 */
    uint8_t started;    /**< 有测量在飞行 */
    uint8_t awaiting;   /**< 1 = 本固件刚启动了一次测量，只接受它的结果 */
    uint8_t valid_before; /**< START 之前结果槽里是否已有未读结果 */
    uint32_t seq_before;  /**< START 之前的 RESULT_SEQ（用于区分新旧结果） */

    /* 统计 */
    uint32_t cmd_count;       /**< 成功处理的命令数 */
    uint32_t err_param_count; /**< ERR_PARAM 次数 */
    uint32_t err_busy_count;  /**< ERR_BUSY 次数 */
    uint32_t drop_frames;     /**< 协议层丢帧数（由上层喂入） */

    /* 通知回调（PS→PC 主动上报）；返回 0 表示发送成功 */
    int (*emit)(void *user, const uint8_t *frame, uint32_t len);
    void *emit_user;
} dpm_cmd_ctx_t;

/* --------------------------------------------------------------- 生命周期 */

/** 初始化上下文：注入 IO、时钟，写默认配置（闸门 2000、单次、片内源、中断开）。
 *
 * 只写 PL 的 GATE_TARGET / CTRL / SIG_CTRL，不做 DRP 配置，可在 PL 未就绪时调用。
 */
void dpm_cmd_init(dpm_cmd_ctx_t *c, const dpm_io_t *io, void *io_ctx, const dpm_clocks_t *clk);

/* ------------------------------------------------------------- PL 访问包装 */

/** 读 PL STATUS（真实 AXI 读；仿真后端借它推进时间）。 */
uint32_t dpm_cmd_status(const dpm_cmd_ctx_t *c);

/** 读 PL SIG_STATUS。 */
uint32_t dpm_cmd_sig_status(const dpm_cmd_ctx_t *c);

/** 读 MMCM DRP 状态字（bit0 = locked）。 */
uint32_t dpm_cmd_drp_status(const dpm_cmd_ctx_t *c);

/** 当前选中的源是否可用（与 PL 的 SIG_STATUS.sourceReady 同义）。 */
int dpm_cmd_source_ready(const dpm_cmd_ctx_t *c);

/* ------------------------------------------------------------- 测量状态机 */

/** 启动一次测量（非阻塞）。
 *
 * @return 0 = 已写入 CTRL；1 = 源不可用；2 = 正在忙；3 = 写 CTRL 未生效（配置未送出）。
 *         1/2/3 都对应协议层的 ERR_BUSY。
 */
int dpm_cmd_start(dpm_cmd_ctx_t *c, uint32_t now_ms);

/** 若 PL 有未读结果则锁存到 `c->last` 并写 IRQ_STATUS 清标志。
 *
 * @return 1 = 取到新结果，0 = 没有新结果。
 */
int dpm_cmd_fetch(dpm_cmd_ctx_t *c);

/** 等待并取回一次结果（同时更新丢失统计）。
 *
 * @return 0 = 成功（`c->last` 有效），1 = 超时（含「测量已结束但没有新结果」），2 = 源不可用。
 *
 * 返回非 0 表示「这次没等到结果」，**由调用方按命令的字段能力表达**：
 *   - `GET_RESULT` 有 `valid` 字段（协议 §3.0 规定它是结果就绪的权威探针）→ 回 `valid = 0`，
 *     **不回 ERR_BUSY**（「源没有边沿」是正常输入状态，不是「命令不被允许」）；
 *   - `GET_MEAS` 没有 `valid` 字段，只能用 `ERR_BUSY` 表达「暂无结果」，上位机按继续轮询处理。
 */
int dpm_cmd_wait_result(dpm_cmd_ctx_t *c, uint32_t timeout_ms, uint32_t poll_limit);

/** 周期任务：连续模式重启、流上报、MMCM 失锁/恢复事件。
 *
 * 主循环（PS 侧）与验证台泵（宿主机）在每次迭代调用；`now_ms` 来自 IO 时间源，
 * 为 0 时跳过与时间相关的调度。
 */
void dpm_cmd_tick(dpm_cmd_ctx_t *c, uint32_t now_ms);

/* --------------------------------------------------------------- 命令分发 */

/** 处理一条请求，把响应帧写进 `out`（容量至少 `DPM_FRAME_MAX`）。
 *
 * @param cmd 请求命令码（bit7 = 0）
 * @param payload/payload_len 请求 PAYLOAD（可为 0）
 * @return 响应帧字节数；0 表示不回响应（未知命令或方向位非法）。
 */
uint32_t dpm_cmd_handle(dpm_cmd_ctx_t *c, uint8_t cmd, const uint8_t *payload, uint8_t payload_len,
                        uint8_t *out, uint32_t out_cap);

/* --------------------------------------------------- PAYLOAD 编解码（可单测） */

/** 请求解码：返回 1 = 字段长度合法（取值合法性另判）；0 = 长度不符（ERR_PARAM）。 */
uint8_t dpm_dec_set_gate(const uint8_t *p, uint8_t len, uint32_t *gate_target);
uint8_t dpm_dec_set_mode(const uint8_t *p, uint8_t len, uint8_t *mode);
uint8_t dpm_dec_set_source(const uint8_t *p, uint8_t len, uint8_t *source);
uint8_t dpm_dec_set_sig(const uint8_t *p, uint8_t len, uint32_t *freq_hz, uint32_t *phase_milli_deg);
uint8_t dpm_dec_stream(const uint8_t *p, uint8_t len, uint8_t *enable, uint16_t *interval_ms);

/** 响应编码：返回值 = 写入字节数；`cap` 不足返回 0。 */
size_t dpm_enc_identify(uint8_t *out, size_t cap, const dpm_identify_t *v);
size_t dpm_enc_status(uint8_t *out, size_t cap, const dpm_status_reply_t *v);
size_t dpm_enc_result(uint8_t *out, size_t cap, const dpm_result_reply_t *v);
size_t dpm_enc_meas(uint8_t *out, size_t cap, const dpm_meas_reply_t *v);
size_t dpm_enc_sig(uint8_t *out, size_t cap, const dpm_sig_reply_t *v);
size_t dpm_enc_stream(uint8_t *out, size_t cap, const dpm_stream_data_t *v);
size_t dpm_enc_event(uint8_t *out, size_t cap, const dpm_event_t *v);

/* ------------------------------------------------------- 片内源（dpm_sig.c） */

/** 相位 1 步（1/8 VCO 周期）对应的时间（ps）。步长只由 VCO 决定。 */
uint32_t dpm_sig_phase_step_ps(uint32_t vco_hz);

/** 把请求相位吸附到硬件网格（1/8 VCO 周期 = 45°/divide）后返回实际相位（毫度）。
 *
 * 方案 A 下相位由 MMCM 的 PHASE_MUX（1/8 VCO 周期）与 DELAY_TIME（整数 VCO 周期）
 * 共同表示，因此网格是 `45°/D` 而不是 45°：22.5° 在 D=40 下精确可表示。 */
uint32_t dpm_sig_snap_phase_milli_deg(uint32_t phase_milli_deg, uint32_t divide);

/** 兼容旧接口的历史函数：方案 A 下相位不再有独立的毫度字，恒返回 0。 */
uint32_t dpm_sig_phase_word_mdeg(const dpm_sig_t *sig);

/** 按协议 §6 的 5 步时序把工作点写进 MMCM DRP 并等待重新锁定。
 *
 * 调用前会检查 `STATUS.busy == 0`（DRP 重配置必须在 idle 时做）。
 *
 * @return 0 = 成功；2 = MMCM 未锁定/超时（ERR_BUSY）；3 = 测量正在进行（ERR_BUSY）。
 */
int dpm_sig_apply(const dpm_cmd_ctx_t *c, const dpm_sig_t *sig);

#endif /* DPM_CMD_H */
