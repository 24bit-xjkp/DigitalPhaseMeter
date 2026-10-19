# sim/：Vivado xsim 仿真

AXI4 接口、顶层 IP 与片内源联调的验证用 **Vivado 自带 xsim + SystemVerilog 测试激励**；测量通路
与复位同步（`Controller` / `EdgeDetector` / `ResetSynchronizer` / `MmcmDrpController`）的 Verilator
单元测试仍在 `test/` 下（`xmake test -v`）。工具用法与实测坑的完整清单见
[`../.agents/skills/dpm-vivado-ip/SKILL.md`](../.agents/skills/dpm-vivado-ip/SKILL.md)。

| 文件 | 说明 |
| --- | --- |
| `tb_digital_phase_meter_top.sv` | 顶层 IP 的测试激励：AXI4-Lite 主机 BFM、测量激励与结果寄存器校验（用理想信号源驱动 `intSig_sig1/2`） |
| `run_xsim.sh` | 一键脚本：生成 RTL → `xvlog` 编译 → `xelab` 细化 → `xsim` 运行 |
| `tb_src_handoff.sv` | 片内源 ↔ 测量通路联调：真实 `dpm_mmcm_src`（`MMCME2_ADV`）+ `MmcmDrpController` + `DigitalPhaseMeterTop`，按完整地址写 MMCM 寄存器镜像，并直接量两路输出的周期与相位差 |
| `run_handoff_tb.sh` | 联调激励的运行脚本：10 个频点 × 逐频点相位点 = 80 个点（每点判 4 条），约 17s 墙钟 |

## 运行

```bash
sim/run_xsim.sh              # 先用 sbtn 生成 RTL，再编译运行
sim/run_xsim.sh --no-gen     # 跳过 sbtn 生成（RTL 已是最新时）
sim/run_xsim.sh --gui        # 运行结束后用 xsim GUI 打开波形
sim/run_xsim.sh --clean      # 先清理 build/xsim

sim/run_handoff_tb.sh            # 默认重生成 RTL 后跑全部点
sim/run_handoff_tb.sh --no-gen   # 跳过 sbtn 生成
XILINX_VIVADO=/path/to/Vivado sim/run_xsim.sh   # 指定 Vivado 安装路径（两份脚本都认）
```

两份脚本都消费 `build/chisel/<Name>/` 下的生成产物（每个模块只有一份产物，Verilator、xsim 与综合
共用；复位同步是纯 RTL 的 `ResetSynchronizer`，产物里没有 XPM 原语），日志与波形落在 `build/xsim/`
与 `build/xsim_handoff/`（不入库）。联调脚本另需 `vivado/rtl/dpm_mmcm_src.v` 与 unisims_ver 的
`MMCME2_ADV`/`glbl`。判定成败不要只看 xsim 退出码（`$fatal` 也可能返回 0）：顶层用例看
`==== 测试通过`，联调看 `HANDOFF_SUMMARY ... fails=0` 且 `HANDOFF_MEAS` 行数 = 2×点数。

## 覆盖内容

- 复位：`resetn` 低期间所有 `ready` 为低、不接受请求；复位释放后寄存器为默认值。
- AXI4-Lite：只读寄存器、读写寄存器、写地址/写数据通道顺序颠倒、字节选通只改选中字节、只读
  寄存器写忽略、写响应与读延迟。
- 测量：`CTRL.start` 后 `busy` 置起，完成后 `resultValid`/`irqPending` 与中断拉高；`busy` 期间
  重复写 `start` 被忽略（只产生一条结果）。
- 中断：电平中断与 `irqEnable` 门控（不使能时挂起位仍置位、引脚不拉高）；`IRQ_STATUS` 写 1
  分别清中断挂起、清 `resultValid`、清丢失计数。
- 结果通道（GPIO 式寄存器）：读 4 个结果寄存器拿一条结果；结果只保留最新一条，不读走就再次测量
  会覆盖旧结果并累加 `LOST_COUNT`；`CTRL.clear` 后序号重新计数。
- 结果正确性：用测量模型（[`dpm-measurement-model`](../.agents/skills/dpm-measurement-model/SKILL.md)）
  校验 `gateCnt` / `edgeCnt` / `phaseCnt` 与 `seq`。闸门计数容限按被测信号周期给
  （`$ceil(signal_period / CLOCK_PERIOD) + 8`），因此片内源这种「周期是低速时钟周期整数倍」的信号也适用。
- 片内源与输入切换：写 `SIG_CTRL` 并读回、读 `SIG_STATUS` 四个状态位（含复位默认值）；选中片内源
  后测量跟随片内信号（仿真里用周期 640ns、相位差 160ns 的两路理想源代替 MMCM 输出），切回外部
  输入后跟随引脚信号（400ns / 120ns）——两条通路理论值不同，能区分「切换生效」与「没生效」。
- MMCM 联调（`tb_src_handoff.sv`）：软件给出的 MMCM 寄存器值真的让输出相位差等于请求值、
  频率 = VCO / D，且测量通路读数与「直接量到的输出相位差」一致（1 个 DSP 周期内）。
- **顶层 AXI 用例是寄存器堆通路的唯一守门人**：`test/` 下的 Verilator 单测直接驱动 `Controller` /
  `SignalSource` 端口，**不经过 AXI 从口与 `ControlRegs`**；动 AXI 寄存器通路必须跑
  `sim/run_xsim.sh`，并把新寄存器纳入激励（读回、字节选通、复位默认值、「配置真的改变测量结果」）。
  顶层用例用理想信号源，MMCM 寄存器本身的覆盖见 `tb_src_handoff.sv`。

## 实测陷阱

- **`$time` 显示单位**：`--timescale 1ns/1ps` 下打印的 `$time` 是皮秒量级（`[41510000]` 表示 41.51µs）。
- **非 ASCII 字符串会被破坏**：xsim 会把经 `string` 参数或字符串拼接传递的中文破坏成 `0xff`。
  静态 `$display` 字面量可以用中文，任务参数/`$sformatf` 结果（检查名、错误消息）用 ASCII。
- **timescale 必须显式给**：生成的 RTL 没有 `` `timescale`` 而激励有，`xelab` 要加 `--timescale 1ns/1ps`。
- **`filelist.f` 路径**：里面的文件名相对生成目录，`xvlog -f` 按当前工作目录解析，脚本先补成绝对路径。
- **`glbl`/厂商库只在需要原语时**：顶层用例不含厂商原语，`run_xsim.sh` 不需要 `xpm_cdc.sv`/`glbl`；
  联调激励仿真真实 MMCM，必须编译 `glbl.v` 并在 `xelab` 时 `-L unisims_ver`、把 `glbl` 一起作顶层。
- **联调激励的时基要与 MMCM 整数比**：取 `T_MAIN=18900ps`、`T_DSP=2100ps`（严格 9:1，且 18900 能
  被频点表全部 MULT `{12,15,18,20,21}` 整除）。直接按标称 50MHz/450MHz 取 20000ps/2222ps 时，MMCM
  行为模型输出沿按 1ps 取整，使「信号周期 / DSP 周期」偏离整数比，几百个参考沿累加后
  `phaseCnt % edgeCnt` 出现非零余数、部分频点第二路输出周期在 166650/166651ps 间抖动——是仿真
  时基的近似，不是设计缺陷；绝对频率偏离标称不影响该激励的比值判据。
- **联调激励必须按真实地址**（`0x43C1_0000 + 偏移`）驱动 AXI，否则测不出地址译码类缺陷。
- **默认每次重新生成 RTL**：两份脚本都这样，避免源文件改过而 `build/chisel/` 里还是旧产物；
  确认产物已最新时用 `--no-gen` 省时间。
