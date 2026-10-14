package DigitalPhaseMeter

import chisel3._

/** 片内信号源使能与输入选择的配置负载
  *
  * 由寄存器堆（低速域）组装，整字经 `CdcConfigBus` 跨到测量时钟域：使能与选择位同时生效。
  */
class SignalConfigPayload extends Bundle {
    // 片内信号源使能，0 时两路输出恒低
    val enable = Bool()
    // 输入选择：0 = 外部输入，1 = 片内信号源（MMCM 的两路输出）
    val srcSelect = Bool()
}

/** 测量输入选择：在外部引脚输入与片内信号源之间切换
  *
  * 工作在高速时钟域，是测量核心之前的一个纯组合二选一：
  *
  * {{{
  *   sig1External（引脚 W18）─┐
  *                          ├─► Mux ─► sig1 ─► PhaseMeter（内部再经异步采样链同步）
  *   sig1Internal（信号源）──┘
  * }}}
  *
  * 两路候选信号都在高速时钟域内被使用：外部输入由 `PhaseMeter` 的 `EdgeDetector` 异步采样链 同步，片内信号源本来就由同一个时钟驱动；两者共用同一条采样链，因此切换不会改变测量通路的延迟。
  *
  * 切换位来自 AXI 寄存器，已随配置字跨到高速域。切换是异步生效的：在测量进行中切换会让本次 结果无效（甚至一直等不到边沿），因此软件应在 `STATUS.busy = 0` 时切换。
  */
class SignalInputMux extends Module {
    val io = IO(new Bundle {
        // 外部输入（引脚送来，异步）
        val sig1External = Input(Bool())
        val sig2External = Input(Bool())
        // 片内信号源输出
        val sig1Internal = Input(Bool())
        val sig2Internal = Input(Bool())
        // 输入选择：0 = 外部输入，1 = 片内信号源
        val selectInternal = Input(Bool())
        // 送给测量核心的信号
        val sig1 = Output(Bool())
        val sig2 = Output(Bool())
    })

    io.sig1 := Mux(io.selectInternal, io.sig1Internal, io.sig1External)
    io.sig2 := Mux(io.selectInternal, io.sig2Internal, io.sig2External)
}

object EmitSignalInputMux extends Main {
    emitSystemVerilog(new SignalInputMux)
}
