/** @file test_proto.c
 *  @brief 宿主机单测：帧层（CRC 向量、组帧/收帧、错帧丢弃）与 PAYLOAD 编解码。
 *
 * 构建/运行见 `ps/CMakeLists.txt`（`ctest` 目标 `test_proto`）。退出码 0 = 全过。
 */

#include <stdio.h>
#include <string.h>

#include "dpm_cmd.h"
#include "dpm_proto.h"

static int g_fail;
static int g_checks;

#define CHECK(cond, ...)                                                     \
    do {                                                                     \
        g_checks++;                                                          \
        if (!(cond)) {                                                       \
            g_fail++;                                                        \
            printf("  [FAIL] %s:%d: ", __FILE__, __LINE__);                  \
            printf(__VA_ARGS__);                                             \
            printf("\n");                                                    \
        }                                                                    \
    } while (0)

static void test_crc(void)
{
    static const uint8_t check[] = "123456789";

    printf("== CRC-16/MODBUS 标准向量\n");
    CHECK(dpm_crc16(check, 9) == 0x4B37u, "check 值 0x%04X != 0x4B37", dpm_crc16(check, 9));
    CHECK(dpm_crc16((const uint8_t *)"", 0) == 0xFFFFu, "空串应为 init 值");
    /* 小端序：帧里低字节先 */
    {
        uint8_t frame[8];
        size_t n = dpm_frame_encode(frame, sizeof(frame), 0x01, 0, 0);
        CHECK(n == 5u, "空 PAYLOAD 帧长应为 5，实际 %u", (unsigned)n);
        CHECK(frame[0] == DPM_SOF && frame[1] == 1u && frame[2] == 0x01u, "帧头 %02X %02X %02X",
              frame[0], frame[1], frame[2]);
        CHECK(dpm_crc16(&frame[1], 2) == (uint16_t)(frame[3] | (frame[4] << 8)),
              "CRC 位置/字节序不对");
    }
}

static void test_roundtrip(void)
{
    dpm_rx_t rx;
    uint8_t frame[DPM_FRAME_MAX];
    uint8_t payload[8];
    size_t n;
    uint8_t cmd = 0;
    const uint8_t *pl = 0;
    uint8_t plen = 0;
    int got = 0;

    printf("== 组帧 → 收帧往返\n");
    dpm_wr_u32(payload, 0x12345678u);
    dpm_wr_u32(&payload[4], 0x0000BEEFu);
    n = dpm_frame_encode(frame, sizeof(frame), DPM_CMD_SET_SIG, payload, 8u);
    CHECK(n == 13u, "帧长 %u != 13", (unsigned)n);

    dpm_rx_init(&rx);
    for (size_t i = 0; i < n; i++) {
        if (dpm_rx_push(&rx, frame[i], &cmd, &pl, &plen) != 0) {
            got = 1;
        }
    }
    CHECK(got == 1, "未收到完整帧");
    CHECK(cmd == DPM_CMD_SET_SIG, "CMD = 0x%02X", cmd);
    CHECK(plen == 8u && dpm_rd_u32(pl) == 0x12345678u && dpm_rd_u32(&pl[4]) == 0xBEEFu,
          "PAYLOAD 往返不一致");

    /* 逐字节喂：中间状态不得提前出帧 */
    dpm_rx_init(&rx);
    got = 0;
    for (size_t i = 0; i < n; i++) {
        CHECK(dpm_rx_push(&rx, frame[i], &cmd, &pl, &plen) == 0 || i == n - 1, "第 %u 字节提前出帧",
              (unsigned)i);
        if (i == n - 1) {
            got = 1;
        }
    }
    CHECK(got == 1, "逐字节喂未出帧");
}

static void test_bad_frames(void)
{
    dpm_rx_t rx;
    uint8_t frame[DPM_FRAME_MAX];
    uint8_t cmd;
    const uint8_t *pl;
    uint8_t plen;
    size_t n;

    printf("== 错帧丢弃（CRC 错 / LEN 越界 / 噪声）\n");
    /* CRC 错 */
    n = dpm_frame_encode(frame, sizeof(frame), DPM_CMD_PING, 0, 0);
    frame[n - 1] ^= 0x01u;
    dpm_rx_init(&rx);
    for (size_t i = 0; i < n; i++) {
        CHECK(dpm_rx_push(&rx, frame[i], &cmd, &pl, &plen) == 0, "CRC 错的帧被接受");
    }

    /* LEN = 0 与 LEN = 65 越界 */
    {
        uint8_t bad0[] = {DPM_SOF, 0x00, DPM_CMD_PING, 0x00, 0x00};
        uint8_t bad65[70];
        dpm_rx_init(&rx);
        for (size_t i = 0; i < sizeof(bad0); i++) {
            CHECK(dpm_rx_push(&rx, bad0[i], &cmd, &pl, &plen) == 0, "LEN=0 被接受");
        }
        memset(bad65, 0, sizeof(bad65));
        bad65[0] = DPM_SOF;
        bad65[1] = 65u;
        dpm_rx_init(&rx);
        for (size_t i = 0; i < sizeof(bad65); i++) {
            CHECK(dpm_rx_push(&rx, bad65[i], &cmd, &pl, &plen) == 0, "LEN=65 被接受");
        }
    }

    /* 噪声后重新同步 */
    n = dpm_frame_encode(frame, sizeof(frame), DPM_CMD_PING, 0, 0);
    dpm_rx_init(&rx);
    {
        uint8_t noise[] = {0x11u, 0x22u, 0x33u};
        int ok = 0;
        for (size_t i = 0; i < sizeof(noise); i++) {
            (void)dpm_rx_push(&rx, noise[i], &cmd, &pl, &plen);
        }
        for (size_t i = 0; i < n; i++) {
            if (dpm_rx_push(&rx, frame[i], &cmd, &pl, &plen) != 0) {
                ok = 1;
            }
        }
        CHECK(ok == 1 && cmd == DPM_CMD_PING, "噪声后未重新同步");
    }
}

static void test_payload_codecs(void)
{
    uint8_t p[16];
    uint32_t u32v = 0;
    uint16_t u16v = 0;
    uint8_t u8v = 0;

    printf("== PAYLOAD 编解码与长度校验\n");
    dpm_wr_u32(p, 2000u);
    CHECK(dpm_dec_set_gate(p, 4u, &u32v) == 1u && u32v == 2000u, "SET_GATE 解码");
    CHECK(dpm_dec_set_gate(p, 3u, &u32v) == 0u, "SET_GATE 长度不符应拒绝");
    CHECK(dpm_dec_set_gate(p, 5u, &u32v) == 0u, "SET_GATE 长度不符应拒绝");

    p[0] = 1u;
    CHECK(dpm_dec_set_mode(p, 1u, &u8v) == 1u && u8v == 1u, "SET_MODE 解码");
    CHECK(dpm_dec_set_mode(p, 4u, &u8v) == 0u, "SET_MODE 长度不符应拒绝");
    CHECK(dpm_dec_set_source(p, 1u, &u8v) == 1u && u8v == 1u, "SET_SOURCE 解码");

    dpm_wr_u32(p, 25000000u);
    dpm_wr_u32(&p[4], 90000u);
    {
        uint32_t u32w = 0;
        CHECK(dpm_dec_set_sig(p, 8u, &u32v, &u32w) == 1u && u32v == 25000000u && u32w == 90000u,
              "SET_SIG 解码");
        CHECK(dpm_dec_set_sig(p, 7u, &u32v, &u32w) == 0u, "SET_SIG 长度不符应拒绝");
    }

    p[0] = 1u;
    dpm_wr_u16(&p[1], 100u);
    CHECK(dpm_dec_stream(p, 3u, &u8v, &u16v) == 1u && u8v == 1u && u16v == 100u, "STREAM 解码");
    CHECK(dpm_dec_stream(p, 2u, &u8v, &u16v) == 0u, "STREAM 长度不符应拒绝");

    /* 响应编码：字段顺序/长度 */
    {
        dpm_identify_t id;
        uint8_t out[40];
        memset(&id, 0, sizeof(id));
        id.ver = 0x00010000u;
        id.version_reg = DPM_VERSION_VALUE;
        id.caps_reg = 0x00030104u;
        id.dsp_clk_hz = 400000000u;
        id.slow_clk_hz = 50000000u;
        id.flags = 0x07u;
        CHECK(dpm_enc_identify(out, sizeof(out), &id) == 30u, "IDENTIFY 长度应为 30");
        CHECK(dpm_rd_u32(&out[12]) == DPM_VERSION_VALUE, "versionReg 位置不对");
        CHECK(dpm_rd_u32(&out[16]) == 0x00030104u, "capsReg 位置不对");
        CHECK(dpm_rd_u32(&out[20]) == 400000000u && dpm_rd_u32(&out[24]) == 50000000u,
              "时钟字段位置不对");
        CHECK(dpm_rd_u16(&out[28]) == 0x07u, "flags 位置不对");
        CHECK(dpm_enc_identify(out, 29u, &id) == 0u, "容量不足应返回 0");
    }
}

int main(void)
{
    printf("== test_proto（帧层与 PAYLOAD）\n");
    test_crc();
    test_roundtrip();
    test_bad_frames();
    test_payload_codecs();
    printf("== %d 项检查，%d 项失败\n", g_checks, g_fail);
    return g_fail == 0 ? 0 : 1;
}
