#!/bin/bash
# 离线演练 `ps/tools/onboard_accept.py`：不接板，用**与固件同一份** dpm_cmd.c 的 C 验证台
# （`ps/build/host_main`）当服务端，经 socat 的一对 pty 对接脚本。
#
#   bash ps/tools/onboard_accept_sim.sh [--outdir DIR]
#
# 为什么用 socat 而不是 `host_main --pty`：验收脚本会先开一次串口做链路检查、关掉，
# 再用客户端连接；`--pty` 的从端节点在客户端关闭后会消失，第二次打开就 ENOENT。
# socat 自己持有两个 pty 的主端，路径始终有效。
#
# 用途：改完验收脚本先在这里过一遍（协议 + 流 + 32 点扫描全绿）再上板。
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT=$(pwd)
OUT=${1:-build/onboard_accept_sim}
HOST_MAIN=${HOST_MAIN:-ps/build/host_main}
BAUD=${BAUD:-115200}

if [ ! -x "$HOST_MAIN" ]; then
    echo "找不到 $HOST_MAIN，先跑 bash ps/build.sh" >&2
    exit 2
fi
command -v socat >/dev/null || { echo "缺少 socat（apt install socat）" >&2; exit 2; }

WORK=$(mktemp -d)
SRV_LINK="$WORK/srv"
CLI_LINK="$WORK/cli"
cleanup() {
    [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null || true
    [ -n "${SOCAT_PID:-}" ] && kill "$SOCAT_PID" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

socat -d -d "pty,raw,echo=0,link=$SRV_LINK" "pty,raw,echo=0,link=$CLI_LINK" >"$WORK/socat.log" 2>&1 &
SOCAT_PID=$!
for _ in $(seq 1 100); do
    [ -e "$SRV_LINK" ] && [ -e "$CLI_LINK" ] && break
    sleep 0.1
done
[ -e "$CLI_LINK" ] || { echo "socat 未能建立 pty 对，见 $WORK/socat.log" >&2; exit 2; }
echo "== 离线演练：$HOST_MAIN（srv=$(readlink "$SRV_LINK") cli=$(readlink "$CLI_LINK")）=="

# --speed 让模型虚拟时间跑得比墙钟快，32 点扫描才不至于慢到不可用
"$HOST_MAIN" --device "$SRV_LINK" --freq-hz 25000000 --phase-mdeg 90000 --gate 2000 \
    --speed 65536 >"$WORK/host_main.out" 2>"$WORK/host_main.err" &
SRV_PID=$!
sleep 0.6

set +e
# 优先用装了 dpm_host 的解释器；没有就用系统 python3（脚本头部自带 sys.path 兜底）
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
"$PY" ps/tools/onboard_accept.py --no-boot --port "$CLI_LINK" --baud "$BAUD" \
    --banner-timeout 0.5 --outdir "$OUT"
RC=$?
set -e
echo "离线演练结束：退出码 $RC（报告在 $OUT/）"
exit $RC
