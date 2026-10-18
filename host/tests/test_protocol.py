"""帧协议与 PAYLOAD 编解码的测试（纯函数，无 IO）。

运行：
    仓库根目录：`uv run pytest host/tests/test_protocol.py -q`
    host 目录： `uv run pytest tests/test_protocol.py -q`
"""

from __future__ import annotations

import pytest
from dpm_host import protocol as P

# ------------------------------------------------------------------ CRC-16/MODBUS


def test_known_check_value() -> None:
    """CRC-16/MODBUS 的标准 check 值：ASCII "123456789" → 0x4B37。"""
    assert P.crc16_modbus(b"123456789") == 0x4B37


def test_init_value() -> None:
    """空输入的初值就是 CRC 初值 0xFFFF。"""
    assert P.crc16_modbus(b"") == 0xFFFF


def test_frame_crc_is_little_endian() -> None:
    """帧尾 CRC 低字节在前，且计算范围是 LEN + CMD（不含 SOF）。"""
    frame = P.encode_frame(P.Cmd.PING)
    body = frame[1:3]
    crc = P.crc16_modbus(body)
    assert frame[-2] == crc & 0xFF
    assert frame[-1] == (crc >> 8) & 0xFF


# ------------------------------------------------------------------ 帧封装/解析与边界


def test_ping_frame_bytes() -> None:
    """SOF LEN CMD CRC_L CRC_H；LEN = 1（只有 CMD）。"""
    frame = P.encode_frame(P.Cmd.PING)
    assert len(frame) == 5
    assert frame[0] == P.SOF
    assert frame[1] == 1
    assert frame[2] == 0x01


#: 全部命令的「编码 → 解码」往返用例（原 `test_round_trip_all_commands` 的 6 个子用例）。
_ROUND_TRIP_CASES = [
    (P.Cmd.PING, b""),
    (P.Cmd.SET_GATE, P.encode_set_gate(2000)),
    (P.Cmd.SET_MODE, P.encode_set_mode(1)),
    (P.Cmd.SET_SOURCE, P.encode_set_source(1)),
    (P.Cmd.SET_SIG, P.encode_set_sig(25_000_000, 90_000)),
    (P.Cmd.STREAM, P.encode_stream(1, 100)),
]


@pytest.mark.parametrize("cmd, payload", _ROUND_TRIP_CASES, ids=[cmd.name for cmd, _ in _ROUND_TRIP_CASES])
def test_round_trip_all_commands(cmd: P.Cmd, payload: bytes) -> None:
    frame = P.decode_frame(P.encode_frame(cmd, payload))
    assert frame.cmd == int(cmd)
    assert frame.payload == payload
    assert frame.name == cmd.name


def test_max_payload() -> None:
    """LEN = 64 是上限：整帧恰好 MAX_FRAME_BYTES 字节，且能原样解回。"""
    payload = bytes(range(63))  # LEN = 64（上限）
    frame = P.encode_frame(0x7F, payload)
    assert frame[1] == P.LEN_MAX
    assert len(frame) == P.MAX_FRAME_BYTES
    assert P.decode_frame(frame).payload == payload


def test_payload_too_long() -> None:
    with pytest.raises(P.ProtocolError):
        P.encode_frame(P.Cmd.PING, bytes(64))  # LEN 会变成 65


def test_decode_rejects_bad_sof() -> None:
    frame = bytearray(P.encode_frame(P.Cmd.PING))
    frame[0] = 0xA4
    with pytest.raises(P.ProtocolError):
        P.decode_frame(bytes(frame))


@pytest.mark.parametrize("len_byte", [0x00, 0x41], ids=["LEN=0x00", "LEN=0x41"])
def test_decode_rejects_bad_len(len_byte: int) -> None:
    """LEN 越界（< 1 或 > 64）都必须拒绝（原用例的两个断言各占一个参数）。"""
    with pytest.raises(P.ProtocolError):
        P.decode_frame(bytes([P.SOF, len_byte, 0x01, 0x00, 0x00]))


def test_decode_rejects_bad_length() -> None:
    with pytest.raises(P.ProtocolError):
        P.decode_frame(P.encode_frame(P.Cmd.PING) + b"\x00")


def test_decode_rejects_bad_crc() -> None:
    frame = bytearray(P.encode_frame(P.Cmd.PING))
    frame[-1] ^= 0xFF
    with pytest.raises(P.ProtocolError):
        P.decode_frame(bytes(frame))


def test_cmd_direction_bit() -> None:
    """响应码 = 请求码 | 0x80；错误码与通知码的分类标志。"""
    assert P.Cmd.PING.response == 0x81
    assert P.Cmd.IDENTIFY.response == 0x82
    assert P.Cmd.GET_MEAS.response == 0x86
    assert P.Cmd.GET_MEAS.response == 0x86
    assert P.Cmd.ERR_PARAM.is_error
    assert P.Cmd.ERR_BUSY.is_error
    assert P.Cmd.STREAM_DATA.is_notification
    assert P.Cmd.EVENT.is_notification
    assert not P.Cmd.GET_MEAS.is_notification


def test_notification_codes_are_separate_namespace() -> None:
    """协议 v1.1：通知码 0xE1/0xE2，与响应码 0x81..0x8E 不再撞码。"""
    assert int(P.Cmd.STREAM_DATA) == 0xE1
    assert int(P.Cmd.EVENT) == 0xE2
    assert P.Cmd.PING.response != int(P.Cmd.STREAM_DATA)
    assert P.Cmd.IDENTIFY.response != int(P.Cmd.EVENT)
    assert P.Cmd.STREAM_DATA.is_notification
    assert P.Cmd.EVENT.is_notification
    assert not P.Cmd.PING.is_notification
    assert P.IDENTIFY_SIZE == 30


@pytest.mark.parametrize("code", [0xE0, 0xE1, 0xE2, 0xEF], ids=lambda c: hex(c))
def test_is_notification_code_true(code: int) -> None:
    """判定规则：cmd & 0xF0 == 0xE0 即通知。"""
    assert P.is_notification_code(code)


@pytest.mark.parametrize("code", [0x01, 0x0E, 0x81, 0x82, 0x8E, 0xFE, 0xFF], ids=lambda c: hex(c))
def test_is_notification_code_false(code: int) -> None:
    """非 0xEx 段一律不是通知（响应码、错误码都不算）。"""
    assert not P.is_notification_code(code)


# ------------------------------------------------------------------ 流式解析：粘包/拆包/噪声/坏帧重同步


def test_two_frames_in_one_chunk() -> None:
    data = P.encode_frame(P.Cmd.PING) + P.encode_frame(P.Cmd.START)
    frames, errors = P.parse_stream(data)
    assert [f.cmd for f in frames] == [0x01, 0x04]
    assert errors == []


def test_byte_by_byte() -> None:
    """逐字节喂入：收全之前不出帧，收全后恰好出 1 帧且缓冲区清空。"""
    data = P.encode_frame(P.Cmd.IDENTIFY.response, P.encode_identify(_identify()))
    parser = P.FrameParser()
    frames = []
    for i in range(len(data)):
        events = parser.feed(data[i : i + 1])
        frames.extend(ev for ev in events if isinstance(ev, P.Frame))
        if i < len(data) - 1:
            assert frames == []
    assert len(frames) == 1
    assert frames[0].cmd == 0x82  # IDENTIFY 的响应码（0x02 | 0x80）
    assert P.decode_identify(frames[0].payload).dsp_clk_hz == 450_000_000
    assert parser.pending == b""


def test_noise_is_skipped() -> None:
    data = b"\x00\x11\x22" + P.encode_frame(P.Cmd.PING) + b"\x33"
    frames, errors = P.parse_stream(data)
    assert len(frames) == 1
    assert sum(1 for e in errors if e.kind == "noise") == 2


def test_bad_crc_is_discarded_and_resyncs() -> None:
    bad = bytearray(P.encode_frame(P.Cmd.PING))
    bad[2] ^= 0xFF  # 改 CMD，CRC 不再匹配
    good = P.encode_frame(P.Cmd.START)
    frames, errors = P.parse_stream(bytes(bad) + good)
    assert [f.cmd for f in frames] == [0x04]
    assert any(e.kind == "crc" for e in errors)


def test_bad_len_resyncs() -> None:
    data = bytes([P.SOF, 0x00]) + P.encode_frame(P.Cmd.PING)
    frames, errors = P.parse_stream(data)
    assert [f.cmd for f in frames] == [0x01]
    assert any(e.kind == "len" for e in errors)


def test_incomplete_frame_is_buffered() -> None:
    data = P.encode_frame(P.Cmd.PING)
    parser = P.FrameParser()
    assert parser.feed(data[:-1]) == []
    assert len(parser.pending) == len(data) - 1
    events = parser.feed(data[-1:])
    assert len(events) == 1


# ------------------------------------------------------------------ 各命令 PAYLOAD 的小端编解码与字长校验


def test_identify_round_trip() -> None:
    info = _identify()
    payload = P.encode_identify(info)
    assert len(payload) == P.IDENTIFY_SIZE
    assert P.decode_identify(payload) == info
    assert P.decode_identify(payload).has_signal_source
    assert P.decode_identify(payload).ver_text == "0.1.0"
    # 小端检查：ver 的低字节在前
    assert payload[0] == 0x00
    assert payload[3] == 0x00


def test_status_bits() -> None:
    st = P.Status(P.STATUS_BUSY | P.STATUS_RESULT_VALID, P.SIGST_ENABLE | P.SIGST_READY | P.SIGST_LOCKED, 2000, 3, 7)
    payload = P.encode_status(st)
    assert len(payload) == P.STATUS_SIZE
    got = P.decode_status(payload)
    assert got == st
    assert got.busy and got.result_valid and not got.lost and not got.irq_pending
    assert got.sig_enable and got.src_locked and got.source_ready and not got.sig_select
    assert got.status_text == "busy|resultValid"


def test_result_round_trip() -> None:
    r = P.Result(True, 5, 2002, 1001, 4004)
    payload = P.encode_result(r)
    assert len(payload) == P.RESULT_SIZE
    assert P.decode_result(payload) == r
    assert r.phase_cycles == 4
    assert not P.decode_result(P.encode_result(P.Result(False, 0, 0, 0, 0))).valid


def test_meas_round_trip() -> None:
    m = P.Meas(9, 2002, 1001, 4004, 25_000_000, 90_000)
    payload = P.encode_meas(m)
    assert len(payload) == P.MEAS_SIZE
    assert P.decode_meas(payload) == m
    assert m.freq_hz_field == 25_000_000
    assert m.as_row()["f_meas_mHz"] == 25_000_000


def test_stream_data_round_trip() -> None:
    data = P.StreamData(P.Meas(1, 2000, 1000, 3000, 25_000_000, 90_000), 4)
    payload = P.encode_stream_data(data)
    assert len(payload) == P.STREAM_DATA_SIZE
    assert P.decode_stream_data(payload) == data
    # 通知码是 0xE1，不再与 PING 响应（0x81）撞码
    frame = P.decode_frame(P.encode_frame(P.Cmd.STREAM_DATA, payload))
    assert frame.is_notification
    assert frame.name == "STREAM_DATA"


def test_event_round_trip() -> None:
    ev = P.Event(1, 12345)
    payload = P.encode_event(ev)
    assert len(payload) == P.EVENT_SIZE
    assert P.decode_event(payload) == ev
    assert ev.name == "结果丢失"
    assert P.Event(99, 0).name == "未知事件 99"


def test_sig_info_round_trip() -> None:
    info = P.SigInfo(25_000_000, 90_000, 1_000_000_000, 40, 2)
    payload = P.encode_sig_info(info)
    assert len(payload) == P.SIG_INFO_SIZE
    assert P.decode_sig_info(payload) == info
    assert info.divide_field == 40  # 低字节 = D 本体（实测）
    assert P.decode_set_sig(P.encode_set_sig(8_000_000, 45_000)) == (8_000_000, 45_000)


def test_stream_round_trip() -> None:
    assert P.decode_stream(P.encode_stream(1, 100)) == (1, 100)


@pytest.mark.parametrize(
    "decode",
    [
        pytest.param(lambda: P.decode_meas(b"\x00" * 20), id="MEAS-20-字节"),
        pytest.param(lambda: P.decode_set_gate(b"\x00\x00"), id="SET_GATE-2-字节"),
        pytest.param(lambda: P.decode_identify(b"\x00" * 29), id="IDENTIFY-29-字节"),
        pytest.param(lambda: P.check_request_payload(P.Cmd.SET_MODE, b"\x00\x00"), id="SET_MODE-请求-2-字节"),
        pytest.param(lambda: P.check_response_payload(P.Cmd.GET_STATUS, b"\x00" * 4), id="GET_STATUS-响应-4-字节"),
    ],
)
def test_length_validation(decode) -> None:
    """字长不符一律抛 `PayloadError`（原用例的 5 个断言各占一个参数）。"""
    with pytest.raises(P.PayloadError):
        decode()


@pytest.mark.parametrize(
    "encode",
    [
        pytest.param(lambda: P.encode_set_sig(2**32, 0), id="SET_SIG-频率-2^32"),
        pytest.param(lambda: P.encode_stream(0, 2**16), id="STREAM-period-2^16"),
    ],
)
def test_u32_range_check(encode) -> None:
    """参数超出字段位宽（u32 / u16）必须拒绝。"""
    with pytest.raises(P.PayloadError):
        encode()


@pytest.mark.parametrize(
    "code, name",
    [
        pytest.param(P.Cmd.ERR_PARAM, "ERR_PARAM", id="ERR_PARAM"),
        pytest.param(P.Cmd.ERR_BUSY, "ERR_BUSY", id="ERR_BUSY"),
        pytest.param(0x00, "NO_RESPONSE", id="0x00"),
        pytest.param(0x33, "UNKNOWN", id="0x33"),
    ],
)
def test_error_names(code: int, name: str) -> None:
    assert P.error_name(code) == name


def test_error_exception_text() -> None:
    exc = P.DpmError(P.Cmd.ERR_BUSY, P.Cmd.SET_SIG)
    assert "ERR_BUSY" in str(exc)
    assert "SET_SIG" in str(exc)
    assert "timeout=0.5s" in str(P.DpmTimeoutError(P.Cmd.PING, 0.5))


def test_decode_response_helper() -> None:
    frame = P.decode_frame(P.encode_frame(P.Cmd.GET_STATUS.response, P.encode_status(P.Status(1, 2, 3, 4, 5))))
    assert P.decode_response(frame) == P.Status(1, 2, 3, 4, 5)
    assert P.decode_response(P.decode_frame(P.encode_frame(P.Cmd.PING.response))) is None
    stream = P.decode_frame(P.encode_frame(P.Cmd.STREAM_DATA, P.encode_stream_data(P.StreamData(P.Meas(0, 1, 2, 3, 4, 5), 6))))
    assert isinstance(P.decode_response(stream), P.StreamData)
    # 旧的撞码场景：0x81 带 26 字节 PAYLOAD 现在只会被当成（未知长度的）响应
    legacy = P.Frame(cmd=0x81, payload=b"\x00" * P.STREAM_DATA_SIZE)
    assert not legacy.is_notification  # 0x81 只是 PING 的响应码
    assert legacy.name == "PING 响应"
    assert P.cmd_name(P.Cmd.STREAM_DATA) == "STREAM_DATA"
    assert P.cmd_name(0x33) == "0x33"


def test_mode_and_source_parsing() -> None:
    assert P.str_to_mode("continuous") == P.MODE_CONTINUOUS
    assert P.str_to_mode("single") == P.MODE_SINGLE
    assert P.str_to_source("int") == P.SOURCE_INTERNAL
    assert P.str_to_source("external") == P.SOURCE_EXTERNAL
    with pytest.raises(ValueError):
        P.str_to_mode("bogus")
    with pytest.raises(ValueError):
        P.str_to_source("bogus")


def _identify() -> P.Identify:
    return P.Identify(
        ver=0x00010000,
        git_rev=0xDEADBEEF,
        caps=0x00030104,
        version_reg=0x44504D02,
        caps_reg=0x00030104,
        dsp_clk_hz=450_000_000,
        slow_clk_hz=50_000_000,
        flags=0x000F,
    )
