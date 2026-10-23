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
  * 内部锁存寄存器只用**实际使用的低位**（`addrUsedWidth` / `dataUsedWidth`），交回内部命令总线前再零扩展成 完整 payload 宽度；AXI
  * 端口本身仍是完整宽度。下游本来就忽略这些高位（例如寄存器堆只按 `addr[5:2]` 译码），因此行为与全宽锁存完全一致；这样做还能避免综合器把未读的寄存器高位 trim 掉并报 `[Synth 8-3936]`。
  *
  * @param addrWidth
  *   地址宽度
  * @param dataWidth
  *   数据宽度
  * @param addrUsedWidth
  *   地址实际使用宽度：内部只锁存低这么多位地址，其余位零扩展。取 0（默认值）表示用满 `addrWidth`，即不剪裁
  * @param dataUsedWidth
  *   数据实际使用宽度：内部只锁存低这么多位数据、字节选通只保留对应字节，其余位零扩展。取 0（默认值）表示用满 `dataWidth`，即不剪裁；非 0 时必须是 8 的整数倍
  */
class Axi4LiteSlave(
    val addrWidth: Int = 32,
    val dataWidth: Int = 32,
    addrUsedWidth: Int = 0,
    dataUsedWidth: Int = 0
) extends Module {
    require(dataWidth % 8 == 0)
    // 0 是「用满全宽」的简写（Scala 2 的默认参数不能引用前面的构造参数，因此这里用 0 做哨兵值）
    require(addrUsedWidth >= 0 && addrUsedWidth <= addrWidth, "addrUsedWidth 必须为 0（全宽）或 1..addrWidth")
    require(dataUsedWidth >= 0 && dataUsedWidth <= dataWidth, "dataUsedWidth 必须为 0（全宽）或 1..dataWidth")
    require(dataUsedWidth == 0 || dataUsedWidth % 8 == 0, "dataUsedWidth 必须是 8 的整数倍（0 表示全宽）")
    // 内部锁存寄存器的实际位宽：构造参数取 0 表示用满全宽
    private val usedAddrWidth = if (addrUsedWidth == 0) addrWidth else addrUsedWidth
    private val usedDataWidth = if (dataUsedWidth == 0) dataWidth else dataUsedWidth
    private val strbWidth = dataWidth / 8
    private val usedStrbWidth = usedDataWidth / 8

    val io = IO(new Bundle {
        // AXI4-Lite 从口
        val axi = new Axi4LiteSlavePorts(addrWidth, dataWidth)
        // 写命令，到寄存器堆
        val wr = EnqIO(new AxiLiteWritePayload(addrWidth, dataWidth))
        // 读命令，到寄存器堆
        val rd = EnqIO(new AxiLiteReadPayload(addrWidth))
        // 读数据，由寄存器堆组合给出
        val rdData = Input(UInt(dataWidth.W))
    })

    // ---- 写通道 ----
    // 锁存寄存器只保存实际使用的低位（交回 payload 时由 pad 补 0），
    // 高位不再进寄存器，综合器也就没有「未读寄存器位」可 trim
    private val awAddr = Reg(UInt(usedAddrWidth.W))
    private val awHeld = RegInit(false.B)
    private val wData = Reg(UInt(usedDataWidth.W))
    private val wStrb = Reg(UInt(usedStrbWidth.W))
    private val wHeld = RegInit(false.B)
    private val bValid = RegInit(false.B)

    // 写响应未取走前不再接收新的写地址与写数据；复位期间不接受任何传输
    io.axi.awready := !awHeld && !bValid && !reset.asBool
    io.axi.wready := !wHeld && !bValid && !reset.asBool
    io.axi.bvalid := bValid
    io.axi.bresp := AxiResp.okay.asUInt

    when(io.axi.awready && io.axi.awvalid) {
        awAddr := io.axi.awaddr.take(usedAddrWidth)
        awHeld := true.B
    }
    when(io.axi.wready && io.axi.wvalid) {
        wData := io.axi.wdata.take(usedDataWidth)
        wStrb := io.axi.wstrb.take(usedStrbWidth)
        wHeld := true.B
    }

    // 地址与数据都锁存后发出写命令；写命令保持有效直到寄存器堆接收。
    // 高位零扩展回完整 payload 宽度：下游只消费低位，高位恒 0 与原全宽锁存等价
    io.wr.valid := awHeld && wHeld && !bValid
    io.wr.bits.addr := awAddr.pad(addrWidth)
    io.wr.bits.data := wData.pad(dataWidth)
    io.wr.bits.strb := wStrb.pad(strbWidth)
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
    // 读地址是组合路径：只把实际使用的低位交给寄存器堆，其余位零扩展
    io.rd.bits.addr := io.axi.araddr.take(usedAddrWidth).pad(addrWidth)
    io.axi.rvalid := rValid
    io.axi.rdata := rData
    io.axi.rresp := AxiResp.okay.asUInt

    // 先默认撤销数据有效，同一拍接收新读地址时再置位（后写优先）
    when(io.axi.rvalid && io.axi.rready) { rValid := false.B }
    when(io.rd.fire) {
        rData := io.rdData
        rValid := true.B
    }
}

object Axi4LiteSlave {
    def apply(
        addrWidth: Int = 32,
        dataWidth: Int = 32,
        addrUsedWidth: Int = 0,
        dataUsedWidth: Int = 0
    ) = {
        val axiLiteSlave = Module(new Axi4LiteSlave(addrWidth, dataWidth, addrUsedWidth, dataUsedWidth)).suggestName("")
        axiLiteSlave.io
    }
}

object EmitAxi4LiteSlave extends Main { emitSystemVerilog(new Axi4LiteSlave) }
