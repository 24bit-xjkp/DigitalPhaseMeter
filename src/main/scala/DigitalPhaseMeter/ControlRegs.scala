package DigitalPhaseMeter

import chisel3._
import chisel3.util._
import chisel3.experimental.prefix

/** 控制/状态与测量结果寄存器堆
  *
  * 位于 AXI4-Lite 从口与 `Controller` 之间。一次测量的结果只有 4 个字，数据量很小，因此结果不经过 BRAM， 而是像 GPIO 一样直接落在可由 PS 读回的寄存器里：软件收到中断后依次读 4
  * 个结果寄存器即可，不需要任何突发传输或片内存储。
  *
  * 寄存器映射（地址按字对齐，取地址的第 5:2 位作为寄存器索引，偏移为字节地址）：
  *
  * | 偏移   | 名称           | 属性  | 说明                                                                    |
  * |:-----|:-------------|:----|:----------------------------------------------------------------------|
  * | 0x00 | CTRL         | 混合  | b0 start（写 1 启动，只写）、b1 irqEnable（读写）、b2 clear（写 1 当拍复位结果区与标志，不清 busy） |
  * | 0x04 | STATUS       | 只读  | b0 busy、b1 resultValid、b2 lost、b3 irqPending                          |
  * | 0x08 | GATE_TARGET  | 读写  | 目标闸门计数（低 `gateCntWidth` 位有效）                                          |
  * | 0x0C | RESULT_GATE  | 只读  | 最新结果的 gateCnt（实际闸门计数）                                                 |
  * | 0x10 | RESULT_EDGE  | 只读  | 最新结果的 edgeCnt（窗口内参考信号上升沿数）                                            |
  * | 0x14 | RESULT_PHASE | 只读  | 最新结果的 phaseCnt（相位计数）                                                  |
  * | 0x18 | RESULT_SEQ   | 只读  | 最新结果的记录序号                                                             |
  * | 0x1C | LOST_COUNT   | 只读  | 结果未被读走即被覆盖的次数                                                         |
  * | 0x20 | IRQ_STATUS   | W1C | b0 irqPending、b1 resultValid（软件读走结果）、b2 lost                          |
  * | 0x24 | VERSION      | 只读  | 版本号 0x44504D02（"DPM2"，寄存器数据通道）                                        |
  * | 0x28 | CAPS         | 只读  | b[7:0] 每条结果字数、b[15:8] 结果深度（条）、b16 数据通道为寄存器、b17 有片内信号源与输入选择            |
  * | 0x2C | SIG_CTRL     | 读写  | b0 enable（片内源使能，0 时两路输出恒低）、b1 srcSelect（0 = 外部输入，1 = 片内源）             |
  * | 0x30 | SIG_STATUS   | 只读  | b0 enable、b1 srcSelect、b2 srcLocked（MMCM 已锁定）、b3 sourceReady          |
  *
  * 时序要点：
  *
  *   - 写 CTRL.start 产生**一拍**请求；`GATE_TARGET` 由 `Controller` 在启动的那一拍锁存，测量期间 `Controller` 使用锁存值， 因此软件在测量中改写 GATE_TARGET
  *     不会破坏本次测量（`in.bits.targetGate` 不必在测量期间保持稳定）。
  *   - `Controller` 的 `out.valid` 那一拍把 gateCnt/edgeCnt/phaseCnt 与记录序号一起锁存进结果寄存器，同时撤销 busy 并置起中断挂起位。
  *   - 结果寄存器只保留最新一条：`out.valid` 到来时若上一次结果还没被软件读走（`resultValid` 仍为 1），新结果会覆盖它并把 `lostCount` 加一。软件用序号跳变或 `LOST_COUNT`
  *     判断是否漏读。
  *   - 中断是电平：`irqPending && irqEnable`。软件写 `IRQ_STATUS` 的 b0 清中断挂起位，写 b1 表示结果已读走（清 resultValid）， 写 b2 清丢失计数。
  *   - 写 `CTRL.clear` **当拍生效**（不挂起）：复位 `LOST_COUNT`、记录序号与全部结果/标志，但**不改变 `busy`**——正在进行的测量继续跑完并照常锁存结果（序号从 0 重新计数）。 它与
  *     `out.valid` 同拍竞争时 clear 优先，该条结果被丢弃（`busy` 仍按测量完成撤销）。
  *   - 软件写 `IRQ_STATUS` 清标志与 `out.valid` 新结果同拍竞争时按**新结果优先**处理：结果寄存器与 `resultValid` 保持新结果，中断不会被误清。
  *   - 片内信号源使能与输入选择（`SIG_CTRL`）就在这里生效：`io.sigEnable` / `io.srcSelect`
  *     直接驱动主时钟域的组合输入选择（`SignalInputMux`），同一拍生效、不跨时钟域，因此写通道始终 `ready`；切换的效果由 `SIG_STATUS.sourceReady` 反映（选片内源要求使能有效且
  *     MMCM 已锁定）。
  *   - 配置在测量进行中同样会立即生效（切换信号源会让本次结果无效），因此应在 `STATUS.busy = 0` 时配置。
  *
  * @param addrWidth
  *   AXI 地址宽度
  * @param dataWidth
  *   数据宽度，当前只支持 32
  * @param gateCntWidth
  *   闸门计数器宽度，与 `Controller` 一致
  * @param edgeCntWidth
  *   边沿计数器宽度，与 `Controller` 一致
  * @param phaseCntWidth
  *   相位计数器宽度，与 `Controller` 一致
  */
class ControlRegs(
    val addrWidth: Int = 32,
    val dataWidth: Int = 32,
    val gateCntWidth: Int = 28,
    val edgeCntWidth: Int = 32,
    val phaseCntWidth: Int = 32
) extends Module {
    require(dataWidth == 32, "寄存器堆当前只支持 32 位数据宽度")
    // 每条测量结果占用的字数
    val recordWords = 4
    // 结果寄存器只能保留最新一条
    val resultDepth = 1

    /** 寄存器索引
      *
      * 声明顺序即寄存器索引（写译码与读 mux 都用 `.asUInt`），不要插入或改变顺序。
      */
    private object RegIndex extends ChiselEnum {
        val iCtrl, iStatus, iGateTarget, iResultGate, iResultEdge, iResultPhase, iResultSeq, iLostCnt, iIrqStatus,
            iVersion, iCaps, iSigCtrl, iSigStatus = Value
    }
    import RegIndex._
    private val indexWidth = RegIndex.getWidth
    // 版本号："DPM2"，末尾 02 表示数据经寄存器（GPIO 式）传递
    private val versionValue = Cat("DPM".map(_.U(8.W)) :+ 2.U(8.W))
    // 能力寄存器：每条结果 4 个字、结果深度 1 条、数据通道为寄存器（而非 BRAM 突发口）、
    // b17 表示带片内信号源与输入选择寄存器
    private val hasSignalSource = true.B
    private val capsValue = Cat(hasSignalSource, true.B, resultDepth.U(8.W), recordWords.U(8.W)).pad(dataWidth)

    val io = IO(new Bundle {
        // 来自 AXI4-Lite 从口的写命令
        val wr = DeqIO(new AxiLiteWritePayload(addrWidth, dataWidth))
        // 来自 AXI4-Lite 从口的读命令
        val rd = DeqIO(new AxiLiteReadPayload(addrWidth))
        // 组合读数据
        val rdData = Output(UInt(dataWidth.W))
        // 测量请求，接 Controller 的 in
        val request = ValidIO(new ControllerInPayload(gateCntWidth))
        // 测量结果，接 Controller 的 out
        val response = Flipped(ValidIO(new ControllerOutPayload(gateCntWidth, edgeCntWidth, phaseCntWidth)))
        // 测量完成中断，高有效电平
        val interrupt = Output(Bool())
        // MMCM 锁定状态（异步输入，寄存器堆内部同步后供状态位使用）
        val srcLocked = Input(Bool())
        // 片内信号源使能与输入选择（主时钟域电平），接主时钟域的 `SignalInputMux`
        val sigEnable = Output(Bool())
        val srcSelect = Output(Bool())
    })

    // ---- 寄存器 ----
    private val targetGate = RegInit(0.U(dataWidth.W))
    private val irqEnable = RegInit(false.B)
    private val irqPending = RegInit(false.B)
    private val busy = RegInit(false.B)
    private val resultValid = RegInit(false.B)
    private val lostCnt = RegInit(0.U(dataWidth.W))
    // 最新一条结果的 4 个字（记录序号在锁存时取当时的 `seq`，随后 `seq` 自增）
    private class ResultBundle extends ControllerOutPayload(gateCntWidth, edgeCntWidth, phaseCntWidth) {
        val seqCnt = UInt(dataWidth.W)
    }
    private val result = RegInit(0.U.asTypeOf(new ResultBundle))
    // 下一条结果的记录序号：与结果寄存器分开保存，锁存时先取旧值再自增（首条结果序号为 0）
    private val seq = RegInit(0.U(dataWidth.W))
    // 片内信号源与输入选择：软件侧寄存器，直接驱动主时钟域的组合输入选择
    private val sigEnable = RegInit(false.B)
    private val srcSelect = RegInit(false.B)
    // MMCM 的 locked 异步于主时钟，先同步成主时钟域的电平再进状态位
    private val srcLocked = CdcLevel(io.srcLocked)

    // ---- 写通道：解析控制寄存器的写 ----
    private val startPulse = WireDefault(false.B)
    private val clearPulse = WireDefault(false.B)
    // 写通道不挂起任何命令：配置与输入选择同在本域生效，不需要等跨时钟域送达
    io.wr.ready := true.B
    prefix("wrImpl") {
        when(io.wr.fire) {
            switch(io.wr.bits.addr(indexWidth + 1, 2)) {
                is(iCtrl.asUInt) {
                    when(io.wr.bits.strb(0)) {
                        irqEnable := io.wr.bits.data(1)
                        when(io.wr.bits.data(0)) { startPulse := true.B }
                        when(io.wr.bits.data(2)) { clearPulse := true.B }
                    }
                }
                is(iGateTarget.asUInt) {
                    targetGate := ByteStrobe.merge(targetGate, io.wr.bits.data, io.wr.bits.strb)
                }
                is(iIrqStatus.asUInt) {
                    when(io.wr.bits.strb(0)) {
                        when(io.wr.bits.data(0)) { irqPending := false.B }
                        when(io.wr.bits.data(1)) { resultValid := false.B }
                        when(io.wr.bits.data(2)) { lostCnt := 0.U }
                    }
                }
                is(iSigCtrl.asUInt) {
                    when(io.wr.bits.strb(0)) {
                        sigEnable := io.wr.bits.data(0)
                        srcSelect := io.wr.bits.data(1)
                    }
                }
            }
        }
    }

    // ---- 输入选择输出：直接驱动主时钟域的组合门控与二选一，同拍生效 ----
    io.sigEnable := sigEnable
    io.srcSelect := srcSelect

    // ---- 与 Controller 的握手：空闲时给出实时目标闸门，由 Controller 在启动拍锁存 ----
    // 锁存正确性依赖「busy ⇔ Controller 非 idle」：request.valid 被 !busy 门控，busy 只在
    // response.valid 时撤销，因此请求只会在 Controller idle 且 busy=0 的周期出现
    io.request.valid := startPulse && !busy
    io.request.bits.targetGate := targetGate.take(gateCntWidth)
    when(io.request.valid) { busy := true.B }

    // ---- 结果锁存：只保留最新一条，被覆盖的旧结果计入丢失 ----
    // 本块写在写通道之后：同一拍 IRQ_STATUS 的 W1C 与新结果竞争时新结果优先（后写优先）；
    // CTRL.clear 写在更后面，同拍清零优先
    when(io.response.valid) {
        result.waiveAllAs[ControllerOutPayload] :<= io.response.bits
        // 先保存本次的序号，再自增给下一条结果：首条结果序号为 0
        result.seqCnt := seq
        seq := seq + 1.U
        busy := false.B
        irqPending := true.B
        resultValid := true.B
        when(resultValid) { lostCnt := lostCnt + 1.U }
    }

    // ---- 清零 ----
    // 写 CTRL.clear 当拍生效（不挂起）：只复位结果区与标志，不动 busy，正在进行的测量继续跑完并照常锁存结果
    when(clearPulse) {
        lostCnt := 0.U
        seq := 0.U
        irqPending := false.B
        resultValid := false.B
        result := 0.U.asTypeOf(result)
    }

    io.interrupt := irqPending && irqEnable

    // ---- 读通道：组合读数据 ----
    // 选择外部输入时一定有信号可用（是否有边沿由外部决定）；选择片内源时要求使能有效且 MMCM 已锁定，
    // 否则两路输出恒低，测量 FSM 会一直等不到参考沿
    private val sourceReady = !srcSelect || (sigEnable && srcLocked)
    private val lostFlag = lostCnt =/= 0.U
    io.rd.ready := true.B
    io.rdData := MuxLookup(io.rd.bits.addr(indexWidth + 1, 2), 0.U)(
        Seq(
            iCtrl -> Cat(irqEnable, 0.U(1.W)).pad(dataWidth),
            iStatus -> Cat(irqPending, lostFlag, resultValid, busy).pad(dataWidth),
            iGateTarget -> targetGate.pad(dataWidth),
            iResultGate -> result.gateCnt,
            iResultEdge -> result.edgeCnt,
            iResultPhase -> result.phaseCnt,
            iResultSeq -> result.seqCnt,
            iLostCnt -> lostCnt,
            iIrqStatus -> Cat(lostFlag, resultValid, irqPending).pad(dataWidth),
            iVersion -> versionValue,
            iCaps -> capsValue,
            iSigCtrl -> Cat(srcSelect, sigEnable).pad(dataWidth),
            iSigStatus -> Cat(sourceReady, srcLocked, srcSelect, sigEnable).pad(dataWidth)
        ).map { case (i, v) => (i.asUInt, v) }
    )
}

object EmitControlRegs extends Main { emitSystemVerilog(new ControlRegs) }
