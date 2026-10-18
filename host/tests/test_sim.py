"""PL 仿真后端（寄存器语义 + 测量模型 + 假 MMCM/DRP 镜像）与 PS 固件逻辑的单元测试。

片内源是「方案 A」：模型里 DRP 段在平坦地址空间的 `0x100 + 偏移`（真实硬件是另一个
AXI 从口 `0x43C1_0000`），只有 8 个 16 位 MMCM 寄存器镜像（0x08..0x24）+ LOAD（0x28，写 1
触发）+ ID/STATUS（0x00/0x04）。写镜像不立即生效，LOAD 才整批生效；旧 `clk_wiz_src` 方案的
`MmcmConfig` / `mmcm_active` / `srdy` / SADDR 静态-动态切换都不存在了。

运行：
    仓库根目录：`uv run pytest host/tests/test_sim.py -q`
    host 目录： `uv run pytest tests/test_sim.py -q`
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import pytest
from dpm_host import mathx as M
from dpm_host import protocol as P
from dpm_host import sig as S
from dpm_host import sim_fpga as F
from dpm_host.sim_server import ServerOptions, SimPs, SimServer

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_PATH = os.path.join(os.path.dirname(HOST_DIR), "spec", "golden", "vectors.json")

#: 片内源 DRP 段在模型平坦地址空间里的基偏移（`F.DRP_FLAT_BASE` = 0x100）。
DRP_BASE = F.DRP_FLAT_BASE

#: 8 个 MMCM 寄存器镜像（LOAD 不占镜像字典，它是触发位）。
DRP_MIRROR_OFFSETS = (
    S.DRP_OFF_CLKFBOUT,
    S.DRP_OFF_CLKFBOUT2,
    S.DRP_OFF_CLKOUT0,
    S.DRP_OFF_CLKOUT0_2,
    S.DRP_OFF_CLKOUT1,
    S.DRP_OFF_CLKOUT1_2,
    S.DRP_OFF_DIVCLK,
    S.DRP_OFF_POWER,
)


def new_fpga(**kwargs) -> tuple[F.SimFpga, F.FakeClock]:
    """建一个推进过一次 `poll()` 的模型（输入选择已跟随、MMCM 已锁定）。"""
    clock = F.FakeClock(1000.0)
    fpga = F.SimFpga(clock=clock, **kwargs)
    clock.advance(0.001)
    fpga.poll()
    return fpga, clock


def drp_status(fpga: F.SimFpga) -> int:
    """片内源 DRP 段的 STATUS 字（bit0 = locked、bit1/bit2 = 重配置进行中）。"""
    return fpga.read(DRP_BASE + S.DRP_OFF_STATUS)


# ------------------------------------------------------------------ 测量模型


def _load_golden_vectors() -> list[dict]:
    """读 `spec/golden/vectors.json`；文件缺失时返回空表（对应用例自动跳过）。"""
    if not os.path.exists(GOLDEN_PATH):
        return []
    with open(GOLDEN_PATH, encoding="utf-8") as fh:
        return json.load(fh).get("vectors") or []


GOLDEN_VECTORS = _load_golden_vectors()
_GOLDEN_IDS = [f"{vec['freq_hz']}Hz-{vec['phase_requested_mdeg']}mdeg" for vec in GOLDEN_VECTORS]


@pytest.mark.skipif(not GOLDEN_VECTORS, reason="spec/golden/vectors.json 不存在")
@pytest.mark.parametrize("vec", GOLDEN_VECTORS, ids=_GOLDEN_IDS)
def test_matches_golden_counts(vec: dict) -> None:
    """测量模型必须与 `spec/gen_golden.py` 的独立参考实现逐位一致（原 subTest 逐组展开）。"""
    fpga, _clock = new_fpga()
    counts = fpga.measurement_model(2000, vec["freq_hz"], vec["phase_requested_mdeg"])
    assert counts == (vec["gate_cnt"], vec["edge_cnt"], vec["phase_cnt"])


def test_formula_shape() -> None:
    fpga, _clock = new_fpga()
    gate, edge, phase = fpga.measurement_model(2000, 10_000_000, 90_000)
    assert gate == 2000 + 5  # ceil(50MHz / 10MHz)
    assert edge == (2005 * 10_000_000) // 50_000_000
    # 90° @10MHz = 25ns = 11.25 个 DSP 周期（450MHz）→ RNE → 11 个
    assert phase == 11 * edge


def test_zero_frequency() -> None:
    fpga, _clock = new_fpga()
    assert fpga.measurement_model(2000, 0, 90_000) == (0, 0, 0)


# ------------------------------------------------------------------ 寄存器语义


def test_version_and_caps() -> None:
    fpga, _clock = new_fpga()
    assert fpga.read(F.REG_VERSION) == 0x44504D02
    assert fpga.read(F.REG_CAPS) == 0x00030104


def test_status_bits_after_start_and_completion() -> None:
    fpga, clock = new_fpga(freq_hz=10_000_000)
    assert fpga.start()
    assert fpga.read(F.REG_STATUS) & P.STATUS_BUSY
    assert not fpga.read(F.REG_STATUS) & P.STATUS_RESULT_VALID
    clock.advance(0.01)
    fpga.poll()
    status = fpga.read(F.REG_STATUS)
    assert not status & P.STATUS_BUSY
    assert status & P.STATUS_RESULT_VALID
    assert status & P.STATUS_IRQ_PENDING
    assert not status & P.STATUS_LOST
    assert fpga.read(F.REG_RESULT_SEQ) == 0
    assert fpga.read(F.REG_RESULT_GATE) == 2005
    assert fpga.read(F.REG_RESULT_EDGE) == 401
    assert fpga.read(F.REG_RESULT_PHASE) == 4411  # 11 个 DSP 周期 × 401 个沿


def test_start_ignored_while_busy() -> None:
    fpga, clock = new_fpga(freq_hz=10_000_000, gate_target=2000)
    fpga.start()
    fpga.write(F.REG_GATE_TARGET, 4000)
    assert not fpga.start()
    assert fpga.start_ignored_while_busy == 1
    clock.advance(0.01)
    fpga.poll()
    # 启动时锁存的目标闸门仍然是 2000，测量中改 GATE_TARGET 不影响本次
    assert fpga.result_gate == 2005
    assert fpga.target_gate == 4000


def test_result_overwrite_counts_as_lost() -> None:
    fpga, clock = new_fpga(freq_hz=10_000_000)
    fpga.start()
    clock.advance(0.01)
    fpga.poll()
    assert fpga.lost_count == 0
    fpga.start()  # 旧结果没读走 → 新结果覆盖
    clock.advance(0.01)
    fpga.poll()
    assert fpga.lost_count == 1
    assert fpga.result_seq == 1
    assert fpga.read(F.REG_STATUS) & P.STATUS_LOST


def test_irq_status_w1c() -> None:
    fpga, clock = new_fpga(freq_hz=10_000_000)
    fpga.start()
    clock.advance(0.01)
    fpga.poll()
    assert fpga.read(F.REG_IRQ_STATUS) == F.IRQ_PENDING | F.IRQ_RESULT_READ
    # 写 b0|b1：清中断挂起 + 结果已读走
    fpga.write(F.REG_IRQ_STATUS, F.IRQ_PENDING | F.IRQ_RESULT_READ)
    assert fpga.read(F.REG_IRQ_STATUS) == 0
    assert not fpga.result_valid
    # 再来一次并故意不读走 → 覆盖时 lostCount++
    fpga.start()
    clock.advance(0.01)
    fpga.poll()
    fpga.start()
    clock.advance(0.01)
    fpga.poll()
    assert fpga.lost_count == 1
    assert fpga.read(F.REG_IRQ_STATUS) == F.IRQ_PENDING | F.IRQ_RESULT_READ | F.IRQ_LOST_CLEAR
    fpga.write(F.REG_IRQ_STATUS, F.IRQ_LOST_CLEAR)
    assert fpga.lost_count == 0


def test_clear_is_pended_while_busy() -> None:
    fpga, clock = new_fpga(freq_hz=10_000_000)
    fpga.start()
    fpga.write(F.REG_CTRL, F.CTRL_IRQ_ENABLE | F.CTRL_CLEAR)
    assert fpga.busy
    assert fpga.clear_req
    clock.advance(0.01)
    fpga.poll()
    assert not fpga.busy
    assert not fpga.clear_req
    # clear 生效：结果区与标志复位、seq 归零
    assert not fpga.result_valid
    assert (fpga.result_gate, fpga.result_edge, fpga.result_phase, fpga.seq) == (0, 0, 0, 0)


def test_gate_target_byte_strobe() -> None:
    fpga, _clock = new_fpga()
    fpga.write(F.REG_GATE_TARGET, 0x0000ABCD)
    # 只写低两字节，高两字节保持
    fpga.write(F.REG_GATE_TARGET, 0x00001234, strb=0x3)
    assert fpga.target_gate == 0x00001234
    fpga.write(F.REG_GATE_TARGET, 0xFFFF0000, strb=0xC)
    assert fpga.target_gate == 0x0FFF1234 & F.GATE_TARGET_MAX


def test_ctrl_read_back() -> None:
    fpga, _clock = new_fpga()
    fpga.write(F.REG_CTRL, F.CTRL_IRQ_ENABLE)
    assert fpga.read(F.REG_CTRL) == F.CTRL_IRQ_ENABLE
    fpga.write(F.REG_CTRL, 0)
    assert fpga.read(F.REG_CTRL) == 0


def test_source_ready_follows_sig_ctrl_without_cdc() -> None:
    """SIG_CTRL 与输入选择同域：写完即可用，没有跨时钟域送达延迟。"""
    fpga, clock = new_fpga()
    fpga.write(F.REG_SIG_CTRL, P.SIGST_SELECT | P.SIGST_ENABLE)
    clock.advance(0.01)
    fpga.poll()  # 输入选择跟随（与实际 RTL 同域，只差一拍及可配的写服务延迟）
    assert fpga.source_ready  # 已使能且 MMCM 已锁定
    assert fpga.read(F.REG_SIG_STATUS) & P.SIGST_READY == P.SIGST_READY

    # 只选片内源、关掉使能 → 不可用（判据本身不变）
    fpga.write(F.REG_SIG_CTRL, P.SIGST_SELECT)
    clock.advance(0.01)
    fpga.poll()
    assert not fpga.source_ready
    assert fpga.read(F.REG_SIG_STATUS) & P.SIGST_READY == 0


def test_external_source_ready_always() -> None:
    fpga, clock = new_fpga()
    fpga.write(F.REG_SIG_CTRL, 0)  # 选外部
    clock.advance(0.01)
    fpga.poll()  # 输入选择跟随
    assert fpga.source_ready  # 选外部恒可用


def test_measurement_never_completes_without_source() -> None:
    """片内源未使能 → 两路恒低 → 测量永不完成（上位机必须能超时退出）。"""
    fpga, clock = new_fpga(sig_enable=False)
    fpga.start()
    clock.advance(60.0)
    fpga.poll()
    assert fpga.busy
    assert not fpga.result_valid


# ------------------------------------------------- 假 MMCM / DRP 寄存器镜像（方案 A）


def test_drp_mirror_writes_need_load() -> None:
    """写镜像不生效，LOAD（0x28）写 1 才整批生效（`docs/ps-protocol.md` 6.1/6.3）。

    取代旧 `test_drp_load_saddr_selects_static_or_dynamic`：方案 A 没有 SADDR 的
    「静态/动态配置」二选一，LOAD 的 bit0 是唯一的触发位。
    """
    fpga, clock = new_fpga(freq_hz=10_000_000)
    assert (fpga.int_freq_hz, fpga.div) == (10_000_000, 100)

    words = S.drp_words(S.solve_sig(25_000_000, 45_000))
    for off, value in words.as_pairs()[:-1]:  # 最后一项是 LOAD，先不写
        fpga.write(DRP_BASE + off, value)
    assert fpga.drp[S.DRP_OFF_CLKOUT0] == words.clkout0  # 镜像本身写进去了
    assert (fpga.int_freq_hz, fpga.div) == (10_000_000, 100)  # 但工作点没变

    # LOAD 的 bit0 = 0 → 不触发重配置
    fpga.write(DRP_BASE + S.DRP_OFF_LOAD, 0x00)
    assert (fpga.int_freq_hz, fpga.div) == (10_000_000, 100)
    assert not fpga.mmcm_reconfiguring

    # LOAD = 1 → 整批生效；重配置期间两路输出停止、MMCM 未锁定
    fpga.write(DRP_BASE + S.DRP_OFF_LOAD, S.DRP_LOAD_TRIGGER)
    assert fpga.mmcm_reconfiguring
    assert not fpga.mmcm_locked
    assert fpga.current_signal() == (False, 25_000_000, 45_000)
    assert drp_status(fpga) & S.DRP_STATUS_LOCKED == 0
    assert drp_status(fpga) & S.DRP_STATUS_BUSY
    assert fpga.read(F.REG_SIG_STATUS) & P.SIGST_READY == 0

    clock.advance(0.01)
    fpga.poll()
    assert not fpga.mmcm_reconfiguring
    assert fpga.mmcm_locked
    assert drp_status(fpga) == S.DRP_STATUS_LOCKED
    assert (fpga.int_freq_hz, fpga.div, fpga.int_phase_milli_deg) == (25_000_000, 40, 45_000)
    assert fpga.read(F.REG_SIG_STATUS) & (P.SIGST_LOCKED | P.SIGST_READY) == (P.SIGST_LOCKED | P.SIGST_READY)


def test_drp_id_status_and_window() -> None:
    """DRP 段的 ID/STATUS、8 个镜像窗口与窗口外的读写（取代旧的「0x27C 恒 0」断言）。"""
    fpga, clock = new_fpga()
    assert fpga.read(DRP_BASE + S.DRP_OFF_ID) == S.DRP_ID_VALUE == 0x44504D03
    assert fpga.read(DRP_BASE + S.DRP_OFF_STATUS) == S.DRP_STATUS_LOCKED
    assert set(fpga.drp) == set(DRP_MIRROR_OFFSETS)
    # 窗口内未定义的偏移与窗口外都读 0；窗口内未定义偏移的写被忽略
    assert fpga.read(DRP_BASE + 0x30) == 0
    assert fpga.read(F.DRP_FLAT_LIMIT) == 0
    before = dict(fpga.drp)
    fpga.write(DRP_BASE + 0x30, 0xDEAD)
    fpga.write(F.DRP_FLAT_LIMIT, 0xDEAD)
    assert fpga.drp == before

    # 重配置中：locked 落 0、busy 置起；重配置结束恢复
    fpga.configure_sig(10_000_000, 0)
    assert fpga.mmcm_reconfiguring
    assert drp_status(fpga) == S.DRP_STATUS_BUSY
    clock.advance(0.01)
    fpga.poll()
    assert drp_status(fpga) == S.DRP_STATUS_LOCKED
    assert (fpga.int_freq_hz, fpga.div) == (10_000_000, 100)


def test_drp_load_while_busy_is_counted() -> None:
    """测量进行中触发 LOAD 会被计数（协议第 6 节：重配置前必须先等 STATUS.busy = 0）。"""
    fpga, _clock = new_fpga(freq_hz=10_000_000)
    assert fpga.drp_write_while_busy == 0
    fpga.start()
    fpga.write(DRP_BASE + S.DRP_OFF_LOAD, S.DRP_LOAD_TRIGGER)
    fpga.write(DRP_BASE + S.DRP_OFF_LOAD, S.DRP_LOAD_TRIGGER)
    assert fpga.drp_write_while_busy == 2

    idle, _clock2 = new_fpga(freq_hz=10_000_000)
    idle.write(DRP_BASE + S.DRP_OFF_LOAD, S.DRP_LOAD_TRIGGER)
    assert idle.drp_write_while_busy == 0


def test_phase_model_static_ignores_phase_word() -> None:
    """`--phase-model static`：复现 PHASE_DUTY_CONFIG=false 的旧 bitstream（相位字段未接线）。"""
    fpga, _clock = new_fpga(phase_model="static")
    assert fpga.phase_model == "static"
    for phase in (0, 22_500, 45_000, 135_000):
        fpga.configure_sig(10_000_000, phase)
        assert fpga.int_phase_milli_deg == 90_000
    # static 模型把 CLKOUT1 的相位字段按 0 写（等价旧 bitstream：写相位不生效），输出恒 90°
    assert fpga.drp[S.DRP_OFF_CLKOUT1] & 0xE000 == 0  # PHASE_MUX
    assert fpga.drp[S.DRP_OFF_CLKOUT1_2] & 0x3F == 0  # DELAY_TIME
    assert fpga.phase_quantum_milli_deg() is None


def test_phase_model_controllable_quantizes() -> None:
    fpga, _clock = new_fpga(phase_model="controllable")
    assert fpga.phase_model == "controllable"
    fpga.configure_sig(10_000_000, 22_500)
    assert fpga.int_phase_milli_deg == 22_500  # D=100 → 网格 0.45°，22.5° 是整步
    assert fpga.phase_field == 6  # DELAY_TIME = 相位码 50 >> 3
    assert fpga.phase_quantum_milli_deg() == 450
    fpga.configure_sig(10_000_000, 100_000)
    assert fpga.int_phase_milli_deg == 99_900  # 100/0.45 = 222.2 → 222 步 = 99.9°
    assert fpga.phase_field == 27
    assert fpga.phase_quantum_milli_deg(125) == 360  # 显式给 D：网格 45/125


def test_phase_model_auto_follows_semantics() -> None:
    """`phase_model="auto"`（默认）跟随语义结论：相位不可控时退化成 static。"""
    fpga, _clock = new_fpga(semantics=S.DrpSemantics(phase_controllable=False, source="test:不可控"))
    assert fpga.phase_model == "static"
    assert fpga.phase_quantum_milli_deg() is None
    fpga.configure_sig(10_000_000, 45_000)
    assert fpga.int_phase_milli_deg == 90_000
    assert fpga.drp[S.DRP_OFF_CLKOUT1_2] & 0x3F == 0

    controllable, _clock2 = new_fpga(semantics=S.DrpSemantics(phase_controllable=True, source="test:可控"))
    assert controllable.phase_model == "controllable"
    assert controllable.phase_quantum_milli_deg(125) == 360


def test_measurement_uses_active_source_phase() -> None:
    fpga, clock = new_fpga(phase_model="controllable", freq_hz=10_000_000, phase_milli_deg=45_000)
    fpga.start()
    clock.advance(0.01)
    fpga.poll()
    calc = M.meas_calc(fpga.result_gate, fpga.result_edge, fpga.result_phase, fpga.slow_hz, fpga.dsp_hz)
    assert calc.freq_hz == 10_000_000
    # 45° @10MHz 先被 DSP 网格量化：5.625 → RNE → 6 个 DSP 周期 = 48.0°
    assert calc.phase_milli_deg == 48_000


# --------------------------------------------------------------- PS 固件逻辑


@dataclass
class PsEnv:
    """旧 `SimPsTests.setUp` 的等价物：PL 模型 + PS 固件逻辑 + 服务端。"""

    fpga: F.SimFpga
    clock: F.FakeClock
    ps: SimPs
    server: SimServer

    def request(self, cmd: int, payload: bytes = b"") -> P.Frame:
        """喂一帧请求，断言有响应，返回解码后的响应帧（原 `_request`）。"""
        out = self.server.feed(P.encode_frame(cmd, payload))
        assert out, f"命令 0x{cmd:02X} 没有得到响应"
        return P.decode_frame(out[0])


@pytest.fixture
def env() -> PsEnv:
    clock = F.FakeClock(2000.0)
    fpga = F.SimFpga(clock=clock, phase_model="controllable", freq_hz=25_000_000, phase_milli_deg=90_000)
    clock.advance(0.001)
    fpga.poll()
    ps = SimPs(fpga, options=ServerOptions())
    return PsEnv(fpga=fpga, clock=clock, ps=ps, server=SimServer(ps))


def test_ping_and_identify(env: PsEnv) -> None:
    frame = env.request(P.Cmd.PING)
    assert frame.cmd == P.Cmd.PING.response
    assert frame.payload == b""
    info = P.decode_identify(env.request(P.Cmd.IDENTIFY).payload)
    assert info.version_reg == 0x44504D02
    assert info.caps == 0x00030104
    assert info.dsp_clk_hz == 450_000_000
    assert info.slow_clk_hz == 50_000_000
    assert info.has_signal_source and info.supports_stream


def test_get_status_fields(env: PsEnv) -> None:
    st = P.decode_status(env.request(P.Cmd.GET_STATUS).payload)
    assert st.gate_target == 2000
    assert st.sig_select and st.sig_enable and st.source_ready
    assert st.seq == 0


def test_measurement_flow(env: PsEnv) -> None:
    # GET_MEAS 在无结果时回 ERR_BUSY
    assert env.request(P.Cmd.GET_MEAS).cmd == P.Cmd.ERR_BUSY
    # GET_RESULT 用 valid=0 表达「还没有结果」
    result = P.decode_result(env.request(P.Cmd.GET_RESULT).payload)
    assert not result.valid
    env.request(P.Cmd.START)
    env.clock.advance(0.01)
    env.fpga.poll()
    meas = P.decode_meas(env.request(P.Cmd.GET_MEAS).payload)
    assert meas.freq_milli_hz == 25_000_000  # 字段 = 整数 Hz
    # 90° @25MHz = 4.5 个 DSP 周期（450MHz）→ RNE（半数取偶）→ 4 个 = 80.0°
    assert meas.phase_milli_deg == 80_000
    # 结果读走后 resultValid 被清（协议第 4 节：W1C 的 resultValid 位）
    assert not env.fpga.result_valid
    # 再测一次，改用 GET_RESULT 读原始计数
    env.request(P.Cmd.START)
    env.clock.advance(0.01)
    env.fpga.poll()
    result = P.decode_result(env.request(P.Cmd.GET_RESULT).payload)
    assert result.valid
    assert (result.gate_cnt, result.edge_cnt, result.phase_cnt) == (2002, 1001, 4004)


def test_set_gate_mode_source(env: PsEnv) -> None:
    assert P.decode_set_gate(env.request(P.Cmd.SET_GATE, P.encode_set_gate(4000)).payload) == 4000
    assert env.fpga.target_gate == 4000
    assert P.decode_set_mode(env.request(P.Cmd.SET_MODE, P.encode_set_mode(1)).payload) == 1
    assert env.ps.mode == P.MODE_CONTINUOUS
    sig_status = P.decode_sig_status_u32(env.request(P.Cmd.SET_SOURCE, P.encode_set_source(0)).payload)
    assert not sig_status & P.SIGST_SELECT
    assert not sig_status & P.SIGST_ENABLE


def test_error_responses(env: PsEnv) -> None:
    # 模式/源越界
    assert env.request(P.Cmd.SET_MODE, b"\x05").cmd == P.Cmd.ERR_PARAM
    assert env.request(P.Cmd.SET_SOURCE, b"\x09").cmd == P.Cmd.ERR_PARAM
    # 字长不符
    assert env.request(P.Cmd.SET_GATE, b"\x00\x01").cmd == P.Cmd.ERR_PARAM
    # 闸门超 28 位
    assert env.request(P.Cmd.SET_GATE, P.encode_set_gate(1 << 29)).cmd == P.Cmd.ERR_PARAM
    # STREAM enable 越界
    assert env.request(P.Cmd.STREAM, b"\x07\x00\x00").cmd == P.Cmd.ERR_PARAM
    # 未知命令
    assert env.request(0x40).cmd == P.Cmd.ERR_PARAM


def test_set_sig_and_get_sig(env: PsEnv) -> None:
    info = P.decode_sig_info(env.request(P.Cmd.SET_SIG, P.encode_set_sig(10_000_000, 45_000)).payload)
    assert info.freq_hz == 10_000_000
    assert info.phase_milli_deg == 45_000
    assert info.vco_hz == 1_000_000_000
    assert info.div == 100
    assert info.phase_field == 12  # DELAY_TIME = 相位码 100 >> 3（方案 A 起不再是恒 0 的历史字段）
    echo = P.decode_sig_info(env.request(P.Cmd.GET_SIG).payload)
    assert echo == info
    # 相位真的编码进了 CLKOUT1 的两个镜像（旧方案的「毫度相位字」寄存器已删除）
    fpga = env.fpga
    assert (fpga.drp[S.DRP_OFF_CLKOUT1] >> 13) & 0x7 == 4  # PHASE_MUX
    assert fpga.drp[S.DRP_OFF_CLKOUT1_2] & 0x3F == 12  # DELAY_TIME
    assert S.mmcm_decode_phase_mdeg(fpga.drp[S.DRP_OFF_CLKOUT1], fpga.drp[S.DRP_OFF_CLKOUT1_2], fpga.div) == 45_000


def test_set_sig_while_busy_is_err_busy(env: PsEnv) -> None:
    env.request(P.Cmd.START)
    assert env.fpga.busy
    assert env.request(P.Cmd.SET_SIG, P.encode_set_sig(10_000_000, 0)).cmd == P.Cmd.ERR_BUSY


def test_continuous_mode_restarts_after_read(env: PsEnv) -> None:
    env.request(P.Cmd.SET_MODE, P.encode_set_mode(P.MODE_CONTINUOUS))
    env.request(P.Cmd.START)
    env.clock.advance(0.01)
    env.fpga.poll()
    env.request(P.Cmd.GET_MEAS)
    # 结果读走后立即重启下一次测量
    assert env.fpga.busy
    env.clock.advance(0.01)
    env.fpga.poll()
    assert env.fpga.result_seq == 1


def test_clear_and_reset_counters(env: PsEnv) -> None:
    env.request(P.Cmd.START)
    env.clock.advance(0.01)
    env.fpga.poll()
    env.request(P.Cmd.CLEAR)
    assert not env.fpga.result_valid
    assert env.fpga.seq == 0
    env.request(P.Cmd.RESET_COUNTERS)
    assert env.fpga.lost_count == 0


def test_stream_notifications(env: PsEnv) -> None:
    assert P.decode_stream(env.request(P.Cmd.STREAM, P.encode_stream(1, 10)).payload) == (1, 10)
    env.request(P.Cmd.START)
    env.clock.advance(0.01)
    env.fpga.poll()
    notifications = env.ps.notifications(now=env.clock() + 0.05)
    stream_data = [P.decode_frame(raw) for raw in notifications]
    assert any(frame.cmd == P.Cmd.STREAM_DATA for frame in stream_data)
    payload = next(frame for frame in stream_data if frame.cmd == P.Cmd.STREAM_DATA)
    data = P.decode_stream_data(payload.payload)
    assert data.lost_delta == 0
    assert data.meas.freq_milli_hz == 25_000_000
    # 流上报也会把结果读走
    assert not env.fpga.result_valid


def test_stream_event_on_lost_result(env: PsEnv) -> None:
    env.request(P.Cmd.STREAM, P.encode_stream(1, 10))
    # 制造一次丢失：不读走结果就再启动
    env.request(P.Cmd.START)
    env.clock.advance(0.01)
    env.fpga.poll()
    env.request(P.Cmd.SET_MODE, P.encode_set_mode(P.MODE_SINGLE))
    env.request(P.Cmd.START)
    env.clock.advance(0.01)
    env.fpga.poll()
    assert env.fpga.lost_count == 1
    notifications = [P.decode_frame(raw) for raw in env.ps.notifications(now=env.clock() + 0.05)]
    events = [P.decode_event(frame.payload) for frame in notifications if frame.cmd == P.Cmd.EVENT]
    assert any(event.code == 1 for event in events)


def test_notification_frames_are_ignored(env: PsEnv) -> None:
    # PC → PS 方向不会出现通知帧；收到就当噪声忽略
    assert env.ps.handle(P.Frame(cmd=P.Cmd.STREAM_DATA, payload=b"\x00" * 26)) is None
    assert env.ps.handle(P.Frame(cmd=P.Cmd.EVENT, payload=b"\x00" * 5)) is None
