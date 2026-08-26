import json
import os
import subprocess
import sys


def test_database_cli_init_and_status(tmp_path):
    database = tmp_path / "cli.db"

    initialized = subprocess.run(
        [
            sys.executable,
            "-m",
            "insightagent",
            "db",
            "init",
            "--path",
            str(database),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    init_payload = json.loads(initialized.stdout)
    assert init_payload["initialized"] is True
    assert init_payload["schema_version"] == 2

    status = subprocess.run(
        [
            sys.executable,
            "-m",
            "insightagent",
            "db",
            "status",
            "--path",
            str(database),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    status_payload = json.loads(status.stdout)
    assert status_payload["tables_ok"] is True
    assert status_payload["journal_mode"] == "wal"


def test_analyze_fixture_missing_stock_does_not_need_api(tmp_path):
    database = tmp_path / "cli.db"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "insightagent",
            "analyze",
            "999999",
            "--fixture",
            "--path",
            str(database),
            "--artifact-root",
            str(tmp_path / "artifacts"),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "DEEPSEEK_API_KEY": ""},
    )
    assert "stance / score / abstain: abstain" in result.stdout
    assert "非投资建议" in result.stdout
    assert "timing_score" in result.stdout
    assert "未评估" in result.stdout
    assert "可对本次结果反馈" in result.stdout


def test_feedback_none_prompt_does_not_need_api(tmp_path):
    database = tmp_path / "cli.db"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "insightagent",
            "feedback",
            "missing-run",
            "--prompt",
            "none",
            "--path",
            str(database),
            "--artifact-root",
            str(tmp_path / "artifacts"),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "DEEPSEEK_API_KEY": ""},
    )
    assert result.returncode == 0
    assert result.stdout == ""
