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
        id.dsp_clk_hz = 450000000u;
        id.slow_clk_hz = 50000000u;
        id.flags = 0x07u;
        CHECK(dpm_enc_identify(out, sizeof(out), &id) == 30u, "IDENTIFY 长度应为 30");
        CHECK(dpm_rd_u32(&out[12]) == DPM_VERSION_VALUE, "versionReg 位置不对");
        CHECK(dpm_rd_u32(&out[16]) == 0x00030104u, "capsReg 位置不对");
        CHECK(dpm_rd_u32(&out[20]) == 450000000u && dpm_rd_u32(&out[24]) == 50000000u,
              "时钟字段位置不对");
        CHECK(dpm_rd_u16(&out[28]) == 0x07u, "flags 位置不对");
        CHECK(dpm_enc_identify(out, 29u, &id) == 0u, "容量不足应返回 0");
    }
}

/** 命令层的等待上限：`50ms + 4 × 闸门时间（秒）`（量纲修复，见 `docs/verify-report.md` §14）。
 *
 * 旧实现把闸门时间的**微秒数值**当毫秒用，长闸门下放大 1000 倍（2.4s 闸门 → 9600s）。
 * 这里钉住「上限与闸门秒数同量级、且始终有界」这条性质，避免再退回去。 */
static void test_wait_budget(void)
{
    printf("== 命令层等待上限（闸门时间的量纲）\n");
    /* 默认闸门 2000 周期 @50MHz = 40µs：50ms + 4 × 0.04ms → 50ms（整除截断） */
    CHECK(DPM_WAIT_TIMEOUT_MS(2000u, 50000000u) == 50u, "默认闸门应为 50ms，实际 %u",
          (unsigned)DPM_WAIT_TIMEOUT_MS(2000u, 50000000u));
    /* 100ms 闸门（5e6 周期 @50MHz）：50ms + 4 × 100ms = 450ms */
    CHECK(DPM_WAIT_TIMEOUT_MS(5000000u, 50000000u) == 450u, "100ms 闸门应为 450ms，实际 %u",
          (unsigned)DPM_WAIT_TIMEOUT_MS(5000000u, 50000000u));
    /* 2.4s 闸门（1.2e8 周期 @50MHz）：50ms + 4 × 2.4s = 9650ms（旧实现给 9600050ms） */
    CHECK(DPM_WAIT_TIMEOUT_MS(120000000u, 50000000u) == 9650u, "2.4s 闸门应为 9650ms，实际 %u",
          (unsigned)DPM_WAIT_TIMEOUT_MS(120000000u, 50000000u));
    /* 28 位闸门上界（≈5.37s）：50ms + 4 × 5.3687s ≈ 21524ms——与闸门同量级、仍有界 */
    CHECK(DPM_WAIT_TIMEOUT_MS(0x0FFFFFFFu, 50000000u) == 21524u, "最大闸门应为 21524ms，实际 %u",
          (unsigned)DPM_WAIT_TIMEOUT_MS(0x0FFFFFFFu, 50000000u));
    CHECK(DPM_WAIT_TIMEOUT_MS(0x0FFFFFFFu, 50000000u) < 30000u, "最大闸门下的上限必须仍然有界");
    /* slow_hz = 0 不能除零（非法配置只需不崩） */
    CHECK(DPM_WAIT_TIMEOUT_MS(2000u, 0u) >= 50u, "slow_hz = 0 时应退化为固定底数");
    /* 兜底轮询上限是**慢时钟周期**意义上的界（不含时间换算）：随闸门线性增长 */
    CHECK(DPM_WAIT_POLL_LIMIT(2000u) == 208000u, "默认闸门的轮询上限应为 208000，实际 %u",
          (unsigned)DPM_WAIT_POLL_LIMIT(2000u));
    CHECK(DPM_WAIT_POLL_LIMIT(120000000u) == 480200000u, "长闸门的轮询上限应随闸门线性增长，实际 %u",
          (unsigned)DPM_WAIT_POLL_LIMIT(120000000u));
}

int main(void)
{
    printf("== test_proto（帧层与 PAYLOAD）\n");
    test_crc();
    test_roundtrip();
    test_bad_frames();
    test_payload_codecs();
    test_wait_budget();
    printf("== %d 项检查，%d 项失败\n", g_checks, g_fail);
    return g_fail == 0 ? 0 : 1;
}
