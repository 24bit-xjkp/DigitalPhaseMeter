package DigitalPhaseMeter

import chisel3._

/** 单 bit 电平的跨时钟域握手
  *
  * 用「电平翻转 + 同步链」表达跨域事件：发送方翻转电平，接收方用同步链采到翻转沿即得到 一个目的域脉冲。位宽为 1，因此不需要额外的数据通路。
  */
object CdcToggleHandShake {
    def receive(sig: Bool): Bool = { EdgeDetector(sig, EdgeType.Both) }

    class Sender(name: String) {
        private val sig = RegInit(false.B).suggestName(name)
        def send() = sig := !sig
    }
    object Sender {
        import scala.language.implicitConversions
        implicit def toBool(self: Sender): Bool = self.sig
    }

    def send(sig: Bool) = {
        val toggleHandShaker = new Sender(s"${sig.name}HandShaker")
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
    def apply(sig: Bool, n: Int = 3): Bool = MyShiftRegister(sig, n)
}
