"""One-shot environment check before starting the board.

Run from the repo root:

    .venv/Scripts/python.exe scripts/doctor.py          # Windows
    .venv/bin/python scripts/doctor.py                  # macOS / Linux

It only reads: no database is created, no model is called, no quote is fetched.
The single optional network probe is `--net` (one tiny DeepSeek request) and
`--quotes` (reports quote freshness only, still no fetching).

Exit code 0 = everything needed to demo is in place, 1 = at least one FAIL.
"""

from __future__ import annotations

import argparse
import importlib
import os
import socket
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

REQUIRED_MODULES = (
    ("openai", "LLM 适配器"),
    ("pydantic", "合同模型"),
    ("fastapi", "看板 API"),
    ("uvicorn", "看板服务"),
    ("akshare", "行情采集"),
    ("pandas", "akshare 依赖"),
)

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_rows: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str) -> None:
    _rows.append((status, name, detail))


def check_python() -> None:
    major, minor = sys.version_info[:2]
    detail = "{}.{}.{} ({})".format(major, minor, sys.version_info[2], sys.executable)
    if (major, minor) < (3, 9):
        record(FAIL, "Python 版本", detail + " — 需要 >= 3.9")
    else:
        record(PASS, "Python 版本", detail)


def check_modules() -> None:
    missing = []
    for module, purpose in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except Exception as error:  # noqa: BLE001 - report anything importable-fatal
            missing.append("{} ({})".format(module, purpose))
    if missing:
        record(
            FAIL,
            "依赖包",
            "缺少: " + ", ".join(missing) + " — 运行 pip install -e \".[dev,board,akshare]\"",
        )
    else:
        record(PASS, "依赖包", "{} 个全部可导入".format(len(REQUIRED_MODULES)))


def check_env() -> None:
    from insightagent.env import load_dotenv, resolve_path  # noqa: PLC0415

    env_file = ROOT / ".env"
    if not env_file.is_file():
        record(FAIL, ".env 文件", "缺失 — cp .env.example .env 后填入 DEEPSEEK_API_KEY")
    else:
        record(PASS, ".env 文件", str(env_file))
    load_dotenv()
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        record(FAIL, "DEEPSEEK_API_KEY", "未设置 — 首次分析 / 追踪 / 画像都会失败")
    elif len(key) < 20 or key.startswith("sk-请") or "your" in key.lower():
        record(FAIL, "DEEPSEEK_API_KEY", "看起来是占位符（长度 {}）".format(len(key)))
    else:
        record(PASS, "DEEPSEEK_API_KEY", "已设置，长度 {}，尾号 ****{}".format(len(key), key[-4:]))
    record(
        PASS,
        "路径基准",
        resolve_path("", default_relative="data/insightagent.db"),
    )


def _tables(path: Path) -> list[str]:
    connection = sqlite3.connect("file:{}?mode=ro".format(path.as_posix()), uri=True)
    try:
        rows = connection.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        connection.close()


def check_databases() -> None:
    research = ROOT / "data" / "insightagent.db"
    board = ROOT / "data" / "board.db"
    kb = ROOT / "data" / "kb.db"

    if not research.is_file():
        record(
            FAIL,
            "研究库 insightagent.db",
            "缺失 — 先跑 python -m insightagent db init",
        )
    else:
        tables = _tables(research)
        record(PASS, "研究库 insightagent.db", "{} 张表".format(len(tables)))

    if not board.is_file():
        record(
            FAIL,
            "看板库 board.db",
            "缺失 — 先跑 python -m insightboard --db data/board.db init",
        )
    else:
        connection = sqlite3.connect("file:{}?mode=ro".format(board.as_posix()), uri=True)
        try:
            stocks = connection.execute("select count(*) from quote_snapshot").fetchone()[0]
            last = connection.execute(
                "select max(as_of) from quote_batch where status = 'succeeded'"
            ).fetchone()[0]
        except sqlite3.Error:
            stocks, last = -1, None
        finally:
            connection.close()
        if stocks <= 0:
            record(
                FAIL,
                "看板行情",
                "0 条 — 先跑 python -m insightboard --db data/board.db collect-once",
            )
        else:
            record(PASS, "看板行情", "{} 只股票，最近批次 {}".format(stocks, last or "未知"))
        connection = sqlite3.connect("file:{}?mode=ro".format(board.as_posix()), uri=True)
        try:
            rows = dict(
                connection.execute(
                    "select status, count(*) from research_job group by status"
                ).fetchall()
            )
        except sqlite3.Error:
            rows = {}
        finally:
            connection.close()
        if rows:
            record(
                WARN if rows.get("failed") else PASS,
                "研究任务队列",
                " ".join("{}={}".format(key, value) for key, value in sorted(rows.items())),
            )
        else:
            record(PASS, "研究任务队列", "空（还没在页面上发起过任务）")

    if not kb.is_file():
        record(FAIL, "知识库 kb.db", "缺失 — python -m insightagent kb seed")
        return
    connection = sqlite3.connect("file:{}?mode=ro".format(kb.as_posix()), uri=True)
    try:
        counts = dict(
            connection.execute(
                "select status, count(*) from methodology_entries group by status"
            ).fetchall()
        )
    finally:
        connection.close()
    approved = counts.get("approved", 0)
    detail = "approved {} / candidate {} / rejected {}".format(
        approved, counts.get("candidate", 0), counts.get("rejected", 0)
    )
    record(PASS if approved else FAIL, "知识库 kb.db", detail)


def check_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        busy = sock.connect_ex(("127.0.0.1", port)) == 0
    if busy:
        record(WARN, "端口 {}".format(port), "已被占用（可能已在运行），换端口用 --port 8001")
    else:
        record(PASS, "端口 {}".format(port), "空闲")


def probe_key() -> None:
    from insightagent.env import load_dotenv  # noqa: PLC0415

    load_dotenv()
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        record(WARN, "DeepSeek 连通性", "跳过：没有 key")
        return
    try:
        from openai import OpenAI  # noqa: PLC0415

        client = OpenAI(
            api_key=key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
        client.chat.completions.create(
            model=os.environ.get("INSIGHTAGENT_MODEL", "deepseek-v4-flash"),
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        record(PASS, "DeepSeek 连通性", "key 可用")
    except Exception as error:  # noqa: BLE001 - any failure is the signal
        record(
            FAIL,
            "DeepSeek 连通性",
            "{}: {}".format(type(error).__name__, str(error)[:160]),
        )


def _display_width(text: str) -> int:
    return sum(2 if "\u4e00" <= char <= "\u9fff" else 1 for char in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def render() -> None:
    width = max(_display_width(row[1]) for row in _rows)
    for status, name, detail in _rows:
        print("[{}] {}  {}".format(status, _pad(name, width), detail))
    failures = sum(1 for row in _rows if row[0] == FAIL)
    warnings = sum(1 for row in _rows if row[0] == WARN)
    print()
    if failures:
        print("结论：{} 项不通过，{} 项提醒。按上面 FAIL 行修完再启动。".format(failures, warnings))
    else:
        print("结论：全部通过{}。可以启动：.venv/Scripts/python.exe -m insightboard serve".format(
            "" if not warnings else "（{} 项提醒）".format(warnings)
        ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动前自检")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--net",
        action="store_true",
        help="额外发一次 1 token 的 DeepSeek 请求，验证 key 是否真的可用",
    )
    args = parser.parse_args(argv)

    check_python()
    check_modules()
    check_env()
    check_databases()
    check_port(args.port)
    if args.net:
        probe_key()
    render()
    return 1 if any(row[0] == FAIL for row in _rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
