/** @file host_main.c
 *  @brief 宿主机（x86-64）验证台：在 pty / tty / stdio 上跑**同一份**命令处理核心。
 *
 * 用途：无硬件时验证协议、参数校验、单次/连续模式与流上报；PL 侧由
 * `fpga_model.c` 的寄存器级 + 测量级仿真后端代替。
 *
 * 用法（与 Lead 的验证脚本约定一致）：
 *
 *     ps/build/host_main --device /dev/pts/N [--freq-hz 25000000] [--phase-mdeg 90000]
 *                        [--gate 2000] [--dsp-hz 400000000] [--slow-hz 50000000]
 *                        [--seed N] [--verbose]
 *     ps/build/host_main --pty          # 自己建 pty 并把从端路径打印到 stdout
 *     ps/build/host_main --stdio        # 协议字节走 stdin/stdout
 *
 * 约定：
 *   - 协议字节只走数据通道（`--device` 的 fd / pty 主端 / stdio 的 stdin+stdout），
 *     日志一律走 stderr（只有 `--verbose` 时才输出）；
 *   - 进程在数据通道 EOF、stdin（交互 tty）EOF 或 SIGTERM/SIGINT 时干净退出，退出码 0；
 *   - 模型是**确定性**的（与 `spec/golden/vectors.json` 逐位对齐），`--seed` 仅为
 *     CLI 兼容保留（打印在 verbose 日志里，不影响结果）。
 */

/* pty / termios 需要 GNU/POSIX 扩展声明（Linux 上 cfmakeraw、posix_openpt、ptsname_r 等） */
#define _GNU_SOURCE 1
#define _DEFAULT_SOURCE 1

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#if defined(__linux__)
#include <pty.h>
#endif

#include "dpm_cmd.h"
#include "dpm_io.h"
#include "fpga_model.h"

#ifndef DPM_HOST_DEFAULT_FREQ_HZ
#define DPM_HOST_DEFAULT_FREQ_HZ 25000000u
#endif
#ifndef DPM_HOST_DEFAULT_PHASE_MDEG
#define DPM_HOST_DEFAULT_PHASE_MDEG 90000u
#endif
#ifndef DPM_HOST_DEFAULT_GATE
#define DPM_HOST_DEFAULT_GATE 2000u
#endif

static volatile sig_atomic_t g_stop = 0;
static int g_verbose = 0;
static uint64_t g_poll_ns = 0, g_work_ns = 0, g_last_hb_ns = 0;

/** 单调时钟（ns），仅用于 --verbose 的耗时统计。 */
static uint64_t now_ns(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        return 0u;
    }
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

/* ------------------------------------------------------------- 输出辅助 */

static int write_all(int fd, const uint8_t *buf, uint32_t len)
{
    uint32_t off = 0u;

    while (off < len) {
        ssize_t n = write(fd, &buf[off], (size_t)(len - off));
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        off += (uint32_t)n;
    }
    return 0;
}

/** 通知帧回调：直接写到数据通道。 */
static int host_emit(void *user, const uint8_t *frame, uint32_t len)
{
    int fd = *(int *)user;
    if (g_verbose != 0) {
        fprintf(stderr, "[host_main] 通知帧 0x%02X len=%u\n", frame[2], len);
    }
    return write_all(fd, frame, len);
}

/* ------------------------------------------------------------- tty 配置 */

static void set_raw(int fd)
{
    struct termios tio;

    if (isatty(fd) == 0) {
        return;
    }
    if (tcgetattr(fd, &tio) != 0) {
        return;
    }
    cfmakeraw(&tio);
    tio.c_cflag |= (CLOCAL | CREAD);
    tio.c_cflag &= ~CRTSCTS;
    (void)cfsetispeed(&tio, B115200);
    (void)cfsetospeed(&tio, B115200);
    tio.c_cc[VMIN] = 0;
    tio.c_cc[VTIME] = 0;
    (void)tcsetattr(fd, TCSANOW, &tio);
}

/* ------------------------------------------------------------- CLI */

static void usage(const char *argv0)
{
    fprintf(stderr,
            "用法: %s [--device PATH | --pty | --stdio] [选项]\n"
            "  --device PATH     打开已存在的 tty/pty 从端（Lead 的验证脚本用这个）\n"
            "  --pty             自己建 pty，把从端路径打印到 stdout\n"
            "  --stdio           协议字节走 stdin/stdout\n"
            "  --freq-hz N       片内源初始频率（默认 25000000）\n"
            "  --phase-mdeg N    片内源初始相位差，毫度（默认 90000）\n"
            "  --gate N          初始闸门目标（默认 2000）\n"
            "  --dsp-hz N        高速时钟（默认 400000000）\n"
            "  --slow-hz N       低速时钟（默认 50000000）\n"
            "  --seed N          随机种子（模型确定性，仅为 CLI 兼容保留）\n"
            "  --speed N         每轮主循环额外推进的慢周期数（默认 4096，0 = 纯事务推进）\n"
            "  --ext-present     模拟外部引脚有信号（默认无信号 → 测量停在 busy）\n"
            "  --ext-freq-hz N   外部信号频率（配合 --ext-present，默认 10000000）\n"
            "  --ext-phase-mdeg N 外部信号相位差（毫度，默认 0）\n"
            "  --unlock          测试钩子：让 MMCM 永不锁定（覆盖 ERR_BUSY 路径）\n"
            "  --watch-stdin     同时监视 stdin，EOF 即退出\n"
            "  --verbose         把命令/结果日志打到 stderr\n",
            argv0);
}

static int parse_u32(const char *s, uint32_t *out)
{
    char *end = 0;
    unsigned long v;

    if (s == 0 || *s == '\0') {
        return 0;
    }
    errno = 0;
    v = strtoul(s, &end, 0);
    if (errno != 0 || end == 0 || *end != '\0' || v > 0xFFFFFFFFul) {
        return 0;
    }
    *out = (uint32_t)v;
    return 1;
}

/* ------------------------------------------------------------- 主程序 */

int main(int argc, char **argv)
{
    dpm_model_t model;
    dpm_cmd_ctx_t ctx;
    dpm_rx_t rx;
    const dpm_io_t *io = dpm_model_io();
    uint32_t slow_hz = 50000000u;
    uint32_t dsp_hz = 400000000u;
    uint32_t freq_hz = DPM_HOST_DEFAULT_FREQ_HZ;
    uint32_t phase_mdeg = DPM_HOST_DEFAULT_PHASE_MDEG;
    uint32_t gate = DPM_HOST_DEFAULT_GATE;
    uint32_t seed = 1u;
    uint32_t speed = DPM_MODEL_CYCLES_PER_ITER;
    uint32_t ext_freq_hz = 10000000u;
    uint32_t ext_phase_mdeg = 0u;
    int ext_present = 0;
    int verbose = 0;
    int watch_stdin = 0;
    int force_unlock = 0;
    int data_fd = -1;
    int in_fd = -1;
    int out_fd = -1;
    int pty_master = -1;
    int i;
    const char *device = 0;
    int use_pty = 0;
    int use_stdio = 0;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--device") == 0 && i + 1 < argc) {
            device = argv[++i];
        } else if (strcmp(argv[i], "--pty") == 0) {
            use_pty = 1;
        } else if (strcmp(argv[i], "--stdio") == 0) {
            use_stdio = 1;
        } else if (strcmp(argv[i], "--freq-hz") == 0 && i + 1 < argc) {
            if (parse_u32(argv[++i], &freq_hz) == 0) {
                fprintf(stderr, "非法 --freq-hz\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--phase-mdeg") == 0 && i + 1 < argc) {
            if (parse_u32(argv[++i], &phase_mdeg) == 0) {
                fprintf(stderr, "非法 --phase-mdeg\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--gate") == 0 && i + 1 < argc) {
            if (parse_u32(argv[++i], &gate) == 0) {
                fprintf(stderr, "非法 --gate\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--dsp-hz") == 0 && i + 1 < argc) {
            if (parse_u32(argv[++i], &dsp_hz) == 0) {
                fprintf(stderr, "非法 --dsp-hz\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--slow-hz") == 0 && i + 1 < argc) {
            if (parse_u32(argv[++i], &slow_hz) == 0) {
                fprintf(stderr, "非法 --slow-hz\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--seed") == 0 && i + 1 < argc) {
            if (parse_u32(argv[++i], &seed) == 0) {
                fprintf(stderr, "非法 --seed\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--ext-present") == 0) {
            ext_present = 1;
        } else if (strcmp(argv[i], "--ext-freq-hz") == 0 && i + 1 < argc) {
            if (parse_u32(argv[++i], &ext_freq_hz) == 0) {
                fprintf(stderr, "非法 --ext-freq-hz\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--ext-phase-mdeg") == 0 && i + 1 < argc) {
            if (parse_u32(argv[++i], &ext_phase_mdeg) == 0) {
                fprintf(stderr, "非法 --ext-phase-mdeg\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--speed") == 0 && i + 1 < argc) {
            if (parse_u32(argv[++i], &speed) == 0) {
                fprintf(stderr, "非法 --speed\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--unlock") == 0) {
            force_unlock = 1;
        } else if (strcmp(argv[i], "--watch-stdin") == 0) {
            watch_stdin = 1;
        } else if (strcmp(argv[i], "--verbose") == 0 || strcmp(argv[i], "-v") == 0) {
            verbose = 1;
            g_verbose = 1;
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            usage(argv[0]);
            return 0;
        } else {
            fprintf(stderr, "未知参数: %s\n", argv[i]);
            usage(argv[0]);
            return 2;
        }
    }

    if (use_pty != 0) {
        char name[256];
#if defined(__linux__)
        pty_master = posix_openpt(O_RDWR | O_NOCTTY);
        if (pty_master < 0 || grantpt(pty_master) != 0 || unlockpt(pty_master) != 0) {
            fprintf(stderr, "建立 pty 失败: %s\n", strerror(errno));
            return 3;
        }
        if (ptsname_r(pty_master, name, sizeof(name)) != 0) {
            fprintf(stderr, "取 pty 从端名失败: %s\n", strerror(errno));
            return 3;
        }
        set_raw(pty_master);
        printf("%s\n", name);
        fflush(stdout);
        data_fd = pty_master;
        in_fd = pty_master;
        out_fd = pty_master;
        if (verbose != 0) {
            fprintf(stderr, "[host_main] pty 从端: %s\n", name);
        }
#else
        fprintf(stderr, "本平台不支持 --pty\n");
        return 3;
#endif
    } else if (use_stdio != 0) {
        in_fd = STDIN_FILENO;
        out_fd = STDOUT_FILENO;
        data_fd = STDOUT_FILENO;
    } else if (device != 0) {
        data_fd = open(device, O_RDWR | O_NOCTTY);
        if (data_fd < 0) {
            fprintf(stderr, "打开 %s 失败: %s\n", device, strerror(errno));
            return 3;
        }
        set_raw(data_fd);
        in_fd = data_fd;
        out_fd = data_fd;
        if (verbose != 0) {
            fprintf(stderr, "[host_main] 数据通道: %s (fd %d)\n", device, data_fd);
        }
    } else {
        fprintf(stderr, "需要 --device / --pty / --stdio 之一\n");
        usage(argv[0]);
        return 2;
    }

    (void)signal(SIGTERM, on_signal);
    (void)signal(SIGINT, on_signal);
    (void)signal(SIGPIPE, SIG_IGN);

    dpm_model_init(&model, slow_hz, dsp_hz);
    if (force_unlock != 0) {
        dpm_model_set_lock_stuck(&model, 1);
    }
    dpm_model_set_external(&model, ext_freq_hz, ext_phase_mdeg, ext_present != 0 ? 0 : 1);
    dpm_model_advance(&model, 1000u);

    {
        dpm_clocks_t clk;
        clk.slow_hz = slow_hz;
        clk.dsp_hz = dsp_hz;
        dpm_cmd_init(&ctx, io, &model, &clk);
    }
    ctx.emit = host_emit;
    ctx.emit_user = &out_fd;

    /* 初始闸门目标与片内源工作点（默认 25MHz/90°，与协议 §7 一致） */
    dpm_reg_wr(io, &model, DPM_REG_GATE_TARGET, gate);
    ctx.gate_target = gate;
    {
        dpm_sig_t sig;
        dpm_sig_solve_t sst;
        dpm_sig_solve(freq_hz, phase_mdeg, slow_hz, &sig, &sst);
        if (sst.ok != 0) {
            if (dpm_sig_apply(&ctx, &sig) == 0) {
                ctx.sig = sig;
                ctx.sig_valid = 1u;
            }
        }
        if (verbose != 0) {
            fprintf(stderr,
                    "[host_main] 初值: f=%u Hz phase=%u mdeg → VCO=%u D=%u field=%u (err=%u mHz)\n",
                    sig.freq_hz, sig.phase_milli_deg, sig.vco_hz, (unsigned)sig.div,
                    (unsigned)sig.phase_field, sst.error_milli_hz);
        }
    }

    dpm_rx_init(&rx);
    if (verbose != 0) {
        fprintf(stderr, "[host_main] ready: slow=%u Hz dsp=%u Hz seed=%u\n",
                slow_hz, dsp_hz, seed);
    }

    uint32_t loop_iters = 0u;
    while (g_stop == 0) {
        struct pollfd pfd[2];
        loop_iters++;
        uint64_t dbg_t0 = (verbose != 0) ? now_ns() : 0u;
        nfds_t nfds = 0;
        int pr;

        pfd[nfds].fd = in_fd;
        pfd[nfds].events = POLLIN;
        pfd[nfds].revents = 0;
        nfds++;
        if (watch_stdin != 0 && use_stdio == 0) {
            pfd[nfds].fd = STDIN_FILENO;
            pfd[nfds].events = POLLIN;
            pfd[nfds].revents = 0;
            nfds++;
        } else if (use_stdio == 0 && use_pty == 0 && device != 0 && isatty(STDIN_FILENO) != 0) {
            /* 交互使用：Ctrl-D 退出 */
            pfd[nfds].fd = STDIN_FILENO;
            pfd[nfds].events = POLLIN;
            pfd[nfds].revents = 0;
            nfds++;
        }

        pr = poll(pfd, nfds, 2);
        if (verbose != 0) {
            uint64_t dbg_t1 = now_ns();
            g_poll_ns += (dbg_t1 - dbg_t0);
            dbg_t0 = dbg_t1;
        }
        if (pr < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        if (pr > 0) {
            for (nfds_t k = 0; k < nfds; k++) {
                if ((pfd[k].revents & (POLLIN | POLLHUP | POLLERR)) == 0) {
                    continue;
                }
                if (pfd[k].fd == STDIN_FILENO && (use_stdio == 0)) {
                    uint8_t sink[64];
                    ssize_t n = read(STDIN_FILENO, sink, sizeof(sink));
                    if (n <= 0) {
                        g_stop = 1; /* stdin EOF/关闭 → 干净退出 */
                    }
                    continue;
                }
                for (;;) {
                    uint8_t buf[256];
                    ssize_t n = read(in_fd, buf, sizeof(buf));
                    if (n < 0) {
                        if (errno == EINTR) {
                            continue;
                        }
                        if (errno == EAGAIN || errno == EWOULDBLOCK) {
                            break;
                        }
                        g_stop = 1; /* 对端关闭（pty 主端会返回 EIO） */
                        break;
                    }
                    if (n == 0) {
                        g_stop = 1;
                        break;
                    }
                    for (ssize_t b = 0; b < n; b++) {
                        uint8_t cmd = 0u;
                        const uint8_t *payload = 0;
                        uint8_t plen = 0u;
                        if (dpm_rx_push(&rx, buf[b], &cmd, &payload, &plen) != 0) {
                            uint8_t frame[DPM_FRAME_MAX];
                            uint32_t flen = dpm_cmd_handle(&ctx, cmd, payload, plen, frame,
                                                           (uint32_t)sizeof(frame));
                            if (verbose != 0) {
                                fprintf(stderr, "[host_main] cmd=0x%02X len=%u → %u 字节\n", cmd,
                                        (unsigned)plen, flen);
                            }
                            if (flen != 0u) {
                                (void)write_all(out_fd, frame, flen);
                            }
                        }
                    }
                    if (n < (ssize_t)sizeof(buf)) {
                        break;
                    }
                }
            }
        }
        if (speed != 0u) {
            dpm_model_advance(&model, speed); /* 虚拟时间与墙钟解耦（见 fpga_model.h） */
        }
        dpm_cmd_tick(&ctx, dpm_model_now_ms(&model));
        if (verbose != 0) {
            g_work_ns += (now_ns() - dbg_t0);
            if ((now_ns() - g_last_hb_ns) >= 5000000000ull) {
                g_last_hb_ns = now_ns();
                fprintf(stderr, "[host_main] 心跳 迭代=%u 事务=%u 仿真=%u ms 命令=%u\n", loop_iters,
                        model.txn_count, dpm_model_now_ms(&model), ctx.cmd_count);
            }
        }
    }

    if (verbose != 0) {
        fprintf(stderr,
                "[host_main] 退出: 命令 %u、ERR_PARAM %u、ERR_BUSY %u、启动 %u 次、事务 %u\n",
                ctx.cmd_count, ctx.err_param_count, ctx.err_busy_count, model.start_count,
                model.txn_count);
        fprintf(stderr, "[host_main] 主循环迭代 %u 次；poll 累计 %llu ms、其余 %llu ms\n",
                loop_iters, (unsigned long long)(g_poll_ns / 1000000ull),
                (unsigned long long)(g_work_ns / 1000000ull));
    }
    if (data_fd >= 0 && data_fd != STDIN_FILENO && data_fd != STDOUT_FILENO) {
        (void)close(data_fd);
    }
    return 0;
}
