"""寄存器级 + 测量级 PL 模型（无硬件时验证上位机用）。

模型分三块，语义分别对齐 RTL / 协议文档：

1. **寄存器语义**（对齐 `src/main/scala/DigitalPhaseMeter/ControlRegs.scala` 与
   `ps/include/dpm_regs.h`）：

   | 偏移  | 名称          | 语义                                                       |
   | ----- | ------------- | ---------------------------------------------------------- |
   | 0x00  | CTRL          | b0 start（写 1 触发，**busy 期间被忽略**）、b1 irqEnable、b2 clear（busy 期间挂起） |
   | 0x04  | STATUS        | b0 busy、b1 resultValid、b2 lost、b3 irqPending             |
   | 0x08  | GATE_TARGET   | 低 28 位有效，按字节选通合并                                |
   | 0x0C..0x18 | RESULT_GATE/EDGE/PHASE/SEQ | 最新一条结果                     |
   | 0x1C  | LOST_COUNT    | 结果未被读走即被覆盖的次数                                  |
   | 0x20  | IRQ_STATUS    | **W1C**：b0 irqPending、b1 resultValid、b2 lostCount        |
   | 0x24  | VERSION       | `0x44504D02`                                                |
   | 0x28  | CAPS          | `0x00030104`（4 字/条、深度 1、寄存器通道、带片内源）        |
   | 0x2C  | SIG_CTRL      | b0 enable、b1 srcSelect                                     |
   | 0x30  | SIG_STATUS    | b0 enable、b1 srcSelect、b2 srcLocked、b3 sourceReady        |
   | 0x200+4n | MMCM DRP 配置字（n = 0..31），0x25C = LOAD（bit0 触发、bit1 SADDR；0x07 与 0x03 等价） |
   | 0x100    | 模型里的 clk_wiz locked 状态（真实硬件是 clk_wiz 段偏移 0x004；0x27C 实测恒 0） |
   | 0x218    | clk_out2 相位字（毫度；当前 IP 未接线，`phase_model=controllable` 时才生效） |

   `SIG_CTRL` 写入后先进入 `configPending`（配置要跨到测量域），这段时间 `SIG_STATUS.sourceReady`
   读回 0 —— 这是任务书要求的「生效前延迟」，也与 RTL 注释里「快照未送走前写通道会挂起」一致
   （RTL 里 `sourceReady` 是软件侧寄存器组合出来的，本模型更严格一点，见 README 的模型假设）。

2. **测量模型**（与 `spec/gen_golden.py` 的独立参考实现逐位一致）：

   .. code-block:: text

       gateCnt   = targetGate + ceil(T_sig / T_slow)
       edgeCnt   = (gateCnt * f_sig) // f_slow                （整数截断）
       dspCycles = RNE(T_phase / T_dsp)                       （相位先被 DSP 采样网格量化）
       phaseCnt  = dspCycles * edgeCnt

   `RNE` = 半数取偶（整数运算）；由此 `phaseCnt / edgeCnt` 恰好是整数个 DSP 周期，
   即相位量化下界。**不覆盖**：MMCM 抖动/相位噪声、真实布线延迟与边沿检测通路的固定
   延迟差、异步采样链的 CDC 亚稳态、闸门停止握手的额外低速周期（RTL 里 stop 要等
   `sig2` 下一个上升沿，容限见 dpm-measurement-model）。

3. **假 MMCM**：按实测的 DRP 语义建模（`spec/drp_semantics.json`）——低字节 = 分频比 D 本体、
   LOAD 的 bit0 触发/bit1 选择静态或动态配置、相位量化到 1/8 VCO 周期；LOAD 后重新锁定
   （`srcLocked` 先 0 后 1）。相位模型由 `phase_model` 决定：

   - ``controllable``：相位字（0x218，毫度）生效，用于上位机相位通路自测；
   - ``static``：复现实测的当前 bitstream（相位字未接线，clk_out2 相位恒为静态 90°）；
   - ``auto``（默认）：跟随 `spec/drp_semantics.json` 的 `phase_controllable` ——
     实测为 **false**，因此默认行为是「相位字写进去也不生效，clk_out2 恒为静态 90°」，
     与真实 bitstream 一致（否则端到端验证会掩盖这个限制）。

   片内源未使能或未锁定时两路恒低 → 测量**永远不会完成**（这正是要让上位机超时退出的路径）。

时间由注入的 `clock`（默认 `time.monotonic`）驱动：模型是事件式的，调用 :meth:`SimFpga.poll`
才会推进状态（配置送达、锁定、测量完成、挂起的 clear）。测试里用 :class:`FakeClock` 精确控制。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable

from . import mathx as M
from . import protocol as P
from . import sig as S

# ------------------------------------------------------------- 寄存器偏移

REG_CTRL = 0x00
REG_STATUS = 0x04
REG_GATE_TARGET = 0x08
REG_RESULT_GATE = 0x0C
REG_RESULT_EDGE = 0x10
REG_RESULT_PHASE = 0x14
REG_RESULT_SEQ = 0x18
REG_LOST_COUNT = 0x1C
REG_IRQ_STATUS = 0x20
REG_VERSION = 0x24
REG_CAPS = 0x28
REG_SIG_CTRL = 0x2C
REG_SIG_STATUS = 0x30

CTRL_START = 1 << 0
CTRL_IRQ_ENABLE = 1 << 1
CTRL_CLEAR = 1 << 2

IRQ_PENDING = 1 << 0
IRQ_RESULT_READ = 1 << 1
IRQ_LOST_CLEAR = 1 << 2

VERSION_VALUE = 0x44504D02
CAPS_VALUE = 0x00030104
GATE_TARGET_MAX = (1 << 28) - 1
GATE_CNT_MASK = (1 << 28) - 1
U32_MASK = 0xFFFFFFFF
U16_MASK = 0xFFFF

#: 本模型里 clk_wiz 的 locked 状态寄存器用的平坦偏移。
#:
#: 真实硬件上它在 clk_wiz AXI 窗口内的偏移是 **0x004**（实测），但本模型把 DPM 寄存器与
#: clk_wiz DRP 放在同一个平坦地址空间里，0x004 与 DPM 自己的 STATUS 冲突，因此模型用 0x100
#: 表示它（模拟「另一段 AXI 基地址」）。PS 侧 C 代码用的是 DPM_DRP_BASE + 0x004，不受影响。
DRP_STATUS_FLAT_OFF = 0x100

#: DRP 配置字索引 → 偏移（0x200 + 4n）。
DRP_WORD_OFFS: dict[int, int] = {n: S.drp_word_off(n) for n in range(32)}


@dataclass
class MmcmConfig:
    """MMCM 工作点（静态 S1_* 配置或 DRP 寄存器文件里的动态配置）。"""

    mult: int = 20
    divclk: int = 1
    div: int = 40
    phase_milli_deg: int = 90_000

    @property
    def value(self) -> str:
        return f"MULT={self.mult} DIVCLK={self.divclk} D={self.div} 相位={self.phase_milli_deg / 1000:.3f}°"


class FakeClock:
    """可手动推进的时钟（单元测试用）。"""

    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t


class SimFpga:
    """PL 行为的寄存器级模型（含假 MMCM 与测量模型）。"""

    def __init__(
        self,
        *,
        slow_hz: int = P.DEFAULT_SLOW_HZ,
        dsp_hz: int = P.DEFAULT_DSP_HZ,
        gate_target: int = P.DEFAULT_GATE_TARGET,
        source: int = P.SOURCE_INTERNAL,
        sig_enable: bool = True,
        freq_hz: int = P.DEFAULT_SIG_FREQ_HZ,
        phase_milli_deg: int = P.DEFAULT_SIG_PHASE_MILLI_DEG,
        meas_latency_s: float = 5e-4,
        config_delay_s: float = 2e-4,
        mmcm_lock_s: float = 5e-4,
        ext_freq_hz: int = 10_000_000,
        ext_phase_milli_deg: int = 0,
        ext_enabled: bool = True,
        semantics: S.DrpSemantics | None = None,
        phase_model: str = "auto",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.slow_hz = int(slow_hz)
        self.dsp_hz = int(dsp_hz)
        self.clock = clock
        self.meas_latency_s = float(meas_latency_s)
        self.config_delay_s = float(config_delay_s)
        self.mmcm_lock_s = float(mmcm_lock_s)
        self.semantics = semantics or S.current_semantics()

        # ---- 寄存器 ----
        self.target_gate = int(gate_target) & GATE_TARGET_MAX
        self.active_target_gate = 0
        self.irq_enable = True
        self.irq_pending = False
        self.busy = False
        self.result_valid = False
        self.lost_count = 0
        self.seq = 0  # 下一个结果的记录序号
        self.result_gate = 0
        self.result_edge = 0
        self.result_phase = 0
        self.result_seq = 0
        self.clear_req = False

        # ---- SIG_CTRL：软件侧寄存器与已送到测量域的配置快照 ----
        self.sig_enable = bool(sig_enable)
        self.src_select = bool(source == P.SOURCE_INTERNAL)
        self.cfg_enable = self.sig_enable
        self.cfg_select = self.src_select
        self.config_dirty = False
        self.config_pending = False
        self._config_deliver_at: float | None = None

        # ---- 假 MMCM ----
        #: 相位模型：controllable（相位字可用，默认，供上位机相位通路自测）/
        #: static（复现实测的当前 bitstream：相位字未接线，相位恒为静态 90°）/ auto（跟随 JSON）。
        self._phase_model = phase_model
        self._reload_guard = False
        self.static_phase_milli_deg = int(self.semantics.phase_static_milli_deg)
        self.mmcm_static = MmcmConfig(mult=20, divclk=1, div=40, phase_milli_deg=self.static_phase_milli_deg)
        self.mmcm_dynamic = MmcmConfig(mult=20, divclk=1, div=40, phase_milli_deg=self.static_phase_milli_deg)
        self.mmcm_active = "static"
        self.drp: dict[int, int] = {}
        self.mmcm_locked = True
        self.srdy = False
        self._locked_at: float = float("-inf")
        self.vco_hz = 0
        self.mult = 0
        self.divclk = S.DIVCLK_DEFAULT
        self.div = 0
        self.phase_field = 0
        self.legacy_status_reads = 0
        #: 片内源当前输出的频率与相位（由 DRP 配置推出）。
        self.int_freq_hz = int(freq_hz)
        self.int_phase_milli_deg = int(phase_milli_deg)
        #: 外部输入模拟信号（仿真选项，真实系统里由信号源决定）。
        self.ext_freq_hz = int(ext_freq_hz)
        self.ext_phase_milli_deg = int(ext_phase_milli_deg)
        self.ext_enabled = bool(ext_enabled)

        # ---- 测量 ----
        self._meas_deadline: float | None = None
        self._meas_started_at: float | None = None
        self._meas_counts: tuple[int, int, int] | None = None

        # ---- 统计/诊断 ----
        self.drp_write_while_busy = 0
        self.start_ignored_while_busy = 0
        self.results_latched = 0

        # 初始配置也走一次「写 SIG_CTRL」的延迟路径，行为更接近上电后 PS 配置
        self._write_sig_ctrl(self.src_select, self.sig_enable)
        # 用初始工作点配置假 MMCM（等价于上电后 PS 立刻 SET_SIG）
        self.configure_sig(int(freq_hz), int(phase_milli_deg), lock_immediately=True)
        self.poll()

    # ------------------------------------------------------------ 时间推进

    def now(self) -> float:
        """当前模型时间（秒）。"""
        return float(self.clock())

    def poll(self, now: float | None = None) -> None:
        """推进时间相关状态：配置送达、MMCM 锁定、测量完成、挂起的 clear。"""
        t = self.now() if now is None else float(now)

        # 1) 配置快照跨到测量域
        if self.config_pending:
            if self._config_deliver_at is not None and t >= self._config_deliver_at:
                self.cfg_enable = self.sig_enable
                self.cfg_select = self.src_select
                self.config_pending = False
                self._config_deliver_at = None
        elif self.config_dirty:
            self.config_pending = True
            self.config_dirty = False
            self._config_deliver_at = t + self.config_delay_s

        # 2) MMCM 锁定
        if not self.mmcm_locked and t >= self._locked_at:
            self.mmcm_locked = True

        # 3) 测量：信号可用时才能算完（片内源未使能/未锁定 → 永不完成）
        if self.busy:
            if self._meas_deadline is None:
                active, freq_hz, phase_mdeg = self.current_signal()
                if active and freq_hz > 0:
                    gate_cnt, edge_cnt, phase_cnt = self.measurement_model(
                        self.active_target_gate, freq_hz, phase_mdeg
                    )
                    self._meas_counts = (gate_cnt, edge_cnt, phase_cnt)
                    span = gate_cnt / self.slow_hz if self.slow_hz > 0 else 0.0
                    self._meas_deadline = (
                        self._meas_started_at if self._meas_started_at is not None else t
                    ) + span + self.meas_latency_s
            if self._meas_deadline is not None and t >= self._meas_deadline:
                gate_cnt, edge_cnt, phase_cnt = self._meas_counts
                self._commit_result(gate_cnt, edge_cnt, phase_cnt)
                self._meas_deadline = None
                self._meas_started_at = None
                self._meas_counts = None

        # 4) clear 在 busy 期间挂起，busy 结束后生效
        if self.clear_req and not self.busy:
            self._apply_clear()

    # ------------------------------------------------------------ 测量模型

    def measurement_model(self, target_gate: int, freq_hz: int, phase_milli_deg: int) -> tuple[int, int, int]:
        """按 `spec/gen_golden.py`（独立参考实现）的模型算 (gateCnt, edgeCnt, phaseCnt)。

        .. code-block:: text

            gateCnt  = targetGate + ceil(T_sig / T_slow)
            edgeCnt  = (gateCnt * f_sig) // f_slow                （整数截断）
            dspCycles = RNE(T_phase / T_dsp)                      （相位先被 DSP 网格量化）
            phaseCnt = dspCycles * edgeCnt

        与 `docs/ps-protocol.md` 第 5 节的测量语义一致：`phaseCnt` 是「每个参考周期内
        sig1 上升沿 → sig2 上升沿」的整数 DSP 周期计数之和，所以 `phaseCnt / edgeCnt`
        恰好是整数个 DSP 周期（相位量化下界 = 1 个 DSP 周期，不是缺陷）。
        这里用精确有理数实现 RNE，和 golden 的「整数 ps + RNE」逐位等价。
        """
        slow, dsp = self.slow_hz, self.dsp_hz
        if freq_hz <= 0 or slow <= 0 or dsp <= 0:
            return 0, 0, 0
        # gateCnt = targetGate + ceil(T_sig / T_slow) = targetGate + ceil(f_slow / f_sig)
        extra = -(-slow // freq_hz)
        gate_cnt = int(target_gate) + extra
        # edgeCnt = gateCnt * T_slow / T_sig 的整数截断
        edge_cnt = (gate_cnt * freq_hz) // slow
        # dspCycles = RNE(phase_deg/360 * T_sig/T_dsp) = RNE(phase_mdeg * f_dsp / (360000 * f_sig))
        if edge_cnt:
            dsp_cycles = M.round_half_even_frac(Fraction(int(phase_milli_deg) * dsp, 360_000 * freq_hz))
        else:
            dsp_cycles = 0
        phase_cnt = dsp_cycles * edge_cnt
        return gate_cnt & GATE_CNT_MASK, edge_cnt & U32_MASK, phase_cnt & U32_MASK

    def current_signal(self) -> tuple[bool, int, int]:
        """当前输入通路的 (是否有时钟, 频率 Hz, 相位 1e-3 度)。"""
        if self.cfg_select:
            active = self.cfg_enable and self.mmcm_locked
            return active, self.int_freq_hz, self.int_phase_milli_deg
        return self.ext_enabled, self.ext_freq_hz, self.ext_phase_milli_deg

    def _commit_result(self, gate_cnt: int, edge_cnt: int, phase_cnt: int) -> None:
        """结果锁存：busy 撤销、中断挂起、覆盖未读走的旧结果时 lostCount++。"""
        self.result_gate = int(gate_cnt) & U32_MASK
        self.result_edge = int(edge_cnt) & U32_MASK
        self.result_phase = int(phase_cnt) & U32_MASK
        self.result_seq = self.seq & U32_MASK
        self.seq = (self.seq + 1) & U32_MASK
        self.busy = False
        self.irq_pending = True
        self.results_latched += 1
        if self.result_valid:
            # 旧结果还没被读走 → 记为丢失
            self.lost_count = (self.lost_count + 1) & U32_MASK
        self.result_valid = True

    def _apply_clear(self) -> None:
        """`CTRL.clear` 生效：结果区与全部标志复位，seq 归零。"""
        self.clear_req = False
        self.seq = 0
        self.lost_count = 0
        self.irq_pending = False
        self.result_valid = False
        self.result_gate = 0
        self.result_edge = 0
        self.result_phase = 0
        self.result_seq = 0

    # ------------------------------------------------------------ 寄存器访问

    @property
    def wr_ready(self) -> bool:
        """AXI 写通道是否可接受新写（配置快照未送走时挂起，对齐 RTL 的 `io.wr.ready`）。"""
        return not self.config_dirty and not self.config_pending

    def read(self, offset: int) -> int:
        """读寄存器（组合读数据，对齐 RTL 的 `rdData`）。"""
        self.poll()
        off = int(offset) & 0xFFF
        if off >= S.DRP_OFF_BASE or off == DRP_STATUS_FLAT_OFF:
            return self._read_drp(off)
        if off == REG_CTRL:
            return CTRL_IRQ_ENABLE if self.irq_enable else 0
        if off == REG_STATUS:
            return self.status_word()
        if off == REG_GATE_TARGET:
            return self.target_gate & GATE_TARGET_MAX
        if off == REG_RESULT_GATE:
            return self.result_gate
        if off == REG_RESULT_EDGE:
            return self.result_edge
        if off == REG_RESULT_PHASE:
            return self.result_phase
        if off == REG_RESULT_SEQ:
            return self.result_seq
        if off == REG_LOST_COUNT:
            return self.lost_count
        if off == REG_IRQ_STATUS:
            return self.irq_status_word()
        if off == REG_VERSION:
            return VERSION_VALUE
        if off == REG_CAPS:
            return CAPS_VALUE
        if off == REG_SIG_CTRL:
            return (P.SIGST_SELECT if self.src_select else 0) | (P.SIGST_ENABLE if self.sig_enable else 0)
        if off == REG_SIG_STATUS:
            return self.sig_status_word()
        return 0

    def write(self, offset: int, data: int, strb: int = 0xF) -> None:
        """写寄存器（`strb` 为字节选通；所有写都是 32 位整字写）。"""
        self.poll()
        off = int(offset) & 0xFFF
        data = int(data) & U32_MASK
        strb = int(strb) & 0xF
        if off >= S.DRP_OFF_BASE:
            self._write_drp(off, data, strb)
            return
        if off == REG_CTRL:
            if strb & 0x1:
                self.irq_enable = bool(data & CTRL_IRQ_ENABLE)
                if data & CTRL_START:
                    self.start()
                if data & CTRL_CLEAR:
                    # busy 期间挂起，由 poll() 在空闲时生效
                    self.clear_req = True
            return
        if off == REG_GATE_TARGET:
            merged = 0
            for i in range(4):
                byte = (self.target_gate >> (8 * i)) & 0xFF
                if strb & (1 << i):
                    byte = (data >> (8 * i)) & 0xFF
                merged |= byte << (8 * i)
            self.target_gate = merged & GATE_TARGET_MAX
            return
        if off == REG_IRQ_STATUS:
            if strb & 0x1:
                if data & IRQ_PENDING:
                    self.irq_pending = False
                if data & IRQ_RESULT_READ:
                    self.result_valid = False
                if data & IRQ_LOST_CLEAR:
                    self.lost_count = 0
            return
        if off == REG_SIG_CTRL:
            if strb & 0x1:
                self._write_sig_ctrl(bool(data & P.SIGST_SELECT), bool(data & P.SIGST_ENABLE))
            return
        # 只读寄存器或未定义偏移：忽略写入

    def _write_sig_ctrl(self, src_select: bool, sig_enable: bool) -> None:
        self.src_select = bool(src_select)
        self.sig_enable = bool(sig_enable)
        self.config_dirty = True  # 下一拍组装快照 → configPending

    # ---- 状态字

    def status_word(self) -> int:
        value = 0
        if self.busy:
            value |= P.STATUS_BUSY
        if self.result_valid:
            value |= P.STATUS_RESULT_VALID
        if self.lost_count != 0:
            value |= P.STATUS_LOST
        if self.irq_pending:
            value |= P.STATUS_IRQ_PENDING
        return value

    def irq_status_word(self) -> int:
        value = 0
        if self.irq_pending:
            value |= IRQ_PENDING
        if self.result_valid:
            value |= IRQ_RESULT_READ
        if self.lost_count != 0:
            value |= IRQ_LOST_CLEAR
        return value

    def sig_status_word(self) -> int:
        """SIG_STATUS：位 0/1 是软件侧寄存器（对齐 RTL），b3 sourceReady 在配置未送达时读回 0。"""
        value = 0
        if self.sig_enable:
            value |= P.SIGST_ENABLE
        if self.src_select:
            value |= P.SIGST_SELECT
        if self.mmcm_locked:
            value |= P.SIGST_LOCKED
        if self.source_ready:
            value |= P.SIGST_READY
        return value

    @property
    def source_ready(self) -> bool:
        """当前选择的输入在配置上是否可用：配置未送达 → 0；选外部恒为 1。"""
        if self.config_pending or self.config_dirty:
            return False
        return (not self.cfg_select) or (self.cfg_enable and self.mmcm_locked)

    # ------------------------------------------------------------ 测量控制

    def start(self) -> bool:
        """写 CTRL.start：busy 期间被忽略；返回本次是否真的启动。"""
        self.poll()
        if self.busy:
            self.start_ignored_while_busy += 1
            return False
        self.active_target_gate = self.target_gate & GATE_TARGET_MAX
        self.busy = True
        self._meas_started_at = self.now()
        self._meas_counts = None
        self._meas_deadline = None
        return True

    def ack_result(self) -> None:
        """结果被读走：写 `IRQ_STATUS = irqPending | resultValid`（协议第 4 节）。"""
        self.write(REG_IRQ_STATUS, IRQ_PENDING | IRQ_RESULT_READ)

    # ------------------------------------------------------------ 假 MMCM / DRP
    #
    # 寄存器语义按实测结论（spec/drp_semantics.json）建模，与任务书最初写的
    # 「相位字段在字 31 / STATUS 在 0x27C」不同：
    #
    #   - CLKFBOUT 字（0x200）低字节 = DIVCLK，bit[15:8] = MULT；
    #   - CLKOUT0 字（0x208）低字节 = D，bit[15:8] 在真实 IP 里是小数分频 FRAC[7:0]（不是相位）；
    #   - CLKOUT1 字（0x214）低字节 = D（clk_out2 的分频），bit[15:8] 未连接；
    #   - 相位字（0x218 = 字索引 6）单位是**毫度**，但当前 IP 未接线（`phase_word_index.wired=false`）；
    #   - LOAD（0x25C）bit0 = 触发重配置，bit1 = SADDR（1 = 用动态寄存器文件，0 = 回到静态配置 S1_*）；
    #   - locked 状态在 IPIF 本地寄存器 **0x004**（bit0 = locked、bit1 = srdy），0x27C 恒为 0。

    def _read_drp(self, off: int) -> int:
        if off == DRP_STATUS_FLAT_OFF:  # 实测的 locked/srdy 状态（真实硬件 = clk_wiz 段 0x004）
            value = S.DRP_STATUS_LOCKED if self.mmcm_locked else 0
            if self.srdy:
                value |= S.DRP_STATUS_SRDY
            return value
        if off == S.DRP_STATUS_LEGACY_OFF:  # 0x27C：真实 IP 里恒 0
            self.legacy_status_reads += 1
            return 0
        index = (off - S.DRP_OFF_BASE) // 4
        if 0 <= index < 32 and off == S.drp_word_off(index):
            return self.drp.get(index, 0)
        return 0

    def _write_drp(self, off: int, data: int, strb: int) -> None:
        if off == S.DRP_LOAD_OFF:
            self.drp[S.DRP_IDX_LOAD] = data
            if data & S.DRP_LOAD_TRIGGER_BIT:
                if self.busy:
                    # 协议第 6 节：重配置期间两路输出停止，必须先等 STATUS.busy = 0
                    self.drp_write_while_busy += 1
                self._reload_mmcm(data)
            return
        index = (off - S.DRP_OFF_BASE) // 4
        if not (0 <= index < 32) or off != S.drp_word_off(index):
            return
        self.drp[index] = data
        # 写配置字本身不立即生效，要等 LOAD（对齐 PG065 的 DRP 语义）

    @property
    def phase_model(self) -> str:
        """相位模型：``controllable``（相位字可用）/ ``static``（复现实测的当前 bitstream）。"""
        if self._phase_model != "auto":
            return self._phase_model
        sem = self.semantics
        return "controllable" if (sem.phase_controllable and sem.mode != "none") else "static"

    def _phase_grid(self, div: int) -> Fraction | None:
        """精确网格：45°/D（分数，D=80 时是 562.5 毫度）。"""
        if self.phase_model == "static" or int(div) <= 0:
            return None
        return S.mmcm_phase_grid_milli_deg(int(div))

    def phase_quantum_milli_deg(self, div: int | None = None) -> int | None:
        """当前模型下相位可实现的最小步长（1e-3 度）；不可控时为 ``None``。

        实测：MMCM 把相位量化到 1/8 VCO 周期（125ps @ VCO=1000MHz），折算到输出即
        `45° / D`（D=40 → 1.125°，D=125 → 0.36°）。
        """
        d = int(div if div is not None else self.div)
        grid = self._phase_grid(d)
        return None if grid is None else S.round_half_up_int(grid.numerator, grid.denominator)

    def _quantize_phase(self, phase_milli_deg: int, div: int) -> int:
        """把请求相位量化到硬件可实现网格（45/D 度）。"""
        grid = self._phase_grid(div)
        if grid is None:
            return int(self.semantics.phase_static_milli_deg)
        steps = S.round_half_up_int(int(phase_milli_deg) * int(div), 45_000)
        return S.round_half_up_int(steps * 45_000, int(div))

    def _reload_mmcm(self, load_value: int) -> None:
        """LOAD 后应用配置并重新锁定。

        `SADDR = 0` → 回到静态配置（S1_*，BD 初值：MULT=20 / DIVCLK=1 / D=40 / 相位 90°）；
        `SADDR = 1` → 用 DRP 寄存器文件里的动态配置（分频、以及接线后的相位字）。
        """
        use_dynamic = bool(load_value & S.DRP_LOAD_SADDR_BIT)
        cfg = self.mmcm_dynamic if use_dynamic else self.mmcm_static
        self.mmcm_active = "dynamic" if use_dynamic else "static"
        self.mult = cfg.mult
        self.divclk = cfg.divclk
        self.div = cfg.div
        self.vco_hz = (self.slow_hz * cfg.mult) // cfg.divclk if cfg.mult else 0
        self.int_freq_hz = S.round_half_up_int(self.vco_hz, cfg.div) if cfg.div else 0
        self.phase_field = S.phase_field_get(self.drp.get(S.DRP_IDX_CLKOUT1, 0))
        self.int_phase_milli_deg = cfg.phase_milli_deg
        # LOAD 触发重新锁定：srcLocked 先掉再起（实测重配置约 9µs，期间两路输出停止）
        self.mmcm_locked = False
        self.srdy = False
        self._locked_at = self.now() + self.mmcm_lock_s

    def configure_sig(self, freq_hz: int, phase_milli_deg: int, *, lock_immediately: bool = False) -> S.SigSolution:
        """等价于一次 `SET_SIG`：求解 → 写 DRP 字（含相位字）→ LOAD。

        相位模型为 ``static`` 时**不改相位**（写入的相位字不生效，输出保持静态相位），
        与实测的当前 bitstream 行为一致。
        """
        if self._reload_guard:
            raise RuntimeError("configure_sig 递归调用（内部错误）")
        sol = S.solve_sig(int(freq_hz), int(phase_milli_deg), self.slow_hz, self.semantics)
        if not sol.ok:
            return sol
        phase_requested = int(phase_milli_deg)
        words = S.drp_words(sol, self.semantics)
        for off, value in words.as_pairs()[:-1]:
            self._write_drp(off, value, 0xF)
        # 独立相位字：0x20C = clk_out1 相位（0）、0x218 = clk_out2 相位（毫度）。
        # 按 docs/ps-protocol.md 6.2/6.4：即使当前 bitstream 没接线也照写（无害）。
        self._write_drp(S.drp_word_off(S.DRP_IDX_CLKOUT0_PHASE), 0, 0xF)
        self._write_drp(S.drp_word_off(S.DRP_IDX_CLKOUT1_PHASE), phase_requested & 0xFFFFFFFF, 0xF)
        self._reload_guard = True
        try:
            # 先把动态配置登记下来，再 LOAD（SADDR=1）让它生效
            self._register_dynamic_config(sol, phase_requested)
            self._write_drp(words.load_off, S.DRP_LOAD_BOTH, 0xF)
        finally:
            self._reload_guard = False
        if lock_immediately:
            self.mmcm_locked = True
            self._locked_at = float("-inf")
        return sol

    def _register_dynamic_config(self, sol: S.SigSolution, phase_requested: int) -> None:
        """把一次 SET_SIG 的结果登记成「动态配置」（供 SADDR=1 的 LOAD 使用）。"""
        if self.phase_model == "controllable":
            phase_mdeg = self._quantize_phase(phase_requested, sol.div)
        else:
            # 复现实测行为：相位字未接线，输出固定为静态相位
            phase_mdeg = int(self.semantics.phase_static_milli_deg)
        self.mmcm_dynamic = MmcmConfig(
            mult=sol.mult,
            divclk=sol.divclk,
            div=sol.div,
            phase_milli_deg=phase_mdeg,
        )

    # ------------------------------------------------------------ 诊断

    def describe(self) -> dict[str, object]:
        """当前状态快照（CLI/GUI/日志用）。"""
        self.poll()
        active, freq, phase = self.current_signal()
        return {
            "busy": self.busy,
            "resultValid": self.result_valid,
            "lost": self.lost_count != 0,
            "irqPending": self.irq_pending,
            "lostCount": self.lost_count,
            "seq": self.seq,
            "resultSeq": self.result_seq,
            "gateTarget": self.target_gate,
            "sigEnable": self.sig_enable,
            "srcSelect": self.src_select,
            "cfgEnable": self.cfg_enable,
            "cfgSelect": self.cfg_select,
            "configPending": self.config_pending,
            "sourceReady": self.source_ready,
            "srcLocked": self.mmcm_locked,
            "signalActive": active,
            "signalFreqHz": freq,
            "signalPhaseMilliDeg": phase,
            "vcoHz": self.vco_hz,
            "mult": self.mult,
            "div": self.div,
            "phaseField": self.phase_field,
        }


__all__ = [
    "SimFpga",
    "FakeClock",
    "MmcmConfig",
    "DRP_STATUS_FLAT_OFF",
    "REG_CTRL",
    "REG_STATUS",
    "REG_GATE_TARGET",
    "REG_RESULT_GATE",
    "REG_RESULT_EDGE",
    "REG_RESULT_PHASE",
    "REG_RESULT_SEQ",
    "REG_LOST_COUNT",
    "REG_IRQ_STATUS",
    "REG_VERSION",
    "REG_CAPS",
    "REG_SIG_CTRL",
    "REG_SIG_STATUS",
    "CTRL_START",
    "CTRL_IRQ_ENABLE",
    "CTRL_CLEAR",
    "IRQ_PENDING",
    "IRQ_RESULT_READ",
    "IRQ_LOST_CLEAR",
    "VERSION_VALUE",
    "CAPS_VALUE",
    "GATE_TARGET_MAX",
]
