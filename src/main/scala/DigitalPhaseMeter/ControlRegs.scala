package DigitalPhaseMeter

import chisel3._
import chisel3.util._

/** 控制/状态与测量结果寄存器堆
  *
  * 位于 AXI4-Lite 从口与 `Controller` 之间。一次测量的结果只有 4 个字，数据量很小，因此结果不经过 BRAM， 而是像 GPIO 一样直接落在可由 PS 读回的寄存器里：软件收到中断后依次读 4
  * 个结果寄存器即可，不需要任何突发传输或片内存储。
  *
  * 寄存器映射（地址按字对齐，取地址的第 5:2 位作为寄存器索引，偏移为字节地址）：
  *
  * | 偏移   | 名称           | 属性  | 说明                                                           |
  * |:-----|:-------------|:----|:-------------------------------------------------------------|
  * | 0x00 | CTRL         | 混合  | b0 start（写 1 启动，只写）、b1 irqEnable（读写）、b2 clear（写 1 复位结果区与标志）  |
  * | 0x04 | STATUS       | 只读  | b0 busy、b1 resultValid、b2 lost、b3 irqPending                 |
  * | 0x08 | GATE_TARGET  | 读写  | 目标闸门计数（低 `gateCntWidth` 位有效）                                 |
  * | 0x0C | RESULT_GATE  | 只读  | 最新结果的 gateCnt（实际闸门计数）                                        |
  * | 0x10 | RESULT_EDGE  | 只读  | 最新结果的 edgeCnt（窗口内参考信号上升沿数）                                   |
  * | 0x14 | RESULT_PHASE | 只读  | 最新结果的 phaseCnt（相位计数）                                         |
  * | 0x18 | RESULT_SEQ   | 只读  | 最新结果的记录序号                                                    |
  * | 0x1C | LOST_COUNT   | 只读  | 结果未被读走即被覆盖的次数                                                |
  * | 0x20 | IRQ_STATUS   | W1C | b0 irqPending、b1 resultValid（软件读走结果）、b2 lost                 |
  * | 0x24 | VERSION      | 只读  | 版本号 0x44504D02（"DPM2"，寄存器数据通道）                               |
  * | 0x28 | CAPS         | 只读  | b[7:0] 每条结果字数、b[15:8] 结果深度（条）、b16 数据通道为寄存器、b17 有片内信号源与输入选择   |
  * | 0x2C | SIG_CTRL     | 读写  | b0 enable（片内源使能，0 时两路输出恒低）、b1 srcSelect（0 = 外部输入，1 = 片内源）    |
  * | 0x30 | SIG_STATUS   | 只读  | b0 enable、b1 srcSelect、b2 srcLocked（MMCM 已锁定）、b3 sourceReady |
  *
  * 时序要点：
  *
  *   - 写 CTRL.start 产生**一拍**请求；请求发出的同一拍把当前的 GATE_TARGET 锁存进 `activeTargetGate`，测量期间 `in.bits.targetGate`
  *     始终取锁存值，因此软件在测量中改写 GATE_TARGET 不会破坏本次测量（对应「闸门计数在测量期间保持不变」）。
  *   - `Controller` 的 `out.valid` 那一拍把 gateCnt/edgeCnt/phaseCnt 与记录序号一起锁存进结果寄存器，同时撤销 busy 并置起中断挂起位。
  *   - 结果寄存器只保留最新一条：`out.valid` 到来时若上一次结果还没被软件读走（`resultValid` 仍为 1），新结果会覆盖它并把 `lostCount` 加一。软件用序号跳变或 `LOST_COUNT`
  *     判断是否漏读。
  *   - 中断是电平：`irqPending && irqEnable`。软件写 `IRQ_STATUS` 的 b0 清中断挂起位，写 b1 表示结果已读走（清 resultValid）， 写 b2 清丢失计数。
  *   - 同一个周期里「软件清标志」与「新结果到达」竞争时按新结果优先处理：结果寄存器与 `resultValid` 保持新结果，中断不会被误清。
  *   - 片内信号源与输入选择的配置（SIG_FREQ / SIG_PHASE / SIG_CTRL）要跨到高速测量域，走 `io.config` 的 整字握手：写这些寄存器的那一拍把新值组装成一份配置快照，交给
  *     `Controller` 里的跨时钟域总线送过去。 快照未送走前 `io.wr.ready` 保持低（AXI 写被挂起），因此软件连写两笔配置不会丢掉前一笔； `SIG_STATUS.configPending`
  *     给出同一信息，软件可以轮询它确认配置已经到达测量域。
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
    assert(dataWidth == 32, "寄存器堆当前只支持 32 位数据宽度")
    // 每条测量结果占用的字数
    val recordWords = 4
    // 结果寄存器只能保留最新一条
    val resultDepth = 1
    private val strbWidth = dataWidth / 8
    // 地址的第 5:2 位为寄存器索引，共 16 个寄存器
    private val indexWidth = 4

    /** 寄存器索引
      */
    private object RegIndex {
        val ctrl = 0
        val status = 1
        val gateTarget = 2
        val resultGate = 3
        val resultEdge = 4
        val resultPhase = 5
        val resultSeq = 6
        val lostCount = 7
        val irqStatus = 8
        val version = 9
        val caps = 10
        val sigCtrl = 11
        val sigStatus = 12
    }
    // 版本号："DPM2"，末尾 02 表示数据经寄存器（GPIO 式）传递
    private val versionValue = 0x44504d02L
    // 能力寄存器：每条结果 4 个字、结果深度 1 条、数据通道为寄存器（而非 BRAM 突发口）、
    // b17 表示带片内信号源与输入选择寄存器
    private val hasSignalSource = true.B
    private val capsValue = Cat(0.U(14.W), hasSignalSource, true.B, resultDepth.U(8.W), recordWords.U(8.W))

    val io = IO(new Bundle {
        // 来自 AXI4-Lite 从口的写命令
        val wr = Flipped(DecoupledIO(new AxiLiteWritePayload(addrWidth, dataWidth)))
        // 来自 AXI4-Lite 从口的读命令
        val rd = Flipped(DecoupledIO(new AxiLiteReadPayload(addrWidth)))
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
        // 片内信号源后分频与输入选择配置，接 Controller 的 config（整字跨到高速域）
        val config = DecoupledIO(new SignalConfigPayload)
    })

    // ---- 寄存器 ----
    private val targetGate = RegInit(0.U(dataWidth.W))
    private val activeTargetGate = RegInit(0.U(dataWidth.W))
    private val irqEnable = RegInit(false.B)
    private val irqPending = RegInit(false.B)
    private val busy = RegInit(false.B)
    private val resultValid = RegInit(false.B)
    private val lostCount = RegInit(0.U(dataWidth.W))
    private val seq = RegInit(0.U(dataWidth.W))
    private val clearReq = RegInit(false.B)
    // 最新一条结果的 4 个字
    private val resultGate = RegInit(0.U(dataWidth.W))
    private val resultEdge = RegInit(0.U(dataWidth.W))
    private val resultPhase = RegInit(0.U(dataWidth.W))
    private val resultSeq = RegInit(0.U(dataWidth.W))
    // 片内信号源与输入选择：软件侧寄存器
    private val sigEnable = RegInit(false.B)
    private val srcSelect = RegInit(false.B)
    // MMCM 的 locked 异步于主时钟，先同步成主时钟域的电平再进状态位
    private val srcLocked = CdcLevel(io.srcLocked)
    // 待送出的配置快照与「快照尚未组装」「配置尚未送到测量域」标志
    private val configSnapshot = Reg(new SignalConfigPayload)
    private val configDirty = RegInit(false.B)
    private val configPending = RegInit(false.B)

    // ---- 写通道：解析控制寄存器的写 ----
    private val startPulse = WireDefault(false.B)
    // 上一笔配置还没送走就挂起后续写命令：读通道不受影响，软件可轮询 SIG_STATUS
    io.wr.ready := !configDirty && !configPending
    when(io.wr.fire) {
        switch(io.wr.bits.addr(indexWidth + 1, 2)) {
            is(RegIndex.ctrl.U) {
                when(io.wr.bits.strb(0)) {
                    irqEnable := io.wr.bits.data(1)
                    when(io.wr.bits.data(0)) { startPulse := true.B }
                    when(io.wr.bits.data(2)) { clearReq := true.B }
                }
            }
            is(RegIndex.gateTarget.U) {
                targetGate := ByteStrobe.merge(targetGate, io.wr.bits.data, io.wr.bits.strb)
            }
            is(RegIndex.irqStatus.U) {
                when(io.wr.bits.strb(0)) {
                    when(io.wr.bits.data(0)) { irqPending := false.B }
                    when(io.wr.bits.data(1)) { resultValid := false.B }
                    when(io.wr.bits.data(2)) { lostCount := 0.U }
                }
            }
            is(RegIndex.sigCtrl.U) {
                when(io.wr.bits.strb(0)) {
                    sigEnable := io.wr.bits.data(0)
                    srcSelect := io.wr.bits.data(1)
                    configDirty := true.B
                }
            }
        }
    }

    // ---- 配置快照：在寄存器更新后的下一拍整字组装，整份一起跨到测量域 ----
    when(configDirty) {
        configSnapshot.enable := sigEnable
        configSnapshot.srcSelect := srcSelect
        configPending := true.B
        configDirty := false.B
    }
    io.config.valid := configPending
    io.config.bits := configSnapshot
    when(io.config.fire) { configPending := false.B }

    // ---- 与 Controller 的握手：空闲时给出实时目标闸门，启动的那一拍锁存 ----
    io.request.valid := startPulse && !busy
    io.request.bits.targetGate := Mux(busy, activeTargetGate, targetGate)(gateCntWidth - 1, 0)
    when(io.request.valid) {
        activeTargetGate := targetGate
        busy := true.B
    }

    // ---- 结果锁存：只保留最新一条，被覆盖的旧结果计入丢失 ----
    // 本块写在写通道之后：同一拍软件清标志与新结果竞争时，新结果优先（后写优先）
    when(io.response.valid) {
        resultGate := io.response.bits.gateCnt
        resultEdge := io.response.bits.edgeCnt
        resultPhase := io.response.bits.phaseCnt
        resultSeq := seq
        seq := seq + 1.U
        busy := false.B
        irqPending := true.B
        resultValid := true.B
        when(resultValid) { lostCount := lostCount + 1.U }
    }

    // ---- 清零：busy 期间挂起，避免打断正在进行的测量 ----
    when(clearReq && !busy) {
        clearReq := false.B
        seq := 0.U
        lostCount := 0.U
        irqPending := false.B
        resultValid := false.B
        resultGate := 0.U
        resultEdge := 0.U
        resultPhase := 0.U
        resultSeq := 0.U
    }

    io.interrupt := irqPending && irqEnable

    // ---- 读通道：组合读数据 ----
    // 选择外部输入时一定有信号可用（是否有边沿由外部决定）；选择片内源时要求使能有效且 MMCM 已锁定，
    // 否则两路输出恒低，测量 FSM 会一直等不到参考沿
    private val sourceReady = !srcSelect || (sigEnable && srcLocked)
    private val readIndex = io.rd.bits.addr(indexWidth + 1, 2)
    io.rd.ready := true.B
    io.rdData := MuxCase(
        0.U(dataWidth.W),
        Seq(
            (readIndex === RegIndex.ctrl.U) -> Cat(0.U(30.W), irqEnable, 0.U(1.W)),
            (readIndex === RegIndex.status.U) -> Cat(0.U(28.W), irqPending, lostCount =/= 0.U, resultValid, busy),
            (readIndex === RegIndex.gateTarget.U) -> targetGate,
            (readIndex === RegIndex.resultGate.U) -> resultGate,
            (readIndex === RegIndex.resultEdge.U) -> resultEdge,
            (readIndex === RegIndex.resultPhase.U) -> resultPhase,
            (readIndex === RegIndex.resultSeq.U) -> resultSeq,
            (readIndex === RegIndex.lostCount.U) -> lostCount,
            (readIndex === RegIndex.irqStatus.U) -> Cat(0.U(29.W), lostCount =/= 0.U, resultValid, irqPending),
            (readIndex === RegIndex.version.U) -> versionValue.U(dataWidth.W),
            (readIndex === RegIndex.caps.U) -> capsValue,
            (readIndex === RegIndex.sigCtrl.U) -> Cat(0.U(30.W), srcSelect, sigEnable),
            (readIndex === RegIndex.sigStatus.U) -> Cat(0.U(28.W), sourceReady, srcLocked, srcSelect, sigEnable)
        )
    )
}

object EmitControlRegs extends Main {
    emitSystemVerilog(new ControlRegs)
}
