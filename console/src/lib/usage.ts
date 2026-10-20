// Admin › Usage: number formatting, and the per-call drill-down (GET
// /usage/calls): its query params, paging and how a call reads in the
// table. Pure functions (tested in tests/usage.test.mjs).
import type { UsageCall, UsageFilters } from '../types';

export function formatUsd(value: number | null | undefined): string {
  if (value == null) return '—';
  if (value > 0 && value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return `${n}`;
}

/** Calls per page of the drill-down (the API allows up to 500). */
export const CALLS_PAGE_SIZE = 50;

export interface UsageCallsOptions {
  offset?: number;
  limit?: number;
  unpricedOnly?: boolean;
  errorsOnly?: boolean;
}

/**
 * The query params for GET /usage/calls: the page's filters (the same ones
 * the summary and time series get), the page, and the two narrowing
 * switches, sent only when on.
 */
export function usageCallsParams(filters: UsageFilters, options: UsageCallsOptions = {}): Record<string, unknown> {
  const params: Record<string, unknown> = {
    ...filters,
    limit: options.limit ?? CALLS_PAGE_SIZE,
    offset: Math.max(0, options.offset ?? 0),
  };
  if (options.unpricedOnly) params.unpriced_only = true;
  if (options.errorsOnly) params.errors_only = true;
  return params;
}

/** "1–50", or "" for an empty page. */
export function pageRangeLabel(offset: number, count: number): string {
  if (count <= 0) return '';
  return `${offset + 1}–${offset + count}`;
}

export function previousOffset(offset: number, pageSize: number = CALLS_PAGE_SIZE): number {
  return Math.max(0, offset - pageSize);
}

export interface CallRow {
  id: string;
  /** Local date and time, or '—' when the call has none. */
  time: string;
  /** Full ISO timestamp for a tooltip. */
  timeTitle: string;
  agent: string;
  source: string;
  provider: string;
  model: string;
  /** The user's email when the API resolved it, else their id, else '—'. */
  user: string;
  input: string;
  output: string;
  /** Cache read + write, '' when there were none. */
  cached: string;
  cachedTitle: string;
  cost: string;
  unpriced: boolean;
  status: 'ok' | 'error';
  error: string;
  latency: string;
  batch: boolean;
}

export function formatLatency(ms: number | null | undefined): string {
  if (ms == null) return '';
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${ms}ms`;
}

export function callRow(call: UsageCall, locale?: string): CallRow {
  const created = call.created_at ? new Date(call.created_at) : null;
  const valid = created && !Number.isNaN(created.getTime());
  const cacheRead = call.cache_read_tokens || 0;
  const cacheWrite = call.cache_write_tokens || 0;
  return {
    id: call.id,
    time: valid
      ? created!.toLocaleString(locale ?? 'en-US', {
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        })
      : '—',
    timeTitle: call.created_at ?? '',
    agent: call.agent || '—',
    source: call.source || '',
    provider: call.provider_name || call.provider_type || '—',
    model: call.model || '—',
    user: call.user_email || call.user_id || '—',
    input: formatTokens(call.prompt_tokens || 0),
    output: formatTokens(call.completion_tokens || 0),
    cached: cacheRead + cacheWrite > 0 ? formatTokens(cacheRead + cacheWrite) : '',
    cachedTitle:
      cacheRead + cacheWrite > 0
        ? `${cacheRead.toLocaleString('en-US')} read, ${cacheWrite.toLocaleString('en-US')} written`
        : '',
    cost: call.cost_usd == null ? 'unpriced' : formatUsd(call.cost_usd),
    unpriced: call.cost_usd == null,
    status: call.error ? 'error' : 'ok',
    error: call.error || '',
    latency: formatLatency(call.latency_ms),
    batch: !!call.batch,
  };
}
