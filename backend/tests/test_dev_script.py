"""在临时项目内验证启动脚本，不启动或停止真实业务服务。"""

from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "dev.sh"


def run_functions(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    definitions = SCRIPT.read_text().rsplit('case "${1:-}" in', 1)[0]
    return subprocess.run(
        ["/bin/bash", "-c", definitions + "\n" + body, str(tmp_path / "dev.sh")],
        capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("missing", ["node", "pnpm", "uv", "python3"])
def test_missing_tool_fails_before_launch(tmp_path: Path, missing: str) -> None:
    result = run_functions(tmp_path, f'''
command() {{ [[ "$2" != "{missing}" ]]; }}
start_backend() {{ echo unexpected-launch; }}
start_all
''')
    assert result.returncode != 0
    assert missing in result.stdout
    assert "unexpected-launch" not in result.stdout


def test_missing_installed_dependencies_fails_before_launch(tmp_path: Path) -> None:
    for file in ["backend/pyproject.toml", "backend/uv.lock", "frontend/package.json", "frontend/pnpm-lock.yaml"]:
        target = tmp_path / file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    result = run_functions(tmp_path, '''
require_command() { :; }
start_backend() { echo unexpected-launch; }
start_all
''')
    assert result.returncode != 0
    assert "依赖" in result.stdout
    assert "unexpected-launch" not in result.stdout


@pytest.mark.parametrize("component", ["后端", "前端"])
def test_dead_component_fails_readiness(tmp_path: Path, component: str) -> None:
    result = run_functions(tmp_path, f'''
read_pid() {{ if [[ "$1" == "$BACKEND_PID_FILE" ]]; then echo backend; else echo frontend; fi; }}
is_pid_running() {{ [[ "$1" != "{'backend' if component == '后端' else 'frontend'}" ]]; }}
curl() {{ return 0; }}
wait_until_ready
''')
    assert result.returncode != 0
    assert component in result.stdout
    assert "退出" in result.stdout


def test_readiness_has_shared_deadline(tmp_path: Path) -> None:
    result = run_functions(tmp_path, '''
read_pid() { echo 123; }
is_pid_running() { return 0; }
curl() { return 1; }
sleep() { SECONDS=$((SECONDS + 21)); }
wait_until_ready
''')
    assert result.returncode != 0
    assert "20 秒" in result.stdout


def test_both_components_must_be_ready(tmp_path: Path) -> None:
    result = run_functions(tmp_path, '''
read_pid() { echo 123; }
is_pid_running() { return 0; }
curl() { echo 200; }
listener_belongs_to() { return 0; }
wait_until_ready
''')
    assert result.returncode == 0


def test_occupied_port_is_rejected_before_launch(tmp_path: Path) -> None:
    result = run_functions(tmp_path, '''
find_listener_pid() { echo 9876; }
launch_in_own_session() { echo unexpected-launch; }
start_backend
''')
    assert result.returncode != 0
    assert "8000" in result.stdout
    assert "unexpected-launch" not in result.stdout


def test_failure_cleanup_preserves_preexisting_component(tmp_path: Path) -> None:
    result = run_functions(tmp_path, '''
NEW_BACKEND_PID=""
NEW_FRONTEND_PID=123
is_pid_running() { return 0; }
pid_matches_command() { return 0; }
get_process_group_id() { echo "$1"; }
stop_process_group() { echo "cleaned:$1:$2"; }
cleanup_started
''')
    assert result.returncode == 0
    assert "cleaned:123:" in result.stdout
    assert "后端" not in result.stdout


def test_cleanup_rejects_reused_pid(tmp_path: Path) -> None:
    result = run_functions(tmp_path, '''
NEW_BACKEND_PID=123
NEW_FRONTEND_PID=""
is_pid_running() { return 0; }
pid_matches_command() { return 1; }
stop_process_group() { echo unexpected-kill; }
cleanup_started
''')
    assert "unexpected-kill" not in result.stdout


def test_failed_start_rolls_back_only_new_frontend(tmp_path: Path) -> None:
    python = tmp_path / "backend/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to("/usr/bin/true")
    result = run_functions(tmp_path, '''
require_command() { :; }
require_file() { :; }
start_backend() { echo existing-backend; }
start_frontend() { NEW_FRONTEND_PID=123; }
wait_until_ready() { return 1; }
cleanup_started() { echo "rollback:$NEW_BACKEND_PID:$NEW_FRONTEND_PID"; }
start_all
''')
    assert result.returncode != 0
    assert "rollback::123" in result.stdout
    assert "启动完成" not in result.stdout


def test_strict_port_and_offline_launch_arguments(tmp_path: Path) -> None:
    result = run_functions(tmp_path, '''
printf '%s\\n' "${BACKEND_CMD[*]}" "${FRONTEND_CMD[*]}"
''')
    assert "--no-sync --offline" in result.stdout
    assert "--port 5173 --strictPort" in result.stdout
    assert "COREPACK_ENABLE_NETWORK=0" in result.stdout
