import { perMillion } from '../lib/ratecard';
import type { ModelPricing, RatecardSource } from '../types';

// The deployment ratecard's sources: configured or not, models, last fetch, error.
export function RatecardSources({ sources }: { sources: RatecardSource[] }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
      {sources.map((s) => (
        <div key={s.source} className="rounded-md border border-line-soft p-3">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wider">
            MODEL_PRICES_{s.source.toUpperCase()}
          </p>
          {s.configured ? (
            <>
              <p className="text-sm text-gray-100 mt-1">{s.models.toLocaleString()} models</p>
              {(s.url || s.path) && <p className="text-xs text-gray-500 truncate" title={s.url || s.path || ''}>{s.url || s.path}</p>}
              {s.fetched_at && (
                <p className="text-xs text-gray-500">Fetched {new Date(s.fetched_at).toLocaleString()}</p>
              )}
              {s.error && <p className="text-xs text-red-400 mt-1">{s.error}</p>}
            </>
          ) : (
            <p className="text-sm text-gray-500 mt-1">Not set</p>
          )}
        </div>
      ))}
    </div>
  );
}

export function ModelTable({
  rows,
  showPrice,
}: {
  rows: Array<{
    provider: string | null;
    model: string;
    calls: number;
    last_used: string | null;
    priced_as: string | null;
    matched_key: string | null;
    source: string | null;
    origin: string | null;
    discount: number | null;
    price: ModelPricing | null;
  }>;
  showPrice: boolean;
}) {
  const headers = showPrice
    ? ['Provider', 'Model', 'Priced as', 'From', 'Input', 'Cached input', 'Output', 'Calls']
    : ['Provider', 'Model', 'Calls', 'Last used'];
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-white/[0.06]">
        <thead className="bg-surface-0">
          <tr>
            {headers.map((h) => (
              <th key={h} className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="bg-surface-1 divide-y divide-white/[0.06]">
          {rows.length === 0 ? (
            <tr>
              <td colSpan={headers.length} className="px-6 py-6 text-sm text-gray-500">
                No priced calls yet.
              </td>
            </tr>
          ) : (
            rows.map((m) => (
              <tr key={`${m.provider}/${m.model}`}>
                <td className="px-6 py-3 text-sm text-gray-300">{m.provider || '—'}</td>
                <td className="px-6 py-3 text-sm font-mono text-gray-100">{m.model}</td>
                {showPrice ? (
                  <>
                    <td className="px-6 py-3 text-sm font-mono text-gray-400">{m.matched_key}</td>
                    <td className="px-6 py-3 text-sm text-gray-400">
                      {m.source === 'provider' ? 'provider' : `ratecard (${m.origin})`}
                      {m.discount != null && <span className="text-xs text-gray-500"> × {m.discount}</span>}
                    </td>
                    <td className="px-6 py-3 text-sm text-gray-300">{perMillion(m.price?.input_cost_per_token)}</td>
                    <td className="px-6 py-3 text-sm text-gray-300">{perMillion(m.price?.cache_read_input_token_cost)}</td>
                    <td className="px-6 py-3 text-sm text-gray-300">{perMillion(m.price?.output_cost_per_token)}</td>
                    <td className="px-6 py-3 text-sm text-gray-300">{m.calls.toLocaleString()}</td>
                  </>
                ) : (
                  <>
                    <td className="px-6 py-3 text-sm text-gray-300">{m.calls.toLocaleString()}</td>
                    <td className="px-6 py-3 text-sm text-gray-400">
                      {m.last_used ? new Date(m.last_used).toLocaleString() : '—'}
                    </td>
                  </>
                )}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
