# sim/：Vivado xsim 仿真

AXI4 接口与顶层 IP 的验证用 **Vivado 自带 xsim + SystemVerilog 测试激励**，
不再为此写 Verilator C++ 测试；测量通路（`Controller` / `EdgeDetector`）的 Verilator
单元测试仍然保留在 `test/` 下（`xmake test -v`）。

| 文件                            | 说明                                                             |
| ------------------------------- | ---------------------------------------------------------------- |
| `tb_digital_phase_meter_top.sv` | 顶层 IP 的测试激励：AXI4-Lite 主机 BFM、测量激励与结果寄存器校验 |
| `run_xsim.sh`                   | 一键脚本：生成 RTL → `xvlog` 编译 → `xelab` 细化 → `xsim` 运行   |

## 运行

```bash
sim/run_xsim.sh              # 先用 sbtn 生成 --type=syn 产物，再编译运行
sim/run_xsim.sh --no-gen     # 跳过 sbtn 生成（RTL 已是最新时）
sim/run_xsim.sh --gui        # 运行结束后用 xsim GUI 打开波形
sim/run_xsim.sh --clean      # 先清理 build/xsim
XILINX_VIVADO=/path/to/Vivado sim/run_xsim.sh    # 指定 Vivado 安装路径
```

脚本用 **综合产物**（`build/chisel/DigitalPhaseMeterTop/syn`，其中复位同步链实例化
`xpm_cdc_sync_rst`）仿真，因此仿真的就是会被综合的那份 RTL；产物与日志落在 `build/xsim/`
（不入库）。判定依据是日志里出现 `==== 测试通过`，同时打印 xsim 的退出码。
补充：仿真产物与综合产物分目录存放（`build/chisel/<Name>/sim` 与 `.../syn`），
两者内容不同且不能共用目录——Verilator 侧没有 XPM 模型，见 `dpm-chisel-rtl`。

## 覆盖内容

- 复位：`resetn` 拉低期间所有 `ready` 为低、不接受请求（持续发 `arvalid`/`awvalid` 也不握手），
  复位释放后寄存器为默认值；
- AXI4-Lite：只读寄存器、读写寄存器、写地址/写数据通道顺序颠倒、字节选通只改选中字节、
  只读寄存器写忽略、写响应与读延迟；
- 测量：`CTRL.start` 后 `busy` 置起，完成后 `resultValid`/`irqPending` 置起、中断拉高，
  `busy` 期间重复写 `start` 被忽略（只产生一条结果）；
- 中断：电平中断与 `irqEnable` 门控（不使能时挂起位仍置位、引脚不拉高）、
  `IRQ_STATUS` 写 1 分别清中断挂起位 / 清 `resultValid` / 清丢失计数；
- 数据通道（GPIO 式寄存器）：读 4 个结果寄存器拿一条结果；结果只保留最新一条，
  不读走就再次测量会覆盖旧结果并累加 `LOST_COUNT`；`CTRL.clear` 后序号重新计数；
- 结果正确性：用测量模型（[`dpm-measurement-model`](../.agents/skills/dpm-measurement-model/SKILL.md)）
  校验 `gateCnt` / `edgeCnt` / `phaseCnt` 与 `seq` 序号（容限里的闸门计数余量按被测信号周期给，
  因此片内信号源这种「周期是低速时钟周期整数倍」的信号也适用）；
- 片内信号源与输入切换：写 `SIG_CTRL` 并读回、读 `SIG_STATUS` 的四个状态位（含复位默认值），
  选中片内源后测量结果跟随片内信号（周期 640ns、相位差 160ns），切回外部输入后跟随引脚信号
  （400ns / 120ns）；两条通路的理论值不同，因此能区分「切换生效」与「切换没生效」。
  片内源的两路信号在仿真里由测试激励直接驱动，模拟 PL 内 MMCM 的两个输出。
- 该用例曾经抓到一个 Verilator 单测覆盖不到的缺陷：`ControlRegs` 的配置输出被优化成常量 0
  （写通道里的标志位没有被消费），上层测量因此一直用外部输入。**顶层 AXI 通路上的改动必须跑 xsim**。
- MMCM 本身不在这个激励里：仿真只喂两路固定频率/相位差的源信号（等价于 MMCM 已按目标参数锁定），
  因此「MMCM 的动态重配置寄存器是否被正确写入」不在本激励的覆盖范围内——那部分由厂商 IP
  自己的仿真模型与 PG065 寄存器映射保证，Vivado 侧只做 `validate_bd_design` 与地址分配检查。

## 实测陷阱（写激励时踩过的）

- **`$time` 显示单位**：xsim 在 `--timescale 1ns/1ps` 下打印的 `$time` 是皮秒量级，
  日志里 `[41510000]` 表示 41.51µs，不要按 ns 读。
- **非 ASCII 字符串会被破坏**：xsim 会把经过 `string` 类型参数或字符串拼接传递的
  多字节字符（中文）破坏成 `0xff` 字节。因此本激励里**静态提示**（`$display` 的字面量）
  用中文，而所有**作为任务参数/`$sformatf` 结果传递的字符串**（检查名、错误消息）用 ASCII。
- **XPM 需要 `glbl`**：实例化了 XPM 原语的设计在 xsim 下必须额外编译
  `$XILINX_VIVADO/data/verilog/src/glbl.v`，并在 `xelab` 时把 `glbl` 一起作为顶层。
- **timescale 必须一致**：生成的 RTL 没有 `` `timescale``，而激励有，
  `xelab` 需要显式给 `--timescale 1ns/1ps`，否则报
  `Module ... doesn't have a timescale but at least one module in design has a timescale`。
- **filelist.f 的路径**：`filelist.f` 里的文件名相对于生成目录，`xvlog -f` 按当前工作目录解析，
  脚本里先补成绝对路径再传给 `xvlog`。
