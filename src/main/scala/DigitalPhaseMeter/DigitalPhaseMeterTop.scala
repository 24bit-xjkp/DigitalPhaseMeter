package DigitalPhaseMeter

import chisel3._
import chisel3.reflect.DataMirror

/** 数字相位计顶层
  *
  * 把 PS 侧的 AXI4-Lite 从口、控制/结果寄存器堆、输入选择、测量控制器与中断连成一个 Vivado IP：
  *
  * {{{
  *   s_axi_lite ──► Axi4LiteSlave ──► ControlRegs ──► Controller ──► PhaseMeter（dspClock 域）
  *                                        ▲              ▲
  *                                        │              │ extSig_sig1/sig2（异步被测信号）
  *                                        │         SignalInputMux（主时钟域，门控 + 二选一）
  *                                        │              ▲        ▲
  *                                        └──────────────┘        │
  *                                          结果（4 个只读寄存器）  │
  *                                     intSig_sig1/srcSig2（MMCM 两路输出）  extSig_sig1/sig2（外部引脚）
  * }}}
  *
  * 数据通道就是寄存器本身（GPIO 式）：一次测量的结果只有 4 个字，`ControlRegs` 直接把最新结果锁存进 只读寄存器，PS 收到中断后读走即可，因此 IP 内部没有 BRAM，也只有一个 AXI4-Lite 从口。
  *
  * 信号输入有两条来源，由 `SIG_CTRL` 在 PL 内切换（使能门控与二选一都在主时钟域，见 `SignalInputMux`）：
  *
  *   - **外部输入**：`extSig_sig1` / `extSig_sig2` 两个引脚（本仓库默认约束到 W18 / W19，LVCMOS33），异步信号由 测量核心的采样链同步；
  *   - **片内信号源**：PL 内 MMCM（`dpm_mmcm_src`，动态可重配置）以主时钟为参考产生**两路同频、相位差可调**的 时钟，频率与相位差都由 MMCM 的配置寄存器通过 DRP
  *     设定；两路时钟对测量域同样是异步输入，与外部引脚 共用同一条采样链。
  *
  * 时钟与复位：`clock` 是主时钟（低速域，PS 的 FCLK_CLK0，50MHz），`resetn` 是 PS 送来的低有效 异步复位。**每个时钟域各例化一个
  * `ResetSynchronizer`**（异步置位、与各自时钟同步释放）：`mainReset` 与 `clock` 同步，主时钟域的所有模块（含 `SignalInputMux`）都在
  * `withClockAndReset(clock, mainReset)` 里例化；`dspReset` 与 `dspClock` 同步，由 `Controller` 交给高速域的
  * `PhaseMeter`。复位释放必须与**消费它的时钟**同域：两个时钟都源自 MMCM、相位关系不 固定，拿主时钟域的复位去释放高速域的寄存器会在释放沿上违反建立/保持时间。
  *
  * 本模块用 RawModule：顶层必须显式声明时钟与复位，不能依赖隐式复位。
  *
  * @param addrWidth
  *   AXI 地址宽度
  * @param dataWidth
  *   AXI 数据宽度，当前只支持 32
  * @param gateCntWidth
  *   闸门计数器宽度
  * @param edgeCntWidth
  *   边沿计数器宽度
  * @param phaseCntWidth
  *   相位计数器宽度
  * @param resetDepth
  *   复位同步链级数
  */
class DigitalPhaseMeterTop(
    val addrWidth: Int = 32,
    val dataWidth: Int = 32,
    val gateCntWidth: Int = 28,
    val edgeCntWidth: Int = 32,
    val phaseCntWidth: Int = 32,
    val resetDepth: Int = 4
) extends RawModule {
    // 主时钟，低速域
    val clock = IO(Input(Clock()))
    // 异步复位输入，低有效
    val resetn = IO(Input(Bool()))
    // 高速测量时钟
    val dspClock = IO(Input(Clock()))
    // 外部被测信号输入（异步）
    val extSig = IO(Input(new PhaseMeterSignalPayload))
    // 片内信号源：PL 内 MMCM 产生的两路同频、相位差可调的时钟（异步于测量时钟域）
    val intSig = IO(Input(new PhaseMeterSignalPayload))
    // 片内信号源 MMCM 的锁定指示（异步电平）
    val srcLocked = IO(Input(Bool()))
    // 控制/状态与结果寄存器从口（AXI4-Lite）
    val s_axi_lite = IO(new Axi4LiteSlavePorts(addrWidth, dataWidth))
    // 测量完成中断，高有效电平
    val interrupt = IO(Output(Bool()))

    // 各时钟域一个复位同步器：异步置位、与**各自域的时钟**同步释放。
    // 复位释放与消费它的时钟同域，跨域释放会在释放沿上违反建立/保持时间（两个时钟相位关系不固定）
    private val mainReset = ResetSynchronizer(clock, resetn, resetDepth)

    withClockAndReset(clock, mainReset) {
        val controller = Module(new Controller(gateCntWidth, edgeCntWidth, phaseCntWidth))
        controller.dspClock := dspClock

        val controlRegs = Module(
            new ControlRegs(addrWidth, dataWidth, gateCntWidth, edgeCntWidth, phaseCntWidth)
        )
        // MMCM 的 locked 异步于主时钟，同步后再进状态寄存器
        controlRegs.io.srcLocked := srcLocked

        // 输入选择在主时钟域完成（门控 + 二选一），输出作为异步被测信号进控制器；
        // 使能与选择位取自同一域的寄存器堆，因此写寄存器与切换同拍生效，不需要跨时钟域
        controller.io.sig := SignalInputMux(controlRegs.io.sigEnable, controlRegs.io.srcSelect, extSig, intSig)

        val axiLiteSlave = Axi4LiteSlave(addrWidth, dataWidth)

        // AXI4-Lite 从口 ↔ 寄存器堆
        s_axi_lite :<>= axiLiteSlave.axi
        controlRegs.io.wr :<>= axiLiteSlave.wr
        controlRegs.io.rd :<>= axiLiteSlave.rd
        axiLiteSlave.rdData := controlRegs.io.rdData

        // 寄存器堆 ↔ 控制器（测量请求/结果）
        controller.io.in := controlRegs.io.request
        controlRegs.io.response := controller.io.out

        interrupt := controlRegs.io.interrupt
    }
}

object EmitDigitalPhaseMeterTop extends Main { emitSystemVerilog(new DigitalPhaseMeterTop) }
