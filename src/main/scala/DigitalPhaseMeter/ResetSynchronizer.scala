package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** 复位同步器：异步置位、同步释放
  *
  * @param n
  *   同步释放的延迟级数
  */
class ResetSynchronizer(val n: Int = 4) extends RawModule {
    assert(n >= 2)
    val clock = IO(Input(Clock()))
    // 异步复位输入，低有效
    val resetn = IO(Input(Bool()))
    // 与 clock 同步的复位输出，高有效
    val reset = IO(Output(Bool()))

    // 复位链的寄存器用异步复位：resetn 拉低时立即清零，输出随之置位
    reset := withClockAndReset(clock, (!resetn).asAsyncReset) { MyShiftRegister(false.B, n, true.B) }
}

object ResetSynchronizer {
    def apply(clock: Clock, resetn: Bool, n: Int = 4): Bool = {
        val resetSynchronizer = Module(new ResetSynchronizer(n))
        resetSynchronizer.clock := clock
        resetSynchronizer.resetn := resetn
        resetSynchronizer.reset
    }
}

object EmitResetSynchronizer extends Main { emitSystemVerilog(new ResetSynchronizer) }
