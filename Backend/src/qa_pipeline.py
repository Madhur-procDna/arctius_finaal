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
from nl_row_format import append_row_count_note
from redis_cache import get_cached_pipeline, is_time_volatile_question, set_cached_pipeline
from sql_agent import SQLAgent

# ── chart suggestion helpers ──────────────────────────────────────────────────

_TREND_RE = re.compile(
    r"\b(trend|over time|by (month|year|quarter|week)|monthly|yearly|quarterly|yoy)\b",
    re.IGNORECASE,
)
_PIE_RE = re.compile(
    r"\b(share|proportion|breakdown|split|distribution|percentage|percent|mix)\b",
    re.IGNORECASE,
)
_BAR_RE = re.compile(
    r"\b(top\s*\d+|rank|ranking|highest|lowest|most|least|growth|accelerat|stall|compare|"
    r"by (hcp|territory|territories|brand|product|rep|region|regions|state))\b",
    re.IGNORECASE,
)
_CONTRIB_RE = re.compile(r"\b(contribution|contributed|contribute|share contributed)\b", re.IGNORECASE)
_COMPARE_RE = re.compile(r"\b(compare|comparison|vs\.?|versus)\b", re.IGNORECASE)
_METRIC_HINT_RE = re.compile(
    r"(growth|pct|percent|delta|change|trx|nrx|rank|score|value|amount|total|yoy|mom|qoq)",
    re.IGNORECASE,
)
_PERCENTISH_COL_RE = re.compile(r"(pct|percent|percentage|share|ratio|rate)\b", re.IGNORECASE)
_LABEL_HINT_RE = re.compile(
    r"(territory|region|hcp|rep|name|city|state|area|district|market|segment|brand|product)",
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


def _extract_wide_monthly_and_quarterly(
    row: dict,
) -> tuple[list[tuple[int, int, str, float]], list[tuple[int, int, str, float]]]:
    """Split wide row into strictly monthly vs quarterly ordered points."""
    months: list[tuple[int, int, str, float]] = []
    quarters: list[tuple[int, int, str, float]] = []
    for k, v in row.items():
        metric = _scalar_for_metric(v)
        if metric is None:
            continue
        key = str(k).strip().lower().replace("-", "_")
        mm = _MONTH_KEY_RE.match(key)
        if mm:
            mon = mm.group(1).lower()
            year = _norm_year(mm.group(2))
            m_idx = _MONTH_INDEX.get(mon)
            if m_idx:
                months.append((year, m_idx, f"{mon.title()} {year}", metric))
            continue
        mq = _QUARTER_KEY_RE.match(key)
        if mq:
            q_idx = int(mq.group(1))
            year = _norm_year(mq.group(2))
            quarters.append((year, q_idx, f"Q{q_idx} {year}", metric))
    months.sort(key=lambda x: (x[0], x[1]))
    quarters.sort(key=lambda x: (x[0], x[1]))
    return months, quarters


def _has_backward_time(points: list[tuple[int, int, str, float]]) -> bool:
    if len(points) < 2:
        return False
    for i in range(1, len(points)):
        if (points[i][0], points[i][1]) < (points[i - 1][0], points[i - 1][1]):
            return True
    return False


def _detect_spike_warning(points: list[tuple[int, int, str, float]]) -> str | None:
    """Flag likely aggregation/spike issues for user-facing warning text."""
    if len(points) < 4:
        return None
    deltas = [abs(points[i][3] - points[i - 1][3]) for i in range(1, len(points))]
    sorted_d = sorted(deltas)
    median = sorted_d[len(sorted_d) // 2]
    if median <= 0:
        return None
    max_delta = max(deltas)
    if max_delta >= 4.0 * median:
        return (
            "Potential visualization/data issue: a sudden jump is disproportionately large "
            "vs typical period-to-period movement (possible aggregation mismatch)."
        )
    return None


def _row_label_string(row: dict, col: str) -> str:
    v = row.get(col)
    if v is None:
        return ""
    s = str(v).strip()
    return s[:120]


def _scalar_for_metric(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (not v == v):  # nan
            return None
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "").replace("(", "-").replace(")", "")
    try:
        return float(s)
    except ValueError:
        return None


def _boolish_rank_col(name: str) -> bool:
    n = name.lower()
    return bool(
        re.match(r"^(rn|row_num|rownum|idx|index|seq|ordinal|_?rank)\b", n)
        or n in ("rank", "ordinal", "rowid", "num", "no", "#")
    )


def _pick_label_and_metric_cols(rows: list[dict]) -> tuple[str | None, str | None]:
    """
    Choose the best name column and numeric metric for bar/line/pie.
    Avoids assuming SQLite column order (first col is rarely guaranteed).
    """
    if not rows:
        return None, None
    r0 = rows[0]
    keys = list(r0.keys())
    if not keys:
        return None, None

    # Metric: prefer column name matching growth / trx / pct ...
    metric_candidates: list[str] = []
    for k in keys:
        if _METRIC_HINT_RE.search(k):
            if _scalar_for_metric(r0.get(k)) is not None:
                metric_candidates.append(k)
    if metric_candidates:
        preferred = [c for c in metric_candidates if re.search(r"growth|pct|percent|delta|change|yoy|qoq|mom", c, re.I)]
        metric_col = preferred[0] if preferred else metric_candidates[0]
    else:
        metric_col = None
        for k in keys:
            if _boolish_rank_col(k):
                continue
            if _scalar_for_metric(r0.get(k)) is not None:
                metric_col = k
                break

    if not metric_col:
        return None, None

    # Label: prefer readable entity column, not the metric
    label_candidates = [k for k in keys if k != metric_col and not _boolish_rank_col(k)]
    label_col = None
    for k in label_candidates:
        if _LABEL_HINT_RE.search(k):
            v = r0.get(k)
            if v is not None and not isinstance(v, bool):
                if isinstance(v, (int, float)) and _scalar_for_metric(v) is not None and not _METRIC_HINT_RE.search(k):
                    # small int id — skip as label if we have better
                    continue
                label_col = k
                break
    if not label_col:
        for k in label_candidates:
            if _scalar_for_metric(r0.get(k)) is None:
                label_col = k
                break
    if not label_col:
        # Fall back: first non-metric column
        for k in keys:
            if k != metric_col:
                label_col = k
                break

    return label_col, metric_col


_TIME_SERIES_Q = re.compile(
    r"\b(over time|monthly|each month|per month|by month|yearly|by year|yoy|mom|qoq|time series)\b",
    re.IGNORECASE,
)
_MOM_QOQ_RE = re.compile(
    r"\b(mom|month[- ]over[- ]month|qoq|quarter[- ]over[- ]quarter|trajectory)\b",
    re.IGNORECASE,
)
_MONTH_KEY_RE = re.compile(
    r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[ _-]?'?(\d{2,4})$",
    re.IGNORECASE,
)
_QUARTER_KEY_RE = re.compile(r"^q([1-4])[ _-]?'?(\d{2,4})$", re.IGNORECASE)
_YEAR_LABEL_RE = re.compile(r"^(19|20)\d{2}$")
_TIME_LABEL_TOKEN_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|q[1-4]|fy\d{2,4}|wk\d{1,2}|week|month|quarter|year)\b",
    re.IGNORECASE,
)
_MONTH_INDEX = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _norm_year(year_token: str) -> int:
    y = int(year_token)
    return 2000 + y if y < 100 else y


def _looks_like_time_label(label: str) -> bool:
    s = (label or "").strip().lower().replace("-", " ").replace("_", " ")
    if not s:
        return False
    if _YEAR_LABEL_RE.match(s):
        return True
    if _MONTH_KEY_RE.match(s.replace(" ", "_")):
        return True
    if _QUARTER_KEY_RE.match(s.replace(" ", "_")):
        return True
    return bool(_TIME_LABEL_TOKEN_RE.search(s))


def _is_probably_time_series_data(data: list[dict]) -> bool:
    if len(data) < 3:
        return False
    names = [str(d.get("name", "")).strip() for d in data]
    temporal_hits = sum(1 for n in names if _looks_like_time_label(n))
    return temporal_hits >= max(2, int(0.6 * len(names)))


def _format_pct(delta: float, base: float) -> str:
    if abs(base) < 1e-9:
        return "n/a"
    return f"{(delta / base) * 100:+.2f}%"


def _build_trend_math_answer(question: str, rows: list[dict]) -> str | None:
    """
    Deterministic answer for single-row wide monthly trend payloads with MoM/QoQ asks.
    Avoids LLM guesswork for "which months drove growth" questions.
    """
    if not rows or len(rows) != 1:
        return None
    if not _TREND_RE.search(question or ""):
        return None
    if not _MOM_QOQ_RE.search(question or ""):
        return None

    month_points, quarter_points = _extract_wide_monthly_and_quarterly(rows[0])
    if len(month_points) < 3:
        return None
    backward_months = _has_backward_time(month_points)
    backward_quarters = _has_backward_time(quarter_points)
    month_spike_warning = _detect_spike_warning(month_points)
    quarter_spike_warning = _detect_spike_warning(quarter_points)

    mom_changes: list[dict[str, float | str]] = []
    for i in range(1, len(month_points)):
        prev = month_points[i - 1]
        cur = month_points[i]
        delta = cur[3] - prev[3]
        mom_changes.append(
            {
                "label": cur[2],
                "prev_label": prev[2],
                "cur": cur[3],
                "prev": prev[3],
                "delta": delta,
            }
        )

    top_growth = sorted(mom_changes, key=lambda x: float(x["delta"]), reverse=True)[:3]
    top_drop = min(mom_changes, key=lambda x: float(x["delta"]))
    qoq_changes: list[dict[str, float | str]] = []
    for i in range(1, len(quarter_points)):
        prev = quarter_points[i - 1]
        cur = quarter_points[i]
        qoq_changes.append(
            {
                "label": cur[2],
                "prev_label": prev[2],
                "cur": cur[3],
                "prev": prev[3],
                "delta": cur[3] - prev[3],
            }
        )

    latest = month_points[-1][3]
    earliest = month_points[0][3]
    net_delta = latest - earliest
    trend_word = "upward" if net_delta > 0 else ("downward" if net_delta < 0 else "flat")

    lines: list[str] = [
        "Summary",
        (
            f"ZORYVE TRx is broadly stable across the last {len(month_points)} months, with a "
            f"{trend_word} net move of {net_delta:+.0f} TRx from {month_points[0][2]} to {month_points[-1][2]}."
        ),
        "",
        "Key Insights",
        f"- Largest MoM gain appears in {top_growth[0]['label']} ({float(top_growth[0]['delta']):+.0f} TRx).",
        f"- Largest MoM decline appears in {top_drop['label']} ({float(top_drop['delta']):+.0f} TRx).",
    ]
    if qoq_changes:
        latest_q = qoq_changes[-1]
        lines.append(
            f"- Latest QoQ change: {latest_q['label']} vs {latest_q['prev_label']} "
            f"({float(latest_q['delta']):+.0f} TRx)."
        )
    lines += ["", "Top Results"]
    for g in top_growth:
        cur = float(g["cur"])
        prev = float(g["prev"])
        delta = float(g["delta"])
        lines.append(
            f"- {g['label']} — {cur:,.0f} TRx, up {delta:+.0f} vs {g['prev_label']} "
            f"({_format_pct(delta, prev)} MoM)."
        )
    if qoq_changes:
        best_q = max(qoq_changes, key=lambda x: float(x["delta"]))
        worst_q = min(qoq_changes, key=lambda x: float(x["delta"]))
        lines += [
            "",
            "Supporting Observations",
            f"- Best QoQ improvement: {best_q['label']} ({float(best_q['delta']):+.0f} TRx vs {best_q['prev_label']}).",
            f"- Weakest QoQ movement: {worst_q['label']} ({float(worst_q['delta']):+.0f} TRx vs {worst_q['prev_label']}).",
        ]
    if backward_months or backward_quarters or month_spike_warning or quarter_spike_warning:
        lines += ["", "Data Quality / Visualization Checks"]
        if backward_months:
            lines.append("- Time sequence issue detected in monthly series (periods appear out of order).")
        if backward_quarters:
            lines.append("- Time sequence issue detected in quarterly series (periods appear out of order).")
        if month_spike_warning:
            lines.append(f"- {month_spike_warning}")
        if quarter_spike_warning:
            lines.append(f"- {quarter_spike_warning}")
    lines += ["", "Sources checked: Arcitus data"]
    return "\n".join(lines)


def _suggest_chart(question: str, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Return a chart payload when the data and question clearly warrant a chart.
    Returns None when no chart adds value (single-number results, free-text, etc.).

    Handles two shapes:
    - Long format: many rows, first col = label, second col = metric → bar/pie/line
    - Wide format: 1 row, many numeric columns (e.g. Jan_25, Feb_25 …) → line chart
    """
    if not rows:
        return None
    q = question.lower() if question else ""

    # ── Wide-format (pivoted) single row: all columns are time-period values ──
    if len(rows) == 1 and (_TREND_RE.search(q) or len(rows[0]) >= 4):
        pivoted = _try_wide_pivot(rows[0])
        if pivoted and len(pivoted) >= 3:
            metric = next((k for k in rows[0] if _is_numeric_val(rows[0][k])), "Metric")
            title = f"{metric.replace('_', ' ').title()} Trend Over Time"
            return {"kind": "line", "data": pivoted, "title": title}

    # ── Long format: need at least 2 rows ──────────────────────────────────────
    if len(rows) < 2:
        return None

    cols = list(rows[0].keys())
    if not cols:
        return None

    # Find a numeric metric column (skip the first/label column).
    numeric_cols = [
        c for c in cols[1:]
        if any(_is_numeric_val(row.get(c)) for row in rows)
    ]
    if not numeric_cols:
        return None

    label_col = cols[0]
    metric_col = numeric_cols[0]
    data = [
        {"name": str(r.get(label_col, "")), "value": float(r.get(metric_col, 0))}
        for r in rows
    ]

    # Generate descriptive title
    metric_name = metric_col.replace('_', ' ').title()
    label_name = label_col.replace('_', ' ').title()

    if _TREND_RE.search(q):
        title = f"{metric_name} Trend by {label_name}"
        return {"kind": "line", "data": data, "title": title}
    if _PIE_RE.search(q) and len(rows) <= 12:
        title = f"{metric_name} Distribution by {label_name}"
        return {"kind": "pie", "data": data, "title": title}
    if _BAR_RE.search(q) and len(rows) <= 25:
        title = f"{metric_name} by {label_name}"
        return {"kind": "bar", "data": data, "title": title}
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
_RELATIONSHIP_RE = re.compile(
    r"\b(correlation|relationship|association|vs\.?|versus|impact|effect|call frequency)\b",
    re.IGNORECASE,
)
_EAST_ONLY_Q_RE = re.compile(r"\beast\b", re.IGNORECASE)
_WEST_Q_RE = re.compile(r"\bwest\b", re.IGNORECASE)
_NO_CACHE_Q_RE = re.compile(
    r"(\b(hco|hcp)\b|\b(top\s*\d*|rank|performing|perfom|performance)\b.*\b(hco|hcp|doctor|physician|organization)\b)",
    re.IGNORECASE,
)

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


def _section_heading_normalized(ln: str) -> str:
    s = re.sub(r"^\**\s*", "", ln).strip().rstrip("*").strip().lower().rstrip(":")
    return s


def _inflate_inline_bullet_section(
    text: str,
    *,
    section_heading: str,
    end_heading_prefixes: tuple[str, ...],
) -> str:
    """
    Turn inline bullet runs (• or middot separators) inside a named section into one `- ` line each.
    """
    hl = section_heading.strip().lower()
    if hl not in text.lower():
        return text

    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if _section_heading_normalized(ln) == hl)
    except StopIteration:
        return text

    end = len(lines)
    for j in range(start + 1, len(lines)):
        sh = _section_heading_normalized(lines[j])
        for pref in end_heading_prefixes:
            if sh == pref or sh.startswith(pref + " "):
                end = j
                break
        else:
            continue
        break

    body_lines = lines[start + 1 : end]
    if not body_lines:
        return text

    blob = " ".join(" ".join(body_lines).split())
    if not re.search(r"[\u2022\u00B7]", blob) and blob.count(" · ") < 3:
        return text

    parts = [p.strip() for p in re.split(r"\s+[\u2022]\s+|\s+\u00B7\s+", blob)]
    parts = [re.sub(r"^[-*\s\u2022]+", "", p).strip() for p in parts if p.strip()]
    if len(parts) < 2:
        return text

    block_lines = ["- " + p for p in parts]
    rebuilt = [*lines[: start + 1], *block_lines, *lines[end:]]
    out = "\n".join(rebuilt).strip()
    if text.endswith("\n"):
        out += "\n"
    return out


def _inflate_answer_bullet_lists(text: str) -> str:
    """Flatten inline • / · separators in Key Insights and Top Results."""
    out = text
    out = _inflate_inline_bullet_section(
        out,
        section_heading="key insights",
        end_heading_prefixes=("top results", "supporting observations", "sources checked"),
    )
    out = _inflate_inline_bullet_section(
        out,
        section_heading="top results",
        end_heading_prefixes=("supporting observations", "sources checked"),
    )
    return out


def _looks_aggregated_bucket_rows(rows: list[dict]) -> bool:
    if not rows:
        return False
    keys = " ".join(str(k).lower() for k in rows[0].keys())
    return any(
        token in keys
        for token in ("bucket", "segment", "range", "band", "group", "call_frequency", "call freq")
    )


def _is_blankish_label(v: object) -> bool:
    if v is None:
        return True
    s = str(v).strip().lower()
    if s in ("", "-", "--", "na", "n/a", "null", "none", "(blank)", "blank"):
        return True
    # Common placeholder entities coming from messy source files.
    if s.startswith("unnamed") or s.startswith("anonymous") or s == "unknown":
        return True
    return False


def _drop_unfilled_entity_rows(rows: list[dict]) -> list[dict]:
    """
    Remove rows where the entity/label column is null/blank/placeholder.
    This ensures top-N and counts reflect only filled values.
    """
    if not rows:
        return rows
    label_col, _metric_col = _pick_label_and_metric_cols(rows)
    if not label_col:
        return rows
    cleaned = [r for r in rows if not _is_blankish_label(r.get(label_col))]
    return cleaned if cleaned else rows


def _enforce_relationship_analysis_rules(question: str, answer: str, rows: list[dict]) -> str:
    """Apply safety wording + section naming for variable-relationship analyses."""
    if not _RELATIONSHIP_RE.search(question or ""):
        return answer
    out = answer or ""

    # 1) Correlation vs causation wording guard.
    out = re.sub(r"\bdrives?\b", "is associated with", out, flags=re.IGNORECASE)
    out = re.sub(r"\bcauses?\b", "is associated with", out, flags=re.IGNORECASE)
    out = re.sub(r"\bcausal(?:ly)?\b", "directional", out, flags=re.IGNORECASE)

    # 2) Section relabeling for non-ranked relationship data.
    if not re.search(r"\btop\s+\d+\b|\brank", question or "", flags=re.IGNORECASE):
        out = re.sub(r"(?im)^\s*Top Results\s*:?\s*$", "Segment Performance", out)

    # 3) Explicit limitations for aggregated buckets.
    if _looks_aggregated_bucket_rows(rows) and "HCP-level response cannot be determined" not in out:
        limit_line = (
            "- Limitation: HCP-level response cannot be determined from aggregated bucket data; "
            "individual-level underperformance needs a dedicated entity-level analysis."
        )
        if "Supporting Observations" in out:
            out = re.sub(
                r"(?im)^(\s*Supporting Observations\s*:?\s*)$",
                r"\1\n" + limit_line,
                out,
                count=1,
            )
        elif "Sources checked:" in out:
            head, tail = out.split("Sources checked:", 1)
            out = head.rstrip() + "\n\nSupporting Observations\n" + limit_line + "\n\nSources checked:" + tail
        else:
            out = out.rstrip() + "\n\nSupporting Observations\n" + limit_line
    return out


def _bold_standard_headings(text: str) -> str:
    """
    Bold the required section labels for readability and consistency.
    Applies to:
    Summary, Key Insights, Top Results, Supporting Observations, Total rows, Sources checked
    """
    if not text:
        return text
    out = text
    heading_map = {
        "Summary": "**Summary**",
        "Key Insights": "**Key Insights**",
        "Top Results": "**Top Results**",
        "Supporting Observations": "**Supporting Observations**",
        "Data Quality / Visualization Checks": "**Data Quality / Visualization Checks**",
    }
    lines = out.splitlines()
    bolded: list[str] = []
    for ln in lines:
        s = ln.strip()
        replaced = False
        for plain, strong in heading_map.items():
            if s.lower().rstrip(":") == plain.lower():
                bolded.append(strong)
                replaced = True
                break
        if not replaced:
            bolded.append(ln)
    out = "\n".join(bolded)
    out = re.sub(r"(?im)^sources checked:\s*", "**Sources checked:** ", out)
    out = re.sub(r"(?im)^\*\*sources checked\*\*:\s*", "**Sources checked:** ", out)
    out = re.sub(r"(?im)^total rows:\s*", "**Total rows:** ", out)
    out = re.sub(r"(?im)^\*\*total rows\*\*:\s*", "**Total rows:** ", out)
    return out


def _remove_sources_checked(answer: str) -> str:
    """Remove any Sources checked line while keeping the rest untouched."""
    if not answer:
        return answer
    out = re.sub(r"(?im)^\s*\*\*sources checked\*\*:\s*.*\n?", "", answer)
    out = re.sub(r"(?im)^\s*sources checked:\s*.*\n?", "", out)
    # Be permissive for malformed markdown variants like "Sources checked:** ..."
    out = re.sub(r"(?im)^.*sources\s*checked.*\n?", "", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _finalize_answer_text(answer: str) -> str:
    """Apply final post-processing shared by fresh and cached responses."""
    out = answer or ""
    out = _bold_standard_headings(out)
    out = _remove_sources_checked(out)
    return out


def _enforce_region_scope(question: str, answer: str) -> str:
    """
    If user asks about East only (without asking East vs West), avoid West comparison wording.
    """
    q = question or ""
    out = answer or ""
    east_only = _is_east_only_question(q)
    west_only = _is_west_only_question(q)
    if not east_only and not west_only:
        return out
    banned = "west" if east_only else "east"
    # Remove explicit cross-region comparison phrasing.
    out = re.sub(rf"\bvs\.?\s*{banned}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(rf"\bversus\s*{banned}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(rf"\bthan\s+the\s+{banned}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(rf"\bthan\s+{banned}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(rf"\bcompared\s+to\s+the\s+{banned}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(rf"\bcompared\s+to\s+{banned}\b", "", out, flags=re.IGNORECASE)
    # Drop any opposite-region lines in single-region requests.
    lines = out.splitlines()
    kept: list[str] = []
    for ln in lines:
        if re.search(rf"\b{banned}\b", ln, flags=re.IGNORECASE):
            continue
        kept.append(ln)
    out = "\n".join(kept)
    scope_text = (
        "East-only scope: this explanation focuses on East performance drivers without cross-region comparison."
        if east_only
        else "West-only scope: this explanation focuses on West performance drivers without cross-region comparison."
    )
    if "Summary" in out and scope_text not in out:
        out = re.sub(r"(?im)^(\*\*Summary\*\*|\bSummary\b)\s*$", rf"\1\n- {scope_text}", out, count=1)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _is_east_only_question(question: str) -> bool:
    q = question or ""
    return bool(_EAST_ONLY_Q_RE.search(q) and not _WEST_Q_RE.search(q))


def _is_west_only_question(question: str) -> bool:
    q = question or ""
    return bool(_WEST_Q_RE.search(q) and not _EAST_ONLY_Q_RE.search(q))


def _filter_rows_excluding_region_term(rows: list[dict], banned_term: str) -> list[dict]:
    """Remove rows that explicitly mention a banned region term in any value."""
    if not rows:
        return rows
    kept: list[dict] = []
    for r in rows:
        vals = " ".join(str(v) for v in r.values() if v is not None)
        if re.search(rf"\b{re.escape(banned_term)}\b", vals, flags=re.IGNORECASE):
            continue
        kept.append(r)
    return kept if kept else rows


def _filter_chart_excluding_region_term(chart: dict | None, banned_term: str) -> dict | None:
    """Drop chart rows/slices with banned region labels."""
    if not chart or not isinstance(chart, dict):
        return chart
    data = chart.get("data")
    if not isinstance(data, list):
        return chart
    filtered: list[dict] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        label_blob = " ".join(str(v) for v in d.values() if isinstance(v, (str, int, float)))
        if re.search(rf"\b{re.escape(banned_term)}\b", label_blob, flags=re.IGNORECASE):
            continue
        filtered.append(d)
    if len(filtered) >= 1:
        out = dict(chart)
        out["data"] = filtered
        return out
    return chart


def _enforce_compare_claims_against_rows(question: str, answer: str, rows: list[dict]) -> str:
    """
    For compare asks, remove unsupported cross-region claims if one side is absent in rows.
    Keeps output format; only drops ungrounded lines and adds a data-backed note.
    """
    if not _COMPARE_RE.search(question or "") or not answer:
        return answer
    row_blob = " ".join(" ".join(str(v) for v in r.values() if v is not None) for r in rows)
    has_east = bool(re.search(r"\beast\b", row_blob, flags=re.IGNORECASE))
    has_west = bool(re.search(r"\bwest\b", row_blob, flags=re.IGNORECASE))
    if has_east and has_west:
        return answer
    out = answer
    if not has_west:
        out = re.sub(r"(?im)^.*\bwest\b.*\n?", "", out)
        note = "- Data note: this compare output currently contains only East rows from the executed query result."
    elif not has_east:
        out = re.sub(r"(?im)^.*\beast\b.*\n?", "", out)
        note = "- Data note: this compare output currently contains only West rows from the executed query result."
    else:
        return answer
    if "Supporting Observations" in out and note not in out:
        out = re.sub(r"(?im)^(\*\*Supporting Observations\*\*|\bSupporting Observations\b)\s*$", rf"\1\n{note}", out, count=1)
    elif note not in out:
        out = out.rstrip() + f"\n\n**Supporting Observations**\n{note}"
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _cache_key(question: str) -> str:
    # Versioned to invalidate stale cached answers after output/cleaning logic updates.
    return "v7|" + " ".join((question or "").strip().lower().split())


def _cache_schema_name() -> str:
    return "arcetus_sqlite" if use_sqlite_backend() else "postgres_live"


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
    force_fresh = bool(_TREND_RE.search(q or "") and _MOM_QOQ_RE.search(q or ""))
    no_cache = bool(_NO_CACHE_Q_RE.search(q or ""))
    if q_key and not is_time_volatile_question(q) and not force_fresh and not no_cache:
        local_hit = _LOCAL_QA_CACHE.get(q_key)
        if local_hit:
            cleaned_answer = _finalize_answer_text(local_hit.get("answer", ""))
            return {
                "question": q,
                "sql": local_hit.get("sql"),
                "answer": cleaned_answer,
                "row_count": int(local_hit.get("row_count") or 0),
                "cache_hit": True,
                "sql_agent_llm_rounds": 0,
                "sql_agent_sql_steps": 0,
            }
        # remote_hit = get_cached_pipeline(q, schema=_cache_schema_name())
        # if remote_hit and remote_hit.get("answer"):
        #     cleaned_answer = _finalize_answer_text(remote_hit.get("answer", ""))
        #     _LOCAL_QA_CACHE[q_key] = {
        #         "sql": remote_hit.get("sql"),
        #         "answer": cleaned_answer,
        #         "row_count": int(remote_hit.get("row_count") or 0),
        #     }
        #     return {
        #         "question": q,
        #         "sql": remote_hit.get("sql"),
        #         "answer": cleaned_answer,
        #         "row_count": int(remote_hit.get("row_count") or 0),
        #         "cache_hit": True,
        #         "sql_agent_llm_rounds": 0,
        #         "sql_agent_sql_steps": 0,
        #     }

    agent = SQLAgent()
    resp = agent.run(user_text=q, history=_build_history(conversation), db_state=get_db())

    sql_out = (resp.sql or "").strip() or "(sql-agent)"
    rows = _drop_unfilled_entity_rows(resp.results or [])
    if _is_east_only_question(q):
        rows = _filter_rows_excluding_region_term(rows, "west")
    elif _is_west_only_question(q):
        rows = _filter_rows_excluding_region_term(rows, "east")
    answer = sanitize_user_visible_text(strip_sql_from_nl_chat_markup(resp.content or "")) or ""
    answer = _remove_sql_logic_from_answer(answer)
    answer = _remove_markdown_tables(answer)
    answer = _normalize_answer_sections(answer)
    answer = _normalize_dataset_naming(answer)
    answer = _inflate_answer_bullet_lists(answer)
    answer = _enforce_relationship_analysis_rules(q, answer, rows)
    answer = _enforce_region_scope(q, answer)
    answer = _enforce_compare_claims_against_rows(q, answer, rows)
    deterministic_trend = _build_trend_math_answer(q, rows)
    if deterministic_trend:
        answer = deterministic_trend
    answer = _finalize_answer_text(answer)
    err = sanitize_user_visible_text(resp.error)

    if not err and rows:
        answer = append_row_count_note(answer, total=len(rows))

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
        if _is_east_only_question(q):
            chart = _filter_chart_excluding_region_term(chart, "west")
        elif _is_west_only_question(q):
            chart = _filter_chart_excluding_region_term(chart, "east")
        if chart:
            out["chart"] = chart
        elif _RELATIONSHIP_RE.search(q):
            # Relationship analyses must include a comparable visualization when possible.
            label_col, metric_col = _pick_label_and_metric_cols(rows)
            if label_col and metric_col:
                rel_data: list[dict] = []
                for r in rows:
                    name = _row_label_string(r, label_col)
                    val = _scalar_for_metric(r.get(metric_col))
                    if val is None:
                        continue
                    if _is_blankish_label(name):
                        continue
                    rel_data.append({"name": name or "(blank)", "value": val})
                if len(rel_data) >= 2:
                    out["chart"] = {"kind": "bar", "data": rel_data[:12]}

    if err:
        out["error"] = err
    elif q_key and answer and not is_time_volatile_question(q) and not no_cache:
        _LOCAL_QA_CACHE[q_key] = {
            "sql": sql_out,
            "answer": answer,
            "row_count": len(rows),
        }
        try:
            set_cached_pipeline(
                q,
                schema=_cache_schema_name(),
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
            "answer": "Hi, I am your Arcutis data query assistant, how can I help you?",
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
