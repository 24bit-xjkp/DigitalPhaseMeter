package DigitalPhaseMeter

import chisel3._
import chisel3.reflect.DataMirror

/** 数字相位计顶层
  *
  * 把 PS 侧的 AXI4-Lite 从口、控制/结果寄存器堆、测量控制器与中断连成一个 Vivado IP：
  *
  * {{{
  *   s_axi_lite ──► Axi4LiteSlave ──► ControlRegs ──► Controller ──► PhaseMeter（dspClock 域）
  *                                        ▲              │             ▲
  *                                        │              │             │ 输入选择
  *                                        └──────────────┘      SignalInputMux
  *                                          结果（4 个只读寄存器）   ▲        ▲
  *                                                                  │        │
  *              MMCM（clk_wiz，动态可重配置）───────────────────────┘   sig1/sig2
  *                 │ 频率/相位差由 DRP 设定                              （外部引脚）
  *                 └─► 参考时钟取自主时钟 clock
  * }}}
  *
  * 数据通道就是寄存器本身（GPIO 式）：一次测量的结果只有 4 个字，`ControlRegs` 直接把最新结果锁存进 只读寄存器，PS 收到中断后读走即可，因此 IP 内部没有 BRAM，也只有一个 AXI4-Lite 从口。
  *
  * 信号输入有两条来源，由 `SIG_CTRL.srcSelect` 在 PL 内切换：
  *
  *   - **外部输入**：`sig1` / `sig2` 两个引脚（本仓库默认约束到 W18 / W19，LVCMOS33），异步信号由 测量核心的采样链同步；
  *   - **片内信号源**：PL 内 MMCM（`clk_wiz`，动态可重配置）以主时钟为参考产生**两路同频、相位差可调**的 时钟，频率由 MMCM 的 M/D 分频、相位差由每个输出的相位寄存器通过 DRP
  *     设定；两路时钟在测量时钟域里 被采样成边沿脉冲，再由 `SourceDivider` 数边沿分频出 1kHz～40MHz 的方波（分频比 N 由 `SIG_DIV` 给出）。
  *
  * 时钟与复位：`clock` 是主时钟（低速域，PS 的 FCLK_CLK0，50MHz），`resetn` 是 PS 送来的低有效 异步复位。**每个时钟域各例化一个
  * `ResetSynchronizer`**（异步置位、与各自时钟同步释放）：`mainReset` 与 `clock` 同步，主时钟域的所有模块都在 `withClockAndReset(clock, mainReset)`
  * 里例化；`dspReset` 与 `dspClock` 同步，由 `Controller` 交给高速域的 `PhaseMeter`、 `SignalInputMux` 与 `CdcConfigBus`
  * 的目的域。复位释放必须与**消费它的时钟**同域：两个时钟都源自 MMCM、相位关系不 固定，拿主时钟域的复位去释放高速域的寄存器会在释放沿上违反建立/保持时间。
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
    // 外部被测信号输入（异步），sig1 为参考信号；片内信号源在 Controller 内部与它二选一
    val sig1 = IO(Input(Bool()))
    val sig2 = IO(Input(Bool()))
    // 片内信号源：PL 内 MMCM 产生的两路同频、相位差可调的时钟（异步于测量时钟域）
    val srcSig1 = IO(Input(Bool()))
    val srcSig2 = IO(Input(Bool()))
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
        val controller =
            Module(new Controller(gateCntWidth, edgeCntWidth, phaseCntWidth))
        controller.dspClock := dspClock
        // 外部输入（引脚 W18/W19），片内信号源与输入选择在 Controller 内部
        controller.io.sig.sig1 := sig1
        controller.io.sig.sig2 := sig2
        controller.io.sourceSig1 := srcSig1
        controller.io.sourceSig2 := srcSig2

        val controlRegs = Module(
            new ControlRegs(addrWidth, dataWidth, gateCntWidth, edgeCntWidth, phaseCntWidth)
        )
        // MMCM 的 locked 异步于主时钟，同步后再进状态寄存器
        controlRegs.io.srcLocked := srcLocked
        val axiLiteSlave = Module(new Axi4LiteSlave(addrWidth, dataWidth))

        // AXI4-Lite 从口 ↔ 寄存器堆
        s_axi_lite :<>= axiLiteSlave.io.axi
        controlRegs.io.wr :<>= axiLiteSlave.io.wr
        controlRegs.io.rd :<>= axiLiteSlave.io.rd
        axiLiteSlave.io.rdData := controlRegs.io.rdData

        // 寄存器堆 ↔ 控制器
        controller.io.in := controlRegs.io.request
        controlRegs.io.response := controller.io.out
        // 片内信号源与输入选择配置：整字跨到测量域
        controller.io.config :<>= controlRegs.io.config

        interrupt := controlRegs.io.interrupt
    }
}

object EmitDigitalPhaseMeterTop extends Main {
    emitSystemVerilog(new DigitalPhaseMeterTop)
}
