import os
import time

from claude_wsl_mcp.jobs import JobManager


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_job_survives_manager_restart_and_can_be_stopped(tmp_path):
    state = tmp_path / "state"
    manager = JobManager(state)
    job = manager.start("echo ready; sleep 60", cwd=str(tmp_path), env=dict(os.environ), name="demo", login_shell=False)
    assert wait_until(lambda: "ready" in manager.tail(job, 10)[0])
    assert manager.is_running(job)

    restarted = JobManager(state)  # 模拟 Claude Desktop 重启后新起的服务器实例
    same = restarted.get(job.id)
    assert same is not None and same.name == "demo"
    assert restarted.is_running(same)
    restarted.stop(same, grace_seconds=2)
    assert not restarted.is_running(same)
    assert not wait_until(lambda: manager.is_running(job), timeout=1)


def test_job_exit_code_is_recorded(tmp_path):
    manager = JobManager(tmp_path / "state")
    job = manager.start("echo bye; exit 7", cwd=str(tmp_path), env=dict(os.environ), name=None, login_shell=False)
    assert wait_until(lambda: not manager.is_running(job))
    assert manager.exit_code(job) == 7
    assert "exit_code=7" in manager.describe_status(job)
    assert manager.tail(job, 5)[0] == "bye"


def test_job_command_text_not_in_process_args(tmp_path):
    # 外层记退出码的 bash（$PPID）与内层执行命令的 bash（$$），命令行参数里都不带命令原文
    manager = JobManager(tmp_path / "state")
    job = manager.start(
        "xargs -0 echo < /proc/$PPID/cmdline; xargs -0 echo < /proc/$$/cmdline; true  # marker-9b2e",
        cwd=str(tmp_path),
        env=dict(os.environ),
        name="args",
        login_shell=False,
    )
    assert wait_until(lambda: not manager.is_running(job))
    out = manager.tail(job, 10)[0]
    assert len(out.splitlines()) == 2 and "marker-9b2e" not in out, out
