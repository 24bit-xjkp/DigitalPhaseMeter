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
sim/run_xsim.sh              # 先用 sbt 生成 --type=syn 产物，再编译运行
sim/run_xsim.sh --no-gen     # 跳过 sbt 生成（RTL 已是最新时）
sim/run_xsim.sh --gui        # 运行结束后用 xsim GUI 打开波形
sim/run_xsim.sh --clean      # 先清理 build/xsim
XILINX_VIVADO=/path/to/Vivado sim/run_xsim.sh    # 指定 Vivado 安装路径
```

脚本用 **综合产物**（`build/chisel/DigitalPhaseMeterTop`，其中复位同步链实例化
`xpm_cdc_sync_rst`）仿真，因此仿真的就是会被综合的那份 RTL；产物与日志落在 `build/xsim/`
（不入库）。判定依据是日志里出现 `==== 测试通过`，同时打印 xsim 的退出码。

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
  校验 `gateCnt` / `edgeCnt` / `phaseCnt` 与 `seq` 序号。

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
