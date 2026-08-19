# InsightAgent Runtime

Single-instance agent runtime with:

- isolated agent state and lifecycle
- parallel resource calls
- L0–L4 context compaction
- exponential-backoff retries
- function, skill, knowledge-base, and agent-skill resources
- DeepSeek Chat Completions adapter

The first implementation milestone uses fake models and resources so the runtime
can be tested without an API key.

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## DeepSeek adapter

```python
import os

from insightagent.llm import DeepSeekChatAdapter, DeepSeekConfig
from insightagent.runtime import AgentInstance, RuntimeConfig

adapter = DeepSeekChatAdapter(
    DeepSeekConfig(api_key=os.environ["DEEPSEEK_API_KEY"])
)
agent = AgentInstance(
    name="fundamental",
    llm_adapter=adapter,
    config=RuntimeConfig(
        system_prompt="Analyze fundamentals and bind claims to evidence."
    ),
)
```

No live API call is performed by the test suite.

## Local SQLite

SQLite runs in-process; no database server is required.

```bash
.venv/bin/python -m insightagent db init
.venv/bin/python -m insightagent db status
```

The default database is `data/insightagent.db`. Database files, WAL files, and
artifact data are ignored by Git.
