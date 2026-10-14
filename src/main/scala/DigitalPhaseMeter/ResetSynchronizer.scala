package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** 复位同步器：异步置位、同步释放
  *
  * 顶层用它把 PS 侧送来的低有效异步复位（FCLK_RESET0_N）转换成与主时钟同步的复位， 供所有主时钟域模块使用：输入拉低时输出立即置位（异步），输入释放后经过 n 个时钟
  * 周期才释放（同步），保证主时钟域内的寄存器在同一个时钟沿退出复位。
  *
  * 本模块用 RawModule 显式声明时钟与复位：它本身就是复位的来源，不能依赖隐式复位。
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
    private val pipeline = withClockAndReset(clock, (!resetn).asAsyncReset) { RegInit(VecInit.fill(n)(true.B)) }
    for (i <- 0 until n) { pipeline(i) := (if (i == 0) false.B else pipeline(i - 1)) }
    reset := pipeline(n - 1)
}

object ResetSynchronizer {
    def apply(clock: Clock, resetn: Bool, n: Int = 4): Bool = {
        val resetSynchronizer = Module(new ResetSynchronizer(n))
        resetSynchronizer.clock := clock
        resetSynchronizer.resetn := resetn
        resetSynchronizer.reset
    }
}

object EmitResetSynchronizer extends Main {
    emitSystemVerilog(new ResetSynchronizer)
}
