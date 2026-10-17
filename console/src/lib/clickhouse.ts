// Request logs live in ClickHouse, which a deployment may leave out
// (CLICKHOUSE_HOST empty: the lite profile, or `clickhouse.enabled: false` in
// the Helm chart). `/info` reports it as `features.clickhouse`, and the
// ClickHouse-backed endpoints answer 503 when it's disabled or unreachable.
// Pure, so `npm test` covers it.

export const CLICKHOUSE_ENV_VAR = 'CLICKHOUSE_HOST';
export const CLICKHOUSE_DOCS_URL =
  'https://docs.sinas.co/getting-started/environment-variables#clickhouse-optional';

/** False only when the instance says so: a missing flag (older backend, or
 * /info unreachable) counts as enabled, and a 503 is explained in place. */
export function isClickHouseEnabled(features: Record<string, boolean> | undefined | null): boolean {
  return features?.clickhouse !== false;
}

export type RequestLogsState =
  /** Deliberately off on this deployment: explain how to turn it on. */
  | 'disabled'
  /** Configured, but the backend couldn't reach it (or reported it off). */
  | 'unavailable'
  /** Some other failure; the global error handling already reported it. */
  | 'error'
  | 'ok';

/** What the request-log view should show, from the instance features and
 * the error (if any) of its request. */
export function requestLogsState(
  features: Record<string, boolean> | undefined | null,
  error: unknown,
): RequestLogsState {
  if (!isClickHouseEnabled(features)) return 'disabled';
  if (!error) return 'ok';
  return (error as any)?.response?.status === 503 ? 'unavailable' : 'error';
}

/** The backend's explanation of a 503, or a generic one. */
export function unavailableDetail(error: unknown): string {
  const detail = (error as any)?.response?.data?.detail;
  return typeof detail === 'string' && detail
    ? detail
    : 'Request logs are temporarily unavailable: ClickHouse could not be reached.';
}
