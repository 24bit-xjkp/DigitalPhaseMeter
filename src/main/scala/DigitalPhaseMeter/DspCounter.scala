package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** 使能型二进制计数器（DSP48E1 累加器实现）
  *
  * 语义：`clear` 优先；`clear` 无效且 `enable` 有效时 +1；否则保持（模 `2^width`）。
  *
  * 为什么写成 `when/elsewhen` 而不是 `Mux(clear, 0, cnt + enable)`： 后者的「+enable」会被综合器推断成 DSP48E1 的 CARRYIN 输入，使能要经一级 LUT 才到
  * CARRYIN（2026-10-19 实测这条路径只剩 +0.088ns，另一次实现低到 -0.049ns）。 写成两级条件后映射成 RSTP（同步复位）+ CEP（时钟使能），使能/清零由寄存器直接进 DSP 控制脚，不再有
  * LUT 夹在中间。
  *
  * DSP48E1 在 xc7z010clg400-1（-1 速度等级）上的全寄存器累加器 FMAX 是 464.25MHz （DS187 Table 66），450MHz 用掉约 97%；是否满足以 Vivado
  * 的时序分析为准——实现后 DSP 内部路径与 CEP/RSTP 控制路径都在 2.222ns 约束下检查过，余量见 vivado/README.md。
  *
  * @param width
  *   计数器宽度，上限 48
  * @param withReset
  *   是否给计数器加复位；`PhaseMeter` 里的两个计数器都靠 `clear` 清零，传入 false
  */
class DspCounter(val width: Int = 48, val withReset: Boolean = true) extends Module {
    assert(width <= 48)
    val io = IO(new Bundle {
        val clear = Input(Bool())
        val enable = Input(Bool())
        val value = Output(UInt(width.W))
    })

    private val cnt = if (withReset) RegInit(0.U(width.W)) else Reg(UInt(width.W))
    addAttribute(cnt, """use_dsp = "yes"""")
    when(io.clear) { cnt := 0.U }.elsewhen(io.enable) { cnt := cnt + 1.U }
    io.value := cnt
}

object DspCounter {
    def apply(width: Int, clear: Bool, enable: Bool, withReset: Boolean = true): UInt = {
        val dspCounter = Module(new DspCounter(width, withReset))
        dspCounter.io.clear := clear
        dspCounter.io.enable := enable
        dspCounter.io.value
    }
}

// 独立产物按测量域实际使用的 32 位生成（PhaseMeter 的 edgeCnt/phaseCnt 宽度），
// 这样 test/DspCounter.cpp 的端口类型与真实用例一致。
object EmitDspCounter extends Main { emitSystemVerilog(new DspCounter(32)) }
