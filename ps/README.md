# PS 侧裸机程序与宿主机验证台（`ps/`）

Zynq-7010（`xc7z010clg400-1`）PS 侧的 **freestanding 裸机 C 串口命令服务端**：按
[`docs/ps-protocol.md`](../docs/ps-protocol.md) 解析 PC 上位机的命令帧，读写 PL 的 AXI4-Lite 寄存器
与片内源 MMCM 的 DRP 口，完成单次/连续测量、闸门时间、输入源选择、片内源频率与相位设定、结果回传；
**同一份命令处理核心**（`dpm_cmd.c`）在宿主机（x86-64）上以「寄存器级 PL 仿真后端 + pty 服务端」
的形式运行，用于无硬件时的端到端验证。工程已完成并通过实测验收（2026-10-19）。

## 1. 目录结构

```text
ps/
├── include/   dpm_regs.h(冻结) dpm_calc.h(冻结) dpm_proto.h(冻结)
│              dpm_io.h        # 寄存器 IO vtable（真实 MMIO / 仿真后端共用）
│              dpm_cmd.h       # 命令核心上下文、PAYLOAD 结构
│              dpm_board.h     # 板级驱动接口（UART/GIC/时间源、中断号）
├── src/       dpm_main.c dpm_cmd.c dpm_proto.c dpm_calc.c dpm_sig.c dpm_div64.c
│              dpm_regs_io.c dpm_uart.c dpm_irq.c startup.c linker.ld
├── sim/       fpga_model.{c,h}（PL 仿真后端） host_main.c（pty/stdio 服务端）
├── tests/     test_calc.c test_proto.c test_sig.c golden_table.h
│              test_server_selftest.py e2e_session_record.py gen_golden_table.py find_uart_baud.py
├── tools/     onboard_accept.py onboard_accept_sim.sh jtag_boot.tcl check_bd_uart.py
├── build.sh   CMakeLists.txt
└── build/     构建产物（不入库）
```

## 2. 构建

```bash
bash ps/build.sh              # 交叉编译 dpm_ps.elf/.bin + 宿主机 host_main
bash ps/build.sh --ps-only    # 只交叉编译
bash ps/build.sh --host-only  # 只构建 host_main

cmake -S ps -B ps/build/cmake && cmake --build ps/build/cmake -j8
ctest --test-dir ps/build/cmake --output-on-failure     # 4 个用例
```

**为什么必须 freestanding**：本机的 `arm-none-eabi-gcc` 只装了 M-profile 的 multilib，链接
newlib/libgcc 会报 `conflicting architecture profiles A/M`。因此 PS 侧一律
`-mcpu=cortex-a9 -marm -ffreestanding -nostdlib -nostartfiles -ffunction-sections -fdata-sections
-Wl,--gc-sections`，自带 `startup.c`（栈/BSS/向量）与 `dpm_div64.c`
（`__aeabi_uidiv`/`__aeabi_uidivmod`/`__udivdi3`/`__umoddi3`/`__aeabi_uldivmod`），不 include 任何
libc 头、不使用浮点与可变参数。构建脚本最后用 `nm -u` 断言**没有未定义符号**（没有偷偷依赖
libgcc/newlib 的证据），并要求 `-Wall -Wextra -Werror -Wshadow -Wundef -Wpointer-arith -Wcast-align
-Wstrict-prototypes -Wmissing-prototypes` 下零警告。链接器提示 `has a LOAD segment with RWX
permissions` 是单段 OCM 镜像的正常提示，可忽略。

## 3. 宿主机验证台 `host_main`

```bash
ps/build/host_main --device /dev/pts/N        # 用已有 pty 从端（验证脚本用这个）
ps/build/host_main --pty                      # 自己建 pty，把从端路径打到 stdout
ps/build/host_main --stdio                    # 协议字节走 stdin/stdout

# 可选参数
--freq-hz N --phase-mdeg N --gate N           # 片内源初值 / 初始闸门
--slow-hz N --dsp-hz N --speed N              # 时钟（默认 50MHz / 450MHz）、虚拟时间加速
--ext-present --ext-freq-hz N --ext-phase-mdeg N  # 模拟外部引脚接信号（默认无信号）
--unlock                                      # 测试钩子：MMCM 永不锁定
--watch-stdin --verbose                       # stdin EOF 即退出、日志走 stderr
```

约定：协议字节只走数据通道，日志只走 stderr；数据通道 EOF、交互式 stdin 的 EOF 或
`SIGTERM`/`SIGINT` 都**干净退出且返回 0**。

### 仿真后端的建模范围与简化（不要当成硬件）

| 方面 | 建模 | 简化/假设 |
| --- | --- | --- |
| 寄存器语义 | 逐个对齐 `ControlRegs.scala`：start 脉冲（busy 期间忽略）、busy/resultValid/lost/irqPending、结果被覆盖即 `lostCount++`、IRQ_STATUS W1C、`CTRL.clear` 写当拍生效（只复位结果区与标志、不清 busy，在途测量继续跑完并照常锁存结果） | 输入选择/使能的配置快照经固定几个慢周期送达（模型参数，真实实现同域同拍） |
| 测量模型 | `gate = target + ceil(T_sig/T_slow)`；`edge = floor(gate·f_sig/f_slow)`；相位先量化到整数个 DSP 周期（450MHz 下 1 个周期 = 2.222ns，整数 RNE）再乘沿数 → `phase_cnt = cycles·edge` | 与 `spec/gen_golden.py` 同口径，逐位一致 |
| 时间推进 | **每次 AXI 事务**推进 64 个慢周期；主循环每轮再用 `--speed`（默认 4096）额外推进，让虚拟时间与墙钟解耦 | 真实硬件是自由运行的时钟；长闸门只花很短墙钟 |
| 源不可用 | 片内源未使能/未锁定，或外部引脚无信号（默认）→ 两路恒低：`busy` 恒为 1、不产生结果、`RESULT_SEQ` 不自增 | 与真实 PL 的「等参考沿」一致 |
| 片内源相位 | 由写入的 MMCM 寄存器镜像（`ClkReg1`/`ClkReg2` 的 `DELAY_TIME` + `PHASE_MUX`）反推实现相位，网格 `45°/D` | 不建模 MMCM 抖动、重配置期间的输出停止细节 |
| DRP 映射 | MMCM 原始 16 位寄存器镜像：`0x08..0x24` + `0x28` LOAD，ID/STATUS 在 `0x00`/`0x04`（`0x43C1_0000` 段） | 不建模真实 DRP 时序 |

## 4. 测试与回归

宿主机 C 用例与 pty 端到端自测（当前结果，`ctest` 4/4 通过）：

```text
test_calc         329 项检查，0 失败   # 金标准定点输出逐位一致、除零/溢出边界
test_proto        128 项检查，0 失败   # CRC 标准向量、组帧↔收帧、负向帧、等待上限量纲
test_sig          584 项检查，0 失败   # 冻结频点求解、VCO/MULT/D 约束、方案 A 编码位域
test_server_selftest  631/631 项通过  # pty 上逐条覆盖命令码、错误路径、流上报、长闸门
```

其他入口：

```bash
python3 ps/tests/e2e_session_record.py          # 打印一次完整 pty 会话（帧 hex + 解析字段）
uv run python spec/validate_sweep.py --server "./ps/build/host_main --device {device}"
                                                # 独立参考实现 80 点扫描（当前 80/80 通过）
bash ps/tools/onboard_accept_sim.sh             # 离线验收演练（当前 94/94 项通过，含 80 点扫描）
```

`ps/tests/golden_table.h` 由 `ps/tests/gen_golden_table.py` 从 `spec/golden/vectors.json` 生成，
改金标准后重新生成，不要手改。

## 5. 上板

完整步骤（BD 串口参数、重建 bitstream、BOOT.bin、JTAG 加载、验收）见
[`docs/bringup.md`](../docs/bringup.md)；本目录相关的入口：

```bash
bash ps/build.sh                                    # 生成 ps/build/dpm_ps.elf 与 .bin
xsdb -quiet ps/tools/jtag_boot.tcl                  # JTAG 冷启动（复位停核 → PL → ps7_init → dow → 运行）
uv run python ps/tools/onboard_accept.py --port /dev/ttyUSB0            # 含冷启动的完整上板验收
uv run python ps/tools/onboard_accept.py --port /dev/ttyUSB0 --no-boot  # 板子已在跑，只验收
python3 ps/tools/check_bd_uart.py build/vivado/dpm_system.xsa           # 复验 BD 的 UART 配置
python3 ps/tests/find_uart_baud.py /dev/ttyUSB0                         # 换板时重测 UART 参考时钟
```

上电横幅示例（收到第一条有效帧前每秒重发）：`DPM-PS ready: uart=UART1 baud=115200
uart_ref_hz=86400000 cpu_variant=3 proto=1.1`。`uart_ref_hz` 是 SLCR 送给 UART 的参考时钟（AX7010
实测 86.4MHz），**不是** PL 的 50MHz 慢时钟；换板后按 `find_uart_baud.py` 重测并更新
`DPM_UART_REF_HZ`。

### 中断路径

`bash ps/build.sh` 默认以 `-DDPM_USE_IRQ=1` 构建：向量表 + GIC（优先级/目标 CPU0）+ 测量完成中断
（`DPM_IRQ_ID` 默认 61）+ UART RX 中断；中断里只置标志并清 `IRQ_STATUS`，主循环负责读取。主循环
**始终同时轮询** `STATUS.busy`，因此中断配置异常也不会卡死测量。要退回纯轮询：把 `build.sh` 的
`DEFS_ARM` 改为 `-DDPM_USE_IRQ=0` 后重建。

## 6. 与冻结接口的关系

- `ps/include/dpm_regs.h`、`dpm_calc.h`、`dpm_proto.h` 是冻结接口，本目录只读使用；
  命令核心通过 `dpm_io.h` 的 vtable 访问寄存器，真实 MMIO 与仿真后端共用同一份 `dpm_cmd.c`。
- 片内源（方案 A）寄存器镜像在 `0x43C1_0000` 段：`0x00` ID（`0x44504D03`）、`0x04` STATUS
  （bit0 = locked、bit1/bit2 = busy）、`0x08..0x24` 八个 MMCM 16 位寄存器镜像、`0x28` LOAD（写 1
  整批生效）；分频/相位编码见 `dpm_regs.h` 的 `DPM_MMCM_ENCODE_CLKOUT`，与 `host/dpm_host/sig.py`
  逐位一致，由两侧单测对拍。
- `freqMilliHz` 的**单位是 Hz**（历史命名），`phaseMilliDeg` 是毫度，公式用不溢出的等价形式
  （见 `dpm_calc.h` 与 `docs/ps-protocol.md` §5）。
- 通知帧是 `0xE1`（`STREAM_DATA`）/ `0xE2`（`EVENT`），响应是 `请求码 | 0x80`，错误 `0xFE/0xFF`；
  `flags.b4` 表示片内源相位在线可控（当前为 1），与 `DPM_DRP_PHASE_WRITABLE` 一致。

## 7. 维护者须知

- **freestanding 是硬约束**：新增源文件不得 include libc 头、不得使用浮点/可变参数；`build.sh`
  的 `nm -u` 断言必须保持为空。
- **等待预算**：闸门目标最大 28 位（50MHz 下 ≈5.37s）。固件在 `GET_RESULT`/`GET_MEAS` 内部做
  有界等待，上限 `DPM_WAIT_TIMEOUT_MS = 50ms + 4 × 闸门时间（秒）`（**毫秒界**，不是慢周期）；
  无时间源时用 `DPM_WAIT_POLL_LIMIT`（**慢时钟周期**界）。两者不要混用。
- **验证台虚拟时间**：`host_main` 的模型时间由 AXI 事务与 `--speed` 推进，与墙钟解耦，长闸门
  只花很短墙钟；跨协议/固件的超时行为由 `host/tests/test_gate_timeout.py` 与
  `host/tests/test_client_over_pty.py` 覆盖。
- **UART 中断号**：测量完成中断 `DPM_IRQ_ID` 默认 61、UART0 = 59、UART1 = 82；BD 里
  `IRQ_F2P[0]` 的接线决定 PL 中断号，改 BD 后核对。
