from pathlib import Path

from insightagent.methodology import (
    MethodologyCatalog,
    default_kb_path,
    default_research_db_path,
    split_kb_database,
)
from insightagent.persistence import SQLiteDatabase


def test_split_kb_uses_same_file_for_tmp_research_db(tmp_path, monkeypatch):
    monkeypatch.delenv("INSIGHTAGENT_KB_PATH", raising=False)
    monkeypatch.delenv("INSIGHTAGENT_DB_PATH", raising=False)
    research = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    assert split_kb_database(research).path == research.path


def test_split_kb_env_override(tmp_path, monkeypatch):
    kb = tmp_path / "cards.db"
    monkeypatch.setenv("INSIGHTAGENT_KB_PATH", str(kb))
    research = SQLiteDatabase(str(tmp_path / "logs.db"))
    assert split_kb_database(research).path == kb.resolve()


def test_catalog_can_live_on_kb_only_file(tmp_path):
    kb = SQLiteDatabase(str(tmp_path / "kb.db"))
    catalog = MethodologyCatalog(kb)
    catalog.ensure_seeded()
    assert catalog.get("kb_cashflow_lag")["status"] == "approved"
    research = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    assert split_kb_database(research).path == research.path


def test_default_paths_are_distinct(monkeypatch):
    monkeypatch.delenv("INSIGHTAGENT_KB_PATH", raising=False)
    monkeypatch.delenv("INSIGHTAGENT_DB_PATH", raising=False)
    assert Path(default_kb_path()).name == "kb.db"
    assert Path(default_research_db_path()).name == "insightagent.db"
    assert default_kb_path() != default_research_db_path()
