"""Exercise startup without launching services or touching the project's data."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "space $UNSET ${VAR} #! ' \" \\n \\ trailing\\\n$(touch injected) `touch injected`"


@pytest.fixture
def launcher(tmp_path):
    root = tmp_path / "project with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(ROOT / "scripts/start.sh", scripts / "start.sh")
    shutil.copy(ROOT / "docker-compose.yml", root / "docker-compose.yml")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake = f"#!{sys.executable}\n" + """
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
if name == 'curl':
    pathlib.Path(os.environ['TEST_CURL']).write_text(sys.argv[-1])
elif name == 'uvicorn' or (name == 'docker' and 'up' in sys.argv):
    pathlib.Path(os.environ['TEST_ENV']).write_text(json.dumps({
        key: value for key, value in os.environ.items() if key.startswith('PRVAPT_')
    }))
"""
    # Container ownership migration is independent of quoting; bypass it so the
    # launcher tests also run as an unprivileged CI user without host chown.
    for name in ("docker", "uvicorn", "curl", "find"):
        executable = binaries / name
        executable.write_text(fake)
        executable.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PRVAPT_")}
    env.update(PATH=f"{binaries}:{env['PATH']}", TEST_ENV=str(tmp_path / "captured.json"),
               TEST_CURL=str(tmp_path / "curl-url"))
    return root, env


@pytest.mark.parametrize("engine", ["--docker", "--dev"])
def test_password_passes_literally_without_shell_execution(launcher, engine):
    root, env = launcher
    env["PRVAPT_ADMIN_PASSWORD"] = "overridden-by-explicit-argument"
    subprocess.run(["bash", str(root / "scripts/start.sh"), engine, "--password", PASSWORD],
                   env=env, capture_output=True, text=True, check=True)
    captured = Path(env["TEST_ENV"])
    for _ in range(100):
        if captured.exists():
            break
        time.sleep(0.01)
    assert json.loads(captured.read_text())["PRVAPT_ADMIN_PASSWORD"] == PASSWORD
    assert not (root / "injected").exists()
    assert (root / ".env.runtime").stat().st_mode & 0o777 == 0o600
    assert Path(env["TEST_CURL"]).read_text().endswith("/readyz")


def test_runtime_file_roundtrips_through_real_compose(launcher):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Compose is unavailable")
    root, env = launcher
    subprocess.run(["bash", str(root / "scripts/start.sh"), "--docker", "--password", PASSWORD],
                   env=env, capture_output=True, text=True, check=True)
    result = subprocess.run(
        [docker, "compose", "-f", str(root / "docker-compose.yml"), "--env-file",
         str(root / ".env.runtime"), "config", "--format", "json"],
        env=env, capture_output=True, text=True, check=True,
    )
    config = json.loads(result.stdout)
    # Compose escapes dollars in its serialized configuration for later reuse.
    password = config["services"]["app"]["environment"]["PRVAPT_ADMIN_PASSWORD"].replace("$$", "$")
    assert password == PASSWORD
    assert config["services"]["app"]["volumes"][0]["source"] == str(root / "data")


@pytest.mark.parametrize("port", ["12345", '"12345"'])
def test_status_never_executes_runtime_file(launcher, port):
    root, env = launcher
    (root / ".env.runtime").write_text(
        f"PRVAPT_PORT={port}\nPRVAPT_ADMIN_PASSWORD=$(touch injected)\n"
    )
    subprocess.run(["bash", str(root / "scripts/start.sh"), "status"],
                   env=env, capture_output=True, text=True, check=True)
    assert not (root / "injected").exists()
    assert Path(env["TEST_CURL"]).read_text() == "http://127.0.0.1:12345/readyz"
