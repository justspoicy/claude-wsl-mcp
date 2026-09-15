"""命令执行：在 WSL 里用 bash 跑命令，分别收集 stdout / stderr 与退出码。

- 每条命令是独立进程组；超时或客户端取消时整组终止，不留孤儿；
- 输出超过上限时只把头尾返回给 Claude，完整内容落盘并给出路径，可再用 read_file 查看；
- 默认不给标准输入（/dev/null），避免 git、apt 之类的交互提示把调用挂住。
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Mapping

NON_INTERACTIVE_ENV = {
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "DEBIAN_FRONTEND": "noninteractive",
    "TERM": "dumb",
}

# 命令经这个环境变量交给 bash 再 eval，不放进命令行参数：放进参数的话整条命令会出现在 ps 里，
# `ps | grep` 查进程会匹配到执行命令的这层 bash 自己，而在终端里直接敲命令不会这样。
# 先取消导出再 eval，命令起的子进程不继承它。与 bash -c 相比，只有语法错误提示的前缀由 "-c:" 变成 "eval:"。
COMMAND_ENV = "CLAUDE_WSL_MCP_COMMAND"
EVAL_COMMAND = f'export -n {COMMAND_ENV}; eval "${COMMAND_ENV}"'


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


class StreamCapture:
    """边读边收：不超限时全留内存；超限后开始落盘，内存里只保留开头一半和最新的结尾。"""

    def __init__(self, name: str, limit: int, spill_dir: Path, run_id: str):
        self.name = name
        self.limit = limit
        self.spill_dir = spill_dir
        self.run_id = run_id
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.spill_path: Path | None = None
        self._spill: BinaryIO | None = None

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self._spill is None:
            if len(self.head) + len(chunk) <= self.limit:
                self.head += chunk
                return
            self.spill_dir.mkdir(parents=True, exist_ok=True)
            self.spill_path = self.spill_dir / f"{self.run_id}.{self.name}.log"
            self._spill = open(self.spill_path, "wb")
            self._spill.write(self.head)
            self._spill.write(chunk)
            combined = bytes(self.head) + chunk
            half = self.limit // 2
            self.head = bytearray(combined[:half])
            self.tail = bytearray(combined[half:])
        else:
            self._spill.write(chunk)
            self.tail += chunk
        keep = self.limit - len(self.head)
        if len(self.tail) > keep:
            del self.tail[: len(self.tail) - keep]

    def close(self) -> None:
        if self._spill is not None:
            self._spill.close()

    def text(self) -> str:
        if self.spill_path is None:
            return decode(bytes(self.head))
        head, tail = bytes(self.head), bytes(self.tail)
        # 尽量在换行处截断，避免半行和被切开的多字节字符
        cut = head.rfind(b"\n")
        if cut >= len(head) // 2:
            head = head[: cut + 1]
        cut = tail.find(b"\n")
        if 0 <= cut < len(tail) // 2:
            tail = tail[cut + 1:]
        omitted = self.total - len(head) - len(tail)
        return (
            decode(head)
            + f"…[中间省略 {omitted} 字节，完整输出共 {self.total} 字节：{self.spill_path}]…\n"
            + decode(tail)
        )


@dataclass
class CommandResult:
    command: str
    cwd: str
    exit_code: int | None
    timed_out: bool
    duration: float
    stdout: StreamCapture
    stderr: StreamCapture
    notes: list[str] = field(default_factory=list)

    @property
    def signal_name(self) -> str | None:
        if self.exit_code is not None and self.exit_code < 0:
            try:
                return signal.Signals(-self.exit_code).name
            except ValueError:
                return f"signal {-self.exit_code}"
        return None


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _wait_for_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
    """等进程本身退出。不能用 proc.wait()：它要等所有输出管道关闭才返回，
    命令里用 `xxx &` 启动的后台进程一直占着管道，就会被拖到超时。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    delay = 0.005
    while proc.returncode is None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.1)
    return True


def _close_pipes(proc: asyncio.subprocess.Process) -> None:
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()


def bash_argv(login_shell: bool) -> list[str]:
    return ["/bin/bash", "-lc" if login_shell else "-c", EVAL_COMMAND]


async def run_command(
    command: str,
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout: float,
    login_shell: bool,
    max_output_bytes: int,
    spill_dir: Path,
    stdin_text: str | None = None,
) -> CommandResult:
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    stdout = StreamCapture("stdout", max_output_bytes, spill_dir, run_id)
    stderr = StreamCapture("stderr", max_output_bytes, spill_dir, run_id)
    full_env = {**NON_INTERACTIVE_ENV, **env, COMMAND_ENV: command}
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *bash_argv(login_shell),
        cwd=cwd,
        env=full_env,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    result = CommandResult(command, cwd, None, False, 0.0, stdout, stderr)

    async def pump(stream: asyncio.StreamReader, capture: StreamCapture) -> None:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            capture.feed(chunk)

    async def feed_stdin() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write((stdin_text or "").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    tasks = [asyncio.create_task(pump(proc.stdout, stdout)), asyncio.create_task(pump(proc.stderr, stderr))]
    if stdin_text is not None:
        tasks.append(asyncio.create_task(feed_stdin()))
    try:
        if not await _wait_for_exit(proc, timeout):
            result.timed_out = True
            _signal_group(proc.pid, signal.SIGTERM)
            if not await _wait_for_exit(proc, 3):
                _signal_group(proc.pid, signal.SIGKILL)
                await _wait_for_exit(proc, 5)
            _signal_group(proc.pid, signal.SIGKILL)  # 组里没响应 SIGTERM 的残留
        _, pending = await asyncio.wait(tasks, timeout=2)
        if pending:
            result.notes.append(
                "命令本身已结束，但它启动的后台进程还占着输出管道，已停止读取这部分输出（后台进程未被终止）；"
                "需要长期运行的程序请改用 start_background"
            )
            for task in pending:
                task.cancel()
            _close_pipes(proc)
    except BaseException:
        # 客户端取消或意外异常：同步杀掉整个进程组，不能 await（已处于取消状态）
        _signal_group(proc.pid, signal.SIGKILL)
        for task in tasks:
            task.cancel()
        raise
    finally:
        stdout.close()
        stderr.close()
    result.exit_code = proc.returncode
    result.duration = time.monotonic() - started
    return result


def prune_spill_dir(spill_dir: Path, keep: int = 200) -> None:
    try:
        files = sorted(spill_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except FileNotFoundError:
        return
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
