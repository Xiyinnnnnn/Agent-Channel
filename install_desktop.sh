#!/usr/bin/env bash
# Agent Channel 桌面入口安装脚本（运行一次即可）
# 基于脚本自身位置定位项目 → 项目移动后重跑本脚本即可
set -euo pipefail

# 脚本所在目录 = repo 根（本脚本放 repo 根，与 control/ 平级）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_PY="$SCRIPT_DIR/control/control.py"
TEMPLATE="$SCRIPT_DIR/control/Agent-Channel.desktop"
DEST_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DEST="$DEST_DIR/Agent-Channel.desktop"

[ -f "$CONTROL_PY" ] || { echo "错误：找不到 $CONTROL_PY"; exit 1; }
[ -f "$TEMPLATE" ]  || { echo "错误：找不到模板 $TEMPLATE"; exit 1; }

# 取 Python：优先当前项目解释器（python3 即可，系统自带）
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "错误：未找到 python3"; exit 1; }

mkdir -p "$DEST_DIR"

# 模板占位符 → 实际路径
sed -e "s|__PY__|$PY|g" \
    -e "s|__CONTROL_PY__|$CONTROL_PY|g" \
    "$TEMPLATE" > "$DEST"
chmod +x "$DEST"

# 刷新桌面数据库（可选，失败不影响）
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DEST_DIR" >/dev/null 2>&1 || true
fi

echo "已创建桌面入口：$DEST"
echo "  → Exec: $PY $CONTROL_PY"
echo "现在可以在应用菜单/桌面找到「Agent Channel」。"
echo "（若需放桌面：cp \"$DEST\" ~/Desktop/ 并 chmod +x）"
