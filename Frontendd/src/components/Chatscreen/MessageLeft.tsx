'use client';
import React, { useMemo, useState } from 'react';
import { BeatLoader } from 'react-spinners';
import { AiOutlineStar, AiFillStar } from 'react-icons/ai';
import { FiCopy } from 'react-icons/fi';
import Image from 'next/image';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import styles from './MessageLeft.module.css';
import { QueryResultChart, type ChartPayload } from './QueryResultChart';
import { ResultTablePanel, type ResultTablePayload } from './ResultTablePanel';
import { shouldOmitResultTablePanelAsMarkdownDuplicate } from '@/utils/markdownResultTable';
import { downloadResultTableCsv } from '@/utils/resultTableCsv';
import { userRequestedDataTable } from '@/utils/userRequestedDataTable';

export interface AssistantMessageMeta {
  cacheHit?: boolean;
  durationMs?: number;
  /** Last generated SQL — used client-side to anchor follow-ups when server memory is empty. */
  sql?: string;
  /** True when the API returned a logical or transport error (shown inline, no thrown error). */
  failed?: boolean;
  /** Bar/pie/line chart when the API returned a chart payload (explicit “chart” request and/or auto-chart). */
  chart?: ChartPayload;
  /** Tabular query result for column/row preview and CSV download. */
  result_table?: ResultTablePayload;
  result_table_multipart_last_part?: boolean;
  /** Pipeline row count from API — used only for summaries vs. in-memory rows. */
  row_count?: number;
  /** How many rows to show before expand (explicit N from question, else 10, or 1 for bare "top/most"). */
  result_display_preview_rows?: number;
}

interface MessageLeftProps {
  content: string;
  isLoading?: boolean;
  meta?: AssistantMessageMeta;
  /** Last user message before this assistant reply — used to detect “show as table” intent. */
  pairedUserQuestion?: string;
}

function formatCell(v: unknown): string {
  if (v == null) return '—';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** Replaces a markdown data table when CSV is available: one NL line per row (no HTML grid). */
function ResultRowsNlPreview({
  columns,
  rows,
  maxRows = 30,
}: {
  columns: string[];
  rows: Record<string, unknown>[];
  maxRows?: number;
}) {
  const slice = rows.slice(0, Math.max(1, maxRows));
  if (!slice.length) return null;
  return (
    <div className="mt-3 rounded-lg border border-gray-100 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800">
      <p className="m-0 mb-2 text-xs font-medium text-black">
        Row-by-row view — full spreadsheet: use <span className="font-semibold text-black">Download CSV</span>{' '}
        below.
      </p>
      <ul className="m-0 list-none space-y-2 p-0">
        {slice.map((row, idx) => (
          <li
            key={idx}
            className="border-b border-gray-200/80 pb-2 text-[13px] leading-snug last:border-0 last:pb-0"
          >
            {columns.map((c) => `${c}: ${formatCell(row[c])}`).join(' · ')}
          </li>
        ))}
      </ul>
    </div>
  );
}

const MessageLeft: React.FC<MessageLeftProps> = ({
  content,
  isLoading = false,
  meta,
  pairedUserQuestion,
}) => {
  const [rating, setRating] = useState(0);
  const [hoveredRating, setHoveredRating] = useState(0);
  const [copied, setCopied] = useState(false);

  const resultTable = meta?.result_table;
  const hasResultTablePayload =
    !!resultTable &&
    resultTable.columns.length > 0 &&
    resultTable.rows.length > 0;
  const omitResultTablePanelDuplicate =
    hasResultTablePayload &&
    shouldOmitResultTablePanelAsMarkdownDuplicate(content, resultTable);
  const pipelineRowCount = meta?.row_count ?? 0;
  const tableTotal =
    typeof resultTable?.total_row_count === 'number' ? resultTable.total_row_count : 0;
  const effectiveTotal = Math.max(pipelineRowCount, tableTotal, resultTable?.rows.length ?? 0);
  // Do not hide the CSV strip when the DB returned more rows than fit in the preview payload
  // (or than the markdown table), or when the pipeline row_count exceeds the preview cap.
  // Always show the ResultTablePanel when we have a payload (unless markdown duplicates it).
  // Small ≤10-row answers still get the grid + chart per product spec — do not hide the panel.
  const omitResultTablePanel = omitResultTablePanelDuplicate;

  const wantExplicitTable = userRequestedDataTable(pairedUserQuestion);
  const replaceMarkdownTableWithNl =
    hasResultTablePayload && resultTable && !wantExplicitTable;

  const markdownComponents = useMemo(() => {
    if (!replaceMarkdownTableWithNl || !resultTable) return undefined;
    const cols = resultTable.columns;
    const rws = resultTable.rows;
    return {
      table: () => (
        <ResultRowsNlPreview
          columns={cols}
          rows={rws}
          maxRows={meta?.result_display_preview_rows ?? 30}
        />
      ),
    };
  }, [
    replaceMarkdownTableWithNl,
    resultTable,
    meta?.result_display_preview_rows,
  ]);

  const handleRatingClick = (star: number) => {
    setRating(star);
  };

  const handleCopy = () => {
    navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  // Only render loading state if there's no content
  if (isLoading && (!content || content.trim() === '')) {
    return (
      <div className="flex justify-start mb-6">
        <div className="flex-shrink-0">
          <div className="w-[44px] h-[44px] mb-2 bg-white rounded-full flex items-center justify-center mr-3 shadow-sm border border-gray-100 overflow-hidden">
            <Image
              src="/Images/BotIconInsightSphere.svg"
              alt="Assistant"
              width={40}
              height={40}
              className="h-10 w-10 object-contain p-0.5"
            />
          </div>
        </div>
        <div className="max-w-[70%]">
          <div className="flex items-start space-x-3">
            <div className="flex-1">
              <div className="bg-white rounded-[20px] rounded-tl-[4px] px-6 py-4 shadow-sm border border-gray-100">
                <div className="flex items-center space-x-3">
                  <BeatLoader color="#001e96" size={8} margin={2} />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Render message with content
  return (
    <div className="flex justify-start mb-2">
      <div className="flex-shrink-0">
        <div className="w-[44px] h-[44px] mb-2 bg-white rounded-full flex items-center justify-center mr-3 shadow-sm border border-gray-100 overflow-hidden">
          <Image
            src="/Images/BotIconInsightSphere.svg"
            alt="Assistant"
            width={40}
            height={40}
            className="h-10 w-10 object-contain p-0.5"
          />
        </div>
      </div>
      <div className="max-w-[70%]">
        <div className="flex items-start space-x-3">
          <div className="flex-1">
            <div className="bg-white rounded-[20px] rounded-tl-[4px] px-6 py-4 shadow-sm border border-gray-100">
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                <div
                  className={`${styles.messageMarkdownContent} text-content text-sm leading-[22px] font-normal`}
                  style={{
                    wordWrap: 'break-word',
                    overflowWrap: 'break-word',
                    flex: 1,
                  }}
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                    {content}
                  </ReactMarkdown>
                  {meta?.chart ? (  // ← SIMPLIFIED: just check existence
                    <QueryResultChart chart={meta.chart} />
                  ) : null}
                  {hasResultTablePayload && omitResultTablePanel ? (
                    <div className="mt-3 flex flex-wrap justify-end gap-2">
                      <button
                        type="button"
                        onClick={() =>
                          downloadResultTableCsv(resultTable.columns, resultTable.rows)
                        }
                        className="shrink-0 rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-black hover:bg-gray-50"
                      >
                        {omitResultTablePanelDuplicate
                          ? 'Download CSV (same rows as table above)'
                          : 'Download CSV (rows in this answer)'}
                      </button>
                    </div>
                  ) : null}
                </div>
                {(meta?.cacheHit === true ||
                  meta?.cacheHit === false ||
                  meta?.durationMs != null) && (
                  <p className="mt-2 text-[11px] text-black leading-tight">
                    {meta?.cacheHit === true ? 'Loaded from cache' : 'Fresh query'}
                    {typeof meta?.durationMs === 'number' ? ` · ${meta.durationMs} ms` : ''}
                  </p>
                )}
                {/* Show loader next to content if still streaming */}
                {isLoading && (
                  <div style={{ display: 'flex', alignItems: 'center', paddingLeft: 8 }}>
                    <BeatLoader color="#001e96" size={6} margin={2} />
                  </div>
                )}
              </div>
            </div>

            {/* Rating and Copy Controls */}
            <div className="relative mt-2 flex items-center px-2">
              <div className="flex space-x-1">
                {[1, 2, 3, 4, 5].map((star) => (
                  <button
                    key={star}
                    onClick={() => handleRatingClick(star)}
                    onMouseEnter={() => setHoveredRating(star)}
                    onMouseLeave={() => setHoveredRating(0)}
                    className="cursor-pointer transition-opacity text-black"
                    aria-label={`Rate ${star} star${star > 1 ? 's' : ''}`}
                  >
                    {hoveredRating ? (
                      star <= hoveredRating ? (
                        <AiFillStar size={18} color="#f2d322" />
                      ) : (
                        <AiOutlineStar size={18} />
                      )
                    ) : rating >= star ? (
                      <AiFillStar size={18} color="#f2d322" />
                    ) : (
                      <AiOutlineStar size={18} />
                    )}
                  </button>
                ))}
                <button
                  onClick={handleCopy}
                  className="flex cursor-pointer items-center ml-2 space-x-1 text-black hover:text-black text-xs"
                  aria-label="Copy message"
                >
                  <FiCopy />
                </button>
              </div>
              {copied && (
                <div className={styles.copiedTooltip}>
                  Copied to clipboard
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default MessageLeft;
