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

echo "== 1/6 检查系统依赖 =="
missing=()
for pkg in python3 python3-pip; do
  rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if ((${#missing[@]})); then
  echo "安装 ${missing[*]}"
  yum install -y "${missing[@]}"
else
  echo "python3 / python3-pip 已就绪：$(python3 -V 2>&1)"
fi

PY_VER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "Python 版本：$PY_VER"

echo "== 2/6 建虚拟环境 =="
if [[ ! -x "$VENV/bin/python" ]]; then
  if ! python3 -m venv "$VENV" >/dev/null 2>&1; then
    echo "python3 -m venv 不可用，改用 --without-pip + get-pip 引导"
    python3 -m venv --without-pip "$VENV"
    curl -fsSL "https://bootstrap.pypa.io/pip/${PY_VER}/get-pip.py" | "$VENV/bin/python"
  fi
else
  echo "已存在：$VENV"
fi
"$VENV/bin/python" -V

echo "== 3/6 装 Python 依赖 =="
"$VENV/bin/pip" install -q -U "pip<22" setuptools wheel
if [[ "$PY_VER" == "3.6" ]]; then
  # 3.6 只能装这几代：更新的 pyvmomi / cryptography 都要求 3.8+
  "$VENV/bin/pip" install -q "pyvmomi==8.0.1.0" "cryptography==36.0.2"
else
  "$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"
fi
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
