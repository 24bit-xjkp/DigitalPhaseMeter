package DigitalPhaseMeter

import scala.language.adhocExtensions

import chisel3.*
import chisel3.choice.*

/** 数字相位计顶层端口
  *
  * 平台分支（`DigitalPhaseMeterTopImplS/A`）与最外层 `DigitalPhaseMeterTop` 共用这一份定义： 分支用 `FixedIORawModule`、最外层用
  * `FlatIO`，保证两者的端口逐字段一致。
  */
class DigitalPhaseMeterTopIO extends Bundle:
    val clock = Input(Clock())
    val resetn = Input(AsyncReset())
    // 高速测量时钟
    val dspClock = Input(Clock())
    // 外部被测信号输入（异步）
    val extSig = Input(PhaseMeterSignalPayload())
    // 片内信号源：PL 内 MMCM 产生的两路同频、相位差可调的时钟（异步于测量时钟域）
    val intSig = Input(PhaseMeterSignalPayload())
    // 片内信号源 MMCM 的锁定指示（异步电平）
    val srcLocked = Input(Bool())
    // 控制/状态与结果寄存器从口（AXI4-Lite，固定 32 位地址 / 32 位数据）
    val s_axi_lite = Axi4LiteSlavePorts()
    // 测量完成中断，高有效电平
    val interrupt = Output(Bool())

/** 数字相位计顶层实现（平台分支）
  *
  * 把 PS 侧的 AXI4-Lite 从口、控制/结果寄存器堆、输入选择、测量控制器与中断连成一个 Vivado IP：
  *
  * {{{
  *   s_axi_lite ──► Axi4LiteSlave ──► ControlRegs ──► Controller ──► PhaseMeter（dspClock 域）
  *                                        ▲              ▲
  *                                        │              │ extSig_sig1/sig2（异步被测信号）
  *                                        │         SignalInputMux（主时钟域，门控 + 二选一）
  *                                        │              ▲        ▲
  *                                        └──────────────┘        │
  *                                          结果（4 个只读寄存器）  │
  *                                     intSig_sig1/intSig_sig2（MMCM 两路输出）  extSig_sig1/sig2（外部引脚）
  * }}}
  *
  * 数据通道就是寄存器本身（GPIO 式）：一次测量的结果只有 4 个字，`ControlRegs` 直接把最新结果锁存进 只读寄存器，PS 收到中断后读走即可，因此 IP 内部没有 BRAM，也只有一个 AXI4-Lite 从口。
  *
  * AXI4-Lite 从口固定为 32 位地址 / 32 位数据，不开放配置。寄存器堆只按 `addr[5:2]` 译码，因此 `Axi4LiteSlave` 只把低 6 位地址交给下游（`addrUsedWidth =
  * 6`）；数据通路是 32 位寄存器，写数据保持全宽（`dataUsedWidth = 32`）。
  *
  * 信号输入有两条来源，由 `SIG_CTRL` 在 PL 内切换（使能门控与二选一都在主时钟域，见 `SignalInputMux`）：
  *
  *   - **外部输入**：`extSig_sig1` / `extSig_sig2` 两个引脚（本仓库默认约束到 W18 / W19，LVCMOS33），异步信号由 测量核心的采样链同步；
  *   - **片内信号源**：PL 内 MMCM（`dpm_mmcm_src`，动态可重配置）以主时钟为参考产生**两路同频、相位差可调**的 时钟，频率与相位差都由 MMCM 的配置寄存器通过 DRP
  *     设定；两路时钟对测量域同样是异步输入，与外部引脚 共用同一条采样链。
  *
  * 时钟与复位：`clock` 是主时钟（低速域，PS 的 FCLK_CLK0，50MHz），`resetn` 是 PS 送来的低有效 异步复位。**每个时钟域各例化一个
  * `ResetSynchronizer`**（异步置位、与各自时钟同步释放）：主时钟域的 `reset` 与 `clock` 同步，作为本模块的 `ImplicitReset` 交给 `ControlRegs` /
  * `Axi4LiteSlave` / `Controller`；`dspReset` 与 `dspClock` 同步，由 `Controller` 交给高速域的
  * `PhaseMeter`。复位释放必须与**消费它的时钟**同域：两个时钟都源自 MMCM、 相位关系不固定，拿主时钟域的复位去释放高速域的寄存器会在释放沿上违反建立/保持时间。
  *
  * 平台分支：`cfg.asyncReset = false` 时复位是同步 `Bool`（FPGA，模块名后缀 `S`），`true` 时是 `AsyncReset`（ASIC，后缀 `A`）。最外层
  * `DigitalPhaseMeterTop` 用 `ModuleChoice` 在两者之间选择， 生成 SV 里对应 `` `ifdef targets$Platform$FPGA `` /
  * `` `ifdef targets$Platform$ASIC `` 条件分支。
  *
  * @param cfg
  *   全局配置：复位风格（`asyncReset`）与三个计数器位宽；模块名带 `cfg.suffix`
  */
class DigitalPhaseMeterTopImpl(val cfg: Config)
    extends FixedIORawModule(DigitalPhaseMeterTopIO())
    with ImplicitClock
    with ImplicitReset:
    given Config = cfg
    // AXI4-Lite 端口固定 32 位地址 / 32 位数据；寄存器堆只按 addr[5:2] 译码，
    // 因此从口只把低 6 位地址交给下游，写数据保持全宽（数据通路是 32 位寄存器）
    private val axiAddrUsedWidth = 6
    private val axiDataUsedWidth = 32
    private val reset = ResetSynchronizer(io.clock, io.resetn)
    override protected def implicitClock: Clock = io.clock
    override protected def implicitReset: Reset = reset
    override def desiredName: String = s"DigitalPhaseMeterTopImpl${cfg.suffix}"

    private val controller = Module(Controller())
    controller.dspClock := io.dspClock

    private val controlRegs = Module(ControlRegs(axiAddrUsedWidth, axiDataUsedWidth))
    // MMCM 的 locked 异步于主时钟，同步后再进状态寄存器
    controlRegs.io.srcLocked := io.srcLocked

    // 输入选择在主时钟域完成（门控 + 二选一），输出作为异步被测信号进控制器；
    // 使能与选择位取自同一域的寄存器堆，因此写寄存器与切换同拍生效，不需要跨时钟域
    controller.io.sig := SignalInputMux(controlRegs.io.sigEnable, controlRegs.io.srcSelect, io.extSig, io.intSig)

    // 寄存器堆只按 `addr[5:2]` 译码，地址高位不参与任何逻辑，因此 AXI 从口只把低 6 位地址交给下游
    // （命令总线本身就是 6 位地址，不再零扩展）；数据通路是 32 位寄存器，写数据保持全宽。
    // 行为与全宽锁存完全一致，只是不再产生「未读寄存器位被 trim」的 Synth 8-3936 告警。
    private val axiLiteSlave = Module(Axi4LiteSlave(axiAddrUsedWidth, axiDataUsedWidth))

    // AXI4-Lite 从口 ↔ 寄存器堆
    io.s_axi_lite :<>= axiLiteSlave.io.axi
    controlRegs.io.wr :<>= axiLiteSlave.io.wr
    controlRegs.io.rd :<>= axiLiteSlave.io.rd
    axiLiteSlave.io.rdData := controlRegs.io.rdData

    // 寄存器堆 ↔ 控制器（测量请求/结果）
    controller.io.in := controlRegs.io.request
    controlRegs.io.response := controller.io.out
    controller.io.forceStop := controlRegs.io.forceStop

    io.interrupt := controlRegs.io.interrupt

/** 数字相位计顶层（平台选择）
  *
  * 本层只做平台分支选择，不实现逻辑：`ModuleChoice` 按 `Platform` 例化 `DigitalPhaseMeterTopImplS`（FPGA，同步复位）或
  * `DigitalPhaseMeterTopImplA`（ASIC，异步复位）。 生成 SV 里两者都是 `` `ifdef targets$Platform$FPGA `` /
  * `` `ifdef targets$Platform$ASIC `` 条件分支，消费方必须定义且只定义一个宏：Verilator 用 `-Dtargets$Platform$FPGA`，xsim 用
  * `xvlog -d 'targets$Platform$FPGA'`，Vivado 由打包/综合阶段的 fileset `verilog_define` 注入。
  *
  * 三个位宽参数写入 `Config`，对两个平台分支同时生效。
  *
  * @param gateCntWidth
  *   闸门计数器宽度
  * @param edgeCntWidth
  *   边沿计数器宽度
  * @param phaseCntWidth
  *   相位计数器宽度
  */
class DigitalPhaseMeterTop(val gateCntWidth: Int = 28, val edgeCntWidth: Int = 32, val phaseCntWidth: Int = 32)
    extends RawModule:
    private def makeConfig(asyncReset: Boolean) =
        Config(
            asyncReset = asyncReset,
            gateCntWidth = gateCntWidth,
            edgeCntWidth = edgeCntWidth,
            phaseCntWidth = phaseCntWidth
        )
    private val fpgaCfg = makeConfig(asyncReset = false)
    private val asicCfg = makeConfig(asyncReset = true)
    private def fpgaImpl = DigitalPhaseMeterTopImpl(fpgaCfg)
    private def asicImpl = DigitalPhaseMeterTopImpl(asicCfg)
    private class notImpl extends FixedIOExtModule(DigitalPhaseMeterTopIO())
    private val impl =
        ModuleChoice(notImpl())(Seq(Platform.FPGA -> fpgaImpl, Platform.ASIC -> asicImpl))
    val io = FlatIO(DigitalPhaseMeterTopIO())
    io :<>= impl

object EmitDigitalPhaseMeterTop extends Main:
    protected def emit = emitSystemVerilog(DigitalPhaseMeterTop())
