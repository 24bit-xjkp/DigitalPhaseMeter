package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** 单 bit 电平的跨时钟域握手
  *
  * 用「电平翻转 + 同步链」表达跨域事件：发送方翻转电平，接收方用同步链采到翻转沿即得到 一个目的域脉冲。位宽为 1，因此不需要额外的数据通路。
  */
object CdcToggleHandShake {
    def receive(sig: Bool): Bool = { EdgeDetector(sig, EdgeType.both) }

    class Sender {
        private val sig = RegInit(false.B)
        def send = sig := !sig
    }
    object Sender {
        import scala.language.implicitConversions
        implicit def toBool(self: Sender): Bool = self.sig
    }

    def send(sig: Bool) = {
        val toggleHandShaker = new Sender
        sig := toggleHandShaker
        toggleHandShaker
    }
}

/** 准静态单 bit 电平的跨时钟域同步
  *
  * 用于「软件写一次、之后长期不变」的电平（例如 MMCM 的 locked 状态）：源域电平经 n 级 ASYNC_REG 同步链进入目的域。只适用于准静态信号，不适用于会在目的域被当脉冲使用的事件。
  */
object CdcLevel {

    /** 把 `sig` 同步到当前隐式时钟域
      *
      * @param sig
      *   源时钟域的电平
      * @param n
      *   同步级数，至少 2
      * @return
      *   目的时钟域的电平
      */
    def apply(sig: Bool, n: Int = 3): Bool = { MyShiftRegister(sig, n) }
}

/** 多 bit 配置总线的跨时钟域传递（源时钟域 → 目的时钟域）
  *
  * 多 bit 配置不能像单 bit 电平那样逐位同步：各位的采样时刻可能不同，目的域会读到中间态。 这里沿用本仓库「电平翻转 + 同步链」的握手约定，并把数据整字一起带过去：
  *
  *   1. 源域把数据写进 `dataReg`，同一拍翻转 `toggle` 并置忙；
  *   2. 目的域用 `EdgeDetector`（3 级 ASYNC_REG 同步链 + 双边沿检测）采到翻转后锁存整字， 再翻转 `ack` 应答；
  *   3. 源域采到应答后才放行下一笔，因此 `dataReg` 在整笔传输期间保持不变， 连续快速配置也不会丢中间值（最多把中间态合并掉）。
  *
  * 源域用隐式 `clock` / `reset`，目的域用 `dstClock`；目的域复位由内部 `SyncReset` 从源域复位 同步得到。目的域配置寄存器按 0 复位，即配置的默认值（全 0 = 频率字
  * 0、片内源失能、选外部输入）。
  *
  * @param gen
  *   配置负载类型（不含方向），目的域以 0 作为复位值
  */
class CdcConfigBus[T <: Data](gen: T) extends Module {
    val dstClock = IO(Input(Clock()))
    val io = IO(new Bundle {
        // 源时钟域的配置写入
        val in = Flipped(DecoupledIO(gen.cloneType))
        // 目的时钟域的配置（电平），保持到下一笔配置
        val out = Output(gen.cloneType)
        // 目的时钟域的「新配置到达」脉冲，一拍有效
        val valid = Output(Bool())
    })

    // ---- 源时钟域寄存器：整笔传输期间保持不变 ----
    private val dataReg = Reg(gen.cloneType)
    private val toggleReg = RegInit(false.B)
    private val busyReg = RegInit(false.B)

    // ---- 目的时钟域：同步到请求翻转后锁存配置并应答 ----
    private val dstReset = withClock(dstClock) { SyncReset() }
    private val (configReg, validReg, ackReg) = withClockAndReset(dstClock, dstReset) {
        val config = RegInit(0.U.asTypeOf(gen.cloneType))
        val valid = RegInit(false.B)
        val ack = RegInit(false.B)
        valid := false.B
        when(EdgeDetector(toggleReg, EdgeType.both)) {
            config := dataReg
            valid := true.B
            ack := !ack
        }
        (config, valid, ack)
    }
    io.out := configReg
    io.valid := validReg

    // ---- 源时钟域握手 ----
    io.in.ready := !busyReg
    // 应答先处理、发起后置位：同一拍两者都发生时以「发起新传输」为准（Chisel 后写优先）
    when(CdcToggleHandShake.receive(ackReg)) { busyReg := false.B }
    when(io.in.fire) {
        dataReg := io.in.bits
        toggleReg := !toggleReg
        busyReg := true.B
    }
}
