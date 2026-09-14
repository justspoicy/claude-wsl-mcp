"""配置：TOML 文件 + 默认值。

工作区相关字段（workspace_root / current_project / extra_writable_roots / limits）按文件 mtime 热加载，
改完下一次工具调用即生效；[sandbox] 段只在服务器启动时生效，改了要重启 Claude Desktop。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

WINDOWS_DRIVE_MODES = ("hidden", "readonly", "readwrite")

CONFIG_TEMPLATE = """\
# claude-wsl-mcp 配置。修改后下一次工具调用即生效；[sandbox] 段需要重启 Claude Desktop 才生效。

# 固定工作区：文件工具只允许在它（以及 extra_writable_roots）下面写入、删除
workspace_root = {workspace_root}

# 当前项目：命令默认在这里执行，相对路径以它为基准。换项目只改这一行，
# 或者直接对 Claude 说“切换到 xxx 项目”（set_current_project 工具会改写这一行）
current_project = {current_project}

# 额外允许文件工具写入的目录
extra_writable_roots = ["/tmp"]

# 命令是否用登录 shell（bash -lc）执行，以加载 /etc/profile 与 ~/.profile 里的 PATH
login_shell = true

[sandbox]
# Windows 盘（/mnt/c 等 drvfs 挂载）在 MCP 里的形态：
#   "hidden"    完全看不到（默认）
#   "readonly"  能读不能写
#   "readwrite" 不限制
windows_drives = "hidden"

# 是否允许在 WSL 里调用 Windows 程序（cmd.exe、powershell.exe、explorer.exe 等）
windows_interop = false

# 额外隐藏的路径：目录盖一层空 tmpfs，文件/套接字盖 /dev/null。例如 ["/run/docker.sock"]
hide_paths = []

[limits]
default_timeout_seconds = 120
max_timeout_seconds = 3600
# 单个输出流（stdout / stderr 各自）返回给 Claude 的最大字节数，超出部分落盘并给出路径
max_output_bytes = 30000
"""


class ConfigError(ValueError):
    pass


def default_config_path() -> Path:
    env = os.environ.get("CLAUDE_WSL_MCP_CONFIG")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "claude-wsl-mcp" / "config.toml"


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "claude-wsl-mcp"


def toml_string(value: str) -> str:
    # JSON 字符串转义是 TOML 基本字符串的子集
    return json.dumps(value, ensure_ascii=False)


def render_default_config(workspace_root: str, current_project: str) -> str:
    return CONFIG_TEMPLATE.format(
        workspace_root=toml_string(workspace_root),
        current_project=toml_string(current_project),
    )


@dataclass(frozen=True)
class SandboxConfig:
    windows_drives: str = "hidden"
    windows_interop: bool = False
    hide_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class LimitsConfig:
    default_timeout_seconds: int = 120
    max_timeout_seconds: int = 3600
    max_output_bytes: int = 30000


@dataclass(frozen=True)
class Config:
    path: Path
    exists: bool
    workspace_root: str
    current_project: str
    extra_writable_roots: tuple[str, ...]
    login_shell: bool
    sandbox: SandboxConfig
    limits: LimitsConfig

    @property
    def writable_roots(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.workspace_root, *self.extra_writable_roots)))


def _abs_dir(value: object, key: str, *, base: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} 必须是非空字符串")
    path = os.path.expanduser(value.strip())
    if not os.path.isabs(path):
        if base is None:
            raise ConfigError(f"{key} 必须是绝对路径：{value}")
        path = os.path.join(base, path)
    return os.path.realpath(path)


def _within(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root.rstrip("/") + "/")


def parse_config(text: str, path: Path, *, exists: bool = True) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} 不是合法的 TOML：{exc}") from exc

    home = os.path.expanduser("~")
    default_root = os.path.join(home, "project")
    if not os.path.isdir(default_root):
        default_root = home

    workspace_root = _abs_dir(data.get("workspace_root", default_root), "workspace_root")
    current_project = _abs_dir(data.get("current_project", workspace_root), "current_project", base=workspace_root)
    extra = data.get("extra_writable_roots", ["/tmp"])
    if not isinstance(extra, list):
        raise ConfigError("extra_writable_roots 必须是字符串数组")
    extra_roots = tuple(_abs_dir(item, "extra_writable_roots") for item in extra)
    login_shell = data.get("login_shell", True)
    if not isinstance(login_shell, bool):
        raise ConfigError("login_shell 必须是 true 或 false")

    roots = (workspace_root, *extra_roots)
    if not any(_within(current_project, root) for root in roots):
        raise ConfigError(
            f"current_project（{current_project}）不在 workspace_root 或 extra_writable_roots 之内；"
            "请把它移到工作区里，或把它的上级目录加入 extra_writable_roots"
        )

    sb = data.get("sandbox", {})
    drives = sb.get("windows_drives", "hidden")
    if drives not in WINDOWS_DRIVE_MODES:
        raise ConfigError(f"sandbox.windows_drives 只能是 {' / '.join(WINDOWS_DRIVE_MODES)}，当前是 {drives!r}")
    interop = sb.get("windows_interop", False)
    if not isinstance(interop, bool):
        raise ConfigError("sandbox.windows_interop 必须是 true 或 false")
    hide = sb.get("hide_paths", [])
    if not isinstance(hide, list) or not all(isinstance(p, str) and os.path.isabs(p) for p in hide):
        raise ConfigError("sandbox.hide_paths 必须是绝对路径字符串数组")

    lim = data.get("limits", {})
    limits = LimitsConfig(
        default_timeout_seconds=int(lim.get("default_timeout_seconds", 120)),
        max_timeout_seconds=int(lim.get("max_timeout_seconds", 3600)),
        max_output_bytes=int(lim.get("max_output_bytes", 30000)),
    )
    if limits.default_timeout_seconds < 1 or limits.max_timeout_seconds < limits.default_timeout_seconds:
        raise ConfigError("limits 超时设置不合理：需要 1 <= default_timeout_seconds <= max_timeout_seconds")
    if limits.max_output_bytes < 1000:
        raise ConfigError("limits.max_output_bytes 至少 1000")

    return Config(
        path=path,
        exists=exists,
        workspace_root=workspace_root,
        current_project=current_project,
        extra_writable_roots=extra_roots,
        login_shell=login_shell,
        sandbox=SandboxConfig(drives, interop, tuple(hide)),
        limits=limits,
    )


class ConfigStore:
    """按 mtime 缓存配置；文件被改动后下一次 get() 重新解析。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._config: Config | None = None

    def _current_stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def get(self) -> Config:
        with self._lock:
            stamp = self._current_stamp()
            if self._config is None or stamp != self._stamp:
                if stamp is None:
                    self._config = parse_config("", self.path, exists=False)
                else:
                    self._config = parse_config(self.path.read_text(encoding="utf-8"), self.path)
                self._stamp = stamp
            return self._config

    def set_current_project(self, project: str) -> None:
        """只改写顶层的 current_project 这一行，保留文件其余内容与注释。"""
        with self._lock:
            text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
            line = f"current_project = {toml_string(project)}"
            table = re.search(r"^\s*\[", text, re.M)
            head, tail = (text[: table.start()], text[table.start():]) if table else (text, "")
            pattern = re.compile(r"^current_project\s*=.*$", re.M)
            if pattern.search(head):
                head = pattern.sub(lambda _m: line, head, count=1)
            else:
                if head and not head.endswith("\n"):
                    head += "\n"
                head += line + "\n"
            new_text = head + tail
            parse_config(new_text, self.path)  # 写之前先确认改完仍是合法配置
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(new_text)
                if self.path.exists():
                    os.chmod(tmp, self.path.stat().st_mode & 0o7777)
                os.replace(tmp, self.path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            self._stamp = None
