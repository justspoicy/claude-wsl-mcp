import os

import pytest

from claude_wsl_mcp import files
from claude_wsl_mcp.paths import PathPolicy, PolicyError


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def policy(work):
    return PathPolicy(base_dir=str(work), writable_roots=(str(work),), windows_mounts=("/mnt/c",), windows_drives="hidden")


def test_write_read_edit_search_delete_roundtrip(policy, work):
    assert "已创建" in files.write_file(policy, "src/app.py", "print('hi')\nprint('bye')\n")
    assert "     1\tprint('hi')" in files.read_file(policy, "src/app.py", offset=1, limit=10)

    diff = files.edit_file(policy, "src/app.py", "print('bye')", "print('再见')", replace_all=False)
    assert "+print('再见')" in diff
    assert (work / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\nprint('再见')\n"

    assert "src/app.py" in files.list_directory(policy, ".", depth=2, include_hidden=False, max_entries=100)
    found = files.search_files(policy, ".", pattern="再见", glob="*.py", regex=False, case_sensitive=False, max_results=10)
    assert "src/app.py:2:" in found

    with pytest.raises(files.FileToolError, match="非空目录"):
        files.delete_path(policy, "src", recursive=False)
    assert "递归删除" in files.delete_path(policy, "src", recursive=True)
    assert not (work / "src").exists()


def test_edit_requires_exact_and_unique_match(policy, work):
    (work / "a.txt").write_text("x = 1\nx = 1\n")
    with pytest.raises(files.FileToolError, match="找不到"):
        files.edit_file(policy, "a.txt", "y = 1", "y = 2", replace_all=False)
    with pytest.raises(files.FileToolError, match="出现 2 次"):
        files.edit_file(policy, "a.txt", "x = 1", "x = 2", replace_all=False)
    assert "替换 2 处" in files.edit_file(policy, "a.txt", "x = 1", "x = 2", replace_all=True)


def test_edit_keeps_crlf_line_endings(policy, work):
    (work / "win.txt").write_bytes(b"alpha\r\nbeta\r\n")
    files.edit_file(policy, "win.txt", "alpha\nbeta", "one\ntwo", replace_all=False)
    assert (work / "win.txt").read_bytes() == b"one\r\ntwo\r\n"


def test_write_preserves_existing_mode(policy, work):
    script = work / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    files.write_file(policy, "run.sh", "#!/bin/sh\necho hi\n")
    assert os.stat(script).st_mode & 0o777 == 0o755


def test_binary_file_is_not_read_as_text(policy, work):
    (work / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(files.FileToolError, match="二进制"):
        files.read_file(policy, "blob.bin", offset=1, limit=10)


def test_cannot_delete_writable_root_or_outside(policy, work):
    with pytest.raises(files.FileToolError, match="可写根目录本身"):
        files.delete_path(policy, str(work), recursive=True)
    with pytest.raises(PolicyError):
        files.delete_path(policy, "/etc/hostname", recursive=False)


def test_read_file_paging(policy, work):
    (work / "long.txt").write_text("".join(f"line {i}\n" for i in range(1, 51)))
    out = files.read_file(policy, "long.txt", offset=10, limit=5)
    assert "    10\tline 10" in out and "    14\tline 14" in out
    assert "offset=15" in out
