# 上板指南：ALINX AX7010（xc7z010clg400-1）

本文说明把本仓库的 PL 设计与 PS 侧裸机程序跑到开发板上、并让 PC 上位机通过串口连上所需的步骤。
当前设计已完成上板实测验收；下面是从零重建并复验的流程。

## 0. 硬件与连接

| 项            | 值                                                                 |
| ------------- | ------------------------------------------------------------------ |
| 板卡          | ALINX AX7010，芯片 `xc7z010clg400-1`（JTAG IDCODE `0x03727093`）   |
| JTAG          | Digilent FT232H，`hw_server` 经 `xsdb` 访问                        |
| 板载 USB-UART | CP2102N → `/dev/ttyUSB0`，接 PS **UART1 / MIO48(tx) / MIO49(rx)**  |
| MIO 供电      | Bank0 = 3.3V、Bank1 = 1.8V（MIO48/49 在 Bank1）                    |
| 串口参数      | 115200 8N1，无流控                                                 |

JTAG 电缆首次使用需要 root 安装 udev 规则（Vivado 安装目录下
`data/xicom/cable_drivers/lin64/install_script/install_drivers/install_drivers`）。

## 1. PS 串口：`create_bd.tcl` 的 `-uart` 参数

PS 调试串口是建 BD 时的参数，不是事后补丁：

| 选择 | MIO | 说明 |
| ---- | --- | ---- |
| `uart1`（默认） | MIO48(tx)/49(rx) | Bank1（本板 1.8V），AX7010 板载 CP2102N 的接法 |
| `uart0` | MIO14(tx)/15(rx) | Bank0（本板 3.3V），部分板卡 |
| `none` | — | 不给 PS 配串口（MIO 全 unassigned） |

```bash
vivado -mode batch -source vivado/create_bd.tcl                    # 默认 uart1 + 115200
vivado -mode batch -source vivado/create_bd.tcl -uart uart1 -baud 921600
```

脚本设完属性后会回读校验并打印，校验不过直接报错：

```text
PS7_UART_CHECK: EN_UART1=1 IO='MIO 48 .. 49' BAUD=115200
```

**MIO 号与 bank 电压是板级信息**，请对照板卡原理图确认；若串口接在 MIO14/15（bank 0）就选 `uart0`。

## 2. 重建 bitstream 与 XSA

改了 PS 配置或 RTL 后，先重新生成 Chisel 产物，再运行官方增量入口（仓库根）：

```bash
sbtn -batch "runMain DigitalPhaseMeter.EmitDigitalPhaseMeterTop"
sbtn -batch "runMain DigitalPhaseMeter.EmitMmcmDrpController"
uv sync                                  # 一次性：安装 dpm-vivado-flow 到根 .venv
uv run dpm-vivado-flow                   # 自动只重打包变化的 IP、刷新 BD、重跑受影响的 run
python3 ps/tools/check_bd_uart.py build/vivado/dpm_system.xsa   # 复验串口配置
```

需要从零重建（例如换器件/BD 参数大改）时用 `uv run dpm-vivado-flow --force`；只改 UART 等 BD
参数时也可直接传 `--uart uart0|uart1|none --baud <波特率>`。底层的 `package_ip.tcl` /
`package_src_ip.tcl` / `create_bd.tcl` 仍可手工单独执行，但不会自动复用已有结果。

产物：`build/vivado/dpm_system.xsa`、`build/vivado/bd_proj/dpm_bd.runs/impl_1/dpm_system_wrapper.bit`。
详细流程、增量复用矩阵与注意事项见 [`../vivado/README.md`](../vivado/README.md)。

## 3. 生成 BOOT.bin（本仓库不提供 FSBL）

Zynq-7000 上电先跑 BootROM，再跑 **FSBL**（First Stage Boot Loader），由 FSBL 加载应用。
**FSBL 通常由 Vitis 从 XSA 生成**；本仓库不包含 FSBL 与 BOOT.bin 生成步骤，没装完整 Vitis 时用下面的 JTAG 方式运行。

在装有 Vitis 的机器上手工组 BOOT.bin：

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

把 `BOOT.bin` 放到 SD 卡（FAT 分区）并把启动模式设为 SD。若板上已有 Linux（PYNQ 等），也可以不写
BOOT.bin：用 `devmem`/`uio` 直接操作寄存器，把 `ps/` 下的 C 程序当作协议参考实现（该路径本仓库未提供访问层）。

## 4. 用 JTAG 加载运行

```bash
bash ps/build.sh                                 # 生成 ps/build/dpm_ps.elf 与 .bin（以及宿主机验证台）
xsdb -quiet ps/tools/jtag_boot.tcl               # rst -system -stop → fpga -f → ps7_init → dow -data → PC=0 → con
```

`jtag_boot.tcl` 里已规避三个实测坑：

1. 不能用「`rst -system` 后等待再 stop」：SD 卡的 FSBL/U-Boot 会先跑起来打开 MMU，之后 PC=0 走虚拟地址，程序一个字节都不会执行。必须 `rst -system -stop` 一步到位。
2. `dow`（ELF 形式）在 MMU 开着时会写虚拟地址；改用 `dow -data ps/build/dpm_ps.bin 0x0` 经 APU 物理通路写入。
3. **运行中不要在 Cortex-A9 目标上读内存/寄存器**：APB-AP 会进入粘滞错误态，之后 `rst -dap/-system/-cores` 全部失效。要看程序状态就读 UART/PL 寄存器。

上电横幅示例（收到首帧后停止重发）：

```text
DPM-PS ready: uart=UART1 baud=115200 uart_ref_hz=86400000 cpu_variant=3 proto=1.1
```

UART 参考时钟是 PS 的 86.4MHz（不是 PL 的 50MHz 慢时钟），已写成 `DPM_UART_REF_HZ` 默认值；
需要重测时用 `ps/tests/find_uart_baud.py`。

## 5. 连上位机

```bash
uv run python -m dpm_host --port /dev/ttyUSB0 ping
uv run python -m dpm_host --port /dev/ttyUSB0 identify
uv run python -m dpm_host --port /dev/ttyUSB0 source int
uv run python -m dpm_host --port /dev/ttyUSB0 sig --freq 25e6 --phase 90
uv run python -m dpm_host --port /dev/ttyUSB0 gate 2000
uv run python -m dpm_host --port /dev/ttyUSB0 meas
uv run python -m dpm_host --port /dev/ttyUSB0 scan --out scan.csv
uv run python -m dpm_host --port /dev/ttyUSB0 gui
```

`/dev/ttyUSB0` 通常是板载 CP2102N；插拔后用 `uv run python -m serial.tools.list_ports -v` 确认设备。
协议细节见 [`ps-protocol.md`](ps-protocol.md)。

## 6. 上板验收

```bash
# 一条命令（含 JTAG 冷启动）：前置检查 → 冷启动 → PING/横幅 → 协议与错误路径
# → 连续模式 + 流上报 → 10 个频点 × 逐频点相位点（80 点，含 >180°）扫描 → 写报告
uv run python ps/tools/onboard_accept.py --port /dev/ttyUSB0

# 板子已在运行、只做验收
uv run python ps/tools/onboard_accept.py --port /dev/ttyUSB0 --no-boot
```

产物：`build/onboard_acceptance.{md,json,csv}` 与 `build/onboard_boot.log`；任一项失败退出码非 0。
改验收脚本后先做离线演练（不接板，用与固件同一份 `dpm_cmd.c` 的 C 验证台经 pty 对接）：

```bash
bash ps/tools/onboard_accept_sim.sh
```

**相位判据**：期望值按 **MMCM 的移相网格**量化——MMCM 相位最小步长是 1/8 VCO 周期，折算到输出
就是 `45°/D`（D 取 `SET_SIG` 回显的分频比），即 `exp = round(相位字 / (45°/D)) × (45°/D)`；
容限 = **1 个 DSP 量化步长**（`360·f_sig/f_dsp`，450MHz 下即 2.222ns 对应的度数）+ 1 毫度。
DSP 量化是容限，不是期望值的一部分。量化公式复用 `host/dpm_host/sig.py` 与 `ps/src/dpm_sig.c` 的冻结规则。

## 7. 注意事项

- **中断**：`ps/build.sh` 默认以 `DPM_USE_IRQ=1` 构建；主循环始终同时轮询 `STATUS.busy`，即使中断号配错也不会卡死。中断相关配置（GIC target/priority、UART1 IRQ 号）在上板轮次中已修正。
- **长闸门**：闸门目标最大 28 位（50MHz 下约 5.37s），上位机与固件都按闸门时间放大等待预算，不会把正常测量误报为超时。
- **0° 边界**：两沿落在同一个 DSP 采样拍时量化为 0 个周期，与 0° 同义；验收判据按取模 360° 比较。
- **调试钩子**：上电横幅里的 `DPM_BRINGUP_TRACE` 默认关闭；需要时用 `-DDPM_BRINGUP_TRACE=1` 打开，它会占用闸门目标寄存器，测完务必关掉。
- **SD 卡自启**：本仓库的验证流程使用 JTAG 加载，不修改 SD 卡内容；BOOT.bin 自启需要自备 FSBL。
