import json
import os

import pytest

from insightagent.env import load_dotenv
from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig
from insightagent.persistence import SQLiteDatabase
from insightagent.user_contracts import NONE
from insightagent.user_intent import passes_memory_gate
from insightagent.user_store import UserStore
from insightagent.workflows.initial_research import analyze_stock, format_cli_text

pytestmark = pytest.mark.live

MODEL = "deepseek-v4-flash"
STOCK = "000858"
PROMPT = "#remember 别光看便宜，利润好看会不会是假的"
RAW_BODY = "别光看便宜，利润好看会不会是假的"


def _adapter():
    load_dotenv()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY is not set")
    return DeepSeekChatAdapter(
        DeepSeekConfig(api_key=api_key, default_model=MODEL)
    )


@pytest.mark.asyncio
async def test_live_from_stock_code_and_prompt(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "insightagent.db"))
    await database.initialize()
    adapter = _adapter()
    outcome = await analyze_stock(
        STOCK,
        database=database,
        llm_adapter=adapter,
        artifact_root=str(tmp_path / "artifacts"),
        fixture=False,
        model=MODEL,
        thinking_enabled=True,
        unbound_policy="abstain",
        user_prompt=PROMPT,
    )
    assert outcome.error is None, outcome.error
    assert outcome.run.stock_code == STOCK
    assert outcome.run.status in {"success", "degraded"}
    assert outcome.intent is not None
    assert outcome.show_intent_echo is True
    assert outcome.intent.effect == "remember"
    assert outcome.intent.fundamental != NONE
    assert outcome.intent.fundamental != RAW_BODY
    assert outcome.intent.fundamental != PROMPT
    assert any(
        token in outcome.intent.fundamental
        for token in ("估值", "便宜", "PE", "pe", "价格", "现金", "利润", "质量")
    )
    assert outcome.report is not None
    assert outcome.technical_report is not None
    assert outcome.sentiment_report is not None
    assert outcome.macro_report is not None
    assert outcome.decision is not None

    text = format_cli_text(outcome)
    assert "意图理解（非原话）" in text
    assert PROMPT not in text
    blob = json.dumps(outcome.to_dict(), ensure_ascii=False)
    assert PROMPT not in blob
    assert RAW_BODY not in blob

    prefs = await UserStore(database).active_preferences(
        user_id="local", scope="fundamental", stock_code=STOCK
    )
    if passes_memory_gate(outcome.intent.fundamental):
        assert len(prefs) == 1
        assert prefs[0].statement != RAW_BODY
    else:
        assert prefs == []
