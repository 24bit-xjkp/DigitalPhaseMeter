# 上板与 bring-up（PS 侧软件）

本文件说明「把 `ps/` 里的裸机程序跑到 Zynq PS 上、并让 PC 上位机能通过串口连上」需要做的事，
以及当前仓库**还缺什么**。内容分四步：BD 补丁 → 重建 bitstream → 生成 BOOT.bin → 连上位机。

> **当前状态**：本仓库**没有上板验证过**。默认 bitstream 里 PS UART 是关闭的
> （见下面第 0 节），上位机侧的验证是用仿真后端在 pty 上完成的（见 `docs/verify-plan.md`）。

## 0. 为什么必须先打 BD 补丁

当前 BD 的 PS7 配置里：

| 现象 | 证据 |
| ---- | ---- |
| UART0/UART1 都未使能 | `dpm_system_processing_system7_0_0.xci`：`PCW_EN_UART0 = 0`、`PCW_EN_UART1 = 0`、`PCW_UART0/1_PERIPHERAL_ENABLE = 0` |
| 54 个 MIO 全部未分配 | 同文件：`PCW_MIO_TREE_PERIPHERALS` / `PCW_MIO_TREE_SIGNALS` 全是 `unassigned#...` |
| 没有 board preset（MIO/DDR 都是 IP 默认值） | `PCW_IMPORT_BOARD_PRESET = "None"`、`BASE_BOARD_PART = ""` |

也就是说：**当前 bitstream 上 PS 没有任何串口可用**，`ps/build/dpm_ps.elf` 即使跑起来也发不出一个字节。
必须先按下面第 1 步改 `vivado/create_bd.tcl`（或直接在你的板级工程里改）并重建。

自检脚本（不需要跑 Vivado，直接读 XCI）：

```bash
python3 ps/tools/check_bd_uart.py build/vivado/bd_proj/dpm_bd.srcs/sources_1/bd/dpm_system/ip/dpm_system_processing_system7_0_0/dpm_system_processing_system7_0_0.xci
# 期望：当前输出 "UART 未使能"；打好补丁重建后输出使能的 UART 与外设号
```

## 1. BD 补丁：使能 PS UART

补丁脚本：[`vivado/patch_enable_ps_uart.tcl`](../vivado/patch_enable_ps_uart.tcl)。
它在**已存在的 BD**（`build/vivado/bd_proj/dpm_bd.xpr`）上设置 PS7 属性，不重建整个 BD：

```bash
vivado -mode batch -source vivado/patch_enable_ps_uart.tcl -tclargs uart1
# uart1 → MIO48/49（多数 Digilent 板卡把 USB-UART 接在这里）
# uart0 → MIO14/15（部分板卡）

# 只想验证属性名对不对、不改动工程（dry run，第 4 个参数为 1）：
vivado -mode batch -source vivado/patch_enable_ps_uart.tcl -tclargs uart1 115200 build/vivado/bd_proj/dpm_bd.xpr 1
```

**现状（已实测）**：本仓库在 Vivado 2025.2 上跑过该脚本的 **dry run**（不保存、不
`generate_target`），退出码 0，输出：

```text
PATCH: 使能 UART1（UART 1），MIO48=tx MIO49=rx，波特率 115200
PATCH_CHECK: PCW_EN_UART1=1 PCW_UART1_UART1_IO=MIO 48 .. 49
PATCH_DRY_RUN: 属性设置成功、未保存（dry=1）
```

也就是说**属性名与写法在 2025.2 上确实有效**；但**真正保存 BD + 重建 bitstream 未执行**
（那一步要重跑综合与实现，约数十分钟到数小时，且本仓库当前没有板子可验证），
所以「打补丁后的 bitstream 真能出串口」仍属于**未验证**项。

设置的关键属性（Zynq PS7，Vivado 2025.2）：

```tcl
set_property -dict [list \
  CONFIG.PCW_UART1_PERIPHERAL_ENABLE {1} \
  CONFIG.PCW_UART1_UART1_IO {MIO 48 .. 49} \
  CONFIG.PCW_UART1_BAUD_RATE {115200} \
  CONFIG.PCW_EN_UART1 {1} \
  CONFIG.PCW_MIO_48_PULLUP {enabled} \
  CONFIG.PCW_MIO_48_IOTYPE {LVCMOS 1.8V} \
  CONFIG.PCW_MIO_48_DIRECTION {out} \
  CONFIG.PCW_MIO_48_SLEW {slow} \
  CONFIG.PCW_MIO_49_PULLUP {enabled} \
  CONFIG.PCW_MIO_49_IOTYPE {LVCMOS 1.8V} \
  CONFIG.PCW_MIO_49_DIRECTION {in} \
  CONFIG.PCW_MIO_49_SLEW {slow} \
  CONFIG.PCW_MIO_TREE_PERIPHERALS {...  UART 1#UART 1# ...} \
  CONFIG.PCW_MIO_TREE_SIGNALS     {...  uart1_tx#uart1_rx# ...} \
] [get_bd_cells processing_system7_0]
```

**MIO 号与 bank 电压是板级信息**，请对照你的板卡原理图确认：
Zynq-7010 CLG400 的 MIO bank 1 一般是 1.8V（USB-UART 常见接法），
若你的板子接在 MIO14/15（bank 0，多为 3.3V）就选 `uart0`；
`MIO_TREE_PERIPHERALS`/`MIO_TREE_SIGNALS` 是 54 项的 `#` 分隔串，脚本会自动改对应位置。

## 2. 重建 bitstream 与 XSA

改了 PS 配置必须重跑（顺序不能颠倒，见 `.agents/skills/dpm-vivado-ip`）：

```bash
vivado -mode batch -source vivado/create_bd.tcl          # 重建 BD + wrapper（含上面的 UART 配置）
vivado -mode batch -source vivado/apply_timing_strategy.tcl
vivado -mode batch -source vivado/build_bitstream.tcl    # 综合/实现/bitstream/XSA
python3 ps/tools/check_bd_uart.py build/vivado/bd_proj/.../dpm_system_processing_system7_0_0.xci  # 复验
```

> 如果你是在补丁后的 BD 工程上直接重建，注意 `create_bd.tcl` 会**重新生成** BD，
> 因此补丁要写进脚本（或用 `patch_enable_ps_uart.tcl` 在 `create_bd.tcl` 之后跑一次）。

## 3. 生成 BOOT.bin（本仓库不提供 FSBL）

Zynq-7000 上电后先跑 BootROM，再跑 **FSBL**（First Stage Boot Loader），最后由 FSBL 加载
我们的应用。**FSBL 通常由 Vitis 从 XSA 生成**；本机装的 Vitis 2025.2 缺少 ARM 裸机编译器
（`gnu/aarch32` 不存在）与 `xsct`，因此仓库里**不包含** FSBL 与 BOOT.bin 生成步骤。
在装有完整 Vitis 的机器上：

```bash
# 1) 从 XSA 建 platform 与 FSBL（Vitis GUI 或 vitis -s 脚本）
# 2) 把 ps/build/dpm_ps.elf 作为 application 加入，或手工组 BOOT.bin：
```

手工组 BOOT.bin 的 `.bif`（bootgen 在 `$XILINX_VIVADO/bin/bootgen`，本机存在）：

```text
// ps/boot/dpm_ps.bif
the_1:
{
    [bootloader]fsbl.elf        // 由 Vitis 从 dpm_system.xsa 生成
    dpm_system_wrapper.bit      // build/vivado/bd_proj/dpm_bd.runs/impl_1/dpm_system_wrapper.bit
    dpm_ps.elf                  // ps/build/dpm_ps.elf（本仓库产物）
}
```

```bash
bootgen -image ps/boot/dpm_ps.bif -arch zynq -o BOOT.bin -w on
```

把 `BOOT.bin` 放到 SD 卡（FAT 分区）并设置启动模式为 SD，或用 JTAG 下载（需要能连上
`hw_server`，本机当前 `jtag targets` 为空）。串口参数：**115200 8N1，无流控**。

> 若你的板子已有可用的 Linux（PYNQ 等），也可以不写 BOOT.bin：
> 在 Linux 下用 `devmem`/`uio` 直接操作寄存器，把本仓库的 C 程序当作协议参考实现；
> 但那条路径要自己写 `/dev/mem` 访问层，本仓库未提供。

## 4. 连上位机

```bash
# 板上跑起来后，PC 侧：
python3 -m dpm_host --port /dev/ttyUSB1 ping
python3 -m dpm_host --port /dev/ttyUSB1 identify
python3 -m dpm_host --port /dev/ttyUSB1 source int
python3 -m dpm_host --port /dev/ttyUSB1 sig --freq 25e6 --phase 90
python3 -m dpm_host --port /dev/ttyUSB1 gate 2000
python3 -m dpm_host --port /dev/ttyUSB1 meas
python3 -m dpm_host --port /dev/ttyUSB1 scan --out scan.csv     # 多频点多相位扫描
python3 -m dpm_host --port /dev/ttyUSB1 gui                     # 图形界面
```

`/dev/ttyUSB1` 在本次侦察里是 `Digilent USB Device`（FT232H，很可能是板载 USB-UART），
`/dev/ttyUSB0` 是外接 CP2102N；插拔后请用 `python3 -m serial.tools.list_ports -v` 确认。

## 5. 上板后要先确认的三件事

1. **DRP 结论**：`docs/verify-drp.md` 给出的相位字段单位与「相位能否在线改」是否与实测一致
   （`spec/drp_semantics.json` 的 `phase_controllable`）；
2. **中断真的被 PS 收到**：`DPM_USE_IRQ=1` 编译时看 `IRQ_STATUS` 的 irqPending 是否由中断路径清除；
   若中断不通，退回轮询路径（默认行为）；
3. **测量值与仿真后端的一致性**：按 `docs/verify-plan.md` 第 6 节的清单跑同一套 28 个点，
   容限不变；差异要归因到 MMCM 抖动 / 真实布线延迟 / 外部干扰，而不是直接放宽判据。
