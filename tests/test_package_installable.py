import subprocess, pathlib
def test_uv_sync_succeeds():
    root = pathlib.Path(__file__).resolve().parent.parent
    r = subprocess.run(["uv", "sync"], cwd=root, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
