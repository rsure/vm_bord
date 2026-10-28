#!/usr/bin/env bash
# 把 vm_bord 装成 systemd 服务（CentOS 7 / RHEL 7 验证）。
#
# 前提：本目录（应用根）已经在目标路径上，例如 /usr/local/hk/tools/vmbord，
#       里面有 server/ web/ requirements.txt deploy/。
# 用法：sudo VMBORD_AUTH='admin:admin@123' bash deploy/install.sh [端口]
#
# 幂等：重复执行只会更新依赖、重写 unit 并重启服务。

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8787}"
AUTH="${VMBORD_AUTH:-}"
VENV="$APP_DIR/venv"
UNIT_SRC="$APP_DIR/deploy/vm-bord.service"
UNIT_DST="/etc/systemd/system/vm-bord.service"
ENV_FILE="/etc/vm-bord.env"

if [[ $EUID -ne 0 ]]; then
  echo "请用 root 执行（sudo）" >&2
  exit 1
fi
if [[ -z "$AUTH" ]]; then
  echo "缺少登录账号：sudo VMBORD_AUTH='账号:密码' bash deploy/install.sh" >&2
  exit 1
fi

echo "== 1/6 选一个 >= 3.8 的 Python =="
# 代码用了 from __future__ import annotations，Python 3.7+ 才有；
# 而 CentOS 7 官方源只有 3.6.8，所以这里要么用更早装好的 conda/SCL 解释器，
# 要么用 VMBORD_PYTHON 显式指定。
pick_python() {
  local candidates=() c ver
  [[ -n "${VMBORD_PYTHON:-}" ]] && candidates+=("$VMBORD_PYTHON")
  candidates+=(python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3
               /usr/local/hk/tools/miniconda3/bin/python)
  for c in "${candidates[@]}"; do
    command -v "$c" >/dev/null 2>&1 || continue
    ver="$("$c" -c 'import sys;print("%d%d"%sys.version_info[:2])' 2>/dev/null || echo 0)"
    if [[ "$ver" =~ ^[0-9]+$ ]] && (( ver >= 38 )); then
      echo "$c"
      return 0
    fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  cat >&2 <<'EOF'
没找到 Python 3.8 或更高版本。
CentOS 7 官方源的 python3 是 3.6.8（本程序无法运行），SCL 源已随 EOL 失效。
最省事的办法是装一个自带的 Miniconda：

  curl -fsSL -o /tmp/miniconda.sh \
    https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-py38_4.12.0-Linux-x86_64.sh
  bash /tmp/miniconda.sh -b -p /usr/local/hk/tools/miniconda3

（新版 Miniconda 要求 glibc>=2.28，CentOS 7 只有 2.17，必须用 py38_4.12.0 这个版本。）

装好后重跑：
  sudo VMBORD_AUTH='账号:密码' bash deploy/install.sh 8787
或者显式指定解释器：
  sudo VMBORD_PYTHON=/opt/python3.10/bin/python3 VMBORD_AUTH='...' bash deploy/install.sh
EOF
  exit 1
fi
echo "使用解释器：$PY（$("$PY" -V 2>&1)）"

echo "== 2/6 建虚拟环境 =="
if [[ ! -x "$VENV/bin/python" ]]; then
  if ! "$PY" -m venv "$VENV" >/dev/null 2>&1; then
    echo "venv 创建失败，尝试 --without-pip + get-pip 引导"
    "$PY" -m venv --without-pip "$VENV"
    curl -fsSL "https://bootstrap.pypa.io/pip/$( "$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' )/get-pip.py" \
      | "$VENV/bin/python"
  fi
else
  echo "已存在：$VENV"
fi
"$VENV/bin/python" -V

echo "== 3/6 装 Python 依赖 =="
"$VENV/bin/pip" install -q -U pip setuptools wheel
"$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"
"$VENV/bin/pip" list 2>/dev/null | grep -iE "pyvmomi|cryptography" || true

echo "== 4/6 写登录口令 =="
umask 077
printf 'VMBORD_AUTH=%s\n' "$AUTH" > "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "已写入 $ENV_FILE（0600）"

echo "== 5/6 安装 systemd 单元 =="
sed -e "s#/usr/local/hk/tools/vmbord#$APP_DIR#g" \
    -e "s#--port 8787#--port $PORT#" \
    "$UNIT_SRC" > "$UNIT_DST"
chmod 644 "$UNIT_DST"
systemctl daemon-reload
systemctl enable vm-bord >/dev/null
systemctl restart vm-bord

echo "== 6/6 等待服务就绪 =="
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "健康检查通过：$(curl -fsS "http://127.0.0.1:${PORT}/api/health")"
    echo
    echo "看板地址： http://$(hostname -I | awk '{print $1}'):${PORT}/"
    echo "登录账号： ${AUTH%%:*}"
    echo "查看日志： journalctl -u vm-bord -f"
    exit 0
  fi
  sleep 0.5
done

echo "服务没起来，看日志：" >&2
journalctl -u vm-bord -n 40 --no-pager >&2
exit 1
