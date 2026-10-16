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
   | 0x100 + off | 片内源 DRP 段（另一个 AXI 从口，真实基地址 `0x43C1_0000`）：0x00 ID、0x04 STATUS（b0 locked）、0x08..0x24 八个 MMCM 寄存器镜像、0x28 LOAD（写 1 触发） |

   `SIG_CTRL` **同拍生效**：片内源使能门控与「外部引脚/片内源」二选一都在主时钟域完成
   （与写寄存器的寄存器堆同域），不存在跨时钟域送达延迟。写入后软件寄存器立刻更新，输入选择
   在下一拍跟随（`cfgEnable`/`cfgSelect` 是这一拍之后的实际选择），`configDelayMs` 只表示
   这一拍加上 AXI 写服务的延迟；因此写完成后 `SIG_STATUS.sourceReady` 立即反映新配置
   （判据仍是「选片内源时要求使能有效且 MMCM 已锁定」）。

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

3. **假 MMCM**（方案 A）：PL 侧只有「MMCM 的 16 位寄存器镜像 + LOAD」，频率与相位都由
   `dpm_host.sig` 编码成 MMCM 位域（`DELAY_TIME` = 整数 VCO 周期、`PHASE_MUX` = 1/8 VCO
   周期，网格 `45°/D`）。写镜像不立即生效，LOAD（写 1）才整批生效并重新锁定
   （`srcLocked` 先 0 后 1）。相位模型由 `phase_model` 决定：

   - ``controllable``（默认）：写相位字段即生效（上板已实测）；
   - ``static``：复现旧 `clk_wiz_src` bitstream（相位字段不生效，输出恒为静态 90°）。

   片内源未使能或未锁定时两路恒低 → 测量**永远不会完成**（这正是要让上位机超时退出的路径）。

时间由注入的 `clock`（默认 `time.monotonic`）驱动：模型是事件式的，调用 :meth:`SimFpga.poll`
才会推进状态（配置送达、锁定、测量完成、挂起的 clear）。测试里用 :class:`FakeClock` 精确控制。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
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

#: 片内源 DRP 控制器（`dpm_src_drp`）在模型里的**平坦偏移基地址**。
#:
#: 真实硬件上它是另一个 AXI 从口（`0x43C1_0000`），与 DPM 自己的寄存器（`0x43C0_0000`）
#: 不冲突；本模型把两个从口放在同一个平坦地址空间里（`read/write` 传入的是偏移），
#: 因此给 DRP 段加 0x100 的基偏移模拟「另一段 AXI 基地址」——与 C 侧
#: `DPM_DRP_BASE = 0x43C1_0000` 的语义一致。
DRP_FLAT_BASE = 0x100

#: DRP 段覆盖的偏移窗口（0x00..0x28，4KB 页里只用到前 0x2C 字节）。
DRP_FLAT_LIMIT = DRP_FLAT_BASE + 0x40


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
        #: 输入选择跟随 SIG_CTRL 的延迟（默认下一拍；不再有跨时钟域送达延迟）
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

        # ---- SIG_CTRL：主时钟域的使能与选择寄存器（与输入选择同域，无跨域送达延迟） ----
        self.sig_enable = bool(sig_enable)
        self.src_select = bool(source == P.SOURCE_INTERNAL)
        # 输入选择实际用的位：写入后下一拍（或过了 config_delay_s）跟随软件寄存器
        self.cfg_enable = self.sig_enable
        self.cfg_select = self.src_select
        self._sig_ctrl_dirty = False
        self._sig_ctrl_apply_at: float | None = None

        # ---- 假 MMCM（方案 A：寄存器镜像 + LOAD） ----
        #: 相位模型：controllable（写 PHASE_MUX/DELAY_TIME 即生效，默认）/
        #: static（复现旧 bitstream：写相位字段不生效，相位恒为静态 90°）/ auto（跟随 JSON）。
        self._phase_model = phase_model
        self._reload_guard = False
        self.static_phase_milli_deg = int(self.semantics.phase_static_milli_deg)
        #: MMCM 寄存器镜像：偏移（`S.DRP_OFF_*`）→ 16 位值。
        self.drp: dict[int, int] = {}
        self.mmcm_locked = True
        self.mmcm_reconfiguring = False
        self._locked_at: float = float("-inf")
        self._reconfig_done_at: float = float("-inf")
        self.vco_hz = 0
        self.mult = 0
        self.divclk = S.DIVCLK_DEFAULT
        self.div = 0
        self.phase_field = 0
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

        # 初始配置直接写入（输入选择在下一拍跟随，与 RTL 同域语义一致）
        self._write_sig_ctrl(self.src_select, self.sig_enable)
        # 用初始工作点配置假 MMCM（等价于上电后 PS 立刻 SET_SIG）
        self.configure_sig(int(freq_hz), int(phase_milli_deg), lock_immediately=True)
        self.poll()

    # ------------------------------------------------------------ 时间推进

    def now(self) -> float:
        """当前模型时间（秒）。"""
        return float(self.clock())

    def poll(self, now: float | None = None) -> None:
        """推进时间相关状态：输入选择跟随 SIG_CTRL、MMCM 锁定、测量完成、挂起的 clear。"""
        t = self.now() if now is None else float(now)

        # 1) SIG_CTRL 与输入选择同域：写入后输入选择跟随（默认下一拍，可配延迟）
        if self._sig_ctrl_dirty and self._sig_ctrl_apply_at is not None and t >= self._sig_ctrl_apply_at:
            self.cfg_enable = self.sig_enable
            self.cfg_select = self.src_select
            self._sig_ctrl_dirty = False
            self._sig_ctrl_apply_at = None

        # 2) MMCM：重配置结束（输出恢复）→ 锁定
        if self.mmcm_reconfiguring and t >= self._reconfig_done_at:
            self.mmcm_reconfiguring = False
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
        """AXI 写通道是否可接受新写（对齐 RTL 的 `io.wr.ready`：配置不再跨域，恒为真）。"""
        return True

    def read(self, offset: int) -> int:
        """读寄存器（组合读数据，对齐 RTL 的 `rdData`）。"""
        self.poll()
        off = int(offset) & 0xFFF
        if DRP_FLAT_BASE <= off < DRP_FLAT_LIMIT:
            return self._read_drp(off - DRP_FLAT_BASE)
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
        if DRP_FLAT_BASE <= off < DRP_FLAT_LIMIT:
            self._write_drp(off - DRP_FLAT_BASE, data, strb)
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
        # 软件寄存器同拍更新；输入选择在下一拍（或 config_delay_s 之后）跟随
        self.src_select = bool(src_select)
        self.sig_enable = bool(sig_enable)
        self._sig_ctrl_dirty = True
        self._sig_ctrl_apply_at = self.now() + self.config_delay_s

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
        """当前选择的输入在配置上是否可用（选片内源要求使能有效且 MMCM 已锁定，选外部恒为 1）。"""
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
    # 方案 A：PL 侧的 `dpm_src_drp` 只是 **MMCM 的 16 位寄存器镜像 + LOAD**，
    # 频率与相位都在上位机/PS 里编码好（见 `dpm_host.sig.mmcm_encode_clkout`）：
    #
    #   - 0x08/0x0C  CLKFBOUT     ClkReg1/ClkReg2（MULT）
    #   - 0x10/0x14  CLKOUT0      ClkReg1/ClkReg2（clk_out1：D + 相位 0）
    #   - 0x18/0x1C  CLKOUT1      ClkReg1/ClkReg2（clk_out2：D + PHASE_MUX/DELAY_TIME）
    #   - 0x20       DIVCLK       0x1041（DIVCLK_DIVIDE = 1）
    #   - 0x24       POWER        0xFFFF（使能相位 mux/delay）
    #   - 0x28       LOAD         写 1 触发一次完整重配置（W1S）
    #   - 0x00/0x04  ID / STATUS  ID = 0x44504D03，STATUS bit0 = locked、bit1/bit2 = busy
    #
    # 写寄存器本身不立即生效（对齐 DRP 语义），要等 LOAD。

    def _read_drp(self, off: int) -> int:
        if off == S.DRP_OFF_ID:
            return S.DRP_ID_VALUE
        if off == S.DRP_OFF_STATUS:
            value = S.DRP_STATUS_LOCKED if self.mmcm_locked else 0
            if self.mmcm_reconfiguring:
                value |= S.DRP_STATUS_BUSY
            return value
        if off in self.drp:
            return self.drp[off] & U16_MASK
        return 0

    def _write_drp(self, off: int, data: int, strb: int) -> None:
        del strb  # 8 个镜像都只用低 16 位，整字写
        if off == S.DRP_OFF_LOAD:
            if data & S.DRP_LOAD_TRIGGER:
                if self.busy:
                    # 协议第 6 节：重配置期间两路输出停止，必须先等 STATUS.busy = 0
                    self.drp_write_while_busy += 1
                self._reload_mmcm()
            return
        if off in (S.DRP_OFF_CLKFBOUT, S.DRP_OFF_CLKFBOUT2, S.DRP_OFF_CLKOUT0,
                   S.DRP_OFF_CLKOUT0_2, S.DRP_OFF_CLKOUT1, S.DRP_OFF_CLKOUT1_2,
                   S.DRP_OFF_DIVCLK, S.DRP_OFF_POWER):
            self.drp[off] = int(data) & U16_MASK
        # 窗口外的写被忽略（与 RTL 的页内译码一致）

    @property
    def phase_model(self) -> str:
        """相位模型：``controllable``（写相位字段即生效）/ ``static``（复现旧 bitstream）。"""
        if self._phase_model != "auto":
            return self._phase_model
        return "controllable" if self.semantics.phase_controllable else "static"

    def _phase_grid(self, div: int) -> Fraction | None:
        """精确网格：45°/D（分数，D=80 时是 562.5 毫度）。"""
        if self.phase_model == "static" or int(div) <= 0:
            return None
        return S.mmcm_phase_grid_milli_deg(int(div))

    def phase_quantum_milli_deg(self, div: int | None = None) -> int | None:
        """当前模型下相位可实现的最小步长（1e-3 度）；不可控时为 ``None``。

        硬件：MMCM 把相位量化到 1/8 VCO 周期（125ps @ VCO=1000MHz），折算到输出即
        `45° / D`（D=40 → 1.125°，D=125 → 0.36°）。
        """
        d = int(div if div is not None else self.div)
        grid = self._phase_grid(d)
        return None if grid is None else S.round_half_up_int(grid.numerator, grid.denominator)

    def _quantize_phase(self, phase_milli_deg: int, div: int) -> int:
        """把请求相位量化到硬件可实现网格（45/D 度），与 C/Python 编码同一套整数规则。"""
        if self._phase_grid(div) is None:
            return int(self.semantics.phase_static_milli_deg)
        code = S.mmcm_phase_code(int(phase_milli_deg), int(div))
        return S.mmcm_phase_code_to_mdeg(code, int(div))

    def _reload_mmcm(self) -> None:
        """LOAD 后按寄存器镜像重算工作点并重新锁定（输出会短暂停止）。"""
        clkfbout = self.drp.get(S.DRP_OFF_CLKFBOUT, 0)
        clkout0 = self.drp.get(S.DRP_OFF_CLKOUT0, 0)
        clkout1 = self.drp.get(S.DRP_OFF_CLKOUT1, 0)
        clkout1_2 = self.drp.get(S.DRP_OFF_CLKOUT1_2, 0)
        divclk_word = self.drp.get(S.DRP_OFF_DIVCLK, 0)

        mult = S.mmcm_decode_divide(clkfbout)
        div = S.mmcm_decode_divide(clkout0)
        divclk = S.mmcm_decode_divclk(divclk_word)
        if mult == 0 or div == 0 or divclk == 0:
            return
        self.mult = mult
        self.divclk = divclk
        self.div = div
        self.vco_hz = (self.slow_hz * mult) // divclk
        self.int_freq_hz = S.round_half_up_int(self.vco_hz, div)
        # 相位：PHASE_MUX（ClkReg1）+ DELAY_TIME（ClkReg2）→ 毫度；static 模型下写不生效
        if self.phase_model == "controllable":
            self.int_phase_milli_deg = S.mmcm_decode_phase_mdeg(clkout1, clkout1_2, div)
        else:
            self.int_phase_milli_deg = int(self.semantics.phase_static_milli_deg)
        self.phase_field = (clkout1_2 & 0x3F)  # MMCM 的 DELAY_TIME 字段
        self.mmcm_locked = False
        self.mmcm_reconfiguring = True
        # 重配置期间两路输出停止（先恢复输出，再等锁定）
        self._reconfig_done_at = self.now() + self.config_delay_s
        self._locked_at = self.now() + self.config_delay_s + self.mmcm_lock_s

    def configure_sig(self, freq_hz: int, phase_milli_deg: int, *, lock_immediately: bool = False) -> S.SigSolution:
        """等价于一次 `SET_SIG`：求解 → 写 8 个寄存器镜像 + LOAD。

        相位模型为 ``static`` 时**不改相位**（复现旧 bitstream：写相位字段不生效，
        输出保持静态 90°）。
        """
        if self._reload_guard:
            raise RuntimeError("configure_sig 递归调用（内部错误）")
        sol = S.solve_sig(int(freq_hz), int(phase_milli_deg), self.slow_hz, self.semantics)
        if not sol.ok:
            return sol
        if self.phase_model == "static":
            # static 模型：CLKOUT1 的相位字段按 0 写，输出相位由静态配置（90°）决定
            zero = replace(sol, phase_actual_milli_deg=0, phase_code=0, phase_field=0)
            pairs = S.drp_words(zero, self.semantics).as_pairs()
        else:
            pairs = S.drp_words(sol, self.semantics).as_pairs()
        self._reload_guard = True
        try:
            for off, value in pairs:
                self._write_drp(off, value, 0xF)
        finally:
            self._reload_guard = False
        if lock_immediately:
            self.mmcm_locked = True
            self.mmcm_reconfiguring = False
            self._locked_at = float("-inf")
            self._reconfig_done_at = float("-inf")
        return sol

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
    "DRP_FLAT_BASE",
    "DRP_FLAT_LIMIT",
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
