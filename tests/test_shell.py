import asyncio
import contextlib
import os
import signal
import time

import pytest

from claude_wsl_mcp import shell


def run(command, tmp_path, **overrides):
    params = dict(
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout=10,
        login_shell=False,
        max_output_bytes=2000,
        spill_dir=tmp_path / "spill",
    )
    params.update(overrides)
    return asyncio.run(shell.run_command(command, **params))


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def test_exit_code_and_separate_streams(tmp_path):
    res = run("echo out; echo err >&2; exit 3", tmp_path)
    assert res.exit_code == 3
    assert res.stdout.text() == "out\n"
    assert res.stderr.text() == "err\n"


@pytest.mark.parametrize("login_shell", [False, True])
def test_command_text_not_in_process_args(tmp_path, login_shell):
    # 终端里直接敲的命令不会出现在 ps 里；执行命令的这层 bash 也不能让 `ps | grep` 匹配到自己
    res = run(
        "xargs -0 echo < /proc/$$/cmdline; printenv CLAUDE_WSL_MCP_COMMAND || echo unset  # marker-5d1c",
        tmp_path,
        login_shell=login_shell,
    )
    lines = res.stdout.text().splitlines()
    assert lines[0].startswith("/bin/bash") and "marker-5d1c" not in lines[0], lines
    assert lines[1] == "unset"  # 传命令用的环境变量不留给命令起的子进程


def test_cwd_and_stdin(tmp_path):
    res = run("pwd; cat", tmp_path, stdin_text="hello")
    assert res.stdout.text() == f"{tmp_path}\nhello"


def test_stdin_is_empty_by_default(tmp_path):
    res = run("cat; echo done", tmp_path)
    assert res.exit_code == 0 and res.stdout.text() == "done\n"


def test_timeout_kills_whole_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    res = run(f"sleep 60 & echo $! > {pid_file}; wait", tmp_path, timeout=1)
    assert res.timed_out
    time.sleep(0.3)
    assert not alive(int(pid_file.read_text()))


def test_large_output_is_truncated_and_spilled(tmp_path):
    res = run("seq 1 20000", tmp_path, max_output_bytes=2000)
    text = res.stdout.text()
    assert text.startswith("1\n2\n3\n")
    assert text.rstrip().endswith("20000")
    assert "中间省略" in text
    assert res.stdout.spill_path.read_text().count("\n") == 20000


def test_background_child_holding_pipe_does_not_block(tmp_path):
    res = run("sleep 30 & echo $!", tmp_path, timeout=10)
    pid = int(res.stdout.text().split()[0])
    try:
        assert res.exit_code == 0
        assert not res.timed_out
        assert res.duration < 5
        assert res.notes
        assert alive(pid), "命令正常结束时不应该杀掉它启动的后台进程"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
