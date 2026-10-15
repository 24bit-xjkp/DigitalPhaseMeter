package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** 复位同步器：异步置位、同步释放
  *
  * 顶层**每个时钟域各例化一个**：把 PS 侧送来的低有效异步复位（FCLK_RESET0_N）转换成与**该域时钟**同步的复位 （主时钟域一个、`dspClock` 域一个）。输入拉低时输出立即置位（异步），输入释放后经过 n
  * 个时钟周期才释放（同步）， 保证该时钟域内的寄存器在同一个时钟沿退出复位。
  *
  * 消费端把它当**同步复位**用（`withClockAndReset(clk, reset)` 里的 `Bool` 复位生成 `always @(posedge clk) if (reset)`）：
  * 置位沿由本模块的异步复位寄存器立即给出，释放沿则与该域时钟对齐，两者都在同一域内，没有跨域释放的建立/保持问题。
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

object EmitResetSynchronizer extends Main {
    emitSystemVerilog(new ResetSynchronizer)
}
