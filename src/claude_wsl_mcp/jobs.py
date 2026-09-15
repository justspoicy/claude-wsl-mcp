"""后台任务：开发服务器、watch、长时间构建等放到后台跑。

输出写日志文件，登记表存盘，所以 Claude Desktop 重启（本服务器随之重启）后仍能查看、停止之前启动的任务。
判断进程是否还活着时核对 /proc/<pid>/stat 里的启动时刻，避免 PID 被复用后误判或误杀。
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from .shell import COMMAND_ENV, EVAL_COMMAND, NON_INTERACTIVE_ENV

# $1 = 退出码文件。命令经环境变量交给内层 bash（见 shell.EVAL_COMMAND），哪一层的命令行参数里都没有命令原文
_WRAPPER = '{shell} {command}; code=$?; printf "%s\\n" "$code" > "$1"; exit "$code"'


@dataclass
class Job:
    id: str
    name: str
    command: str
    cwd: str
    pid: int
    start_ticks: int
    started_at: float
    log_path: str
    exit_path: str
    stopped_at: float | None = None


def _start_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            rest = fh.read().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None
    if rest[0] == "Z":
        return None
    return int(rest[19])


class JobManager:
    def __init__(self, state_dir: Path):
        self.dir = state_dir / "jobs"
        self.registry = state_dir / "jobs.json"
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Job]:
        try:
            raw = json.loads(self.registry.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return {item["id"]: Job(**item) for item in raw}

    def _save(self, jobs: dict[str, Job]) -> None:
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.registry.parent, prefix=".jobs.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([asdict(j) for j in jobs.values()], fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.registry)

    def start(self, command: str, *, cwd: str, env: Mapping[str, str], name: str | None, login_shell: bool) -> Job:
        self.dir.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex[:8]
        log_path = self.dir / f"{job_id}.log"
        exit_path = self.dir / f"{job_id}.exit"
        wrapper = _WRAPPER.format(shell="bash -lc" if login_shell else "bash -c", command=shlex.quote(EVAL_COMMAND))
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                ["/bin/bash", "-c", wrapper, "claude-wsl-mcp-job", str(exit_path)],
                cwd=cwd,
                env={**NON_INTERACTIVE_ENV, **env, COMMAND_ENV: command},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        job = Job(
            id=job_id,
            name=name or command.strip().splitlines()[0][:60],
            command=command,
            cwd=cwd,
            pid=proc.pid,
            start_ticks=_start_ticks(proc.pid) or -1,
            started_at=time.time(),
            log_path=str(log_path),
            exit_path=str(exit_path),
        )
        with self._lock:
            self._procs[job_id] = proc
            jobs = self._load()
            jobs[job_id] = job
            self._prune(jobs)
            self._save(jobs)
        return job

    def _prune(self, jobs: dict[str, Job], keep_finished: int = 50) -> None:
        finished = sorted((j for j in jobs.values() if not self.is_running(j)), key=lambda j: j.started_at)
        for old in finished[: max(0, len(finished) - keep_finished)]:
            jobs.pop(old.id, None)
            for path in (old.log_path, old.exit_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def is_running(self, job: Job) -> bool:
        proc = self._procs.get(job.id)
        if proc is not None:
            return proc.poll() is None  # poll 顺带回收僵尸进程
        # 上一个服务器实例启动的任务：只认启动时刻一致的同号进程
        ticks = _start_ticks(job.pid)
        return ticks is not None and ticks == job.start_ticks

    def exit_code(self, job: Job) -> int | None:
        try:
            return int(Path(job.exit_path).read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def describe_status(self, job: Job) -> str:
        if self.is_running(job):
            return "运行中"
        code = self.exit_code(job)
        if code is not None:
            return f"已退出（exit_code={code}）"
        return "已停止（被信号终止，无退出码）"

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._load().values(), key=lambda j: j.started_at, reverse=True)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._load().get(job_id)

    def tail(self, job: Job, lines: int, max_bytes: int = 256_000) -> tuple[str, int]:
        try:
            size = os.path.getsize(job.log_path)
            with open(job.log_path, "rb") as fh:
                fh.seek(max(0, size - max_bytes))
                data = fh.read()
        except FileNotFoundError:
            return "", 0
        text = data.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:]), size

    def stop(self, job: Job, *, grace_seconds: float = 5.0, force: bool = False) -> str:
        if not self.is_running(job):
            return self.describe_status(job)
        try:
            os.killpg(job.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + (0 if force else grace_seconds)
        while self.is_running(job) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.is_running(job):
            try:
                os.killpg(job.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(0.3)
        with self._lock:
            jobs = self._load()
            if job.id in jobs:
                jobs[job.id].stopped_at = time.time()
                self._save(jobs)
        return self.describe_status(job)
