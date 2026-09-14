"""路径策略：文件类工具（读、写、改、删）的边界。

- 一律按 WSL 里的 Linux 路径解释；传入 C:\\... 这类 Windows 路径直接拒绝并提示；
- 相对路径以当前项目目录为基准；
- 读：整个 Linux 文件系统，但 Windows 盘按沙箱配置处理（hidden 不可读）；
- 写/删：只允许在可写根目录（workspace_root + extra_writable_roots）之内，符号链接先解析再判断，
  防止借链接写到边界外。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

_WINDOWS_STYLE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_WINDOWS_MOUNT_PATH = re.compile(r"^/mnt/[A-Za-z](/|$)")


class PolicyError(ValueError):
    pass


def is_within(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root.rstrip("/") + "/")


@dataclass(frozen=True)
class PathPolicy:
    base_dir: str
    writable_roots: tuple[str, ...]
    windows_mounts: tuple[str, ...]
    windows_drives: str  # hidden / readonly / readwrite

    def absolute(self, raw: str) -> str:
        if raw is None or not str(raw).strip():
            raise PolicyError("路径不能为空")
        raw = str(raw)
        if "\x00" in raw:
            raise PolicyError("路径里含有 NUL 字符")
        if _WINDOWS_STYLE.match(raw):
            raise PolicyError(
                f"{raw} 是 Windows 路径。本服务器只操作 WSL 里的 Linux 路径（例如 {self.base_dir}）"
            )
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            path = os.path.join(self.base_dir, path)
        return os.path.normpath(path)

    def resolve(self, raw: str, *, follow_final_symlink: bool = True) -> str:
        path = self.absolute(raw)
        if follow_final_symlink:
            return os.path.realpath(path)
        parent, name = os.path.split(path)
        if not name:
            return os.path.realpath(path)
        return os.path.join(os.path.realpath(parent), name)

    def _on_windows_drive(self, path: str) -> bool:
        return bool(_WINDOWS_MOUNT_PATH.match(path)) or any(is_within(path, mp) for mp in self.windows_mounts)

    def check_read(self, raw: str) -> str:
        path = self.resolve(raw)
        if self._on_windows_drive(path) and self.windows_drives == "hidden":
            raise PolicyError(f"{path} 在 Windows 盘上，按配置对 MCP 隐藏（sandbox.windows_drives = \"hidden\"）")
        return path

    def check_write(self, raw: str, *, follow_final_symlink: bool = True) -> str:
        path = self.resolve(raw, follow_final_symlink=follow_final_symlink)
        if self._on_windows_drive(path) and self.windows_drives != "readwrite":
            raise PolicyError(
                f"拒绝写入 {path}：Windows 盘按配置不可写（sandbox.windows_drives = \"{self.windows_drives}\"）"
            )
        if not any(is_within(path, root) for root in self.writable_roots):
            raise PolicyError(
                f"拒绝写入 {path}：不在可写目录内（{', '.join(self.writable_roots)}）。"
                "需要放开请让用户修改配置里的 workspace_root / extra_writable_roots"
            )
        return path
