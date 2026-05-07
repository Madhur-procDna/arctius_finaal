import type { ChartPayload } from '@/components/Chatscreen/QueryResultChart';
import type { ResultTablePayload } from '@/components/Chatscreen/ResultTablePanel';

export type { ChartPayload, ResultTablePayload };

/** One semicolon-separated sub-query result (workbook / SQLite multipart). */
export interface SubQueryResult {
  index: number;
  question: string;
  response: string;
  sql?: string;
  row_count?: number;
  error?: string;
  chart?: ChartPayload;
  result_table?: ResultTablePayload;
}

interface QueryResponse {
  success: boolean;
  response: string;
  error?: string;
  /** Wall-clock ms for the Python pipeline (FastAPI); also sent as X-Process-Time-Ms */
  duration_ms?: number;
  /** True when the backend served this from Redis QA cache (same question, non-time-volatile). */
  cache_hit?: boolean;
  row_count?: number;
  sql?: string;
  chart?: ChartPayload;
  result_table?: ResultTablePayload;
  result_table_multipart_last_part?: boolean;
  /** Multiple independent answers (semicolon-separated NL in one request). */
  sub_results?: SubQueryResult[];
}

/** `queryChat` return value — check `ok` before treating as a successful data answer. */
export interface QueryChatResult {
  ok: boolean;
  response: string;
  duration_ms?: number;
  cache_hit?: boolean;
  row_count?: number;
  sql?: string;
  chart?: ChartPayload;
  result_table?: ResultTablePayload;
  result_table_multipart_last_part?: boolean;
  sub_results?: SubQueryResult[];
}

/** Completed user → assistant pairs (oldest first). Sent when the server has no in-RAM buffer so multi-turn still works across workers / reloads. */
export type PriorTurn = { user: string; assistant: string };

/** Build up to `maxPairs` completed exchanges from chat messages (oldest first). */
export function buildPriorTurnsForQuery(
  messages: { role: string; content: string }[],
  maxPairs = 6,
): PriorTurn[] {
  const turns: PriorTurn[] = [];
  for (let i = 0; i < messages.length; i++) {
    if (messages[i].role !== 'user') continue;
    const user = messages[i].content.trim();
    if (!user) continue;
    for (let j = i + 1; j < messages.length; j++) {
      if (messages[j].role === 'user') break;
      if (messages[j].role === 'assistant') {
        const chunks: string[] = [];
        let k = j;
        while (k < messages.length && messages[k].role === 'assistant') {
          chunks.push(messages[k].content.trim());
          k += 1;
        }
        turns.push({ user, assistant: chunks.join('\n\n').slice(0, 16000) });
        break;
      }
    }
  }
  return turns.slice(-maxPairs);
}

interface QueryParams {
  question: string;
  sessionId: string;
  /** Last user question that produced `previousSql` — sent so follow-ups work if server buffer is empty. */
  previousQuestion?: string;
  /** SQL from the last successful assistant answer — anchors short follow-ups like "by territory". */
  previousSql?: string;
  /** Recent Q→A pairs so the API can seed context when its buffer is empty (multi-turn / multi-worker). */
  priorTurns?: PriorTurn[];
}

/**
 * Where to POST /query (JSON body — no query string; avoids URL length limits on previous_sql):
 * - In production → always use same-origin **POST /api/query** so Next.js server proxies to
 *   SDA_BACKEND_URL and browser CORS/network differences don't break requests.
 * - In development, if NEXT_PUBLIC_SERVER_URL is set → browser calls FastAPI **POST /query**.
 *   Use the API origin only (e.g. http://127.0.0.1:8000), not .../api.
 * - Else in development → same-origin **POST /api/query**.
 */
function buildQueryUrl(): string {
  if (process.env.NODE_ENV === 'production') {
    return '/api/query';
  }

  const direct = process.env.NEXT_PUBLIC_SERVER_URL?.trim().replace(/\/$/, '');
  if (direct) {
    const base = direct.replace(/\/api\/?$/, '');
    return new URL('/query', base.endsWith('/') ? base : `${base}/`).toString();
  }
  return '/api/query';
}

function parseChartPayload(
  chart: unknown,
): ChartPayload | undefined {
  if (
    chart &&
    typeof chart === 'object' &&
    (chart as { kind?: string }).kind &&
    ['bar', 'pie', 'line', 'stacked_bar'].includes(String((chart as { kind: string }).kind)) &&
    Array.isArray((chart as { data?: unknown }).data)
  ) {
    const c = chart as ChartPayload & { 
      lineSeriesKeys?: string[]; 
      stackSeriesKeys?: string[];
      title?: string;  // ← Explicitly type for title
    };
    return {
      kind: c.kind,
      data: c.data as Record<string, unknown>[],
      title: typeof c.title === 'string' ? c.title : undefined,  // ← EXTRACT TITLE
      ...(Array.isArray(c.lineSeriesKeys) && c.lineSeriesKeys.length > 0
        ? { lineSeriesKeys: c.lineSeriesKeys }
        : {}),
      ...(Array.isArray(c.stackSeriesKeys) && c.stackSeriesKeys.length > 0
        ? { stackSeriesKeys: c.stackSeriesKeys }
        : {}),
    };
  }
  return undefined;
}

function parseResultTablePayload(rt: unknown): ResultTablePayload | undefined {
  if (
    rt &&
    typeof rt === 'object' &&
    Array.isArray((rt as { columns?: unknown }).columns) &&
    Array.isArray((rt as { rows?: unknown }).rows) &&
    (rt as { columns: string[] }).columns.length > 0 &&
    (rt as { rows: unknown[] }).rows.length > 0
  ) {
    const t = rt as ResultTablePayload & {
      total_row_count?: number;
      truncated?: boolean;
      truncated_for_storage?: boolean;
      suppress_remaining_rows?: boolean;
    };
    return {
      columns: t.columns,
      rows: t.rows as Record<string, unknown>[],
      total_row_count: typeof t.total_row_count === 'number' ? t.total_row_count : undefined,
      truncated: Boolean(t.truncated),
      truncated_for_storage: Boolean(t.truncated_for_storage),
      suppress_remaining_rows: Boolean(t.suppress_remaining_rows),
    };
  }
  return undefined;
}

function formatHttpError(status: number, bodyText: string): string {
  const raw = bodyText.trim();
  const lower = raw.toLowerCase();
  if (
    status === 503 &&
    (lower.includes('service suspended') ||
      lower.includes('has been suspended by its owner') ||
      lower.includes('<title>service suspended</title>'))
  ) {
    return [
      'Backend service is currently suspended on Render.',
      'Open gilead-backend in Render and click Resume/Unsuspend, then redeploy once.',
      'Verify: https://gilead-backend.onrender.com/health returns JSON before retrying chat.',
    ].join(' ');
  }
  try {
    const j = JSON.parse(raw) as { detail?: unknown; error?: unknown; response?: unknown };
    if (j.detail != null) return typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    if (j.error != null) return typeof j.error === 'string' ? j.error : JSON.stringify(j.error);
    if (j.response != null) return typeof j.response === 'string' ? j.response : JSON.stringify(j.response);
  } catch {
    if (raw.startsWith('<!DOCTYPE html') || raw.startsWith('<html')) {
      return `HTTP ${status}: upstream HTML error page returned by backend. Check backend logs and /health.`;
    }
  }
  return raw || `HTTP ${status}`;
}

export const queryChat = async ({
  question,
  sessionId,
  previousQuestion,
  previousSql,
  priorTurns,
}: QueryParams): Promise<QueryChatResult> => {
  const url = buildQueryUrl();
  const body = JSON.stringify({
    question,
    session_id: sessionId,
    ...(previousQuestion?.trim() && previousSql?.trim()
      ? { previous_question: previousQuestion.trim(), previous_sql: previousSql.trim() }
      : {}),
    ...(priorTurns && priorTurns.length > 0 ? { prior_turns: priorTurns } : {}),
  });

  /** NL→SQL pipelines can exceed 60s; match proxy default (10 min unless overridden). */
  const _ft = Number(process.env.NEXT_PUBLIC_QUERY_FETCH_TIMEOUT_MS);
  const fetchTimeoutMs = Math.max(
    120_000,
    Number.isFinite(_ft) && _ft > 0 ? _ft : 600_000,
  );

  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body,
      signal: AbortSignal.timeout(fetchTimeoutMs),
    });
  } catch (e) {
    const aborted =
      e instanceof Error &&
      (e.name === 'AbortError' ||
        (typeof DOMException !== 'undefined' && e instanceof DOMException && e.name === 'AbortError'));
    if (aborted) {
      return {
        ok: false,
        response: `Request timed out after ${fetchTimeoutMs / 1000}s. The NL→SQL pipeline or database may need more time — increase NEXT_PUBLIC_QUERY_FETCH_TIMEOUT_MS and SDA_QUERY_PROXY_TIMEOUT_MS, and set POSTGRES_STATEMENT_TIMEOUT_MS / AZURE_OPENAI_HTTP_TIMEOUT_SEC on the backend.`,
      };
    }
    const hint =
      process.env.NEXT_PUBLIC_SERVER_URL?.trim()
        ? `Cannot reach ${process.env.NEXT_PUBLIC_SERVER_URL}`
        : 'Cannot reach Next.js API route. Is `npm run dev` running?';
    const msg =
      e instanceof TypeError
        ? `${hint}. Start the Python API on the port in Frontendd/.env.local (SDA_BACKEND_URL), e.g. Backend\\\\run_api.ps1 or uvicorn on 8000/8001.`
        : String(e);
    return { ok: false, response: msg };
  }

  const bodyText = await response.text();

  if (!response.ok) {
    const detail = formatHttpError(response.status, bodyText);
    if (response.status === 404) {
      const hint404 =
        process.env.NEXT_PUBLIC_SERVER_URL?.trim()
          ? ' Check NEXT_PUBLIC_SERVER_URL is the FastAPI origin only (e.g. http://127.0.0.1:8000), not .../api. Or remove it to use the /api/query proxy in dev.'
          : ' In dev, the route should be POST /api/query — if you see 404, restart `npm run dev` after adding src/app/api/query/route.ts.';
      return { ok: false, response: `API 404: ${detail}.${hint404}` };
    }
    return { ok: false, response: `API ${response.status}: ${detail}` };
  }

  let data: QueryResponse;
  try {
    data = JSON.parse(bodyText) as QueryResponse;
  } catch {
    return { ok: false, response: `Invalid JSON from API: ${bodyText.slice(0, 200)}` };
  }

  if (typeof data.duration_ms === 'number' && process.env.NODE_ENV === 'development') {
    console.info(`[SDA] query duration: ${data.duration_ms}ms`);
  }

  if (data.success) {
    const chartPayload = parseChartPayload(data.chart);
    const resultTable = parseResultTablePayload(data.result_table);
    const rawSubs = data.sub_results;
    const sub_results: SubQueryResult[] | undefined =
      Array.isArray(rawSubs) && rawSubs.length > 0
        ? rawSubs.map((sr) => {
            const o = sr as unknown as Record<string, unknown>;
            return {
              index: typeof o.index === 'number' ? o.index : Number(o.index) || 0,
              question: typeof o.question === 'string' ? o.question : '',
              response: typeof o.response === 'string' ? o.response : '',
              sql: typeof o.sql === 'string' ? o.sql : undefined,
              row_count: typeof o.row_count === 'number' ? o.row_count : undefined,
              error: typeof o.error === 'string' ? o.error : undefined,
              chart: parseChartPayload(o.chart),
              result_table: parseResultTablePayload(o.result_table),
            };
          })
        : undefined;
    return {
      ok: true,
      response: data.response,
      duration_ms: data.duration_ms,
      cache_hit: Boolean(data.cache_hit),
      row_count: typeof data.row_count === 'number' ? data.row_count : undefined,
      sql: typeof data.sql === 'string' ? data.sql : undefined,
      chart: chartPayload,
      result_table: resultTable,
      result_table_multipart_last_part: Boolean(data.result_table_multipart_last_part),
      sub_results,
    };
  }
  return {
    ok: false,
    response: data.error || data.response || 'Unknown error from API',
    duration_ms: data.duration_ms,
    sql: typeof data.sql === 'string' ? data.sql : undefined,
  };
};
const CHAT_SESSION_MAP_KEY = 'chatSessionByChatId';

/** One backend `session_id` per UI chat so switching threads keeps separate context. */
export function getOrCreateSessionIdForChat(chatId: string): string {
  if (typeof window === 'undefined') return '1234';
  const raw = localStorage.getItem(CHAT_SESSION_MAP_KEY);
  const map: Record<string, string> = raw ? (JSON.parse(raw) as Record<string, string>) : {};
  if (!map[chatId]) {
    map[chatId] = `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
    localStorage.setItem(CHAT_SESSION_MAP_KEY, JSON.stringify(map));
  }
  return map[chatId];
}
