"""帧协议与 PAYLOAD 编解码的单元测试（纯函数，无 IO）。

运行：`cd host && python3 -m unittest discover -s tests -t . -v`
（有 pytest 时 `python3 -m pytest tests -q` 也可，见 conftest.py）
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpm_host import protocol as P  # noqa: E402


class CrcTests(unittest.TestCase):
    """CRC-16/MODBUS 的已知校验值。"""

    def test_known_check_value(self) -> None:
        # CRC-16/MODBUS 的标准 check 值：ASCII "123456789" → 0x4B37
        self.assertEqual(P.crc16_modbus(b"123456789"), 0x4B37)

    def test_init_value(self) -> None:
        self.assertEqual(P.crc16_modbus(b""), 0xFFFF)

    def test_frame_crc_is_little_endian(self) -> None:
        frame = P.encode_frame(P.Cmd.PING)
        body = frame[1:3]
        crc = P.crc16_modbus(body)
        self.assertEqual(frame[-2], crc & 0xFF)
        self.assertEqual(frame[-1], (crc >> 8) & 0xFF)


class FrameTests(unittest.TestCase):
    """帧的封装/解析与边界。"""

    def test_ping_frame_bytes(self) -> None:
        # SOF LEN CMD CRC_L CRC_H；LEN = 1（只有 CMD）
        frame = P.encode_frame(P.Cmd.PING)
        self.assertEqual(len(frame), 5)
        self.assertEqual(frame[0], P.SOF)
        self.assertEqual(frame[1], 1)
        self.assertEqual(frame[2], 0x01)

    def test_round_trip_all_commands(self) -> None:
        cases = [
            (P.Cmd.PING, b""),
            (P.Cmd.SET_GATE, P.encode_set_gate(2000)),
            (P.Cmd.SET_MODE, P.encode_set_mode(1)),
            (P.Cmd.SET_SOURCE, P.encode_set_source(1)),
            (P.Cmd.SET_SIG, P.encode_set_sig(25_000_000, 90_000)),
            (P.Cmd.STREAM, P.encode_stream(1, 100)),
        ]
        for cmd, payload in cases:
            with self.subTest(cmd=cmd.name):
                frame = P.decode_frame(P.encode_frame(cmd, payload))
                self.assertEqual(frame.cmd, int(cmd))
                self.assertEqual(frame.payload, payload)
                self.assertEqual(frame.name, cmd.name)

    def test_max_payload(self) -> None:
        payload = bytes(range(63))  # LEN = 64（上限）
        frame = P.encode_frame(0x7F, payload)
        self.assertEqual(frame[1], P.LEN_MAX)
        self.assertEqual(len(frame), P.MAX_FRAME_BYTES)
        self.assertEqual(P.decode_frame(frame).payload, payload)

    def test_payload_too_long(self) -> None:
        with self.assertRaises(P.ProtocolError):
            P.encode_frame(P.Cmd.PING, bytes(64))  # LEN 会变成 65

    def test_decode_rejects_bad_sof(self) -> None:
        frame = bytearray(P.encode_frame(P.Cmd.PING))
        frame[0] = 0xA4
        with self.assertRaises(P.ProtocolError):
            P.decode_frame(bytes(frame))

    def test_decode_rejects_bad_len(self) -> None:
        with self.assertRaises(P.ProtocolError):
            P.decode_frame(bytes([P.SOF, 0x00, 0x01, 0x00, 0x00]))
        with self.assertRaises(P.ProtocolError):
            P.decode_frame(bytes([P.SOF, 0x41, 0x01, 0x00, 0x00]))

    def test_decode_rejects_bad_length(self) -> None:
        with self.assertRaises(P.ProtocolError):
            P.decode_frame(P.encode_frame(P.Cmd.PING) + b"\x00")

    def test_decode_rejects_bad_crc(self) -> None:
        frame = bytearray(P.encode_frame(P.Cmd.PING))
        frame[-1] ^= 0xFF
        with self.assertRaises(P.ProtocolError):
            P.decode_frame(bytes(frame))

    def test_cmd_direction_bit(self) -> None:
        self.assertEqual(P.Cmd.PING.response, 0x81)
        self.assertEqual(P.Cmd.IDENTIFY.response, 0x82)
        self.assertEqual(P.Cmd.GET_MEAS.response, 0x86)
        self.assertEqual(P.Cmd.GET_MEAS.response, 0x86)
        self.assertTrue(P.Cmd.ERR_PARAM.is_error)
        self.assertTrue(P.Cmd.ERR_BUSY.is_error)
        self.assertTrue(P.Cmd.STREAM_DATA.is_notification)
        self.assertTrue(P.Cmd.EVENT.is_notification)
        self.assertFalse(P.Cmd.GET_MEAS.is_notification)

    def test_notification_codes_are_separate_namespace(self) -> None:
        """协议 v1.1：通知码 0xE1/0xE2，与响应码 0x81..0x8E 不再撞码。"""
        self.assertEqual(int(P.Cmd.STREAM_DATA), 0xE1)
        self.assertEqual(int(P.Cmd.EVENT), 0xE2)
        self.assertNotEqual(P.Cmd.PING.response, int(P.Cmd.STREAM_DATA))
        self.assertNotEqual(P.Cmd.IDENTIFY.response, int(P.Cmd.EVENT))
        # 判定规则：cmd & 0xF0 == 0xE0 即通知
        for cmd in (0xE0, 0xE1, 0xE2, 0xEF):
            self.assertTrue(P.is_notification_code(cmd))
        for cmd in (0x01, 0x0E, 0x81, 0x82, 0x8E, 0xFE, 0xFF):
            self.assertFalse(P.is_notification_code(cmd))
        self.assertTrue(P.Cmd.STREAM_DATA.is_notification)
        self.assertTrue(P.Cmd.EVENT.is_notification)
        self.assertFalse(P.Cmd.PING.is_notification)
        self.assertEqual(P.IDENTIFY_SIZE, 30)


class ParserTests(unittest.TestCase):
    """流式解析：粘包/拆包/噪声/坏帧重同步。"""

    def test_two_frames_in_one_chunk(self) -> None:
        data = P.encode_frame(P.Cmd.PING) + P.encode_frame(P.Cmd.START)
        frames, errors = P.parse_stream(data)
        self.assertEqual([f.cmd for f in frames], [0x01, 0x04])
        self.assertEqual(errors, [])

    def test_byte_by_byte(self) -> None:
        data = P.encode_frame(P.Cmd.IDENTIFY.response, P.encode_identify(_identify()))
        parser = P.FrameParser()
        frames = []
        for i in range(len(data)):
            events = parser.feed(data[i : i + 1])
            frames.extend(ev for ev in events if isinstance(ev, P.Frame))
            if i < len(data) - 1:
                self.assertEqual(frames, [])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].cmd, 0x82)  # IDENTIFY 的响应码（0x02 | 0x80）
        self.assertEqual(P.decode_identify(frames[0].payload).dsp_clk_hz, 400_000_000)
        self.assertEqual(parser.pending, b"")

    def test_noise_is_skipped(self) -> None:
        data = b"\x00\x11\x22" + P.encode_frame(P.Cmd.PING) + b"\x33"
        frames, errors = P.parse_stream(data)
        self.assertEqual(len(frames), 1)
        self.assertEqual(sum(1 for e in errors if e.kind == "noise"), 2)

    def test_bad_crc_is_discarded_and_resyncs(self) -> None:
        bad = bytearray(P.encode_frame(P.Cmd.PING))
        bad[2] ^= 0xFF  # 改 CMD，CRC 不再匹配
        good = P.encode_frame(P.Cmd.START)
        frames, errors = P.parse_stream(bytes(bad) + good)
        self.assertEqual([f.cmd for f in frames], [0x04])
        self.assertTrue(any(e.kind == "crc" for e in errors))

    def test_bad_len_resyncs(self) -> None:
        data = bytes([P.SOF, 0x00]) + P.encode_frame(P.Cmd.PING)
        frames, errors = P.parse_stream(data)
        self.assertEqual([f.cmd for f in frames], [0x01])
        self.assertTrue(any(e.kind == "len" for e in errors))

    def test_incomplete_frame_is_buffered(self) -> None:
        data = P.encode_frame(P.Cmd.PING)
        parser = P.FrameParser()
        self.assertEqual(parser.feed(data[:-1]), [])
        self.assertEqual(len(parser.pending), len(data) - 1)
        events = parser.feed(data[-1:])
        self.assertEqual(len(events), 1)


class PayloadTests(unittest.TestCase):
    """各命令 PAYLOAD 的小端编解码与字长校验。"""

    def test_identify_round_trip(self) -> None:
        info = _identify()
        payload = P.encode_identify(info)
        self.assertEqual(len(payload), P.IDENTIFY_SIZE)
        self.assertEqual(P.decode_identify(payload), info)
        self.assertTrue(P.decode_identify(payload).has_signal_source)
        self.assertEqual(P.decode_identify(payload).ver_text, "0.1.0")
        # 小端检查：ver 的低字节在前
        self.assertEqual(payload[0], 0x00)
        self.assertEqual(payload[3], 0x00)

    def test_status_bits(self) -> None:
        st = P.Status(P.STATUS_BUSY | P.STATUS_RESULT_VALID, P.SIGST_ENABLE | P.SIGST_READY | P.SIGST_LOCKED, 2000, 3, 7)
        payload = P.encode_status(st)
        self.assertEqual(len(payload), P.STATUS_SIZE)
        got = P.decode_status(payload)
        self.assertEqual(got, st)
        self.assertTrue(got.busy and got.result_valid and not got.lost and not got.irq_pending)
        self.assertTrue(got.sig_enable and got.src_locked and got.source_ready and not got.sig_select)
        self.assertEqual(got.status_text, "busy|resultValid")

    def test_result_round_trip(self) -> None:
        r = P.Result(True, 5, 2002, 1001, 4004)
        payload = P.encode_result(r)
        self.assertEqual(len(payload), P.RESULT_SIZE)
        self.assertEqual(P.decode_result(payload), r)
        self.assertEqual(r.phase_cycles, 4)
        self.assertFalse(P.decode_result(P.encode_result(P.Result(False, 0, 0, 0, 0))).valid)

    def test_meas_round_trip(self) -> None:
        m = P.Meas(9, 2002, 1001, 4004, 25_000_000, 90_000)
        payload = P.encode_meas(m)
        self.assertEqual(len(payload), P.MEAS_SIZE)
        self.assertEqual(P.decode_meas(payload), m)
        self.assertEqual(m.freq_hz_field, 25_000_000)
        self.assertEqual(m.as_row()["f_meas_mHz"], 25_000_000)

    def test_stream_data_round_trip(self) -> None:
        data = P.StreamData(P.Meas(1, 2000, 1000, 3000, 25_000_000, 90_000), 4)
        payload = P.encode_stream_data(data)
        self.assertEqual(len(payload), P.STREAM_DATA_SIZE)
        self.assertEqual(P.decode_stream_data(payload), data)
        # 通知码是 0xE1，不再与 PING 响应（0x81）撞码
        frame = P.decode_frame(P.encode_frame(P.Cmd.STREAM_DATA, payload))
        self.assertTrue(frame.is_notification)
        self.assertEqual(frame.name, "STREAM_DATA")

    def test_event_round_trip(self) -> None:
        ev = P.Event(1, 12345)
        payload = P.encode_event(ev)
        self.assertEqual(len(payload), P.EVENT_SIZE)
        self.assertEqual(P.decode_event(payload), ev)
        self.assertEqual(ev.name, "结果丢失")
        self.assertEqual(P.Event(99, 0).name, "未知事件 99")

    def test_sig_info_round_trip(self) -> None:
        info = P.SigInfo(25_000_000, 90_000, 1_000_000_000, 40, 2)
        payload = P.encode_sig_info(info)
        self.assertEqual(len(payload), P.SIG_INFO_SIZE)
        self.assertEqual(P.decode_sig_info(payload), info)
        self.assertEqual(info.divide_field, 40)  # 低字节 = D 本体（实测）
        self.assertEqual(P.decode_set_sig(P.encode_set_sig(8_000_000, 45_000)), (8_000_000, 45_000))

    def test_stream_round_trip(self) -> None:
        self.assertEqual(P.decode_stream(P.encode_stream(1, 100)), (1, 100))

    def test_length_validation(self) -> None:
        with self.assertRaises(P.PayloadError):
            P.decode_meas(b"\x00" * 20)
        with self.assertRaises(P.PayloadError):
            P.decode_set_gate(b"\x00\x00")
        with self.assertRaises(P.PayloadError):
            P.decode_identify(b"\x00" * 29)
        with self.assertRaises(P.PayloadError):
            P.check_request_payload(P.Cmd.SET_MODE, b"\x00\x00")
        with self.assertRaises(P.PayloadError):
            P.check_response_payload(P.Cmd.GET_STATUS, b"\x00" * 4)

    def test_u32_range_check(self) -> None:
        with self.assertRaises(P.PayloadError):
            P.encode_set_sig(2**32, 0)
        with self.assertRaises(P.PayloadError):
            P.encode_stream(0, 2**16)

    def test_error_names_and_exception(self) -> None:
        self.assertEqual(P.error_name(P.Cmd.ERR_PARAM), "ERR_PARAM")
        self.assertEqual(P.error_name(P.Cmd.ERR_BUSY), "ERR_BUSY")
        self.assertEqual(P.error_name(0x00), "NO_RESPONSE")
        self.assertEqual(P.error_name(0x33), "UNKNOWN")
        exc = P.DpmError(P.Cmd.ERR_BUSY, P.Cmd.SET_SIG)
        self.assertIn("ERR_BUSY", str(exc))
        self.assertIn("SET_SIG", str(exc))
        self.assertIn("timeout=0.5s", str(P.DpmTimeoutError(P.Cmd.PING, 0.5)))

    def test_decode_response_helper(self) -> None:
        frame = P.decode_frame(P.encode_frame(P.Cmd.GET_STATUS.response, P.encode_status(P.Status(1, 2, 3, 4, 5))))
        self.assertEqual(P.decode_response(frame), P.Status(1, 2, 3, 4, 5))
        self.assertIsNone(P.decode_response(P.decode_frame(P.encode_frame(P.Cmd.PING.response))))
        stream = P.decode_frame(P.encode_frame(P.Cmd.STREAM_DATA, P.encode_stream_data(P.StreamData(P.Meas(0, 1, 2, 3, 4, 5), 6))))
        self.assertIsInstance(P.decode_response(stream), P.StreamData)
        # 旧的撞码场景：0x81 带 26 字节 PAYLOAD 现在只会被当成（未知长度的）响应
        legacy = P.Frame(cmd=0x81, payload=b"\x00" * P.STREAM_DATA_SIZE)
        self.assertFalse(legacy.is_notification)          # 0x81 只是 PING 的响应码
        self.assertEqual(legacy.name, "PING 响应")
        self.assertEqual(P.cmd_name(P.Cmd.STREAM_DATA), "STREAM_DATA")
        self.assertEqual(P.cmd_name(0x33), "0x33")

    def test_mode_and_source_parsing(self) -> None:
        self.assertEqual(P.str_to_mode("continuous"), P.MODE_CONTINUOUS)
        self.assertEqual(P.str_to_mode("single"), P.MODE_SINGLE)
        self.assertEqual(P.str_to_source("int"), P.SOURCE_INTERNAL)
        self.assertEqual(P.str_to_source("external"), P.SOURCE_EXTERNAL)
        with self.assertRaises(ValueError):
            P.str_to_mode("bogus")
        with self.assertRaises(ValueError):
            P.str_to_source("bogus")


def _identify() -> P.Identify:
    return P.Identify(
        ver=0x00010000,
        git_rev=0xDEADBEEF,
        caps=0x00030104,
        version_reg=0x44504D02,
        caps_reg=0x00030104,
        dsp_clk_hz=400_000_000,
        slow_clk_hz=50_000_000,
        flags=0x000F,
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
