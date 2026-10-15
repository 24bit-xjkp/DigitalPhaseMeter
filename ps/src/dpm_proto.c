/** @file dpm_proto.c
 *  @brief 帧层实现：CRC-16/MODBUS、组帧、流式收帧状态机与小端工具。
 *
 * 实现 `ps/include/dpm_proto.h`（Lead 冻结）的接口，语义见 `docs/ps-protocol.md` 第 2 节。
 * 可移植：只用 <stdint.h> / <stddef.h>，无浮点、无 libc。
 */

#include "dpm_proto.h"

/* --------------------------------------------------------------------- CRC */

uint16_t dpm_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFFu;
    size_t i;
    uint8_t b;

    if (data == 0) {
        return crc;
    }
    for (i = 0u; i < len; i++) {
        crc ^= (uint16_t)data[i];
        for (b = 0u; b < 8u; b++) {
            if ((crc & 1u) != 0u) {
                crc = (uint16_t)((crc >> 1) ^ 0xA001u);
            } else {
                crc = (uint16_t)(crc >> 1);
            }
        }
    }
    return crc;
}

/* ------------------------------------------------------------------- 组帧 */

size_t dpm_frame_encode(uint8_t *out, size_t out_cap, uint8_t cmd, const uint8_t *payload,
                        size_t payload_len)
{
    size_t len;   /* LEN 字段 = CMD + PAYLOAD 的字节数 */
    size_t total; /* 帧总长 = SOF + LEN + LEN 字节 + CRC16 */
    size_t i;
    uint16_t crc;

    if (out == 0) {
        return 0u;
    }
    if (payload_len > (size_t)DPM_LEN_MAX - 1u) {
        return 0u;
    }
    len = 1u + payload_len;
    total = len + 4u;
    if (out_cap < total) {
        return 0u;
    }
    out[0] = DPM_SOF;
    out[1] = (uint8_t)len;
    out[2] = cmd;
    for (i = 0u; i < payload_len; i++) {
        out[3u + i] = payload[i];
    }
    /* CRC 范围 = LEN、CMD、PAYLOAD（不含 SOF），低字节先。
     * 帧布局：SOF(0) LEN(1) CMD+PAYLOAD(2..2+len-1) CRC_LO(2+len) CRC_HI(3+len) */
    crc = dpm_crc16(&out[1], len + 1u);
    out[2u + len] = (uint8_t)(crc & 0xFFu);
    out[3u + len] = (uint8_t)(crc >> 8);
    return total;
}

/* --------------------------------------------------------- 收帧状态机实现 */

void dpm_rx_init(dpm_rx_t *rx)
{
    if (rx == 0) {
        return;
    }
    rx->len = 0u;
    rx->need = 0u;
}

int dpm_rx_push(dpm_rx_t *rx, uint8_t byte, uint8_t *cmd, const uint8_t **payload,
                uint8_t *payload_len)
{
    uint16_t expect;
    uint16_t crc_rx;
    uint16_t crc_calc;
    uint8_t len_field;

    if (rx == 0) {
        return 0;
    }

    if (rx->need == 0u) {
        /* 阶段一：等 SOF。非 0xA5 的字节一律丢弃（含帧间噪声与残帧尾巴）。 */
        if (rx->len == 0u) {
            if (byte != DPM_SOF) {
                return 0;
            }
            rx->buf[0] = byte;
            rx->len = 1u;
            return 0;
        }
        /* 阶段二：LEN。越界立即丢弃并回到等 SOF（不回响应）。 */
        len_field = byte;
        if (len_field < DPM_LEN_MIN || len_field > DPM_LEN_MAX) {
            rx->len = 0u;
            return 0;
        }
        rx->buf[1] = byte;
        rx->len = 2u;
        rx->need = (uint8_t)(len_field + 4u); /* SOF+LEN + LEN 字节 + CRC16 */
        return 0;
    }

    /* 阶段三：收满整帧。 */
    if (rx->len < (uint8_t)DPM_FRAME_MAX) {
        rx->buf[rx->len] = byte;
    }
    rx->len++;
    if (rx->len < rx->need) {
        return 0;
    }

    expect = rx->need;
    rx->need = 0u;
    rx->len = 0u;
    if (expect < 5u) { /* 理论不可达：LEN >= 1 → need >= 5 */
        return 0;
    }
    crc_rx = (uint16_t)rx->buf[expect - 2u] | (uint16_t)((uint16_t)rx->buf[expect - 1u] << 8);
    crc_calc = dpm_crc16(&rx->buf[1], (size_t)(expect - 3u)); /* LEN + CMD + PAYLOAD */
    if (crc_rx != crc_calc) {
        return 0;
    }
    if (cmd != 0) {
        *cmd = rx->buf[2];
    }
    if (payload != 0) {
        *payload = &rx->buf[3];
    }
    if (payload_len != 0) {
        *payload_len = (uint8_t)(expect - 5u);
    }
    return 1;
}

/* ------------------------------------------------------------- 小端工具 */

uint16_t dpm_rd_u16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | (uint16_t)((uint16_t)p[1] << 8));
}

uint32_t dpm_rd_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

void dpm_wr_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)(v >> 8);
}

void dpm_wr_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}
