"""端到端：用 SDK 自带的 stdio 客户端拉起真实的启动脚本（含沙箱），逐个调用工具。"""

import os
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from claude_wsl_mcp.config import render_default_config

REPO = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "wsl_info",
    "run_command",
    "start_background",
    "background_status",
    "stop_background",
    "list_directory",
    "read_file",
    "search_files",
    "write_file",
    "edit_file",
    "delete_path",
    "set_current_project",
}

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(os.geteuid() != 0 or not os.environ.get("WSL_DISTRO_NAME"), reason="需要在 WSL 里以 root 运行"),
]


def text_of(result):
    return "\n".join(block.text for block in result.content if getattr(block, "type", "") == "text")


def stdout_of(text):
    # 结果开头会回显 "$ 命令"，断言只看 stdout 段，免得命中命令本身
    return text.split("--- stdout ---\n", 1)[1].split("\n--- stderr ---", 1)[0]


def make_server(tmp_path, drives):
    work = tmp_path / "work"
    work.mkdir()
    conf = tmp_path / "config.toml"
    conf.write_text(render_default_config(str(work), str(work)).replace('windows_drives = "hidden"', f'windows_drives = "{drives}"'))
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")}
    params = StdioServerParameters(command=str(REPO / "bin" / "claude-wsl-mcp"), args=["--config", str(conf)], env=env)
    return params, work, conf


async def test_hidden_mode_tools_and_boundaries(tmp_path):
    params, work, conf = make_server(tmp_path, "hidden")
    async with Client(params) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert EXPECTED_TOOLS <= tools

        info = text_of(await client.call_tool("wsl_info", {}))
        assert "microsoft-standard-WSL2" in info
        assert "Windows 盘：已隐藏" in info

        out = text_of(await client.call_tool("run_command", {"command": "whoami; pwd; echo err >&2; exit 5"}))
        assert "exit_code: 5" in out
        assert f"root\n{work}" in out
        assert "--- stderr ---\nerr" in out

        probe = (
            "ls -A /mnt/c | wc -l; "
            "touch /mnt/c/escape 2>&1; "
            "mount -o remount,rw /mnt/c 2>&1; "
            "ls /proc/1/root/ 2>&1 | head -n1; "
            "nsenter -t 1 -m true 2>&1; "
            "command -v cmd.exe || echo no-cmd; "
            "echo PATH=$PATH"
        )
        out = stdout_of(text_of(await client.call_tool("run_command", {"command": probe})))
        assert out.startswith("0\n")
        assert "Read-only file system" in out
        assert "no-cmd" in out
        assert "/mnt/" not in out.rsplit("PATH=", 1)[1].splitlines()[0]
        assert out.count("ermission denied") >= 2

        assert "已创建" in text_of(await client.call_tool("write_file", {"path": "hello.py", "content": "print('hi')\n"}))
        edited = await client.call_tool("edit_file", {"path": "hello.py", "old_text": "hi", "new_text": "WSL"})
        assert not edited.is_error
        out = text_of(await client.call_tool("run_command", {"command": "python hello.py"}))
        assert "WSL" in out

        denied = await client.call_tool("write_file", {"path": "/mnt/c/x.txt", "content": "x"})
        assert denied.is_error
        denied = await client.call_tool("write_file", {"path": "/etc/claude-wsl-mcp-test", "content": "x"})
        assert denied.is_error and "不在可写目录内" in text_of(denied)

        started = text_of(
            await client.call_tool(
                "start_background",
                {"command": "for i in $(seq 1 300); do echo tick $i; sleep 0.1; done", "name": "ticker", "wait_seconds": 1},
            )
        )
        job_id = started.split("任务 ", 1)[1].split("：", 1)[0]
        assert "运行中" in started
        assert "tick" in text_of(await client.call_tool("background_status", {"job_id": job_id}))
        stopped = text_of(await client.call_tool("stop_background", {"job_id": job_id}))
        assert "运行中" not in stopped.splitlines()[0]

        (work / "sub").mkdir()
        switched = await client.call_tool("set_current_project", {"path": "sub"})
        assert not switched.is_error
        assert f"current_project = \"{work / 'sub'}\"" in conf.read_text()
        out = text_of(await client.call_tool("run_command", {"command": "pwd"}))
        assert f"\n{work / 'sub'}\n" in out
        assert (await client.call_tool("set_current_project", {"path": "/etc"})).is_error

        assert "已删除文件" in text_of(await client.call_tool("delete_path", {"path": str(work / "hello.py")}))


async def test_readonly_mode_blocks_writes_and_interop(tmp_path):
    params, work, _ = make_server(tmp_path, "readonly")
    canary = Path("/mnt/c/Users/Public/claude-wsl-mcp-canary.txt")
    try:
        async with Client(params) as client:
            command = (
                f"test -d /mnt/c/Windows && echo can-read; touch {canary} 2>&1; "
                "timeout 10 /mnt/c/Windows/System32/cmd.exe /c echo INTEROP_OK 2>&1; true"
            )
            out = stdout_of(text_of(await client.call_tool("run_command", {"command": command})))
        assert "can-read" in out
        assert "Read-only file system" in out
        assert "INTEROP_OK" not in out
    finally:
        leaked = canary.exists()
        if leaked:
            canary.unlink()
        assert not leaked, "沙箱失效：在 Windows 盘上写出了文件"
