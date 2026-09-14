"""入口：加载配置 → 建立沙箱 → 以 stdio 运行 MCP Server。

stdout 专供 MCP 协议；日志一律写 stderr（Claude Desktop 会收进 mcp-server-<名字>.log）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, ConfigStore, default_config_path, default_state_dir
from .jobs import JobManager
from .sandbox import SandboxError, apply, disabled_report, sanitized_environment
from .server import Runtime, build_server
from .shell import prune_spill_dir

log = logging.getLogger("claude_wsl_mcp")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-wsl-mcp", description="供 Claude Desktop 调用的 WSL MCP Server（stdio）")
    parser.add_argument("--config", type=Path, help=f"配置文件路径，默认 {default_config_path()}")
    parser.add_argument("--check", action="store_true", help="建立沙箱并打印环境报告后退出，不启动 MCP")
    parser.add_argument("--no-sandbox", action="store_true", help="不建立沙箱（仅供开发调试）")
    parser.add_argument("--version", action="version", version=f"claude-wsl-mcp {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = ConfigStore(args.config or default_config_path())
    try:
        cfg = store.get()
    except ConfigError as exc:
        log.error("配置错误：%s", exc)
        return 2

    try:
        if args.no_sandbox:
            report = disabled_report(cfg.sandbox, "以 --no-sandbox 启动，未建立沙箱")
        else:
            report = apply(cfg.sandbox, chdir_to=cfg.current_project)
    except SandboxError as exc:
        log.error("沙箱建立失败，拒绝启动：%s", exc)
        return 3

    env = sanitized_environment(os.environ, report)
    os.environ.clear()
    os.environ.update(env)
    state_dir = default_state_dir()
    prune_spill_dir(state_dir / "outputs")
    runtime = Runtime(
        store=store,
        sandbox=report,
        startup_config=cfg,
        base_env=dict(os.environ),
        state_dir=state_dir,
        jobs=JobManager(state_dir),
    )

    if args.check:
        print(asyncio.run(runtime.describe()))
        return 0

    log.info(
        "claude-wsl-mcp %s 启动：pid=%s 当前项目=%s 沙箱=%s",
        __version__,
        os.getpid(),
        cfg.current_project,
        f"{report.windows_drives}/interop={'on' if report.windows_interop else 'off'}" if report.enabled else "off",
    )
    build_server(runtime).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
