# 上板与 bring-up（PS 侧软件）

本文件说明「把 `ps/` 里的裸机程序跑到 Zynq PS 上、并让 PC 上位机能通过串口连上」需要做的事，
以及当前仓库**还缺什么**。内容分三步：重建 bitstream（串口是 `create_bd.tcl` 的参数）→
生成 BOOT.bin → 连上位机。

> **当前状态**：串口已在 AX7010 实板上跑通（`uart=UART1 baud=115200`，见
> [`onboard-ax7010.md`](onboard-ax7010.md)）；`vivado/create_bd.tcl` 默认就带 UART1（MIO48/49），
> 重建 BD 即生效。上位机侧另有仿真后端在 pty 上做的一套验证（见 `docs/verify-plan.md`）。

## 0. PS 串口：`create_bd.tcl` 的 `-uart` 参数

PS 的调试串口不再是「事后补丁」，而是建 BD 时的参数：

| 选择 | MIO | 说明 |
| ---- | --- | ---- |
| `uart1`（默认） | MIO48(tx)/49(rx) | Bank1（本板 1.8V），AX7010 板载 CP2102N 的接法 |
| `uart0` | MIO14(tx)/15(rx) | Bank0（本板 3.3V），部分板卡 |
| `none` | — | 不给 PS 配串口（MIO 全 unassigned） |

```bash
vivado -mode batch -source vivado/create_bd.tcl                    # 默认 uart1 + 115200
vivado -mode batch -source vivado/create_bd.tcl -uart uart0         # 换成 MIO14/15
vivado -mode batch -source vivado/create_bd.tcl -uart uart1 -baud 921600
vivado -mode batch -source vivado/create_bd.tcl -uart none          # 不要串口
```

脚本设完属性后会回读校验并打印，校验不过直接报错（原来的 `patch_enable_ps_uart.tcl` 做的就是
这件事，其语句已并入 `create_bd.tcl` 并删除该补丁脚本）：

```text
PS7_UART_CHECK: EN_UART1=1 IO='MIO 48 .. 49' BAUD=115200
PS7_UART_CHECK: MIO48=UART 1/uart1_tx
PS7_UART_CHECK: MIO49=UART 1/uart1_rx
```

自检脚本（不需要跑 Vivado，直接读 XCI 或 XSA）：

```bash
python3 ps/tools/check_bd_uart.py build/vivado/bd_proj/dpm_bd.srcs/sources_1/bd/dpm_system/ip/dpm_system_processing_system7_0_0/dpm_system_processing_system7_0_0.xci
# 期望：UART1: EN=1 PERIPHERAL_ENABLE=1 IO=MIO 48 .. 49 BAUD=115200 → 使能
python3 ps/tools/check_bd_uart.py build/vivado/dpm_system.xsa   # 也可以直接查 XSA 里的 .hwh
```

设置的关键属性（Zynq PS7，Vivado 2025.2；`create_bd.tcl` 按 `-uart` / `-baud` 生成）：

```tcl
set_property -dict [list \
  CONFIG.PCW_UART1_PERIPHERAL_ENABLE {1} \
  CONFIG.PCW_UART1_UART1_IO {MIO 48 .. 49} \
  CONFIG.PCW_UART1_BAUD_RATE {115200} \
  CONFIG.PCW_EN_UART1 {1} \
  CONFIG.PCW_MIO_48_PULLUP {enabled} \
  CONFIG.PCW_MIO_48_DIRECTION {out} \
  CONFIG.PCW_MIO_48_SLEW {slow} \
  CONFIG.PCW_MIO_49_PULLUP {enabled} \
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

## 1. 重建 bitstream 与 XSA

改了 PS 配置必须重跑（顺序不能颠倒，见 `.agents/skills/dpm-vivado-ip`）：

```bash
vivado -mode batch -source vivado/create_bd.tcl          # 重建 BD + wrapper（含上面的 UART 配置）
vivado -mode batch -source vivado/build_bitstream.tcl    # 写策略 → 综合/实现/bitstream/XSA
python3 ps/tools/check_bd_uart.py build/vivado/dpm_system.xsa    # 复验
```

> `create_bd.tcl` 会**重新生成** BD，所以串口配置必须写在它里面（或用它自己的 `-uart` 参数）——
> 这正是原来那种「在已有 BD 上打补丁」的脚本会失效的原因，也是把补丁并进流水线的原因。

## 2. 生成 BOOT.bin（本仓库不提供 FSBL）

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

## 3. 连上位机

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

## 4. 上板后要先确认的三件事

1. **MMCM 寄存器镜像真的生效**：写 `0x43C1_0000` 段的 MMCM 寄存器（`0x08..0x24`）+ `0x28` LOAD
   后，两路输出频率/相位差是否按写入值变化（旧的 clk_wiz DRP 语义与
   `spec/drp_semantics.json` 的 `phase_controllable` 是上一版方案的结论，仅供参考）；
2. **中断真的被 PS 收到**：`DPM_USE_IRQ=1` 编译时看 `IRQ_STATUS` 的 irqPending 是否由中断路径清除；
   若中断不通，退回轮询路径（默认行为）；
3. **测量值与仿真后端的一致性**：按 `docs/verify-plan.md` 第 6 节的清单跑同一套 28 个点，
   容限不变；差异要归因到 MMCM 抖动 / 真实布线延迟 / 外部干扰，而不是直接放宽判据。
