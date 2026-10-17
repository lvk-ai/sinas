import type { ReactNode } from 'react';
import { Database, ExternalLink, Info, AlertTriangle } from 'lucide-react';
import { CLICKHOUSE_DOCS_URL, CLICKHOUSE_ENV_VAR } from '../lib/clickhouse';

/** Full explanatory state for a view that needs ClickHouse, on a deployment
 * that runs without it. */
export function ClickHouseDisabledState({ what = 'Request logs' }: { what?: string }) {
  return (
    <div className="card text-center py-12 px-6">
      <Database className="w-12 h-12 text-gray-500 mx-auto mb-3" />
      <h3 className="text-lg font-medium text-gray-100 mb-1">{what} are not available on this deployment</h3>
      <p className="text-gray-400 max-w-xl mx-auto">
        {what} are stored in ClickHouse, and this deployment runs without it, so requests
        are not being recorded. Everything else works normally.
      </p>
      <p className="text-gray-400 max-w-xl mx-auto mt-3">
        To enable them, run a ClickHouse server and point the backend and workers at it with{' '}
        <span className="font-mono text-gray-300">{CLICKHOUSE_ENV_VAR}</span>{' '}
        (and the other <span className="font-mono text-gray-300">CLICKHOUSE_*</span> settings), then restart.
        Logging starts from that point; earlier requests are not recoverable.
      </p>
      <a
        href={CLICKHOUSE_DOCS_URL}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1.5 mt-4 text-sm text-primary-600 hover:underline"
      >
        ClickHouse configuration
        <ExternalLink className="w-3.5 h-3.5" />
      </a>
    </div>
  );
}

/** ClickHouse is configured but the backend couldn't reach it. */
export function ClickHouseUnavailableState({ detail }: { detail: string }) {
  return (
    <div className="card text-center py-12 px-6">
      <AlertTriangle className="w-12 h-12 text-yellow-500 mx-auto mb-3" />
      <h3 className="text-lg font-medium text-gray-100 mb-1">Request logs are unavailable right now</h3>
      <p className="text-gray-400 max-w-xl mx-auto">{detail}</p>
      <p className="text-gray-500 text-sm max-w-xl mx-auto mt-3">
        Check the ClickHouse service on the System page. The backend retries the connection on its own.
      </p>
    </div>
  );
}

/** One-line note for a widget inside another page. */
export function ClickHouseDisabledNote({ children }: { children?: ReactNode }) {
  return (
    <div className="mb-4 p-3 bg-surface-1 border border-line-soft rounded-lg text-sm text-gray-400 flex items-start gap-2">
      <Info className="w-4 h-4 flex-shrink-0 mt-0.5" />
      <span>
        {children ?? (
          <>
            Not available: ClickHouse is disabled on this deployment
            (<span className="font-mono">{CLICKHOUSE_ENV_VAR}</span> is empty).
          </>
        )}
      </span>
    </div>
  );
}
