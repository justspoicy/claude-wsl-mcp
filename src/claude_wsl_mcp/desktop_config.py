"""把本服务器登记进 Windows 上 Claude Desktop 的 claude_desktop_config.json。

在普通 WSL 终端里运行（不能在本 MCP 的沙箱里：要用互操作问 Windows 的目录，还要写 /mnt/c）。
配置文件位置取决于 Claude Desktop 的安装方式：
- MSIX 版（新安装包 / 微软商店）：%LOCALAPPDATA%\\Packages\\Claude_<发布者ID>\\LocalCache\\Roaming\\Claude
- 传统安装版：%APPDATA%\\Claude
只改 mcpServers.<name> 这一项，其余字段原样保留；内容有变化才写，写之前先备份。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

WSL_EXE = "C:\\Windows\\System32\\wsl.exe"
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


class DesktopConfigError(RuntimeError):
    pass


def _windows_folders() -> tuple[str, str]:
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "[Environment]::GetFolderPath('LocalApplicationData');"
        "[Environment]::GetFolderPath('ApplicationData')"
    )
    exe = POWERSHELL if os.path.exists(POWERSHELL) else "powershell.exe"
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            cwd="/mnt/c" if os.path.isdir("/mnt/c") else None,
            capture_output=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DesktopConfigError(
            f"无法通过 WSL 互操作调用 PowerShell 查询 Windows 目录：{exc}。请在普通 WSL 终端里运行，或用 --config-file 指定"
        ) from exc
    lines = [ln.strip() for ln in proc.stdout.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    if len(lines) < 2:
        raise DesktopConfigError(f"PowerShell 输出不符合预期：{lines!r}")
    return lines[-2], lines[-1]


def _wslpath(windows_path: str) -> Path:
    out = subprocess.run(["wslpath", "-u", windows_path], capture_output=True, text=True, check=True).stdout
    return Path(out.strip())


def find_config_files() -> list[Path]:
    local, roaming = (_wslpath(p) for p in _windows_folders())
    found = []
    for package in sorted((local / "Packages").glob("Claude_*")):
        claude_dir = package / "LocalCache" / "Roaming" / "Claude"
        if claude_dir.is_dir():
            found.append(claude_dir / "claude_desktop_config.json")
    classic = roaming / "Claude"
    if classic.is_dir():
        found.append(classic / "claude_desktop_config.json")
    return found


def server_entry(distro: str, user: str, launcher: str) -> dict:
    return {"command": WSL_EXE, "args": ["-d", distro, "-u", user, "--cd", "~", "--exec", launcher]}


def update_config_file(path: Path, name: str, entry: dict | None, *, dry_run: bool = False) -> str:
    """entry 为 None 表示删除该服务器。返回一句结果描述。"""
    if path.exists():
        raw = path.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise DesktopConfigError(f"{path} 不是合法 JSON，未做任何改动：{exc}") from exc
        if not isinstance(data, dict):
            raise DesktopConfigError(f"{path} 顶层不是 JSON 对象，未做任何改动")
    else:
        data = {}
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        raise DesktopConfigError(f"{path} 里的 mcpServers 不是对象，未做任何改动")

    if entry is None:
        if name not in servers:
            return f"无需改动（没有 mcpServers.{name}）：{path}"
        del servers[name]
        action = "删除"
    else:
        if servers.get(name) == entry:
            return f"无需改动（已是最新）：{path}"
        action = "更新" if name in servers else "新增"
        servers[name] = entry
    data["mcpServers"] = servers
    if dry_run:
        return f"[dry-run] 将{action} mcpServers.{name}：{path}"

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = ""
    if path.exists():
        backup_path = path.with_name(f"{path.name}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup_path)
        backup = f"，原文件备份为 {backup_path.name}"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return f"已{action} mcpServers.{name}：{path}{backup}"


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        prog="python -m claude_wsl_mcp.desktop_config", description="把 claude-wsl-mcp 登记到 Windows 版 Claude Desktop"
    )
    parser.add_argument("--name", default="wsl", help="在 Claude Desktop 里显示的服务器名，默认 wsl")
    parser.add_argument("--launcher", default=str(repo / "bin" / "claude-wsl-mcp"), help="WSL 里启动脚本的绝对路径")
    parser.add_argument("--distro", default=os.environ.get("WSL_DISTRO_NAME"), help="WSL 发行版名，默认当前发行版")
    parser.add_argument("--user", default="root", help="以哪个 WSL 用户启动；建立沙箱需要 root")
    parser.add_argument("--config-file", type=Path, action="append", help="直接指定配置文件（可重复），不再自动探测")
    parser.add_argument("--remove", action="store_true", help="从配置里删除该服务器")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要做的改动")
    args = parser.parse_args(argv)

    if not args.distro:
        print("WSL_DISTRO_NAME 未设置，请用 --distro 指定发行版名（wsl.exe -l -v 可查）", file=sys.stderr)
        return 2
    try:
        paths = args.config_file or find_config_files()
        if not paths:
            raise DesktopConfigError("没找到 Claude Desktop 的配置目录：请先安装并至少打开一次 Claude Desktop，或用 --config-file 指定")
        entry = None if args.remove else server_entry(args.distro, args.user, args.launcher)
        for path in paths:
            print(update_config_file(path, args.name, entry, dry_run=args.dry_run))
    except (DesktopConfigError, subprocess.SubprocessError, OSError) as exc:
        print(f"登记失败：{exc}", file=sys.stderr)
        return 1
    if entry is not None:
        print(json.dumps({args.name: entry}, ensure_ascii=False, indent=2))
    print("生效方式：完全退出 Claude Desktop（任务栏右下角托盘里右键 Claude 图标 → 退出/Quit），再重新打开。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
