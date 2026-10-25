package DigitalPhaseMeter

import chisel3._
import chisel3.util.experimental.InlineInstanceAllowDedup

/** 测量输入选择：在外部引脚输入与片内信号源之间切换，并给片内源加使能门控
  */
class SignalInputMux extends RawModule {
    val io = IO(new Bundle {
        // 外部输入（引脚送来，异步）
        val extSig = Input(new PhaseMeterSignalPayload)
        // 片内信号源输出（MMCM 的两路同频、相位差可调的时钟）
        val intSig = Input(new PhaseMeterSignalPayload)
        // 片内源使能：0 时两路片内源恒低
        val enable = Input(Bool())
        // 输入选择：0 = 外部输入，1 = 片内信号源
        val selectInternal = Input(Bool())
        // 送给控制器、再进测量核心的信号（对测量域是异步输入）
        val outSig = Output(new PhaseMeterSignalPayload)
    })

    // 使能只门控片内源：外部通路始终可用，enable 无效时选的仍是外部输入
    io.outSig := Mux(io.selectInternal, Mux(io.enable, io.intSig, 0.U.asTypeOf(io.intSig)), io.extSig)
}

object SignalInputMux {
    def apply(
        enable: Bool,
        selectInternal: Bool,
        extSig: PhaseMeterSignalPayload,
        intSig: PhaseMeterSignalPayload
    ): PhaseMeterSignalPayload = {
        val signalInputMux = Module(new SignalInputMux with InlineInstanceAllowDedup)
        signalInputMux.io.enable := enable
        signalInputMux.io.selectInternal := selectInternal
        signalInputMux.io.extSig := extSig
        signalInputMux.io.intSig := intSig
        signalInputMux.io.outSig
    }
}

object EmitSignalInputMux extends Main { emitSystemVerilog(new SignalInputMux) }
