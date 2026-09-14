import pytest

from claude_wsl_mcp.paths import PathPolicy, PolicyError


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


def policy_for(work, drives="hidden"):
    return PathPolicy(base_dir=str(work), writable_roots=(str(work),), windows_mounts=("/mnt/c",), windows_drives=drives)


def test_relative_path_resolves_against_current_project(work):
    assert policy_for(work).check_write("a/b.txt") == str(work / "a" / "b.txt")


@pytest.mark.parametrize("raw", [r"C:\Users\admin", "D:/data", r"\\wsl.localhost\Ubuntu\root"])
def test_windows_style_paths_are_rejected(work, raw):
    with pytest.raises(PolicyError, match="Windows 路径"):
        policy_for(work).check_read(raw)


def test_hidden_windows_drive_is_not_readable(work):
    with pytest.raises(PolicyError, match="隐藏"):
        policy_for(work).check_read("/mnt/c/Users")


def test_readonly_windows_drive_is_readable_not_writable(work):
    policy = PathPolicy(base_dir=str(work), writable_roots=("/",), windows_mounts=("/mnt/c",), windows_drives="readonly")
    assert policy.check_read("/mnt/c/Users").startswith("/mnt/c")
    with pytest.raises(PolicyError, match="不可写"):
        policy.check_write("/mnt/c/Users/x.txt")


def test_write_outside_writable_roots_is_rejected(work):
    with pytest.raises(PolicyError, match="不在可写目录内"):
        policy_for(work).check_write("/etc/passwd")


def test_symlink_cannot_escape_writable_root(work, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / "link").symlink_to(outside)
    with pytest.raises(PolicyError):
        policy_for(work).check_write("link/evil.txt")
    # 删除链接本身不跟随链接，允许
    assert policy_for(work).check_write("link", follow_final_symlink=False) == str(work / "link")
