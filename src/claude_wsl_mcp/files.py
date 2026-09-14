"""文件工具的实现：列目录、读、搜索、写、精确替换、删除。

所有路径先过 PathPolicy；写入走“临时文件 + rename”原子替换，保留原文件权限；
内容按原样写入，不改换行符（Windows 工具常见的 CRLF 问题不会在这里发生）。
"""

from __future__ import annotations

import difflib
import errno
import fnmatch
import os
import re
import shutil
import stat
import tempfile
import time

from .paths import PathPolicy, PolicyError, is_within

IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".gradle", ".idea", ".tox"}
MAX_LINE_CHARS = 2000
MAX_READ_OUTPUT = 200_000

_umask = os.umask(0)
os.umask(_umask)


class FileToolError(ValueError):
    pass


def _mtime(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _is_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def atomic_write(path: str, data: bytes) -> None:
    try:
        existing = os.stat(path)
    except FileNotFoundError:
        existing = None
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if existing is not None:
            os.chmod(tmp, stat.S_IMODE(existing.st_mode))
            try:
                os.chown(tmp, existing.st_uid, existing.st_gid)
            except PermissionError:
                pass
        else:
            os.chmod(tmp, 0o666 & ~_umask)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def list_directory(policy: PathPolicy, raw: str, *, depth: int, include_hidden: bool, max_entries: int) -> str:
    root = policy.check_read(raw)
    if not os.path.isdir(root):
        raise FileToolError(f"{root} 不是目录" if os.path.exists(root) else f"{root} 不存在")
    lines: list[str] = []
    skipped: set[str] = set()
    truncated = False

    def walk(directory: str, level: int) -> None:
        nonlocal truncated
        try:
            entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name))
        except PermissionError:
            lines.append(f"?          -  {'':16}  {os.path.relpath(directory, root)}/（无权限）")
            return
        for entry in entries:
            if truncated:
                return
            if not include_hidden and entry.name.startswith("."):
                continue
            if len(lines) >= max_entries:
                truncated = True
                return
            rel = os.path.relpath(entry.path, root)
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                lines.append(f"?          -  {'':16}  {rel}")
                continue
            if entry.is_symlink():
                target = os.readlink(entry.path)
                lines.append(f"l          -  {_mtime(st.st_mtime)}  {rel} -> {target}")
            elif entry.is_dir(follow_symlinks=False):
                lines.append(f"d          -  {_mtime(st.st_mtime)}  {rel}/")
                if level < depth:
                    if entry.name in IGNORED_DIRS:
                        skipped.add(entry.name)
                    else:
                        walk(entry.path, level + 1)
            else:
                lines.append(f"f {st.st_size:>10}  {_mtime(st.st_mtime)}  {rel}")

    walk(root, 1)
    header = f"{root}/（深度 {depth}，{len(lines)} 项{'，已截断' if truncated else ''}）"
    footer = []
    if skipped:
        footer.append(f"未展开的目录：{', '.join(sorted(skipped))}（需要时单独列出）")
    if not include_hidden:
        footer.append("隐藏文件未显示（include_hidden=true 可显示）")
    return "\n".join([header, *lines, *footer])


def read_file(policy: PathPolicy, raw: str, *, offset: int, limit: int) -> str:
    path = policy.check_read(raw)
    if os.path.isdir(path):
        raise FileToolError(f"{path} 是目录，请用 list_directory")
    if not os.path.exists(path):
        raise FileToolError(f"{path} 不存在")
    size = os.path.getsize(path)
    out: list[str] = []
    crlf = False
    total = 0
    chars = 0
    stopped_early = False
    with open(path, "rb") as fh:
        if _is_binary(fh.read(8192)):
            raise FileToolError(f"{path} 是二进制文件（{size} 字节），不按文本读取；可用 run_command 配合 file / xxd 查看")
        fh.seek(0)
        for total, raw_line in enumerate(fh, start=1):
            if total < offset:
                continue
            if len(out) >= limit or chars >= MAX_READ_OUTPUT:
                stopped_early = True
                break
            line = raw_line.decode("utf-8", errors="replace")
            if line.endswith("\n"):
                line = line[:-1]
            if line.endswith("\r"):
                crlf = True
                line = line[:-1]
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + f"…[本行共 {len(line)} 字符，已截断]"
            out.append(f"{total:>6}\t{line}")
            chars += len(line)
    if not out:
        return f"{path}（{size} 字节）：从第 {offset} 行起没有内容" if size else f"{path} 是空文件"
    first = offset
    last = first + len(out) - 1
    header = f"{path}（{size} 字节，第 {first}-{last} 行{'，换行符 CRLF' if crlf else ''}）"
    tail = [f"…还有更多内容，继续读取请用 offset={last + 1}"] if stopped_early else []
    return "\n".join([header, *out, *tail])


def search_files(
    policy: PathPolicy,
    raw: str,
    *,
    pattern: str | None,
    glob: str | None,
    regex: bool,
    case_sensitive: bool,
    max_results: int,
    time_budget: float = 20.0,
) -> str:
    root = policy.check_read(raw)
    if not os.path.isdir(root):
        raise FileToolError(f"{root} 不是目录")
    if not pattern and not glob:
        raise FileToolError("pattern（搜内容）和 glob（按文件名筛选）至少给一个")
    matcher = None
    if pattern:
        try:
            matcher = re.compile(pattern if regex else re.escape(pattern), 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise FileToolError(f"正则表达式无效：{exc}") from exc
    results: list[str] = []
    deadline = time.monotonic() + time_budget
    scanned = 0
    stop_reason = ""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if glob and not (fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(rel, glob)):
                continue
            if time.monotonic() > deadline:
                stop_reason = f"搜索超过 {time_budget:.0f} 秒，已停止；请缩小 path 或加 glob"
                break
            if matcher is None:
                results.append(rel)
            else:
                try:
                    if os.path.getsize(full) > 2_000_000:
                        continue
                    with open(full, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                scanned += 1
                if _is_binary(data[:8192]):
                    continue
                for line_no, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
                    if matcher.search(line):
                        results.append(f"{rel}:{line_no}: {line.strip()[:300]}")
                        if len(results) >= max_results:
                            break
            if len(results) >= max_results:
                stop_reason = f"已达到 max_results={max_results}，结果被截断"
                break
        if stop_reason:
            break
    what = f"内容匹配 {pattern!r}" if pattern else f"文件名匹配 {glob!r}"
    header = f"在 {root} 中{what}{'（限定 ' + glob + '）' if pattern and glob else ''}：{len(results)} 条"
    footer = [stop_reason] if stop_reason else []
    footer.append(f"已跳过目录：{', '.join(sorted(IGNORED_DIRS))}")
    return "\n".join([header, *results, *footer])


def write_file(policy: PathPolicy, raw: str, content: str) -> str:
    path = policy.check_write(raw)
    if os.path.isdir(path):
        raise FileToolError(f"{path} 是目录")
    existed = os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = content.encode("utf-8")
    atomic_write(path, data)
    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return f"已{'覆盖' if existed else '创建'} {path}（{len(data)} 字节，{line_count} 行）"


def edit_file(policy: PathPolicy, raw: str, old_text: str, new_text: str, *, replace_all: bool) -> str:
    path = policy.check_write(raw)
    if not os.path.isfile(path):
        raise FileToolError(f"{path} 不存在或不是普通文件")
    if old_text == "":
        raise FileToolError("old_text 不能为空；新建文件或整体重写请用 write_file")
    if old_text == new_text:
        raise FileToolError("old_text 与 new_text 相同，无需修改")
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileToolError(f"{path} 不是 UTF-8 文本（{exc}），请用 run_command 处理") from exc
    count = text.count(old_text)
    if count == 0 and "\r\n" in text and "\n" in old_text:
        crlf_old = old_text.replace("\r\n", "\n").replace("\n", "\r\n")
        crlf_count = text.count(crlf_old)
        if crlf_count:
            old_text, count = crlf_old, crlf_count
            new_text = new_text.replace("\r\n", "\n").replace("\n", "\r\n")
    if count == 0:
        raise FileToolError(
            f"在 {path} 中找不到 old_text。需要逐字符一致（含缩进、空白）；read_file 输出里行号后的制表符不属于文件内容"
        )
    if count > 1 and not replace_all:
        raise FileToolError(f"old_text 在 {path} 中出现 {count} 次；请带上更多上下文让它唯一，或设 replace_all=true")
    updated = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
    atomic_write(path, updated.encode("utf-8"))
    diff = list(difflib.unified_diff(text.splitlines(), updated.splitlines(), fromfile=path, tofile=path, lineterm="", n=2))
    if len(diff) > 120:
        diff = diff[:120] + [f"…（diff 共 {len(diff)} 行，已截断）"]
    return "\n".join([f"已修改 {path}：替换 {count if replace_all else 1} 处", *diff])


def delete_path(policy: PathPolicy, raw: str, *, recursive: bool) -> str:
    path = policy.check_write(raw, follow_final_symlink=False)
    if path == "/" or any(path == root for root in policy.writable_roots):
        raise FileToolError(f"拒绝删除可写根目录本身：{path}")
    if not os.path.lexists(path):
        raise FileToolError(f"{path} 不存在")
    if os.path.islink(path) or not os.path.isdir(path):
        kind = "符号链接" if os.path.islink(path) else "文件"
        os.unlink(path)
        return f"已删除{kind} {path}"
    if not recursive:
        try:
            os.rmdir(path)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise FileToolError(f"{path} 是非空目录；确认连同内容一起删除请设 recursive=true") from exc
            raise
        return f"已删除空目录 {path}"
    count = sum(len(files) + len(dirs) for _, dirs, files in os.walk(path))
    shutil.rmtree(path)
    return f"已递归删除目录 {path}（含 {count} 个文件/子目录）"


def ensure_project_dir(policy: PathPolicy, raw: str, workspace_root: str) -> str:
    path = policy.resolve(raw)
    if not os.path.isdir(path):
        raise FileToolError(f"{path} 不存在或不是目录")
    if not any(is_within(path, root) for root in policy.writable_roots):
        raise PolicyError(f"{path} 不在工作区（{workspace_root}）或额外可写目录内，不能设为当前项目")
    return path
