"""PL 仿真后端（寄存器语义 + 测量模型 + 假 MMCM）与 PS 固件逻辑的单元测试。"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpm_host import mathx as M  # noqa: E402
from dpm_host import protocol as P  # noqa: E402
from dpm_host import sig as S  # noqa: E402
from dpm_host import sim_fpga as F  # noqa: E402
from dpm_host.sim_server import ServerOptions, SimPs, SimServer  # noqa: E402

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_PATH = os.path.join(os.path.dirname(HOST_DIR), "spec", "golden", "vectors.json")


def new_fpga(**kwargs) -> tuple[F.SimFpga, F.FakeClock]:
    clock = F.FakeClock(1000.0)
    fpga = F.SimFpga(clock=clock, **kwargs)
    clock.advance(0.001)
    fpga.poll()
    return fpga, clock


class MeasurementModelTests(unittest.TestCase):
    """测量模型必须与 `spec/gen_golden.py` 的独立参考实现逐位一致。"""

    @unittest.skipUnless(os.path.exists(GOLDEN_PATH), "spec/golden/vectors.json 不存在")
    def test_matches_golden_counts(self) -> None:
        with open(GOLDEN_PATH, encoding="utf-8") as fh:
            vectors = json.load(fh)["vectors"]
        fpga, _clock = new_fpga()
        for vec in vectors:
            with self.subTest(freq=vec["freq_hz"], phase=vec["phase_requested_mdeg"]):
                counts = fpga.measurement_model(2000, vec["freq_hz"], vec["phase_requested_mdeg"])
                self.assertEqual(counts, (vec["gate_cnt"], vec["edge_cnt"], vec["phase_cnt"]))

    def test_formula_shape(self) -> None:
        fpga, _clock = new_fpga()
        gate, edge, phase = fpga.measurement_model(2000, 10_000_000, 90_000)
        self.assertEqual(gate, 2000 + 5)  # ceil(50MHz / 10MHz)
        self.assertEqual(edge, (2005 * 10_000_000) // 50_000_000)
        self.assertEqual(phase, 10 * edge)  # 90° @10MHz = 10 个 DSP 周期

    def test_zero_frequency(self) -> None:
        fpga, _clock = new_fpga()
        self.assertEqual(fpga.measurement_model(2000, 0, 90_000), (0, 0, 0))


class RegisterSemanticsTests(unittest.TestCase):
    """寄存器级语义（对齐 ControlRegs.scala）。"""

    def test_version_and_caps(self) -> None:
        fpga, _clock = new_fpga()
        self.assertEqual(fpga.read(F.REG_VERSION), 0x44504D02)
        self.assertEqual(fpga.read(F.REG_CAPS), 0x00030104)

    def test_status_bits_after_start_and_completion(self) -> None:
        fpga, clock = new_fpga(freq_hz=10_000_000)
        self.assertTrue(fpga.start())
        self.assertTrue(fpga.read(F.REG_STATUS) & P.STATUS_BUSY)
        self.assertFalse(fpga.read(F.REG_STATUS) & P.STATUS_RESULT_VALID)
        clock.advance(0.01)
        fpga.poll()
        status = fpga.read(F.REG_STATUS)
        self.assertFalse(status & P.STATUS_BUSY)
        self.assertTrue(status & P.STATUS_RESULT_VALID)
        self.assertTrue(status & P.STATUS_IRQ_PENDING)
        self.assertFalse(status & P.STATUS_LOST)
        self.assertEqual(fpga.read(F.REG_RESULT_SEQ), 0)
        self.assertEqual(fpga.read(F.REG_RESULT_GATE), 2005)
        self.assertEqual(fpga.read(F.REG_RESULT_EDGE), 401)
        self.assertEqual(fpga.read(F.REG_RESULT_PHASE), 4010)

    def test_start_ignored_while_busy(self) -> None:
        fpga, clock = new_fpga(freq_hz=10_000_000, gate_target=2000)
        fpga.start()
        fpga.write(F.REG_GATE_TARGET, 4000)
        self.assertFalse(fpga.start())
        self.assertEqual(fpga.start_ignored_while_busy, 1)
        clock.advance(0.01)
        fpga.poll()
        # 启动时锁存的目标闸门仍然是 2000，测量中改 GATE_TARGET 不影响本次
        self.assertEqual(fpga.result_gate, 2005)
        self.assertEqual(fpga.target_gate, 4000)

    def test_result_overwrite_counts_as_lost(self) -> None:
        fpga, clock = new_fpga(freq_hz=10_000_000)
        fpga.start()
        clock.advance(0.01)
        fpga.poll()
        self.assertEqual(fpga.lost_count, 0)
        fpga.start()  # 旧结果没读走 → 新结果覆盖
        clock.advance(0.01)
        fpga.poll()
        self.assertEqual(fpga.lost_count, 1)
        self.assertEqual(fpga.result_seq, 1)
        self.assertTrue(fpga.read(F.REG_STATUS) & P.STATUS_LOST)

    def test_irq_status_w1c(self) -> None:
        fpga, clock = new_fpga(freq_hz=10_000_000)
        fpga.start()
        clock.advance(0.01)
        fpga.poll()
        self.assertEqual(fpga.read(F.REG_IRQ_STATUS), F.IRQ_PENDING | F.IRQ_RESULT_READ)
        # 写 b0|b1：清中断挂起 + 结果已读走
        fpga.write(F.REG_IRQ_STATUS, F.IRQ_PENDING | F.IRQ_RESULT_READ)
        self.assertEqual(fpga.read(F.REG_IRQ_STATUS), 0)
        self.assertFalse(fpga.result_valid)
        # 再来一次并故意不读走 → 覆盖时 lostCount++
        fpga.start()
        clock.advance(0.01)
        fpga.poll()
        fpga.start()
        clock.advance(0.01)
        fpga.poll()
        self.assertEqual(fpga.lost_count, 1)
        self.assertEqual(fpga.read(F.REG_IRQ_STATUS), F.IRQ_PENDING | F.IRQ_RESULT_READ | F.IRQ_LOST_CLEAR)
        fpga.write(F.REG_IRQ_STATUS, F.IRQ_LOST_CLEAR)
        self.assertEqual(fpga.lost_count, 0)

    def test_clear_is_pended_while_busy(self) -> None:
        fpga, clock = new_fpga(freq_hz=10_000_000)
        fpga.start()
        fpga.write(F.REG_CTRL, F.CTRL_IRQ_ENABLE | F.CTRL_CLEAR)
        self.assertTrue(fpga.busy)
        self.assertTrue(fpga.clear_req)
        clock.advance(0.01)
        fpga.poll()
        self.assertFalse(fpga.busy)
        self.assertFalse(fpga.clear_req)
        # clear 生效：结果区与标志复位、seq 归零
        self.assertFalse(fpga.result_valid)
        self.assertEqual((fpga.result_gate, fpga.result_edge, fpga.result_phase, fpga.seq), (0, 0, 0, 0))

    def test_gate_target_byte_strobe(self) -> None:
        fpga, _clock = new_fpga()
        fpga.write(F.REG_GATE_TARGET, 0x0000ABCD)
        # 只写低两字节，高两字节保持
        fpga.write(F.REG_GATE_TARGET, 0x00001234, strb=0x3)
        self.assertEqual(fpga.target_gate, 0x00001234)
        fpga.write(F.REG_GATE_TARGET, 0xFFFF0000, strb=0xC)
        self.assertEqual(fpga.target_gate, 0x0FFF1234 & F.GATE_TARGET_MAX)

    def test_ctrl_read_back(self) -> None:
        fpga, _clock = new_fpga()
        fpga.write(F.REG_CTRL, F.CTRL_IRQ_ENABLE)
        self.assertEqual(fpga.read(F.REG_CTRL), F.CTRL_IRQ_ENABLE)
        fpga.write(F.REG_CTRL, 0)
        self.assertEqual(fpga.read(F.REG_CTRL), 0)

    def test_source_ready_follows_config_pending(self) -> None:
        fpga, clock = new_fpga()
        fpga.write(F.REG_SIG_CTRL, P.SIGST_SELECT | P.SIGST_ENABLE)
        fpga.poll()
        self.assertTrue(fpga.config_pending)
        self.assertFalse(fpga.source_ready)  # 配置快照未送达 → 0
        self.assertFalse(fpga.read(F.REG_SIG_STATUS) & P.SIGST_READY)
        clock.advance(0.01)
        fpga.poll()
        self.assertTrue(fpga.source_ready)
        self.assertEqual(fpga.read(F.REG_SIG_STATUS) & P.SIGST_READY, P.SIGST_READY)

    def test_external_source_ready_always(self) -> None:
        fpga, clock = new_fpga()
        fpga.write(F.REG_SIG_CTRL, 0)  # 选外部
        self.assertFalse(fpga.source_ready)  # 配置快照未送达前仍为 0
        clock.advance(0.01)
        fpga.poll()  # 组装快照 → configPending
        clock.advance(0.01)
        fpga.poll()  # 快照送达测量域
        self.assertFalse(fpga.config_pending)
        self.assertTrue(fpga.source_ready)  # 选外部恒可用

    def test_measurement_never_completes_without_source(self) -> None:
        """片内源未使能 → 两路恒低 → 测量永不完成（上位机必须能超时退出）。"""
        fpga, clock = new_fpga(sig_enable=False)
        fpga.start()
        clock.advance(60.0)
        fpga.poll()
        self.assertTrue(fpga.busy)
        self.assertFalse(fpga.result_valid)

    def test_drp_load_saddr_selects_static_or_dynamic(self) -> None:
        # 构造时按初始频点走过一次 LOAD 0x07（SADDR=1）→ 当前是动态配置
        fpga, _clock = new_fpga(freq_hz=10_000_000)
        self.assertEqual(fpga.mmcm_active, "dynamic")
        self.assertEqual(fpga.int_freq_hz, 10_000_000)
        # LOAD 写 0x01（SADDR=0）→ 回到 BD 静态配置（MULT=20 / D=40 → 25MHz / 90°）
        fpga.write(S.DRP_LOAD_OFF, 0x01)
        self.assertEqual(fpga.mmcm_active, "static")
        self.assertEqual(fpga.int_freq_hz, 25_000_000)
        self.assertEqual(fpga.int_phase_milli_deg, 90_000)
        # LOAD 写 0x02（bit0=0 不触发）→ 配置保持
        fpga.write(S.DRP_LOAD_OFF, 0x02)
        self.assertEqual(fpga.mmcm_active, "static")
        # LOAD 写 0x03（SADDR=1）→ 回到寄存器文件的动态配置
        fpga.write(S.DRP_LOAD_OFF, 0x03)
        self.assertEqual(fpga.mmcm_active, "dynamic")
        self.assertEqual(fpga.int_freq_hz, 10_000_000)

    def test_drp_status_offsets(self) -> None:
        fpga, clock = new_fpga()
        self.assertEqual(fpga.read(F.DRP_STATUS_FLAT_OFF), S.DRP_STATUS_LOCKED)
        self.assertEqual(fpga.read(0x27C), 0)  # 实测：字 31 恒 0
        fpga.configure_sig(10_000_000, 0)
        self.assertEqual(fpga.read(F.DRP_STATUS_FLAT_OFF), 0)  # 重配置中未锁定
        clock.advance(0.01)
        fpga.poll()
        self.assertEqual(fpga.read(F.DRP_STATUS_FLAT_OFF), S.DRP_STATUS_LOCKED)

    def test_phase_model_static_ignores_phase_word(self) -> None:
        """`--phase-model static`：复现 PHASE_DUTY_CONFIG=false 的旧 bitstream（相位字未接线）。"""
        fpga, _clock = new_fpga(phase_model="static")
        self.assertEqual(fpga.phase_model, "static")
        for phase in (0, 22_500, 45_000, 135_000):
            fpga.configure_sig(10_000_000, phase)
            self.assertEqual(fpga.int_phase_milli_deg, 90_000)
        self.assertEqual(fpga.drp.get(S.DRP_IDX_CLKOUT1_PHASE), 135_000)  # 照写（无害）

    def test_phase_model_controllable_quantizes(self) -> None:
        fpga, _clock = new_fpga(phase_model="controllable")
        self.assertEqual(fpga.phase_model, "controllable")
        fpga.configure_sig(10_000_000, 22_500)
        self.assertEqual(fpga.int_phase_milli_deg, 22_500)  # D=100 → 网格 0.45°，22.5° 是整步
        fpga.configure_sig(10_000_000, 100_000)
        self.assertEqual(fpga.int_phase_milli_deg, 99_900)  # 100/0.45 = 222.2 → 222 步 = 99.9°
        self.assertEqual(fpga.phase_quantum_milli_deg(), 450)

    def test_measurement_uses_active_source_phase(self) -> None:
        fpga, clock = new_fpga(phase_model="controllable", freq_hz=10_000_000, phase_milli_deg=45_000)
        fpga.start()
        clock.advance(0.01)
        fpga.poll()
        calc = M.meas_calc(fpga.result_gate, fpga.result_edge, fpga.result_phase, fpga.slow_hz, fpga.dsp_hz)
        self.assertEqual(calc.freq_hz, 10_000_000)
        self.assertEqual(calc.phase_milli_deg, 45_000)


class SimPsTests(unittest.TestCase):
    """PS 固件逻辑：命令分发、连续模式轮转、流通知。"""

    def setUp(self) -> None:
        self.clock = F.FakeClock(2000.0)
        self.fpga = F.SimFpga(clock=self.clock, phase_model="controllable", freq_hz=25_000_000, phase_milli_deg=90_000)
        self.clock.advance(0.001)
        self.fpga.poll()
        self.ps = SimPs(self.fpga, options=ServerOptions())
        self.server = SimServer(self.ps)

    def _request(self, cmd: int, payload: bytes = b"") -> P.Frame | bytes:
        out = self.server.feed(P.encode_frame(cmd, payload))
        self.assertTrue(out, f"命令 0x{cmd:02X} 没有得到响应")
        return P.decode_frame(out[0])

    def test_ping_and_identify(self) -> None:
        frame = self._request(P.Cmd.PING)
        assert isinstance(frame, P.Frame)
        self.assertEqual(frame.cmd, P.Cmd.PING.response)
        self.assertEqual(frame.payload, b"")
        info = P.decode_identify(self._request(P.Cmd.IDENTIFY).payload)  # type: ignore[union-attr]
        self.assertEqual(info.version_reg, 0x44504D02)
        self.assertEqual(info.caps, 0x00030104)
        self.assertEqual(info.dsp_clk_hz, 400_000_000)
        self.assertEqual(info.slow_clk_hz, 50_000_000)
        self.assertTrue(info.has_signal_source and info.supports_stream)

    def test_get_status_fields(self) -> None:
        st = P.decode_status(self._request(P.Cmd.GET_STATUS).payload)  # type: ignore[union-attr]
        self.assertEqual(st.gate_target, 2000)
        self.assertTrue(st.sig_select and st.sig_enable and st.source_ready)
        self.assertEqual(st.seq, 0)

    def test_measurement_flow(self) -> None:
        # GET_MEAS 在无结果时回 ERR_BUSY
        frame = self._request(P.Cmd.GET_MEAS)
        self.assertEqual(frame.cmd, P.Cmd.ERR_BUSY)  # type: ignore[union-attr]
        # GET_RESULT 用 valid=0 表达「还没有结果」
        result = P.decode_result(self._request(P.Cmd.GET_RESULT).payload)  # type: ignore[union-attr]
        self.assertFalse(result.valid)
        self._request(P.Cmd.START)
        self.clock.advance(0.01)
        self.fpga.poll()
        meas = P.decode_meas(self._request(P.Cmd.GET_MEAS).payload)  # type: ignore[union-attr]
        self.assertEqual(meas.freq_milli_hz, 25_000_000)  # 字段 = 整数 Hz
        self.assertEqual(meas.phase_milli_deg, 90_000)
        # 结果读走后 resultValid 被清（协议第 4 节：W1C 的 resultValid 位）
        self.assertFalse(self.fpga.result_valid)
        # 再测一次，改用 GET_RESULT 读原始计数
        self._request(P.Cmd.START)
        self.clock.advance(0.01)
        self.fpga.poll()
        result = P.decode_result(self._request(P.Cmd.GET_RESULT).payload)  # type: ignore[union-attr]
        self.assertTrue(result.valid)
        self.assertEqual((result.gate_cnt, result.edge_cnt, result.phase_cnt), (2002, 1001, 4004))

    def test_set_gate_mode_source(self) -> None:
        self.assertEqual(P.decode_set_gate(self._request(P.Cmd.SET_GATE, P.encode_set_gate(4000)).payload), 4000)  # type: ignore[union-attr]
        self.assertEqual(self.fpga.target_gate, 4000)
        self.assertEqual(P.decode_set_mode(self._request(P.Cmd.SET_MODE, P.encode_set_mode(1)).payload), 1)  # type: ignore[union-attr]
        self.assertEqual(self.ps.mode, P.MODE_CONTINUOUS)
        sig_status = P.decode_sig_status_u32(self._request(P.Cmd.SET_SOURCE, P.encode_set_source(0)).payload)  # type: ignore[union-attr]
        self.assertFalse(sig_status & P.SIGST_SELECT)
        self.assertFalse(sig_status & P.SIGST_ENABLE)

    def test_error_responses(self) -> None:
        # 模式/源越界
        self.assertEqual(self._request(P.Cmd.SET_MODE, b"\x05").cmd, P.Cmd.ERR_PARAM)  # type: ignore[union-attr]
        self.assertEqual(self._request(P.Cmd.SET_SOURCE, b"\x09").cmd, P.Cmd.ERR_PARAM)  # type: ignore[union-attr]
        # 字长不符
        self.assertEqual(self._request(P.Cmd.SET_GATE, b"\x00\x01").cmd, P.Cmd.ERR_PARAM)  # type: ignore[union-attr]
        # 闸门超 28 位
        self.assertEqual(self._request(P.Cmd.SET_GATE, P.encode_set_gate(1 << 29)).cmd, P.Cmd.ERR_PARAM)  # type: ignore[union-attr]
        # STREAM enable 越界
        self.assertEqual(self._request(P.Cmd.STREAM, b"\x07\x00\x00").cmd, P.Cmd.ERR_PARAM)  # type: ignore[union-attr]
        # 未知命令
        self.assertEqual(self._request(0x40).cmd, P.Cmd.ERR_PARAM)  # type: ignore[union-attr]

    def test_set_sig_and_get_sig(self) -> None:
        info = P.decode_sig_info(self._request(P.Cmd.SET_SIG, P.encode_set_sig(10_000_000, 45_000)).payload)  # type: ignore[union-attr]
        self.assertEqual(info.freq_hz, 10_000_000)
        self.assertEqual(info.phase_milli_deg, 45_000)
        self.assertEqual(info.vco_hz, 1_000_000_000)
        self.assertEqual(info.div, 100)
        self.assertEqual(info.phase_field, 0)  # 历史字段固定 0
        echo = P.decode_sig_info(self._request(P.Cmd.GET_SIG).payload)  # type: ignore[union-attr]
        self.assertEqual(echo, info)
        self.assertEqual(self.fpga.drp.get(S.DRP_IDX_CLKOUT1_PHASE), 45_000)

    def test_set_sig_while_busy_is_err_busy(self) -> None:
        self._request(P.Cmd.START)
        self.assertTrue(self.fpga.busy)
        self.assertEqual(self._request(P.Cmd.SET_SIG, P.encode_set_sig(10_000_000, 0)).cmd, P.Cmd.ERR_BUSY)  # type: ignore[union-attr]

    def test_continuous_mode_restarts_after_read(self) -> None:
        self._request(P.Cmd.SET_MODE, P.encode_set_mode(P.MODE_CONTINUOUS))
        self._request(P.Cmd.START)
        self.clock.advance(0.01)
        self.fpga.poll()
        self._request(P.Cmd.GET_MEAS)
        # 结果读走后立即重启下一次测量
        self.assertTrue(self.fpga.busy)
        self.clock.advance(0.01)
        self.fpga.poll()
        self.assertEqual(self.fpga.result_seq, 1)

    def test_clear_and_reset_counters(self) -> None:
        self._request(P.Cmd.START)
        self.clock.advance(0.01)
        self.fpga.poll()
        self._request(P.Cmd.CLEAR)
        self.assertFalse(self.fpga.result_valid)
        self.assertEqual(self.fpga.seq, 0)
        self._request(P.Cmd.RESET_COUNTERS)
        self.assertEqual(self.fpga.lost_count, 0)

    def test_stream_notifications(self) -> None:
        self.assertEqual(P.decode_stream(self._request(P.Cmd.STREAM, P.encode_stream(1, 10)).payload), (1, 10))  # type: ignore[union-attr]
        self._request(P.Cmd.START)
        self.clock.advance(0.01)
        self.fpga.poll()
        notifications = self.ps.notifications(now=self.clock() + 0.05)
        stream_data = [P.decode_frame(raw) for raw in notifications]
        self.assertTrue(any(frame.cmd == P.Cmd.STREAM_DATA for frame in stream_data))
        payload = next(frame for frame in stream_data if frame.cmd == P.Cmd.STREAM_DATA)
        data = P.decode_stream_data(payload.payload)
        self.assertEqual(data.lost_delta, 0)
        self.assertEqual(data.meas.freq_milli_hz, 25_000_000)
        # 流上报也会把结果读走
        self.assertFalse(self.fpga.result_valid)

    def test_stream_event_on_lost_result(self) -> None:
        self._request(P.Cmd.STREAM, P.encode_stream(1, 10))
        # 制造一次丢失：不读走结果就再启动
        self._request(P.Cmd.START)
        self.clock.advance(0.01)
        self.fpga.poll()
        self._request(P.Cmd.SET_MODE, P.encode_set_mode(P.MODE_SINGLE))
        self._request(P.Cmd.START)
        self.clock.advance(0.01)
        self.fpga.poll()
        self.assertEqual(self.fpga.lost_count, 1)
        notifications = [P.decode_frame(raw) for raw in self.ps.notifications(now=self.clock() + 0.05)]
        events = [P.decode_event(frame.payload) for frame in notifications if frame.cmd == P.Cmd.EVENT]
        self.assertTrue(any(event.code == 1 for event in events))

    def test_notification_frames_are_ignored(self) -> None:
        # PC → PS 方向不会出现通知帧；收到就当噪声忽略
        self.assertIsNone(self.ps.handle(P.Frame(cmd=P.Cmd.STREAM_DATA, payload=b"\x00" * 26)))
        self.assertIsNone(self.ps.handle(P.Frame(cmd=P.Cmd.EVENT, payload=b"\x00" * 5)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
