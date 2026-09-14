from claude_wsl_mcp.sandbox import SandboxReport, is_windows_mount, parse_mountinfo, sanitized_environment

SAMPLE = r"""79 82 0:36 / /usr/lib/wsl/drivers ro,nosuid,nodev,noatime - 9p drivers ro,aname=drivers;fmask=222;dmask=222,cache=0x5,access=client,msize=65536,trans=fd,rfd=8,wfd=8
134 82 0:72 / /mnt/c rw,noatime shared:1 - 9p C:\134 rw,aname=drvfs;path=C:\;uid=0;gid=0;symlinkroot=/mnt/,cache=0x5,access=client,msize=65536,trans=fd,rfd=6,wfd=6
82 1 8:48 / / rw,relatime shared:1 - ext4 /dev/sdd rw,discard,errors=remount-ro,data=ordered
140 82 0:80 / /mnt/wsl rw,relatime shared:2 - tmpfs none rw
"""


def test_parse_mountinfo_fields():
    by_mount = {e.mount_point: e for e in parse_mountinfo(SAMPLE)}
    drive = by_mount["/mnt/c"]
    assert drive.fs_type == "9p"
    assert drive.source == "C:\\"
    assert drive.optional_fields == ("shared:1",)
    assert "noatime" in drive.mount_options


def test_only_drvfs_counts_as_windows_mount():
    by_mount = {e.mount_point: e for e in parse_mountinfo(SAMPLE)}
    assert is_windows_mount(by_mount["/mnt/c"])
    assert not is_windows_mount(by_mount["/usr/lib/wsl/drivers"])
    assert not is_windows_mount(by_mount["/"])
    assert not is_windows_mount(by_mount["/mnt/wsl"])


def test_sanitized_environment_strips_windows_path_and_interop():
    report = SandboxReport(enabled=True, windows_drives="hidden", windows_interop=False, windows_mounts=["/mnt/c", "/mnt/f"])
    env = sanitized_environment(
        {"PATH": "/usr/local/bin:/mnt/c/Windows/system32:/usr/bin:/mnt/f/NodeJS/:/usr/bin", "WSL_INTEROP": "/run/WSL/1_interop"},
        report,
    )
    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert "WSL_INTEROP" not in env
    assert env["CLAUDE_WSL_MCP"] == "1"


def test_disabled_sandbox_keeps_environment():
    report = SandboxReport(enabled=False)
    env = sanitized_environment({"PATH": "/usr/bin:/mnt/c/Windows", "WSL_INTEROP": "x"}, report)
    assert env["PATH"] == "/usr/bin:/mnt/c/Windows"
    assert env["WSL_INTEROP"] == "x"
