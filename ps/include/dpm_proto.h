/** @file dpm_proto.h
 *  @brief 串口帧协议：CRC、帧编解码状态机、命令码与 PAYLOAD 布局。
 *
 * 权威定义是 `docs/ps-protocol.md`：本文件只把它表达成可编译的形式，
 * 两侧（PS 侧 C 与 PC 侧 Python `host/dpm_host/protocol.py`）必须与本文件一致。
 *
 * 帧结构：
 *   SOF(0xA5) | LEN(1B, = CMD+PAYLOAD 长度, 1..64) | CMD(1B) | PAYLOAD | CRC16(2B, 低字节先)
 *   CRC-16/MODBUS（反射 poly 0xA001，init 0xFFFF），计算范围 = LEN、CMD、PAYLOAD。
 */

#ifndef DPM_PROTO_H
#define DPM_PROTO_H

#include <stdint.h>
#include <stddef.h>

/* ------------------------------------------------------------- 协议常量 */

#define DPM_SOF 0xA5u
#define DPM_LEN_MIN 1u
#define DPM_LEN_MAX 64u
/** 一帧的最大字节数：SOF + LEN + (LEN + CRC) */
#define DPM_FRAME_MAX (2u + DPM_LEN_MAX + 2u)

/* 命令码（请求） */
#define DPM_CMD_PING 0x01u
#define DPM_CMD_IDENTIFY 0x02u
#define DPM_CMD_GET_STATUS 0x03u
#define DPM_CMD_START 0x04u
#define DPM_CMD_GET_RESULT 0x05u
#define DPM_CMD_GET_MEAS 0x06u
#define DPM_CMD_SET_GATE 0x07u
#define DPM_CMD_SET_MODE 0x08u
#define DPM_CMD_SET_SOURCE 0x09u
#define DPM_CMD_SET_SIG 0x0Au
#define DPM_CMD_GET_SIG 0x0Bu
#define DPM_CMD_CLEAR 0x0Cu
#define DPM_CMD_STREAM 0x0Du
#define DPM_CMD_RESET_COUNTERS 0x0Eu

/* 响应方向位 */
#define DPM_RESP_FLAG 0x80u
/** 响应码 = 请求码 | 0x80 */
#define DPM_CMD_RESP(cmd) ((uint8_t)((cmd) | DPM_RESP_FLAG))
#define DPM_CMD_IS_REQUEST(cmd) (((cmd) & DPM_RESP_FLAG) == 0u)

/* 错误响应码 */
#define DPM_RESP_ERR_PARAM 0xFEu
#define DPM_RESP_ERR_BUSY 0xFFu

/* 通知帧（PS → PC 主动上报）。
 *
 * 取 0xE1/0xE2 而不是 0x81/0x82：响应码是「请求码 | 0x80」，PING 的响应恰好是 0x81、
 * IDENTIFY 的响应是 0x82，旧取值会与响应**撞码**（只能靠 PAYLOAD 长度区分）。
 * 约定：0xE0..0xEF = 通知，0x81..0x8E = 响应。 */
#define DPM_EVT_STREAM_DATA 0xE1u
#define DPM_EVT_EVENT 0xE2u
/** 通知码判定（收到该范围的 CMD 说明是主动上报，不是响应）。 */
#define DPM_CMD_IS_EVENT(cmd) (((cmd) & 0xF0u) == 0xE0u)

/* 枚举：测量模式与输入源 */
#define DPM_MODE_SINGLE 0u
#define DPM_MODE_CONTINUOUS 1u
#define DPM_SRC_EXTERNAL 0u
#define DPM_SRC_INTERNAL 1u

/* IDENTIFY 的 flags 位 */
#define DPM_FLAG_HAS_SIG_SOURCE (1u << 0)
#define DPM_FLAG_CONTINUOUS (1u << 1)
#define DPM_FLAG_STREAM (1u << 2)
#define DPM_FLAG_IRQ (1u << 3)

/* EVENT 的 code */
#define DPM_EVENTCODE_LOST 1u
#define DPM_EVENTCODE_RELOCK 2u
#define DPM_EVENTCODE_OVERRUN 3u

/* --------------------------------------------------------------- CRC-16 */

/** CRC-16/MODBUS：反射 poly 0xA001、init 0xFFFF、无最终异或。 */
uint16_t dpm_crc16(const uint8_t *data, size_t len);

/* ------------------------------------------------------------ 帧编解码器 */

/** 组帧：把 CMD + PAYLOAD 编成一帧写到 `out`（容量至少 DPM_FRAME_MAX）。
 *
 * @return 帧总字节数（= 4 + payload_len），参数非法返回 0。
 */
size_t dpm_frame_encode(uint8_t *out, size_t out_cap, uint8_t cmd, const uint8_t *payload,
                        size_t payload_len);

/** 流式收帧状态机（无动态内存）。 */
typedef struct {
    uint8_t buf[DPM_FRAME_MAX]; /**< 组帧缓冲 */
    uint8_t len;                /**< 已收字节数 */
    uint8_t need;               /**< 本帧还需要多少字节（0 = 尚未读到 LEN） */
} dpm_rx_t;

/** 复位收帧状态机。 */
void dpm_rx_init(dpm_rx_t *rx);

/** 逐字节输入。
 *
 * @return 1 = 收到一帧完整且 CRC 正确的帧，`cmd` 与 `payload` 已就绪；
 *         0 = 还需要更多字节；（CRC 错误或字段越界时静默丢弃并回到等 SOF，同样返回 0）
 */
int dpm_rx_push(dpm_rx_t *rx, uint8_t byte, uint8_t *cmd, const uint8_t **payload,
                uint8_t *payload_len);

/* --------------------------------------------------------- 可移植工具函数 */

/** 小端读/写（不依赖对齐，可用于未对齐的 PAYLOAD 偏移）。 */
uint16_t dpm_rd_u16(const uint8_t *p);
uint32_t dpm_rd_u32(const uint8_t *p);
void dpm_wr_u16(uint8_t *p, uint16_t v);
void dpm_wr_u32(uint8_t *p, uint32_t v);

/** 校验：`need` 字节是否可用（用于 PAYLOAD 长度检查）。 */
#define DPM_PAYLOAD_OK(len, need) ((size_t)(len) == (size_t)(need))

#endif /* DPM_PROTO_H */
