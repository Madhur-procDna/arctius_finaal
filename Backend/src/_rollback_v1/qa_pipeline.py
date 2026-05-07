from __future__ import annotations

import logging
import re
import sys
from typing import Any, Dict

from config import settings
from conversation_context import ConversationBuffer
from data_loader import get_db, load_file
from db_adapter import use_sqlite_backend
from env_loader import force_apply, load_application_dotenv
from nl_row_format import inject_nl_rows_before_sources
from redis_cache import get_cached_pipeline, is_time_volatile_question, set_cached_pipeline
from sql_agent import SQLAgent

# ── chart suggestion helpers ──────────────────────────────────────────────────

_TREND_RE = re.compile(
    r"\b(trend|over time|by (month|year|quarter|week)|monthly|yearly|quarterly|yoy|growth)\b",
    re.IGNORECASE,
)
_PIE_RE = re.compile(
    r"\b(share|proportion|breakdown|split|distribution|percentage|percent|mix)\b",
    re.IGNORECASE,
)
_BAR_RE = re.compile(
    r"\b(top\s*\d+|rank|highest|lowest|most|least|compare|by (hcp|territory|brand|product|rep|region|state))\b",
    re.IGNORECASE,
)


def _is_numeric_val(v: object) -> bool:
    try:
        float(str(v).replace(",", ""))
        return True
    except (ValueError, TypeError):
        return False


def _try_wide_pivot(row: dict) -> list[dict] | None:
    """
    Convert a single wide-format row (columns = time periods, values = metrics)
    into a list of {name, value} pairs suitable for a line chart.
    Requires at least 3 numeric columns to be considered valid.
    """
    pairs = []
    for k, v in row.items():
        if v is None:
            continue
        if _is_numeric_val(v):
            # Pretty-print the column name: Jan_25 → Jan 25
            label = str(k).replace("_", " ")
            pairs.append({"name": label, "value": float(str(v).replace(",", ""))})
    return pairs if len(pairs) >= 3 else None


def _suggest_chart(question: str, rows: list[dict]) -> dict | None:
    """
    Return a chart payload when the data and question clearly warrant a chart.
    Returns None when no chart adds value (single-number results, free-text, etc.).

    Handles two shapes:
    - Long format: many rows, first col = label, second col = metric  →  bar/pie/line
    - Wide format: 1 row, many numeric columns (e.g. Jan_25, Feb_25 …)  →  line chart
    """
    if not rows:
        return None
    q = question or ""

    # ── Wide-format (pivoted) single row: all columns are time-period values ──
    if len(rows) == 1 and (_TREND_RE.search(q) or len(rows[0]) >= 4):
        pivoted = _try_wide_pivot(rows[0])
        if pivoted and len(pivoted) >= 3:
            return {"kind": "line", "data": pivoted}

    # ── Long format: need at least 2 rows ──────────────────────────────────────
    if len(rows) < 2:
        return None

    cols = list(rows[0].keys())
    if not cols:
        return None

    # Find a numeric metric column (skip the first/label column).
    numeric_cols = [
        c for c in cols[1:]
        if _is_numeric_val(rows[0].get(c))
    ]
    if not numeric_cols:
        return None

    label_col = cols[0]
    metric_col = numeric_cols[0]
    data = [{"name": str(r.get(label_col, "")), "value": r.get(metric_col)} for r in rows]

    if _TREND_RE.search(q):
        return {"kind": "line", "data": data}
    if _PIE_RE.search(q) and len(rows) <= 12:
        return {"kind": "pie", "data": data}
    if _BAR_RE.search(q) and len(rows) <= 25:
        return {"kind": "bar", "data": data}
    return None

logger = logging.getLogger(__name__)

_FENCED_SQL_RE = re.compile(r"```sql.*?```", flags=re.IGNORECASE | re.DOTALL)
_XML_SQL_RE = re.compile(r"<sql>.*?</sql>", flags=re.IGNORECASE | re.DOTALL)
_DONE_RE = re.compile(r"</?done\s*/?>", flags=re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_SQL_LOGIC_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:sql\s*logic|query\s*used|sql\s*used)\b.*$",
    flags=re.IGNORECASE,
)
_SQL_KEYWORD_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:`)?\s*(select|with)\b.*\b(from|join|where|limit)\b.*$",
    flags=re.IGNORECASE,
)
_CHAT_ONLY_RE = re.compile(
    r"^\s*(hi|hello|hey|hii+|good\s*(morning|afternoon|evening)|thanks?|thank you)\s*[!.]?\s*$",
    flags=re.IGNORECASE,
)
_MULTI_SPLIT_RE = re.compile(r"\s*;\s+|\s*\n+\s*")
_HEADING_RE = re.compile(r"^\s*#+\s*")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")

_LOCAL_QA_CACHE: dict[str, dict[str, Any]] = {}


def strip_sql_from_nl_chat_markup(text: str | None) -> str:
    if not text:
        return ""
    out = _FENCED_SQL_RE.sub("", text)
    out = _XML_SQL_RE.sub("", out)
    out = _DONE_RE.sub("", out)
    return out.strip()


def sanitize_user_visible_text(text: str | None) -> str | None:
    if text is None:
        return None
    return _CONTROL_RE.sub(" ", text).strip()


def _remove_sql_logic_from_answer(text: str) -> str:
    if not text:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    for ln in lines:
        if _SQL_LOGIC_LINE_RE.match(ln):
            continue
        if _SQL_KEYWORD_LINE_RE.match(ln):
            continue
        kept.append(ln)
    # collapse excessive blank lines
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _remove_markdown_tables(text: str) -> str:
    """Drop markdown table blocks from final NL answer."""
    if not text:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Table starts with header row + separator row.
        if i + 1 < len(lines) and _TABLE_ROW_RE.match(line) and _TABLE_SEP_RE.match(lines[i + 1]):
            i += 2
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                i += 1
            continue
        kept.append(line)
        i += 1
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _normalize_answer_sections(text: str) -> str:
    """Normalize section headers and keep plain NL format."""
    if not text:
        return text
    lines = text.splitlines()
    norm: list[str] = []
    for ln in lines:
        clean = _HEADING_RE.sub("", ln).strip()
        lower = clean.lower().rstrip(":")
        if lower in ("what we verified", "key findings", "detailed analysis", "sources checked"):
            norm.append(clean.title() if lower != "sources checked" else "Sources checked")
        else:
            norm.append(ln.strip())
    out = "\n".join(norm)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _normalize_dataset_naming(text: str) -> str:
    """Keep user-facing wording on Arcitus dataset (hide internal table name labels)."""
    if not text:
        return text
    out = text
    out = re.sub(r"\bDummy_Data\b", "Arcitus data", out, flags=re.IGNORECASE)
    out = re.sub(r"\bDummy Data\b", "Arcitus data", out, flags=re.IGNORECASE)
    out = re.sub(r"\bArcetus data\b", "Arcitus data", out, flags=re.IGNORECASE)
    out = re.sub(r"Sources checked:\s*Arcitus data\s*table", "Sources checked: Arcitus data", out, flags=re.IGNORECASE)
    return out


def _cache_key(question: str) -> str:
    return " ".join((question or "").strip().lower().split())


def _ensure_workbook_loaded() -> None:
    if not use_sqlite_backend():
        return
    if get_db() is not None:
        return
    logger.info("Loading Arcetus workbook from DATA_FILE_PATH: %s", settings.data_file_path)
    load_file(settings.data_file_path)


def _build_history(conversation: ConversationBuffer | None) -> list[dict[str, str]]:
    if conversation is None or len(conversation) == 0:
        return []
    block = (conversation.format_for_prompt() or "").strip()
    if not block:
        return []
    return [{"role": "system", "content": block[:12000]}]

def _split_compound_questions(text: str) -> list[str]:
    raw_parts = [p.strip() for p in _MULTI_SPLIT_RE.split(text or "") if p.strip()]
    # Keep single question untouched unless we have explicit separators.
    return raw_parts if len(raw_parts) > 1 else [text.strip()]

def _run_single_question(
    q: str,
    *,
    conversation: ConversationBuffer | None,
) -> Dict[str, Any]:
    q_key = _cache_key(q)
    if q_key and not is_time_volatile_question(q):
        local_hit = _LOCAL_QA_CACHE.get(q_key)
        if local_hit:
            return {
                "question": q,
                "sql": local_hit.get("sql"),
                "answer": local_hit.get("answer", ""),
                "row_count": int(local_hit.get("row_count") or 0),
                "cache_hit": True,
                "sql_agent_llm_rounds": 0,
                "sql_agent_sql_steps": 0,
            }
        # remote_hit = get_cached_pipeline(q, schema="arcetus_sqlite")
        # if remote_hit and remote_hit.get("answer"):
        #     _LOCAL_QA_CACHE[q_key] = {
        #         "sql": remote_hit.get("sql"),
        #         "answer": remote_hit.get("answer"),
        #         "row_count": int(remote_hit.get("row_count") or 0),
        #     }
        #     return {
        #         "question": q,
        #         "sql": remote_hit.get("sql"),
        #         "answer": remote_hit.get("answer", ""),
        #         "row_count": int(remote_hit.get("row_count") or 0),
        #         "cache_hit": True,
        #         "sql_agent_llm_rounds": 0,
        #         "sql_agent_sql_steps": 0,
        #     }

    agent = SQLAgent()
    resp = agent.run(user_text=q, history=_build_history(conversation), db_state=get_db())

    sql_out = (resp.sql or "").strip() or "(sql-agent)"
    rows = resp.results or []
    answer = sanitize_user_visible_text(strip_sql_from_nl_chat_markup(resp.content or "")) or ""
    answer = _remove_sql_logic_from_answer(answer)
    answer = _remove_markdown_tables(answer)
    answer = _normalize_answer_sections(answer)
    answer = _normalize_dataset_naming(answer)
    err = sanitize_user_visible_text(resp.error)

    if not err and rows:
        answer = inject_nl_rows_before_sources(answer, rows)

    if conversation is not None and answer:
        try:
            conversation.append(q, sql_out, answer)
        except Exception:
            logger.warning("Conversation append failed", exc_info=True)

    out: Dict[str, Any] = {
        "question": q,
        "sql": sql_out,
        "answer": answer,
        "row_count": len(rows),
        "cache_hit": False,
        "sql_agent_llm_rounds": int(getattr(resp, "llm_rounds", 0) or 0),
        "sql_agent_sql_steps": len(resp.all_queries or []),
    }

    # result_table: full row payload for CSV download — only when there are more than 10 rows.
    if not err and rows and len(rows) > 10:
        cols = list(rows[0].keys()) if rows else []
        out["result_table"] = {
            "columns": cols,
            "rows": rows,
            "total_row_count": len(rows),
        }

    # chart: only when the question + data clearly benefit from a visualisation.
    if not err and rows:
        chart = _suggest_chart(q, rows)
        if chart:
            out["chart"] = chart

    if err:
        out["error"] = err
    elif q_key and answer and not is_time_volatile_question(q):
        _LOCAL_QA_CACHE[q_key] = {
            "sql": sql_out,
            "answer": answer,
            "row_count": len(rows),
        }
        try:
            set_cached_pipeline(
                q,
                schema="arcetus_sqlite",
                sql=sql_out,
                answer=answer,
                row_count=len(rows),
            )
        except Exception:
            logger.debug("Redis QA cache set skipped", exc_info=True)
    return out


def run_question_pipeline_turn(
    question: str,
    *,
    conversation: ConversationBuffer | None = None,
    use_cache: bool = True,
    trace_metadata: dict[str, Any] | None = None,
    **_: Any,
) -> Dict[str, Any]:
    _ = use_cache
    _ = trace_metadata

    q = (question or "").strip()
    if not q:
        return {
            "question": question,
            "sql": None,
            "answer": "",
            "row_count": 0,
            "error": "Question is empty.",
            "sql_agent_llm_rounds": 0,
            "sql_agent_sql_steps": 0,
        }

    if _CHAT_ONLY_RE.match(q):
        return {
            "question": q,
            "sql": None,
            "answer": (
                "Hi! I am ready to help with Arcitus data questions. "
                "Try something like: `show total number of HCPs`."
            ),
            "row_count": 0,
            "cache_hit": False,
            "sql_agent_llm_rounds": 0,
            "sql_agent_sql_steps": 0,
        }

    try:
        _ensure_workbook_loaded()
    except Exception as exc:
        logger.exception("Workbook load failed")
        return {
            "question": q,
            "sql": None,
            "answer": "",
            "row_count": 0,
            "error": f"Failed to load Arcetus workbook: {exc}",
            "sql_agent_llm_rounds": 0,
            "sql_agent_sql_steps": 0,
        }

    parts = _split_compound_questions(q)
    if len(parts) == 1:
        return _run_single_question(parts[0], conversation=conversation)

    sub_results: list[Dict[str, Any]] = []
    merged_answer_parts: list[str] = []
    merged_sql_parts: list[str] = []
    total_rows = 0
    first_error: str | None = None
    total_rounds = 0
    total_steps = 0

    for i, part in enumerate(parts, start=1):
        part_out = _run_single_question(part, conversation=conversation)
        sub_results.append(
            {
                "index": i,
                "question": part,
                "response": part_out.get("answer", ""),
                "sql": part_out.get("sql"),
                "row_count": part_out.get("row_count", 0),
                **({"error": part_out["error"]} if part_out.get("error") else {}),
            }
        )
        merged_answer_parts.append(
            f"### Part {i} of {len(parts)}\n\n**Question:** {part}\n\n{part_out.get('answer','')}"
        )
        if part_out.get("sql"):
            merged_sql_parts.append(f"-- Part {i}\n{part_out['sql']}")
        total_rows += int(part_out.get("row_count") or 0)
        total_rounds += int(part_out.get("sql_agent_llm_rounds") or 0)
        total_steps += int(part_out.get("sql_agent_sql_steps") or 0)
        if part_out.get("error") and not first_error:
            first_error = str(part_out["error"])

    out: Dict[str, Any] = {
        "question": q,
        "sql": "\n\n".join(merged_sql_parts) if merged_sql_parts else "(sql-agent)",
        "answer": "\n\n---\n\n".join(merged_answer_parts),
        "row_count": total_rows,
        "cache_hit": False,
        "sub_results": sub_results,
        "sql_agent_llm_rounds": total_rounds,
        "sql_agent_sql_steps": total_steps,
    }
    if first_error and not any(not sr.get("error") for sr in sub_results):
        out["error"] = first_error
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    load_application_dotenv()
    force_apply()
    _ensure_workbook_loaded()
    print("Arcetus QA CLI ready. Press Enter on empty line to exit.")
    buf = ConversationBuffer()
    while True:
        try:
            q = input("\nAsk: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return
        if not q:
            print("Exiting.")
            return
        out = run_question_pipeline_turn(q, conversation=buf, use_cache=False)
        if out.get("error"):
            print("\n[error]", out["error"])
        print("\nSQL:", out.get("sql"))
        print("\nAnswer:\n", out.get("answer") or "(no answer)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[qa_pipeline] fatal error: {exc}", file=sys.stderr)
        raise
