"""沙箱：让本进程及其全部子进程碰不到 Windows 宿主机文件。

需要 root（Claude Desktop 用 `wsl.exe -u root` 拉起）。步骤：
1. unshare 出私有挂载命名空间：之后的挂载改动只影响本进程树，用户自己开的 WSL 终端不受影响；
2. Windows 盘（drvfs）按配置卸载（hidden）或改只读（readonly）；
3. 禁用互操作：/run/WSL 盖一层空 tmpfs 并去掉 WSL_INTEROP，cmd.exe / powershell.exe 等拉不起来；
4. /proc/sys、/sys 改只读，堵住 modprobe、uevent_helper 这类借内核回调在命名空间外执行命令的路；
5. 从能力边界集去掉 sys_admin、sys_ptrace 等：子进程即使是 root，也无法重新挂载、nsenter、
   或经 /proc/<pid>/root 穿到宿主命名空间。

不在防护范围内的（root 可以请特权守护进程代劳）：docker/containerd 套接字、systemd-run、cron。
需要时把对应套接字写进 hide_paths。任何一步失败都拒绝启动（fail closed），不会降级运行。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
from dataclasses import dataclass, field
from typing import Mapping

from .config import SandboxConfig

CLONE_NEWNS = 0x00020000
MS_RDONLY = 0x1
MS_NOSUID = 0x2
MS_NODEV = 0x4
MS_NOEXEC = 0x8
MS_REMOUNT = 0x20
MS_NOATIME = 0x400
MS_NODIRATIME = 0x800
MS_BIND = 0x1000
MS_REC = 0x4000
MS_PRIVATE = 0x40000
MS_RELATIME = 0x200000
MS_STRICTATIME = 0x1000000
MNT_DETACH = 0x2
PR_CAPBSET_READ = 23
PR_CAPBSET_DROP = 24

_PRESERVED_FLAGS = {
    "nosuid": MS_NOSUID,
    "nodev": MS_NODEV,
    "noexec": MS_NOEXEC,
    "noatime": MS_NOATIME,
    "nodiratime": MS_NODIRATIME,
    "relatime": MS_RELATIME,
    "strictatime": MS_STRICTATIME,
}

# 能力编号见 linux/capability.h
DROPPED_CAPABILITIES = {
    "dac_read_search": 2,  # open_by_handle_at 越过挂载视图
    "sys_module": 16,
    "sys_rawio": 17,
    "sys_ptrace": 19,  # /proc/<pid>/root、注入宿主命名空间里的进程
    "sys_admin": 21,  # mount / umount / setns
    "sys_boot": 22,
    "perfmon": 38,
    "bpf": 39,
}

_DRIVE_SOURCE = re.compile(r"^[A-Za-z]:\\")
_DRIVE_MOUNTPOINT = re.compile(r"^/mnt/[A-Za-z]$")
_WINDOWS_PATH_ENTRY = re.compile(r"^/mnt/[A-Za-z](/|$)")


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class MountEntry:
    mount_point: str
    mount_options: tuple[str, ...]
    optional_fields: tuple[str, ...]
    fs_type: str
    source: str
    super_options: str


@dataclass
class SandboxReport:
    enabled: bool
    windows_drives: str = "readwrite"
    windows_interop: bool = True
    host_mount_ns: str = ""
    mount_ns: str = ""
    windows_mounts: list[str] = field(default_factory=list)
    hidden_paths: list[str] = field(default_factory=list)
    read_only_paths: list[str] = field(default_factory=list)
    dropped_capabilities: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def parse_mountinfo(text: str) -> list[MountEntry]:
    entries = []
    for line in text.splitlines():
        parts = line.split(" ")
        try:
            sep = parts.index("-", 6)
        except ValueError:
            continue
        entries.append(
            MountEntry(
                mount_point=_unescape(parts[4]),
                mount_options=tuple(parts[5].split(",")),
                optional_fields=tuple(parts[6:sep]),
                fs_type=parts[sep + 1] if len(parts) > sep + 1 else "",
                source=_unescape(parts[sep + 2]) if len(parts) > sep + 2 else "",
                super_options=parts[sep + 3] if len(parts) > sep + 3 else "",
            )
        )
    return entries


def is_windows_mount(entry: MountEntry) -> bool:
    if entry.fs_type == "drvfs":
        return True
    if entry.fs_type in ("9p", "virtiofs"):
        return (
            "aname=drvfs" in entry.super_options
            or bool(_DRIVE_SOURCE.match(entry.source))
            or bool(_DRIVE_MOUNTPOINT.match(entry.mount_point))
        )
    return False


def _read_mountinfo() -> list[MountEntry]:
    with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as fh:
        return parse_mountinfo(fh.read())


def _preserved_flags(entry: MountEntry | None) -> int:
    if entry is None:
        return 0
    flags = 0
    for opt in entry.mount_options:
        flags |= _PRESERVED_FLAGS.get(opt, 0)
    return flags


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


class _Libc:
    def __init__(self) -> None:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        libc.unshare.argtypes = [ctypes.c_int]
        libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
        libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        self._libc = libc

    @staticmethod
    def _b(value: str | None) -> bytes | None:
        return None if value is None else os.fsencode(value)

    def _check(self, ret: int, what: str) -> None:
        if ret != 0:
            err = ctypes.get_errno()
            raise SandboxError(f"{what} 失败：{os.strerror(err)}（errno {err}）")

    def unshare(self, flags: int) -> None:
        self._check(self._libc.unshare(flags), "unshare")

    def mount(self, source: str | None, target: str, fstype: str | None, flags: int, data: str | None = None) -> None:
        self._check(
            self._libc.mount(self._b(source), self._b(target), self._b(fstype), flags, self._b(data)),
            f"mount {target}",
        )

    def umount_detach(self, target: str) -> None:
        self._check(self._libc.umount2(self._b(target), MNT_DETACH), f"umount {target}")

    def drop_bounding_capability(self, cap: int) -> bool:
        if self._libc.prctl(PR_CAPBSET_READ, cap, 0, 0, 0) < 0:
            return False  # 内核不认识这个能力
        self._check(self._libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0), f"drop capability {cap}")
        return True


def disabled_report(cfg: SandboxConfig, reason: str) -> SandboxReport:
    report = SandboxReport(enabled=False, windows_drives="readwrite", windows_interop=True)
    report.windows_mounts = sorted(e.mount_point for e in _read_mountinfo() if is_windows_mount(e))
    report.warnings.append(reason)
    return report


def apply(cfg: SandboxConfig, *, chdir_to: str) -> SandboxReport:
    if cfg.windows_drives == "readwrite" and cfg.windows_interop and not cfg.hide_paths:
        return disabled_report(cfg, "配置允许读写 Windows 盘且允许互操作，未建立沙箱")
    if os.geteuid() != 0:
        raise SandboxError("建立沙箱需要 root：请用 `wsl.exe -u root` 拉起本服务器")

    libc = _Libc()
    report = SandboxReport(enabled=True, windows_drives=cfg.windows_drives, windows_interop=cfg.windows_interop)
    report.host_mount_ns = os.readlink("/proc/self/ns/mnt")
    libc.unshare(CLONE_NEWNS)
    libc.mount(None, "/", None, MS_REC | MS_PRIVATE)
    report.mount_ns = os.readlink("/proc/self/ns/mnt")
    if report.mount_ns == report.host_mount_ns:
        raise SandboxError("挂载命名空间没有隔离出来")

    entries = _read_mountinfo()
    if any(opt.startswith("shared:") for e in entries for opt in e.optional_fields):
        raise SandboxError("仍有挂载处于共享传播状态，继续操作会影响宿主，已中止")

    windows = [e for e in entries if is_windows_mount(e)]
    report.windows_mounts = sorted(e.mount_point for e in windows)
    for entry in sorted(windows, key=lambda e: e.mount_point.count("/"), reverse=True):
        if cfg.windows_drives == "hidden":
            libc.umount_detach(entry.mount_point)
            # 卸载后露出的是根文件系统上的空目录，写进去会“成功”却不是 Windows 盘，容易误导；
            # 盖一层只读空 tmpfs，让写操作明确报 Read-only file system
            libc.mount("tmpfs", entry.mount_point, "tmpfs", MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, "size=4k,mode=0755")
        elif cfg.windows_drives == "readonly":
            libc.mount(None, entry.mount_point, None, MS_REMOUNT | MS_BIND | MS_RDONLY | _preserved_flags(entry))

    if not cfg.windows_interop and os.path.isdir("/run/WSL"):
        libc.mount("tmpfs", "/run/WSL", "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, "size=16k,mode=0755")
        report.hidden_paths.append("/run/WSL")

    libc.mount("/proc/sys", "/proc/sys", None, MS_BIND | MS_REC)
    libc.mount(None, "/proc/sys", None, MS_REMOUNT | MS_BIND | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC)
    report.read_only_paths.append("/proc/sys")
    sys_entry = next((e for e in _read_mountinfo() if e.mount_point == "/sys"), None)
    if sys_entry is not None:
        libc.mount(None, "/sys", None, MS_REMOUNT | MS_BIND | MS_RDONLY | _preserved_flags(sys_entry))
        report.read_only_paths.append("/sys")

    for path in cfg.hide_paths:
        if not os.path.lexists(path):
            report.warnings.append(f"hide_paths 里的 {path} 不存在，已跳过")
            continue
        if os.path.isdir(path):
            libc.mount("tmpfs", path, "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, "size=16k,mode=0755")
        else:
            libc.mount("/dev/null", path, None, MS_BIND)
        report.hidden_paths.append(path)

    for entry in _read_mountinfo():
        if not is_windows_mount(entry):
            continue
        if cfg.windows_drives == "hidden":
            raise SandboxError(f"{entry.mount_point} 卸载后仍然可见")
        if cfg.windows_drives == "readonly" and "ro" not in entry.mount_options:
            raise SandboxError(f"{entry.mount_point} 没能改成只读")

    # 启动目录可能就在刚隐藏的 Windows 盘上（wsl.exe 会把 Windows 当前目录翻译成 /mnt/...）
    for target in (chdir_to, os.path.expanduser("~"), "/"):
        try:
            os.chdir(target)
            break
        except OSError:
            continue

    for name, cap in DROPPED_CAPABILITIES.items():
        if libc.drop_bounding_capability(cap):
            report.dropped_capabilities.append(name)
    return report


def sanitized_environment(base: Mapping[str, str], report: SandboxReport) -> dict[str, str]:
    """子进程环境：去掉互操作套接字，并把 PATH 里指向 Windows 盘的目录剔除，
    避免 npm、python 之类的命令解析到 /mnt/c/... 下的 Windows 程序。"""
    env = dict(base)
    env["CLAUDE_WSL_MCP"] = "1"
    if not report.enabled:
        return env
    if not report.windows_interop:
        env.pop("WSL_INTEROP", None)
    if not report.windows_interop or report.windows_drives == "hidden":
        kept = [
            entry
            for entry in env.get("PATH", "").split(":")
            if entry
            and not _WINDOWS_PATH_ENTRY.match(entry)
            and not any(_is_within(entry, mp) for mp in report.windows_mounts)
        ]
        env["PATH"] = ":".join(dict.fromkeys(kept)) or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return env
