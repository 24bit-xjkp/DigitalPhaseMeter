package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** AXI4-Lite 从口
  *
  * 把 AXI4-Lite 的五个通道转换成内部的读写命令总线，寄存器堆守在另一端：
  *
  *   - 写通道：写地址与写数据通道互相独立，各自锁存到寄存器；两者都到齐后发出一拍写命令， `io.wr.fire` 的那一拍就是寄存器更新的时刻；随后给出写响应，等主机取走响应才接收下一笔写。
  *   - 读通道：地址通道握手时就地把寄存器堆的组合读数据锁存进数据寄存器，下一拍起用 rvalid 交回主机；数据寄存器被取走的下一个周期即可接收新的读地址。
  *
  * 一次只处理一笔写事务与一笔读事务（读、写通道之间相互独立），足够满足 PS 侧寄存器访问的带宽。
  *
  * @param addrWidth
  *   地址宽度
  * @param dataWidth
  *   数据宽度
  */
class Axi4LiteSlave(val addrWidth: Int = 32, val dataWidth: Int = 32) extends Module {
    assert(dataWidth % 8 == 0)
    private val strbWidth = dataWidth / 8

    val io = IO(new Bundle {
        // AXI4-Lite 从口
        val axi = new Axi4LiteSlavePorts(addrWidth, dataWidth)
        // 写命令，到寄存器堆
        val wr = DecoupledIO(new AxiLiteWritePayload(addrWidth, dataWidth))
        // 读命令，到寄存器堆
        val rd = DecoupledIO(new AxiLiteReadPayload(addrWidth))
        // 读数据，由寄存器堆组合给出
        val rdData = Input(UInt(dataWidth.W))
    })

    // ---- 写通道 ----
    private val awAddr = Reg(UInt(addrWidth.W))
    private val awHeld = RegInit(false.B)
    private val wData = Reg(UInt(dataWidth.W))
    private val wStrb = Reg(UInt(strbWidth.W))
    private val wHeld = RegInit(false.B)
    private val bValid = RegInit(false.B)

    // 写响应未取走前不再接收新的写地址与写数据；复位期间不接受任何传输
    io.axi.awready := !awHeld && !bValid && !reset.asBool
    io.axi.wready := !wHeld && !bValid && !reset.asBool
    io.axi.bvalid := bValid
    io.axi.bresp := AxiResp.okay

    when(io.axi.awready && io.axi.awvalid) {
        awAddr := io.axi.awaddr
        awHeld := true.B
    }
    when(io.axi.wready && io.axi.wvalid) {
        wData := io.axi.wdata
        wStrb := io.axi.wstrb
        wHeld := true.B
    }

    // 地址与数据都锁存后发出写命令；写命令保持有效直到寄存器堆接收
    io.wr.valid := awHeld && wHeld && !bValid
    io.wr.bits.addr := awAddr
    io.wr.bits.data := wData
    io.wr.bits.strb := wStrb
    when(io.wr.fire) {
        awHeld := false.B
        wHeld := false.B
        bValid := true.B
    }
    when(bValid && io.axi.bready) { bValid := false.B }

    // ---- 读通道 ----
    private val rValid = RegInit(false.B)
    private val rData = Reg(UInt(dataWidth.W))

    // 地址通道握手的前提是数据寄存器能腾出来、寄存器堆能接收本次读命令，且不在复位期间
    io.axi.arready := (!rValid || io.axi.rready) && io.rd.ready && !reset.asBool
    io.rd.valid := io.axi.arready && io.axi.arvalid
    io.rd.bits.addr := io.axi.araddr
    io.axi.rvalid := rValid
    io.axi.rdata := rData
    io.axi.rresp := AxiResp.okay

    // 先默认撤销数据有效，同一拍接收新读地址时再置位（后写优先）
    when(io.axi.rvalid && io.axi.rready) { rValid := false.B }
    when(io.rd.fire) {
        rData := io.rdData
        rValid := true.B
    }
}

object EmitAxi4LiteSlave extends Main { emitSystemVerilog(new Axi4LiteSlave) }
