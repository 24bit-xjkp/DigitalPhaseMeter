package DigitalPhaseMeter

import chisel3._

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
