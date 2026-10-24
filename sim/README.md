# sim/：MMCM 联调 xsim 仿真

`sim/` 只保留**依赖 Vivado MMCM 原语**（unisims_ver 的 `MMCME2_ADV`）的联调测试：真实 MMCM +
DRP 控制器 + 测量通路的整条链路。测量通路与顶层 IP 的 AXI4-Lite 寄存器通路由 `test/` 下的
Verilator 目标覆盖（`xmake test -v`），其中顶层 IP 的守门人是
[`../test/DigitalPhaseMeterTop.cpp`](../test/DigitalPhaseMeterTop.cpp)，用法与陷阱见
[`../.agents/skills/dpm-verilator-testing/SKILL.md`](../.agents/skills/dpm-verilator-testing/SKILL.md)。
本目录 xsim 的完整工具用法与实测坑见
[`../.agents/skills/dpm-vivado-ip/SKILL.md`](../.agents/skills/dpm-vivado-ip/SKILL.md)。

| 文件 | 说明 |
| --- | --- |
| `tb_src_handoff.sv` | 片内源 ↔ 测量通路联调：真实 `dpm_mmcm_src`（`MMCME2_ADV`）+ `MmcmDrpController` + `DigitalPhaseMeterTop`，按完整地址写 MMCM 寄存器镜像，并直接量两路输出的周期与相位差 |
| `run_handoff_tb.sh` | 联调激励的运行脚本：10 个频点 × 逐频点相位点 = 80 个点（每点判 4 条），约 17s 墙钟 |

## 运行

```bash
sim/run_handoff_tb.sh            # 默认走生成入口后跑全部点
sim/run_handoff_tb.sh --no-gen   # 跳过生成入口（RTL 已是最新时）
sim/run_handoff_tb.sh --clean    # 先清 build/xsim_handoff
XILINX_VIVADO=/path/to/Vivado sim/run_handoff_tb.sh   # 指定 Vivado 安装路径
```

脚本消费 `build/chisel/<Name>/` 下的生成产物（每个模块只有一份产物，Verilator、xsim 与综合
共用；复位同步是纯 RTL 的 `ResetSynchronizer`，产物里没有 XPM 原语），日志落在
`build/xsim_handoff/`（不入库）。联调另需 `vivado/rtl/dpm_mmcm_src.v` 与 unisims_ver 的
`MMCME2_ADV`/`glbl`。判定成败不要只看 xsim 退出码（`$fatal` 也可能返回 0）：看日志里的
`HANDOFF_SUMMARY ... fails=0`，且 `HANDOFF_MEAS` 行数 = 2×点数。

## 覆盖内容

- MMCM 联调（`tb_src_handoff.sv`）：软件给出的 MMCM 寄存器值真的让输出相位差等于请求值、
  频率 = VCO / D，且测量通路读数与「直接量到的输出相位差」一致（1 个 DSP 周期内）。
  点表覆盖 10 个频点 × 逐频点相位点 = 80 个点（含 ≥180° 的点），每点判 4 条：直接测得相位 =
  MMCM 网格值、测量 = 直接测得、测量 = 网格值、计数模型（判据见
  [`../.agents/skills/dpm-measurement-model/SKILL.md`](../.agents/skills/dpm-measurement-model/SKILL.md)）。
- 联调激励经顶层 IP 的 AXI4-Lite 从口（`0x43C0_0000` + 偏移）写控制/结果寄存器、经 DRP 控制器的
  从口（`0x43C1_0000` + 偏移）写 MMCM 寄存器镜像，但**寄存器堆通路的完整覆盖在 Verilator 顶层用例**
  [`../test/DigitalPhaseMeterTop.cpp`](../test/DigitalPhaseMeterTop.cpp)：复位期间从口不握手与释放后
  寄存器默认值、`GATE_TARGET` 写数据先到与字节选通、测量启动的 busy/resultValid/中断时序与
  `gateCnt`/`edgeCnt`/`phaseCnt` 结果校验、`IRQ_STATUS` W1C 与只读寄存器写忽略、第二次更短闸门测量、
  结果只保留最新一条与 `LOST_COUNT`、`CTRL.clear` 复位结果区与序号、中断使能门控、目标闸门在启动拍
  锁存、busy 期间重复 start 被忽略、片内源与外部输入切换、busy 期间 `CTRL.clear` 当拍生效而在途测量
  继续跑完、第二次复位回到默认值。

## 实测陷阱

- **`$time` 显示单位**：激励声明 `timescale 1ps/1ps`，日志里的 `t=41510000` 是皮秒（41.51µs），
  比较与打印时间时按 ps 换算。
- **非 ASCII 字符串会被破坏**：xsim 会把经 `string` 参数或 `$sformatf` 拼接传递的中文破坏成
  `0xff`。静态 `$display` 字面量可以用中文；任务参数/格式化结果（检查名、错误消息）用 ASCII。
- **timescale 必须显式给**：生成的 RTL 没有 `` `timescale`` 而激励有，`xelab` 要加
  `--timescale 1ns/1ps`（脚本已带）。
- **`filelist.f` 路径**：里面的文件名相对生成目录，脚本按 `build/chisel/<Name>/` 补成绝对路径后
  再交给 `xvlog`。
- **`glbl`/厂商库必须给**：联调仿真真实 MMCM，必须编译 `Vivado/data/verilog/src/glbl.v` 并在
  `xelab` 时 `-L unisims_ver`、把 `glbl` 一起作顶层；**不能**加 `-L unisim`（VHDL 版 unisim 与
  Verilog 版 unisims_ver 冲突）。
- **联调激励的时基要与 MMCM 整数比**：取 `T_MAIN=18900ps`、`T_DSP=2100ps`（严格 9:1，且 18900 能
  被频点表全部 MULT `{12,15,18,20,21}` 整除）。直接按标称 50MHz/450MHz 取 20000ps/2222ps 时，MMCM
  行为模型输出沿按 1ps 取整，使「信号周期 / DSP 周期」偏离整数比，几百个参考沿累加后
  `phaseCnt % edgeCnt` 出现非零余数、部分频点第二路输出周期在 166650/166651ps 间抖动——是仿真
  时基的近似，不是设计缺陷；绝对频率偏离标称不影响该激励的比值判据。
- **长延时要循环拼**：xsim 的 `#` 延时是 32 位有符号（ps 时基下上限约 2.1ms），总超时用
  「200 × 1ms」这种循环写法。
- **联调激励必须按真实地址**（`0x43C0_0000` / `0x43C1_0000` + 偏移）驱动 AXI，否则测不出地址
  译码类缺陷。
- **默认每次走生成入口**：脚本默认调用 `./mill emit DigitalPhaseMeterTop MmcmDrpController`，
  由构建系统的内容缓存判断是否需要重跑，避免源文件改过而 `build/chisel/` 里还是旧产物；
  确认产物已最新时用 `--no-gen` 省时间。
- **MMCM 行为模型很慢**：锁一次约 1~2ms 仿真时间，整轮约 17s 墙钟；看门狗按「25ms 仿真时间无
  进展」判卡死。
