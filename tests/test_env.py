import os

from insightagent.env import _apply_dotenv


def test_dotenv_fills_missing_keys_without_overriding(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "DEEPSEEK_API_KEY=from-file\nALREADY_SET=file-value\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("ALREADY_SET", "process-value")
    _apply_dotenv(path)
    assert os.environ["DEEPSEEK_API_KEY"] == "from-file"
    assert os.environ["ALREADY_SET"] == "process-value"
