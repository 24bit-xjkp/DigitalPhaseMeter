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
  * AXI4-Lite 端口固定为 32 位地址 / 32 位数据，使用方不能修改。下游真正消费的位宽由 `addrUsedWidth` / `dataUsedWidth` 指定：写命令与读命令总线只保留这些低位，超出的 AXI
  * 高位在从口处直接丢弃，不再零扩展回 32 位。 下游本来就忽略这些高位（例如寄存器堆只按 `addr[5:2]` 译码），因此行为与全宽锁存完全一致；这样做还能避免综合器把未读的寄存器高位 trim 掉并报
  * `[Synth 8-3936]`。
  *
  * @param addrUsedWidth
  *   下游实际使用的地址宽度：内部只锁存低这么多位地址，各类命令总线也只交回这些位（默认 32，即全宽）
  * @param dataUsedWidth
  *   下游实际使用的写数据宽度：内部只锁存低这么多位数据、字节选通只保留对应字节，写命令总线也只交回这些位；必须是 8 的整数倍（默认 32，即全宽）
  */
class Axi4LiteSlave(
    addrUsedWidth: Int = 32,
    dataUsedWidth: Int = 32
) extends Module {
    // AXI4-Lite 端口固定位宽，不开放配置
    val axiAddrWidth = 32
    val axiDataWidth = 32
    require(addrUsedWidth >= 1 && addrUsedWidth <= axiAddrWidth, "addrUsedWidth 必须为 1..32")
    require(dataUsedWidth >= 1 && dataUsedWidth <= axiDataWidth, "dataUsedWidth 必须为 1..32")
    require(dataUsedWidth % 8 == 0, "dataUsedWidth 必须是 8 的整数倍")
    private val usedStrbWidth = dataUsedWidth / 8

    // 下游位宽不同的两个实例端口宽度也不同；模块名带上位宽，避免把多个 IP 的生成文件
    // 编进同一个仿真库时（xsim 联调）同名模块互相覆盖
    override def desiredName: String = s"Axi4LiteSlaveA${addrUsedWidth}D${dataUsedWidth}"

    val io = IO(new Bundle {
        // AXI4-Lite 从口（固定 32 位地址 / 32 位数据）
        val axi = new Axi4LiteSlavePorts
        // 写命令，到寄存器堆；地址/数据/字节选通只保留下游实际使用的低位
        val wr = EnqIO(new AxiLiteWritePayload(addrUsedWidth, dataUsedWidth))
        // 读命令，到寄存器堆；地址只保留下游实际使用的低位
        val rd = EnqIO(new AxiLiteReadPayload(addrUsedWidth))
        // 读数据，由寄存器堆组合给出（AXI 侧仍是完整的 32 位）
        val rdData = Input(UInt(dataUsedWidth.W))
    })

    // ---- 写通道 ----
    // 锁存寄存器只保存下游实际使用的低位，AXI 高位在握手时就被丢弃
    private val awAddr = Reg(UInt(addrUsedWidth.W))
    private val awHeld = RegInit(false.B)
    private val wData = Reg(UInt(dataUsedWidth.W))
    private val wStrb = Reg(UInt(usedStrbWidth.W))
    private val wHeld = RegInit(false.B)
    private val bValid = RegInit(false.B)

    // 写响应未取走前不再接收新的写地址与写数据；复位期间不接受任何传输
    io.axi.awready := !awHeld && !bValid && !reset.asBool
    io.axi.wready := !wHeld && !bValid && !reset.asBool
    io.axi.bvalid := bValid
    io.axi.bresp := AxiResp.okay.asUInt

    when(io.axi.awready && io.axi.awvalid) {
        awAddr := io.axi.awaddr.take(addrUsedWidth)
        awHeld := true.B
    }
    when(io.axi.wready && io.axi.wvalid) {
        wData := io.axi.wdata.take(dataUsedWidth)
        wStrb := io.axi.wstrb.take(usedStrbWidth)
        wHeld := true.B
    }

    // 地址与数据都锁存后发出写命令；写命令保持有效直到寄存器堆接收。
    // 命令总线本身就是截断后的宽度，直接交给下游，不再零扩展
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
    private val rData = Reg(UInt(dataUsedWidth.W))

    // 地址通道握手的前提是数据寄存器能腾出来、寄存器堆能接收本次读命令，且不在复位期间
    io.axi.arready := (!rValid || io.axi.rready) && io.rd.ready && !reset.asBool
    io.rd.valid := io.axi.arready && io.axi.arvalid
    // 读地址是组合路径：只把下游实际使用的低位交给寄存器堆，AXI 高位丢弃
    io.rd.bits.addr := io.axi.araddr.take(addrUsedWidth)
    io.axi.rvalid := rValid
    io.axi.rdata := rData.pad(axiDataWidth - dataUsedWidth)
    io.axi.rresp := AxiResp.okay.asUInt

    // 先默认撤销数据有效，同一拍接收新读地址时再置位（后写优先）
    when(io.axi.rvalid && io.axi.rready) { rValid := false.B }
    when(io.rd.fire) {
        rData := io.rdData
        rValid := true.B
    }
}

object Axi4LiteSlave {
    def apply(addrUsedWidth: Int = 32, dataUsedWidth: Int = 32) = {
        val axiLiteSlave = Module(new Axi4LiteSlave(addrUsedWidth, dataUsedWidth)).suggestName("")
        axiLiteSlave.io
    }
}

object EmitAxi4LiteSlave extends Main { emitSystemVerilog(new Axi4LiteSlave) }
