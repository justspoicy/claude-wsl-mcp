#!/usr/bin/env bash
# 在 WSL 里安装 claude-wsl-mcp，并登记到 Windows 版 Claude Desktop。可重复执行。
#
# 用法：./install.sh [--project 目录] [--name 服务器名] [--no-desktop]
#   --project     首次生成配置时的“当前项目”（默认等于工作区 ~/project）
#   --name        Claude Desktop 里显示的服务器名（默认 wsl）
#   --no-desktop  只装 WSL 这一侧，不改 Claude Desktop 配置
set -euo pipefail

repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
name=wsl
project=""
desktop=1
while [ $# -gt 0 ]; do
    case "$1" in
        --project) project=$2; shift 2 ;;
        --name) name=$2; shift 2 ;;
        --no-desktop) desktop=0; shift ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }

if [ -z "${WSL_DISTRO_NAME:-}" ]; then
    echo "请在 WSL 里运行本脚本" >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "建立沙箱需要 root，请用 root 运行（例如 sudo ./install.sh）" >&2
    exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "需要 Python 3.10+，当前 " + sys.version.split()[0])'
python3 -c 'import venv, ensurepip' 2>/dev/null || { echo "缺少 venv 模块，请先 apt install python3-venv" >&2; exit 1; }

step "Python 虚拟环境与依赖"
[ -x "$repo/.venv/bin/python" ] || python3 -m venv "$repo/.venv"
"$repo/.venv/bin/python" -m pip install --quiet --upgrade pip
"$repo/.venv/bin/python" -m pip install --quiet -e "$repo[dev]"
chmod +x "$repo/bin/claude-wsl-mcp"

step "配置文件"
conf_dir=${XDG_CONFIG_HOME:-$HOME/.config}/claude-wsl-mcp
conf=$conf_dir/config.toml
if [ -f "$conf" ]; then
    echo "沿用已有配置：$conf"
else
    workspace=$HOME/project
    [ -d "$workspace" ] || workspace=$HOME
    project=${project:-$workspace}
    mkdir -p "$conf_dir"
    "$repo/.venv/bin/python" - "$conf" "$workspace" "$project" <<'EOF'
import os, sys
from claude_wsl_mcp.config import render_default_config
path, workspace, project = sys.argv[1], os.path.realpath(sys.argv[2]), os.path.realpath(sys.argv[3])
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(render_default_config(workspace, project))
print(f"已生成配置：{path}（工作区 {workspace}，当前项目 {project}）")
EOF
fi

step "自检：在独立挂载命名空间里建立沙箱（不影响本机其它 WSL 会话）"
"$repo/bin/claude-wsl-mcp" --check

if [ "$desktop" -eq 1 ]; then
    step "登记到 Windows 版 Claude Desktop"
    "$repo/.venv/bin/python" -m claude_wsl_mcp.desktop_config --name "$name" --launcher "$repo/bin/claude-wsl-mcp"
fi
