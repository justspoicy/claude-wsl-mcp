"""MCP 工具定义。

每次工具调用都重新取配置（热加载），据此构造路径策略；沙箱状态取启动时的实际结果。
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import platform
import pwd
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Callable, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, files, shell
from .config import Config, ConfigError, ConfigStore
from .jobs import Job, JobManager
from .paths import PathPolicy, PolicyError
from .sandbox import SandboxReport, parse_mountinfo

T = TypeVar("T")

INSTRUCTIONS = """\
这是运行在 WSL（Linux）里的开发环境：本服务器的所有工具都在 WSL 中执行，不是 Windows、PowerShell 或 CMD。
- 命令默认在“当前项目”目录执行，wsl_info 可查看；用户说要换项目时调用 set_current_project。
- git、npm、node、python、pytest 等用 run_command；开发服务器、watch、耗时很长的任务用 start_background，
  再用 background_status 看输出、stop_background 停止。
- 改文件优先用 edit_file（精确替换）；新建或整体重写用 write_file。路径一律用 Linux 路径，不要用 C:\\ 这类 Windows 路径。
- Windows 盘（/mnt/c 等）与 Windows 程序（cmd.exe、powershell.exe）按用户配置被隔离，这是有意设置的安全边界：
  遇到“不存在 / 只读 / 权限不足”不要尝试绕过，直接告诉用户需要调整 claude-wsl-mcp 的配置。
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分钟"


def _os_release() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def format_command_result(res: shell.CommandResult) -> str:
    status = f"exit_code: {res.exit_code}"
    if res.signal_name:
        status += f"（被 {res.signal_name} 终止）"
    if res.timed_out:
        status += "（超时，已终止整个进程组）"
    lines = [
        f"$ {res.command}",
        f"cwd: {res.cwd}",
        f"{status}    耗时 {res.duration:.2f}s",
        "--- stdout ---",
        res.stdout.text().rstrip("\n") or "（空）",
        "--- stderr ---",
        res.stderr.text().rstrip("\n") or "（空）",
    ]
    lines += [f"注意：{note}" for note in res.notes]
    return "\n".join(lines)


@dataclass
class Runtime:
    store: ConfigStore
    sandbox: SandboxReport
    startup_config: Config
    base_env: dict[str, str]
    state_dir: Path
    jobs: JobManager
    started_at: float = field(default_factory=time.time)

    @property
    def spill_dir(self) -> Path:
        return self.state_dir / "outputs"

    def config(self) -> Config:
        try:
            return self.store.get()
        except ConfigError as exc:
            raise ToolError(f"配置文件有误，请修正后重试：{exc}") from exc

    def policy(self, cfg: Config) -> PathPolicy:
        return PathPolicy(
            base_dir=cfg.current_project,
            writable_roots=cfg.writable_roots,
            windows_mounts=tuple(self.sandbox.windows_mounts),
            windows_drives=self.sandbox.windows_drives,
        )

    def guard(self, fn: Callable[..., T], *args, **kwargs) -> T:
        try:
            return fn(*args, **kwargs)
        except (PolicyError, files.FileToolError) as exc:
            raise ToolError(str(exc)) from exc
        except OSError as exc:
            detail = f"：{exc.filename}" if exc.filename else ""
            raise ToolError(f"{exc.strerror or exc}{detail}") from exc

    async def guard_async(self, awaitable):
        try:
            return await awaitable
        except OSError as exc:
            detail = f"：{exc.filename}" if exc.filename else ""
            raise ToolError(f"启动命令失败：{exc.strerror or exc}{detail}") from exc

    def resolve_cwd(self, cfg: Config, cwd: str | None) -> str:
        if not cwd:
            if not os.path.isdir(cfg.current_project):
                raise ToolError(f"当前项目目录 {cfg.current_project} 不存在；请用 set_current_project 换一个，或修改配置")
            return cfg.current_project
        target = self.guard(self.policy(cfg).check_read, cwd)
        if not os.path.isdir(target):
            raise ToolError(f"工作目录 {target} 不存在或不是目录")
        return target

    def clamp_timeout(self, cfg: Config, timeout_seconds: int | None) -> int:
        if timeout_seconds is None:
            return cfg.limits.default_timeout_seconds
        return max(1, min(int(timeout_seconds), cfg.limits.max_timeout_seconds))

    def merged_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        return {**self.base_env, **(extra or {})}

    async def describe(self) -> str:
        cfg = self.config()
        report = self.sandbox
        user = pwd.getpwuid(os.geteuid()).pw_name
        try:
            with open("/proc/self/mountinfo", encoding="utf-8") as fh:
                root_fs = next((e.fs_type for e in parse_mountinfo(fh.read()) if e.mount_point == "/"), "?")
        except OSError:
            root_fs = "?"
        probes = {
            "bash": "bash --version | head -n1",
            "git": "git --version",
            "python": "python --version 2>&1",
            "node": "node --version",
            "npm": "npm --version",
            "命令解析到": "for c in git python node npm; do printf '%s=%s  ' \"$c\" \"$(command -v $c || echo 未安装)\"; done",
        }
        probe_cwd = cfg.current_project if os.path.isdir(cfg.current_project) else "/"

        async def probe(command: str) -> str:
            try:
                res = await shell.run_command(
                    command,
                    cwd=probe_cwd,
                    env=self.base_env,
                    timeout=20,
                    login_shell=cfg.login_shell,
                    max_output_bytes=4000,
                    spill_dir=self.spill_dir,
                )
            except OSError as exc:
                return f"执行失败：{exc}"
            out = res.stdout.text().strip() or res.stderr.text().strip()
            return out if res.exit_code == 0 else f"{out or '无输出'}（exit {res.exit_code}）"

        versions = await asyncio.gather(*(probe(cmd) for cmd in probes.values()))
        try:
            sdk_version = importlib.metadata.version("mcp")
        except importlib.metadata.PackageNotFoundError:
            sdk_version = "?"

        lines = [
            "运行位置：WSL2 里的 Linux（不是 Windows、PowerShell 或 CMD）",
            f"  发行版：{_os_release()}（WSL_DISTRO_NAME={self.base_env.get('WSL_DISTRO_NAME', '未设置')}）",
            f"  内核：{platform.release()}",
            f"  根文件系统：{root_fs}",
            f"  用户：{user}（uid={os.geteuid()}）  主机名：{socket.gethostname()}",
            f"  服务器：claude-wsl-mcp {__version__} · mcp SDK {sdk_version} · Python {platform.python_version()}"
            f" · pid {os.getpid()} · 已运行 {_format_duration(time.time() - self.started_at)}",
            "项目",
            f"  当前项目：{cfg.current_project}{'' if os.path.isdir(cfg.current_project) else '（目录不存在！）'}",
            f"  工作区：{cfg.workspace_root}（文件工具可写：{', '.join(cfg.writable_roots)}）",
            f"  配置文件：{cfg.path}{'' if cfg.exists else '（不存在，正在使用默认值）'}",
            "沙箱",
        ]
        if report.enabled:
            drives = {"hidden": "已隐藏", "readonly": "只读", "readwrite": "可读写"}[report.windows_drives]
            lines += [
                f"  状态：已启用（独立挂载命名空间 {report.mount_ns}，宿主为 {report.host_mount_ns}）",
                f"  Windows 盘：{drives}（{', '.join(report.windows_mounts) or '未检测到'}）",
                f"  Windows 程序互操作：{'允许' if report.windows_interop else '已禁用'}",
                f"  只读：{', '.join(report.read_only_paths) or '无'}  隐藏：{', '.join(report.hidden_paths) or '无'}",
                f"  子进程移除的能力：{', '.join(report.dropped_capabilities)}",
                "  不在防护范围：root 仍可请 docker、systemd-run、cron 等特权守护进程代劳；需要时把它们的套接字加入 sandbox.hide_paths",
            ]
        else:
            lines.append("  状态：未启用 —— 命令可以读写 Windows 盘、调用 Windows 程序")
        lines += [f"  提示：{w}" for w in report.warnings]
        if cfg.sandbox != self.startup_config.sandbox:
            lines.append("  提示：配置里的 [sandbox] 已修改，重启 Claude Desktop 后才会生效")
        lines.append("工具链")
        lines += [f"  {name}：{value}" for name, value in zip(probes, versions)]
        lines.append(f"  PATH：{self.base_env.get('PATH', '')}")
        return "\n".join(lines)

    def job_summary(self, job: Job, tail_lines: int) -> str:
        output, size = self.jobs.tail(job, tail_lines)
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job.started_at))
        return "\n".join(
            [
                f"任务 {job.id}：{self.jobs.describe_status(job)}",
                f"  名称：{job.name}",
                f"  命令：{job.command}",
                f"  目录：{job.cwd}",
                f"  pid：{job.pid}（进程组 {job.pid}）  启动于 {started}",
                f"  日志：{job.log_path}（{size} 字节）",
                f"--- 最近 {tail_lines} 行输出 ---",
                output or "（暂无输出）",
            ]
        )


def build_server(runtime: Runtime) -> MCPServer:
    server = MCPServer(name="claude-wsl-mcp", title="WSL Ubuntu", version=__version__, instructions=INSTRUCTIONS)

    @server.tool(title="WSL 环境信息", annotations=READ_ONLY, structured_output=False)
    async def wsl_info() -> str:
        """查看本服务器所在的 WSL 环境：发行版、内核、用户、当前项目、沙箱状态，以及 git / python / node / npm 的版本与实际路径。
        需要确认“命令确实在 WSL Ubuntu 里执行”或排查环境问题时先调用它。"""
        return await runtime.describe()

    @server.tool(
        title="执行命令",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
        structured_output=False,
    )
    async def run_command(
        command: Annotated[str, Field(description="bash 命令，可用管道、&&、重定向。例：git status、npm install、python -m pytest -q")],
        cwd: Annotated[str | None, Field(description="工作目录（Linux 路径）；默认当前项目，相对路径以当前项目为基准")] = None,
        timeout_seconds: Annotated[int | None, Field(description="超时秒数，默认 120，上限见配置（默认 3600）；超时会终止整个进程组")] = None,
        stdin: Annotated[str | None, Field(description="写入标准输入的文本；不给则标准输入为空，交互式提示会直接读到 EOF")] = None,
        env: Annotated[dict[str, str] | None, Field(description="本次命令额外的环境变量")] = None,
    ) -> str:
        """在 WSL 里用 bash 执行一条命令并等待结束，返回 stdout、stderr、退出码和耗时。
        适合 git、npm/node、python/pip/pytest、make、ls/cat/grep 等会自行结束的命令；
        开发服务器、watch 这类不会自己退出的程序请用 start_background。"""
        cfg = runtime.config()
        workdir = runtime.resolve_cwd(cfg, cwd)
        result = await runtime.guard_async(
            shell.run_command(
                command,
                cwd=workdir,
                env=runtime.merged_env(env),
                timeout=runtime.clamp_timeout(cfg, timeout_seconds),
                login_shell=cfg.login_shell,
                max_output_bytes=cfg.limits.max_output_bytes,
                spill_dir=runtime.spill_dir,
                stdin_text=stdin,
            )
        )
        return format_command_result(result)

    @server.tool(
        title="后台启动",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
        structured_output=False,
    )
    def start_background(
        command: Annotated[str, Field(description="要在后台长期运行的 bash 命令，例：npm run dev、python -m http.server 8000")],
        cwd: Annotated[str | None, Field(description="工作目录；默认当前项目")] = None,
        name: Annotated[str | None, Field(description="便于辨认的任务名")] = None,
        env: Annotated[dict[str, str] | None, Field(description="额外环境变量")] = None,
        wait_seconds: Annotated[float, Field(description="启动后等待多少秒再返回首批输出（0-30）", ge=0, le=30)] = 2.0,
    ) -> str:
        """在 WSL 里后台启动一个长期运行的进程（开发服务器、watch、长时间构建等），立即返回任务 id。
        输出写入日志文件；用 background_status 查看输出与状态，用 stop_background 停止。
        Claude Desktop 重启后任务仍在运行，也仍可查询和停止。"""
        cfg = runtime.config()
        workdir = runtime.resolve_cwd(cfg, cwd)
        job = runtime.guard(
            runtime.jobs.start, command, cwd=workdir, env=runtime.merged_env(env), name=name, login_shell=cfg.login_shell
        )
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and runtime.jobs.is_running(job):
            time.sleep(0.2)
        return "已在后台启动。\n" + runtime.job_summary(job, 40)

    @server.tool(title="后台任务状态", annotations=READ_ONLY, structured_output=False)
    def background_status(
        job_id: Annotated[str | None, Field(description="任务 id；不给则列出全部后台任务")] = None,
        tail_lines: Annotated[int, Field(description="显示最近多少行输出", ge=1, le=2000)] = 60,
    ) -> str:
        """查看后台任务：不给 job_id 时列出全部任务及状态；给 job_id 时显示该任务详情和最近输出。"""
        if job_id:
            job = runtime.jobs.get(job_id)
            if job is None:
                raise ToolError(f"没有 id 为 {job_id} 的后台任务")
            return runtime.job_summary(job, tail_lines)
        jobs = runtime.jobs.list()
        if not jobs:
            return "没有后台任务"
        rows = [f"共 {len(jobs)} 个后台任务（最新在前）："]
        for job in jobs:
            started = time.strftime("%m-%d %H:%M", time.localtime(job.started_at))
            rows.append(f"  {job.id}  {runtime.jobs.describe_status(job)}  {started}  {job.name}  @ {job.cwd}")
        return "\n".join(rows)

    @server.tool(
        title="停止后台任务",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False),
        structured_output=False,
    )
    def stop_background(
        job_id: Annotated[str, Field(description="要停止的任务 id")],
        force: Annotated[bool, Field(description="true 时直接 SIGKILL；默认先 SIGTERM，5 秒后仍未退出再 SIGKILL")] = False,
    ) -> str:
        """停止一个后台任务（向整个进程组发信号），返回最终状态和最后几行输出。"""
        job = runtime.jobs.get(job_id)
        if job is None:
            raise ToolError(f"没有 id 为 {job_id} 的后台任务")
        runtime.jobs.stop(job, force=force)
        return runtime.job_summary(job, 20)

    @server.tool(title="列目录", annotations=READ_ONLY, structured_output=False)
    def list_directory(
        path: Annotated[str, Field(description="目录（Linux 路径）；默认当前项目")] = ".",
        depth: Annotated[int, Field(description="递归层数，1 表示只列本层", ge=1, le=10)] = 1,
        include_hidden: Annotated[bool, Field(description="是否显示以 . 开头的文件")] = False,
        max_entries: Annotated[int, Field(description="最多显示条数", ge=1, le=5000)] = 500,
    ) -> str:
        """列出 WSL 里某个目录的内容（类型、大小、修改时间）。递归时不展开 .git、node_modules、.venv 等目录。"""
        cfg = runtime.config()
        return runtime.guard(
            files.list_directory, runtime.policy(cfg), path, depth=depth, include_hidden=include_hidden, max_entries=max_entries
        )

    @server.tool(title="读文件", annotations=READ_ONLY, structured_output=False)
    def read_file(
        path: Annotated[str, Field(description="文件路径（Linux 路径）；相对路径以当前项目为基准")],
        offset: Annotated[int, Field(description="从第几行开始（1 起）", ge=1)] = 1,
        limit: Annotated[int, Field(description="最多读取行数", ge=1, le=20000)] = 2000,
    ) -> str:
        """读取 WSL 里的文本文件，每行前带行号和制表符（行号不是文件内容）。大文件用 offset / limit 分段读。"""
        cfg = runtime.config()
        return runtime.guard(files.read_file, runtime.policy(cfg), path, offset=offset, limit=limit)

    @server.tool(title="搜索文件", annotations=READ_ONLY, structured_output=False)
    def search_files(
        pattern: Annotated[str | None, Field(description="要搜索的文本；regex=true 时按正则解释。不给则只按 glob 找文件")] = None,
        path: Annotated[str, Field(description="搜索起点目录；默认当前项目")] = ".",
        glob: Annotated[str | None, Field(description="文件名过滤，例：*.py、src/*.ts")] = None,
        regex: Annotated[bool, Field(description="pattern 是否为正则表达式")] = False,
        case_sensitive: Annotated[bool, Field(description="是否区分大小写")] = False,
        max_results: Annotated[int, Field(description="最多返回条数", ge=1, le=5000)] = 200,
    ) -> str:
        """在目录里按内容搜索（返回 文件:行号: 内容）或按文件名查找。自动跳过 .git、node_modules、.venv 与二进制文件。"""
        cfg = runtime.config()
        return runtime.guard(
            files.search_files,
            runtime.policy(cfg),
            path,
            pattern=pattern,
            glob=glob,
            regex=regex,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )

    @server.tool(
        title="写文件",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False),
        structured_output=False,
    )
    def write_file(
        path: Annotated[str, Field(description="文件路径（Linux 路径）；上级目录不存在会自动创建")],
        content: Annotated[str, Field(description="完整文件内容，按原样以 UTF-8 写入")],
    ) -> str:
        """创建新文件，或用给定内容整体覆盖已有文件（原子写入，保留原权限）。只能写在工作区等可写目录内。
        只改文件中的一部分时请用 edit_file。"""
        cfg = runtime.config()
        return runtime.guard(files.write_file, runtime.policy(cfg), path, content)

    @server.tool(
        title="修改文件",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=False,
    )
    def edit_file(
        path: Annotated[str, Field(description="要修改的文件（Linux 路径）")],
        old_text: Annotated[str, Field(description="要被替换的原文，必须与文件内容逐字符一致（含缩进）且默认只能出现一次")],
        new_text: Annotated[str, Field(description="替换后的新文本")],
        replace_all: Annotated[bool, Field(description="true 时替换所有出现处")] = False,
    ) -> str:
        """对文件做精确文本替换，返回 diff。先用 read_file 看清原文；old_text 不唯一时带上更多上下文。"""
        cfg = runtime.config()
        return runtime.guard(files.edit_file, runtime.policy(cfg), path, old_text, new_text, replace_all=replace_all)

    @server.tool(
        title="删除",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=False,
    )
    def delete_path(
        path: Annotated[str, Field(description="要删除的文件、符号链接或目录（Linux 路径）")],
        recursive: Annotated[bool, Field(description="删除非空目录时必须为 true")] = False,
    ) -> str:
        """删除工作区等可写目录内的文件、符号链接或目录（符号链接只删链接本身）。不能删除可写根目录本身。"""
        cfg = runtime.config()
        return runtime.guard(files.delete_path, runtime.policy(cfg), path, recursive=recursive)

    @server.tool(
        title="切换当前项目",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=False,
    )
    def set_current_project(
        path: Annotated[str, Field(description="新的项目目录（Linux 路径，须在工作区内）；相对路径以工作区为基准")],
    ) -> str:
        """切换“当前项目”：之后命令默认在该目录执行、相对路径以它为基准。会写回配置文件，重启后依然有效。"""
        cfg = runtime.config()
        policy = PathPolicy(
            base_dir=cfg.workspace_root,
            writable_roots=cfg.writable_roots,
            windows_mounts=tuple(runtime.sandbox.windows_mounts),
            windows_drives=runtime.sandbox.windows_drives,
        )
        project = runtime.guard(files.ensure_project_dir, policy, path, cfg.workspace_root)
        try:
            runtime.store.set_current_project(project)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        return f"当前项目已切换为 {project}（已写入 {cfg.path}）"

    return server
