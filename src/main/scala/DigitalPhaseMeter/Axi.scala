package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** AXI 响应编码
  */
object AxiResp extends ChiselEnum { val okay, exOkay, slvErr, decErr = Value }

/** AXI4-Lite 从口信号
  *
  * 方向以「本模块是从设备」为准：地址、数据与 valid 为输入，ready 与响应为输出。 该 bundle 同时用于顶层 IP 与内部从口模块，两侧方向约定一致。
  *
  * 本仓库的 `Axi4LiteSlave` 固定使用 32 位地址 / 32 位数据，实例化时总是传 32/32；这里的位宽参数只用于描述 AXI 协议本身。
  *
  * @param addrWidth
  *   地址宽度
  * @param dataWidth
  *   数据宽度，必须为 8 的整数倍
  */
class Axi4LiteSlavePorts extends Bundle {
    val addrWidth = 32
    val dataWidth = 32
    // 写地址通道
    val awaddr = Input(UInt(addrWidth.W))
    val awprot = Input(UInt(3.W))
    val awvalid = Input(Bool())
    val awready = Output(Bool())
    // 写数据通道
    val wdata = Input(UInt(dataWidth.W))
    val wstrb = Input(UInt((dataWidth / 8).W))
    val wvalid = Input(Bool())
    val wready = Output(Bool())
    // 写响应通道
    val bresp = Output(UInt(2.W))
    val bvalid = Output(Bool())
    val bready = Input(Bool())
    // 读地址通道
    val araddr = Input(UInt(addrWidth.W))
    val arprot = Input(UInt(3.W))
    val arvalid = Input(Bool())
    val arready = Output(Bool())
    // 读数据通道
    val rdata = Output(UInt(dataWidth.W))
    val rresp = Output(UInt(2.W))
    val rvalid = Output(Bool())
    val rready = Input(Bool())
}

/** 按字节选通合并数据
  *
  * AXI 的 wstrb 是字节粒度：选通的位置取新数据，其余保持旧值。
  */
object ByteStrobe {

    /** 按字节选通把 `data` 合并进 `old`
      *
      * @param old
      *   原数据
      * @param data
      *   新数据
      * @param strb
      *   字节选通
      * @return
      *   合并结果，宽度与 `old` 相同
      */
    def merge(old: UInt, data: UInt, strb: UInt): UInt = {
        require(old.getWidth % 8 == 0)
        val bytes = (0 until old.getWidth / 8).map(i => Mux(strb(i), data, old)(8 * i + 7, 8 * i))
        Cat(bytes.reverse)
    }
}

/** AXI4-Lite 写命令：地址、数据与字节选通
  *
  * 作为从口与寄存器堆之间的内部总线负载，写命令只持续一个周期。这里的宽度是下游实际使用的宽度， 可能小于 AXI4-Lite 端口固定的 32 位（由 `Axi4LiteSlave` 的 `addrUsedWidth` /
  * `dataUsedWidth` 截断）。
  *
  * @param addrWidth
  *   下游实际使用的地址宽度，可能小于 AXI 端口的 32 位
  * @param dataWidth
  *   下游实际使用的数据宽度，必须为 8 的整数倍，可能小于 AXI 端口的 32 位
  */
class AxiLiteWritePayload(val addrWidth: Int = 32, val dataWidth: Int = 32) extends Bundle {
    require(dataWidth % 8 == 0)
    val addr = UInt(addrWidth.W)
    val data = UInt(dataWidth.W)
    val strb = UInt((dataWidth / 8).W)
}

/** AXI4-Lite 读命令：地址
  *
  * 宽度是下游实际使用的地址宽度，可能小于 AXI4-Lite 端口固定的 32 位（由 `Axi4LiteSlave` 的 `addrUsedWidth` 截断）。
  *
  * @param addrWidth
  *   下游实际使用的地址宽度，可能小于 AXI 端口的 32 位
  */
class AxiLiteReadPayload(val addrWidth: Int = 32) extends Bundle { val addr = UInt(addrWidth.W) }
