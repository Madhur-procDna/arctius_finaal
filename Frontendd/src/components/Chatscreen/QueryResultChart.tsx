'use client';

import React, { useMemo } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

export type ChartKind = 'bar' | 'pie' | 'line' | 'stacked_bar';

export interface ChartPayload {
  kind: ChartKind;
  data: Record<string, unknown>[];
  /** Descriptive title for the chart. */
  title?: string;
  /** When set (2+ keys), line chart draws one series per key (e.g. YoY: revenue_2024, revenue_2025). */
  lineSeriesKeys?: string[];
  /** For stacked bar charts: one key per stack segment (e.g. territories). */
  stackSeriesKeys?: string[];
}

const BRAND = '#001e96';
const PALETTE = ['#001e96', '#1f3db5', '#3b5ccf', '#5a7ae0', '#7d99ec', '#c7a33c', '#9aa1aa'];

function parseChartNumber(raw: unknown): number {
  if (typeof raw === 'number' && Number.isFinite(raw)) return raw;
  if (typeof raw === 'string') {
    const s = raw.trim().replace(/,/g, '').replace(/%/g, '').trim();
    const n = Number(s);
    return Number.isFinite(n) ? n : NaN;
  }
  const n = Number(raw);
  return Number.isFinite(n) ? n : NaN;
}

/** Map heterogeneous SQL row dicts (or API-normalized { name, value }) to { name, value } for Recharts. */
function normalizeChartData(rows: Record<string, unknown>[]): { name: string; value: number }[] {
  if (
    rows.length > 0 &&
    rows.every((r) => r != null && typeof r === 'object') &&
    'name' in (rows[0] as object) &&
    'value' in (rows[0] as object)
  ) {
    return rows.map((row) => {
      const r = row as Record<string, unknown>;
      const v = parseChartNumber(r.value);
      return { name: String(r.name ?? ''), value: Number.isFinite(v) ? v : 0 };
    });
  }
  return rows.map((row) => {
    const entries = Object.entries(row);
    const name = String(entries[0]?.[1] ?? '');
    let value = 0;
    for (let i = 1; i < entries.length; i++) {
      const raw = entries[i][1];
      const n = parseChartNumber(raw);
      if (!Number.isNaN(n) && Number.isFinite(n)) {
        value = n;
        break;
      }
    }
    return { name, value };
  });
}

const YEAR_ALIASES = ['year', 'sale_year', 'calendar_year', 'fiscal_year', 'yr', 'yr_num'] as const;
const MONTH_ALIASES = ['month', 'calendar_month', 'mnth', 'cal_month', 'month_num'] as const;

function resolveKey(row: Record<string, unknown>, aliases: readonly string[]): string | undefined {
  const lower = new Map(Object.keys(row).map((k) => [k.toLowerCase(), k]));
  for (const a of aliases) {
    const k = lower.get(a);
    if (k) return k;
  }
  return undefined;
}

/**
 * Long SQL rows (year, month, revenue) → one row per month with revenue_2024, revenue_2025, …
 * so Recharts can draw multiple Line series. Matches backend pivot logic.
 */
function tryPivotYoYLongFormat(rows: Record<string, unknown>[]): {
  data: Record<string, unknown>[];
  seriesKeys: string[];
} | null {
  if (!rows.length || rows.length < 4) return null;
  const r0 = rows[0];
  const yk = resolveKey(r0, YEAR_ALIASES);
  const mk = resolveKey(r0, MONTH_ALIASES);
  const lower = new Map(Object.keys(r0).map((k) => [k.toLowerCase(), k]));
  const mnamek = lower.get('month_name') ?? lower.get('monthname');

  if (!yk || !mk) return null;

  let metricKey: string | null = null;
  const metricHints = /revenue|total|amount|sales|sum|trx|nrx|value/i;
  for (const k of Object.keys(r0)) {
    if (k === yk || k === mk || k === mnamek) continue;
    const n = parseChartNumber(r0[k]);
    if (!Number.isFinite(n)) continue;
    if (metricHints.test(k)) {
      metricKey = k;
      break;
    }
  }
  if (!metricKey) {
    for (const k of Object.keys(r0)) {
      if (k === yk || k === mk || k === mnamek) continue;
      if (Number.isFinite(parseChartNumber(r0[k]))) {
        metricKey = k;
        break;
      }
    }
  }
  if (!metricKey) return null;

  const byMonth = new Map<string | number, Record<string, unknown>>();
  const years = new Set<number>();

  for (const row of rows) {
    const yv = parseChartNumber(row[yk]);
    const mv = row[mk];
    if (!Number.isFinite(yv)) continue;
    const yi = Math.floor(yv);
    if (yi < 1990 || yi > 2100) continue;
    years.add(yi);

    const pm = parseChartNumber(mv);
    const mKey =
      Number.isFinite(pm) && Math.abs(pm - Math.round(pm)) < 1e-9 ? Math.round(pm) : (mv as string | number);

    const label =
      mnamek && row[mnamek] != null && String(row[mnamek]).trim() !== ''
        ? String(row[mnamek]).trim()
        : String(mKey);

    const col = `${metricKey}_${yi}`;
    const pv = parseChartNumber(row[metricKey]);
    const val = Number.isFinite(pv) ? pv : 0;

    const existing = byMonth.get(mKey as string | number);
    if (!existing) {
      byMonth.set(mKey as string | number, { _label: label, [col]: val });
    } else {
      existing[col] = val;
      if (!existing._label) existing._label = label;
    }
  }

  if (years.size < 2) return null;

  const yearsSorted = [...years].sort((a, b) => a - b);
  const seriesKeys = yearsSorted.map((yi) => `${metricKey}_${yi}`);

  const sortedMonths = [...byMonth.keys()].sort((a, b) => {
    const na = typeof a === 'number' ? a : parseChartNumber(a);
    const nb = typeof b === 'number' ? b : parseChartNumber(b);
    if (Number.isFinite(na) && Number.isFinite(nb)) return (na as number) - (nb as number);
    return String(a).localeCompare(String(b));
  });

  const data: Record<string, unknown>[] = [];
  for (const mKey of sortedMonths) {
    const cell = byMonth.get(mKey)!;
    const pt: Record<string, unknown> = { name: (cell._label as string) ?? String(mKey) };
    for (const sk of seriesKeys) {
      const v = cell[sk];
      pt[sk] = typeof v === 'number' && Number.isFinite(v) ? v : 0;
    }
    data.push(pt);
  }

  return { data, seriesKeys };
}

type LineChartState =
  | { lineData: Record<string, unknown>[]; seriesKeys: string[]; multi: boolean }
  | null;

function buildLineChartState(chart: ChartPayload): LineChartState {
  if (chart.kind !== 'line') return null;
  const raw = chart.data as Record<string, unknown>[];
  if (!raw.length) return null;

  if (Array.isArray(chart.lineSeriesKeys) && chart.lineSeriesKeys.length >= 2) {
    return { lineData: raw, seriesKeys: chart.lineSeriesKeys, multi: true };
  }

  const pivoted = tryPivotYoYLongFormat(raw);
  if (pivoted) {
    return { lineData: pivoted.data, seriesKeys: pivoted.seriesKeys, multi: true };
  }

  return {
    lineData: normalizeChartData(raw) as unknown as Record<string, unknown>[],
    seriesKeys: ['value'],
    multi: false,
  };
}

const RenderTitle = ({ title, kind }: { title?: string; kind: ChartKind }) => {
  if (!title) return null;
  return (
    <h3 className="text-lg font-semibold mb-3 text-gray-900 px-2">
      {title} ({kind.toUpperCase()})
    </h3>
  );
};

export const QueryResultChart: React.FC<{ chart: ChartPayload }> = ({ chart }) => {
  const lineState = useMemo(() => buildLineChartState(chart), [chart]);

  const pieBarData = useMemo(() => {
    if (chart.kind === 'line') return [];
    return normalizeChartData(chart.data);
  }, [chart.kind, chart.data]);

  const common = (
    <Tooltip
      formatter={(value) => {
        const v = value as number | string | undefined;
        const s = typeof v === 'number' ? v.toLocaleString() : String(v ?? '');
        return [s, ''];
      }}
      labelFormatter={(label) => String(label)}
    />
  );

  const chartContainerStyle = { height: chart.kind === 'pie' ? 320 : 300 };

  if (chart.kind === 'pie') {
    if (!pieBarData.length) return null;
    return (
      <div className="mt-4 w-full min-w-0" style={chartContainerStyle}>
        <RenderTitle title={chart.title} kind={chart.kind} />
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={pieBarData}
              dataKey="value"
              nameKey="name"
              cx="50%"
              cy="50%"
              outerRadius={110}
              label={({ name, percent }) =>
                `${name} (${(((percent ?? 0) as number) * 100).toFixed(0)}%)`
              }
            >
              {pieBarData.map((_, i) => (
                <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
              ))}
            </Pie>
            {common}
          </PieChart>
        </ResponsiveContainer>
      </div>
    );
  }

  if (chart.kind === 'line') {
    if (!lineState?.lineData.length) return null;
    const { lineData, seriesKeys, multi } = lineState;

    const lineTooltip = multi ? (
      <Tooltip
        formatter={(value, name) => {
          const v = value as number | string | undefined;
          const s = typeof v === 'number' ? v.toLocaleString() : String(v ?? '');
          return [s, String(name ?? '')];
        }}
        labelFormatter={(label) => String(label)}
      />
    ) : (
      common
    );

    const metricName = chart.title?.split(' ')[0] || 'Value';

    return (
      <div className="mt-4 w-full min-w-0" style={chartContainerStyle}>
        <RenderTitle title={chart.title} kind={chart.kind} />
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={lineData} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
            <XAxis 
              dataKey="name" 
              tick={{ fontSize: 11 }} 
              interval={0} 
              angle={-25} 
              textAnchor="end" 
              height={70}
              label={{ value: 'Time Periods', position: 'insideBottom', offset: -5, fill: '#666' }}
            />
            <YAxis 
              tick={{ fontSize: 11 }} 
              tickFormatter={(v) => Number(v).toLocaleString()}
              label={{ 
                value: metricName, 
                angle: -90, 
                position: 'insideLeft',
                fill: '#666'
              }}
            />
            {lineTooltip}
            {multi ? <Legend wrapperStyle={{ fontSize: 12 }} /> : null}
            {seriesKeys.map((key, i) => (
              <Line
                key={key}
                type="monotone"
                dataKey={key}
                name={key.replace(/_/g, ' ')}
                stroke={multi ? PALETTE[i % PALETTE.length] : BRAND}
                strokeWidth={2}
                dot={{ r: 3 }}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    );
  }

  if (chart.kind === 'stacked_bar') {
    const rows = chart.data as Record<string, unknown>[];
    if (!rows.length) return null;
    const series =
      Array.isArray(chart.stackSeriesKeys) && chart.stackSeriesKeys.length
        ? chart.stackSeriesKeys
        : Object.keys(rows[0]).filter((k) => k !== 'name');
    return (
      <div className="mt-4 w-full min-w-0" style={chartContainerStyle}>
        <RenderTitle title={chart.title} kind={chart.kind} />
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
            <XAxis dataKey="name" tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} tickFormatter={(v) => Number(v).toLocaleString()} />
            <Tooltip
              formatter={(value, name) => {
                const v = Number(value ?? 0);
                return [Number.isFinite(v) ? v.toLocaleString() : String(value ?? ''), String(name ?? '')];
              }}
              labelFormatter={(label) => String(label)}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            {series.map((k, i) => (
              <Bar key={k} dataKey={k} stackId="stack" fill={PALETTE[i % PALETTE.length]} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
    );
  }

  if (!pieBarData.length) return null;

  const metricName = chart.title?.split(' ')[0] || 'Value';

  return (
    <div className="mt-4 w-full min-w-0" style={chartContainerStyle}>
      <RenderTitle title={chart.title} kind={chart.kind} />
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={pieBarData} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
          <XAxis 
            dataKey="name" 
            tick={{ fontSize: 11 }} 
            interval={0} 
            angle={-25} 
            textAnchor="end" 
            height={70}
            label={{ value: 'Categories', position: 'insideBottom', offset: -5, fill: '#666' }}
          />
          <YAxis 
            tick={{ fontSize: 11 }} 
            tickFormatter={(v) => Number(v).toLocaleString()}
            label={{ 
              value: metricName, 
              angle: -90, 
              position: 'insideLeft',
              fill: '#666'
            }}
          />
          {common}
          <Bar dataKey="value" fill={BRAND} radius={[4, 4, 0, 0]} maxBarSize={56} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
};