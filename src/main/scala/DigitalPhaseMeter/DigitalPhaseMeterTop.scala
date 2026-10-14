package DigitalPhaseMeter

import chisel3._
import chisel3.reflect.DataMirror

/** 数字相位计顶层
  *
  * 把 PS 侧的 AXI4-Lite 从口、控制/结果寄存器堆、测量控制器与中断连成一个 Vivado IP：
  *
  * {{{
  *   s_axi_lite ──► Axi4LiteSlave ──► ControlRegs ──► Controller ──► PhaseMeter（dspClock 域）
  *                                        ▲              │
  *                                        └──────────────┘ 结果（4 个只读寄存器）
  * }}}
  *
  * 数据通道就是寄存器本身（GPIO 式）：一次测量的结果只有 4 个字，`ControlRegs` 直接把最新结果锁存进 只读寄存器，PS 收到中断后读走即可，因此 IP 内部没有 BRAM，也只有一个 AXI4-Lite 从口。
  *
  * 时钟与复位：`clock` 是主时钟（低速域，PS 的 FCLK_CLK0，50MHz），`resetn` 是 PS 送来的低有效 异步复位。顶层用 `ResetSynchronizer` 生成与 `clock` 同步的高有效复位
  * `mainReset`，主时钟域的 所有模块都在 `withClockAndReset(clock, mainReset)` 里例化；高速域（`dspClock`）的复位由 `Controller` 内部的 `SyncReset`
  * 从主复位同步得到。
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
    // 被测信号，sig1 为参考信号
    val sig1 = IO(Input(Bool()))
    val sig2 = IO(Input(Bool()))
    // 控制/状态与结果寄存器从口（AXI4-Lite）
    val s_axi_lite = IO(new Axi4LiteSlavePorts(addrWidth, dataWidth))
    // 测量完成中断，高有效电平
    val interrupt = IO(Output(Bool()))

    /** 把顶层 AXI 从口接到内部从口
      *
      * 两者使用同一套 bundle、字段方向相同：内部从口的输入取顶层输入，顶层输出取内部从口输出。 逐字段按方向连接可以避免手写几十行容易漏项的同名连接。
      *
      * @param top
      *   顶层端口
      * @param inner
      *   内部从口
      */
    private def connectTopPort(top: Record, inner: Record): Unit = {
        for ((name, innerField) <- inner.elements) {
            val topField = top.elements(name)
            DataMirror.directionOf(innerField) match {
                case ActualDirection.Input  => innerField := topField
                case ActualDirection.Output => topField := innerField
                case other                  => throw new RuntimeException(s"AXI 端口 $name 的方向未确定：$other")
            }
        }
    }

    // 主时钟域的复位：异步置位、同步释放
    private val resetSynchronizer = Module(new ResetSynchronizer(resetDepth))
    resetSynchronizer.clock := clock
    resetSynchronizer.resetn := resetn
    private val mainReset = resetSynchronizer.reset

    withClockAndReset(clock, mainReset) {
        val controller =
            Module(new Controller(gateCntWidth, edgeCntWidth, phaseCntWidth))
        controller.dspClock := dspClock
        controller.io.sig.sig1 := sig1
        controller.io.sig.sig2 := sig2

        val controlRegs = Module(
            new ControlRegs(addrWidth, dataWidth, gateCntWidth, edgeCntWidth, phaseCntWidth)
        )
        val axiLiteSlave = Module(new Axi4LiteSlave(addrWidth, dataWidth))

        // AXI4-Lite 从口 ↔ 寄存器堆
        connectTopPort(s_axi_lite, axiLiteSlave.io.axi)
        controlRegs.io.wr <> axiLiteSlave.io.wr
        controlRegs.io.rd <> axiLiteSlave.io.rd
        axiLiteSlave.io.rdData := controlRegs.io.rdData

        // 寄存器堆 ↔ 控制器
        controller.io.in.valid := controlRegs.io.request.valid
        controller.io.in.bits := controlRegs.io.request.bits
        controlRegs.io.response.valid := controller.io.out.valid
        controlRegs.io.response.bits := controller.io.out.bits

        interrupt := controlRegs.io.interrupt
    }
}

object EmitDigitalPhaseMeterTop extends Main {
    emitSystemVerilog(new DigitalPhaseMeterTop)
}
