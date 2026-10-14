import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { AlertTriangle, CircleDollarSign, Coins, Hash, RefreshCw, TrendingUp, XCircle } from 'lucide-react';
import { apiClient } from '../lib/api';
import { useToast } from '../lib/toast-context';
import { ErrorDisplay } from '../components/ErrorDisplay';
import type { ModelPricing, UsageDimension, UsageFilters } from '../types';

const RANGES = [
  { label: 'Last 24 hours', hours: 24 },
  { label: 'Last 7 days', hours: 24 * 7 },
  { label: 'Last 30 days', hours: 24 * 30 },
  { label: 'Last 90 days', hours: 24 * 90 },
];

const GROUPS: Array<{ value: UsageDimension; label: string }> = [
  { value: 'agent', label: 'Agent' },
  { value: 'provider', label: 'Provider' },
  { value: 'model', label: 'Model' },
  { value: 'user', label: 'User' },
  { value: 'source', label: 'Source' },
  { value: 'batch', label: 'Batch / regular' },
  { value: 'chat', label: 'Chat' },
  { value: 'day', label: 'Day' },
];

type ChartMetric = 'cost_usd' | 'calls' | 'total_tokens';

const CHART_METRICS: Array<{ value: ChartMetric; label: string }> = [
  { value: 'cost_usd', label: 'Cost' },
  { value: 'calls', label: 'Calls' },
  { value: 'total_tokens', label: 'Tokens' },
];

function formatUsd(value: number | null | undefined): string {
  if (value == null) return '—';
  if (value > 0 && value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return `${n}`;
}

// A per-token price as USD per million tokens
function perMillion(value: number | undefined): string {
  if (value == null) return '—';
  return `$${Number((value * 1_000_000).toPrecision(4))}`;
}

// datetime-local value (local time) -> ISO
function localToIso(value: string): string | undefined {
  return value ? new Date(value).toISOString() : undefined;
}

export function Usage() {
  const queryClient = useQueryClient();
  const { showSuccess, showError } = useToast();
  const [rangeHours, setRangeHours] = useState<number | 'custom'>(24 * 30);
  const [customFrom, setCustomFrom] = useState('');
  const [customTo, setCustomTo] = useState('');
  // "now" for preset ranges, fixed until the range changes or Refresh is pressed
  const [anchor, setAnchor] = useState(() => Date.now());
  const [agent, setAgent] = useState('');
  const [provider, setProvider] = useState('');
  const [modelDraft, setModelDraft] = useState('');
  const [model, setModel] = useState('');
  const [groupBy, setGroupBy] = useState<UsageDimension>('agent');
  const [chartMetric, setChartMetric] = useState<ChartMetric>('cost_usd');

  // The window, resolved once: every query and the backfill send exactly
  // these bounds. A blank custom date falls back the way the API would
  // (to: now, from: 30 days before to).
  const filters: UsageFilters = useMemo(() => {
    const f: UsageFilters = {};
    if (rangeHours === 'custom') {
      const to = customTo ? new Date(customTo).getTime() : anchor;
      f.to = new Date(to).toISOString();
      f.from = customFrom ? localToIso(customFrom) : new Date(to - 30 * 24 * 3600_000).toISOString();
    } else {
      f.to = new Date(anchor).toISOString();
      f.from = new Date(anchor - rangeHours * 3600_000).toISOString();
    }
    if (agent) f.agent = [agent];
    if (provider) f.provider = [provider];
    if (model) f.model = [model];
    return f;
  }, [rangeHours, customFrom, customTo, anchor, agent, provider, model]);
  // Pricing past calls rewrites stored costs: only for a range the user set
  // in full, never for a guessed one.
  const rangeComplete = rangeHours !== 'custom' || (!!customFrom && !!customTo);

  const spanHours =
    filters.from && filters.to ? (Date.parse(filters.to) - Date.parse(filters.from)) / 3600_000 : 24 * 30;
  const interval: 'hour' | 'day' = spanHours <= 48 ? 'hour' : 'day';

  const { data: agents } = useQuery({
    queryKey: ['agents'],
    queryFn: () => apiClient.listAgents(),
    retry: false,
  });

  const summary = useQuery({
    queryKey: ['usage-summary', filters, groupBy],
    queryFn: () => apiClient.getUsageSummary(filters, [groupBy]),
    retry: false,
  });

  const timeseries = useQuery({
    queryKey: ['usage-timeseries', filters, interval],
    queryFn: () => apiClient.getUsageTimeseries(filters, interval),
    retry: false,
  });

  // Admins only (sinas.usage.read:all): the section hides itself otherwise.
  const ratecard = useQuery({
    queryKey: ['usage-ratecard'],
    queryFn: () => apiClient.getRatecard({ days: 30 }),
    retry: false,
  });

  const invalidateUsage = () => {
    queryClient.invalidateQueries({ queryKey: ['usage-summary'] });
    queryClient.invalidateQueries({ queryKey: ['usage-timeseries'] });
    queryClient.invalidateQueries({ queryKey: ['usage-ratecard'] });
  };

  const refreshMutation = useMutation({
    mutationFn: () => apiClient.refreshRatecard(),
    onSuccess: (result) => {
      if (result.refreshed) showSuccess(`Ratecard fetched: ${result.models} models`);
      else showError(result.error || 'The ratecard could not be fetched; the last good copy is kept');
      invalidateUsage();
    },
    onError: () => showError('Could not fetch the ratecard'),
  });

  const backfillMutation = useMutation({
    mutationFn: () => apiClient.backfillUsageCosts({ from: filters.from, to: filters.to }),
    onSuccess: (result) => {
      showSuccess(
        `Priced ${result.priced_calls} of ${result.examined_calls} unpriced calls` +
          (result.unpriced_calls ? ` (${result.unpriced_calls} still have no price)` : '')
      );
      invalidateUsage();
    },
    onError: () => showError('Backfill failed'),
  });

  const totals = summary.data?.totals;
  const cachedTokens = (totals?.cache_read_tokens || 0) + (totals?.cache_write_tokens || 0);
  const stats = [
    { name: 'Cost', value: formatUsd(totals?.cost_usd ?? 0), icon: CircleDollarSign, color: 'text-green-400 bg-green-900/20' },
    { name: 'Calls', value: (totals?.calls ?? 0).toLocaleString(), icon: Hash, color: 'text-blue-400 bg-blue-900/20' },
    {
      name: 'Tokens',
      value: formatTokens(totals?.total_tokens ?? 0),
      subtitle: cachedTokens > 0 ? `${formatTokens(cachedTokens)} cached` : undefined,
      icon: Coins,
      color: 'text-cyan-400 bg-cyan-900/20',
    },
    {
      name: 'Unpriced calls',
      value: (totals?.unpriced_calls ?? 0).toLocaleString(),
      subtitle: totals?.unpriced_calls ? 'not in the cost' : undefined,
      icon: AlertTriangle,
      color: 'text-yellow-400 bg-yellow-900/20',
    },
    { name: 'Errors', value: (totals?.errors ?? 0).toLocaleString(), icon: XCircle, color: 'text-red-400 bg-red-900/20' },
  ];

  const chartData = (timeseries.data?.points || []).map((p) => ({
    ...p,
    label:
      interval === 'hour'
        ? new Date(p.bucket).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', timeZone: 'UTC' })
        : new Date(p.bucket).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' }),
  }));
  const hasChartData = chartData.some((p) => p.calls > 0);

  const groups = summary.data?.groups || [];
  const maxCost = Math.max(0, ...groups.map((g) => g.cost_usd));
  const groupLabel = (key: Record<string, string | boolean | null>): string => {
    const value = key[groupBy];
    if (groupBy === 'user') return (key.user_email as string) || (value as string) || '(no user)';
    if (groupBy === 'batch') return value ? 'Batch' : 'Regular';
    if (groupBy === 'day' && typeof value === 'string') {
      return new Date(value).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' });
    }
    if (value == null || value === '') return `(no ${groupBy})`;
    return String(value);
  };

  const urlSource = ratecard.data?.sources.find((s) => s.source === 'url');
  // From the ratecard (admins); others filter by provider through the breakdown.
  const providers = ratecard.data?.providers;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-gray-100">Usage</h1>
          <p className="text-gray-400 mt-1">LLM calls, tokens and cost</p>
        </div>
        <button
          onClick={() => {
            setAnchor(Date.now());
            invalidateUsage();
          }}
          className="btn btn-secondary flex items-center"
        >
          <RefreshCw className="w-4 h-4 mr-2" />
          Refresh
        </button>
      </div>

      {/* Filters */}
      <div className="bg-surface-1 rounded-lg border border-line-soft p-4">
        <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-6 gap-4">
          <div>
            <label className="label">Range</label>
            <select
              value={String(rangeHours)}
              onChange={(e) => {
                setRangeHours(e.target.value === 'custom' ? 'custom' : Number(e.target.value));
                setAnchor(Date.now());
              }}
              className="input"
            >
              {RANGES.map((r) => (
                <option key={r.hours} value={r.hours}>{r.label}</option>
              ))}
              <option value="custom">Custom</option>
            </select>
          </div>
          {rangeHours === 'custom' && (
            <>
              <div>
                <label className="label">From</label>
                <input type="datetime-local" value={customFrom} onChange={(e) => setCustomFrom(e.target.value)} className="input" />
              </div>
              <div>
                <label className="label">To</label>
                <input type="datetime-local" value={customTo} onChange={(e) => setCustomTo(e.target.value)} className="input" />
              </div>
            </>
          )}
          <div>
            <label className="label">Agent</label>
            <select value={agent} onChange={(e) => setAgent(e.target.value)} className="input">
              <option value="">All agents</option>
              {agents?.map((a) => (
                <option key={a.id} value={`${a.namespace}/${a.name}`}>{a.namespace}/{a.name}</option>
              ))}
            </select>
          </div>
          {providers && (
            <div>
              <label className="label">Provider</label>
              <select value={provider} onChange={(e) => setProvider(e.target.value)} className="input">
                <option value="">All providers</option>
                {providers.map((p) => (
                  <option key={p.name} value={p.name}>{p.name}</option>
                ))}
              </select>
            </div>
          )}
          <div>
            <label className="label">Model</label>
            <input
              type="text"
              value={modelDraft}
              onChange={(e) => setModelDraft(e.target.value)}
              onBlur={() => setModel(modelDraft.trim())}
              onKeyDown={(e) => e.key === 'Enter' && setModel(modelDraft.trim())}
              placeholder="All models"
              className="input"
            />
          </div>
        </div>
      </div>

      {summary.isError && <ErrorDisplay error={summary.error} title="Failed to load usage" />}

      {/* Totals */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
        {stats.map((stat) => {
          const Icon = stat.icon;
          const [iconColor, bg] = stat.color.split(' ');
          return (
            <div key={stat.name} className="card">
              <div className="flex items-center justify-between">
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-gray-400 truncate">{stat.name}</p>
                  <p className="text-2xl font-bold text-gray-100 mt-2">{summary.isLoading ? '…' : stat.value}</p>
                  {stat.subtitle && <p className="text-xs text-gray-500 mt-1 truncate">{stat.subtitle}</p>}
                </div>
                <div className={`p-3 ${bg} rounded-lg flex-shrink-0`}>
                  <Icon className={`w-6 h-6 ${iconColor}`} />
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Over time */}
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-gray-100">
            Over time <span className="text-sm font-normal text-gray-500">(per {interval}, UTC)</span>
          </h3>
          <div className="flex items-center gap-2">
            <select
              value={chartMetric}
              onChange={(e) => setChartMetric(e.target.value as ChartMetric)}
              className="input text-xs !py-1 !w-32"
            >
              {CHART_METRICS.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
            <TrendingUp className="w-5 h-5 text-gray-500" />
          </div>
        </div>
        {timeseries.isError ? (
          <ErrorDisplay error={timeseries.error} title="Failed to load the time series" />
        ) : hasChartData ? (
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#333333" />
              <XAxis dataKey="label" tick={{ fill: '#6b7280', fontSize: 12 }} minTickGap={16} />
              <YAxis
                tick={{ fill: '#6b7280', fontSize: 12 }}
                tickFormatter={(v: number) => (chartMetric === 'cost_usd' ? formatUsd(v) : formatTokens(v))}
                width={70}
              />
              <Tooltip
                contentStyle={{ backgroundColor: 'var(--surface-1)', border: '1px solid var(--line-soft)', borderRadius: '6px', color: 'var(--content)' }}
                cursor={{ fill: 'var(--hover)' }}
                formatter={(v: number) => [
                  chartMetric === 'cost_usd' ? formatUsd(v) : v.toLocaleString(),
                  CHART_METRICS.find((m) => m.value === chartMetric)?.label,
                ]}
              />
              <Bar dataKey={chartMetric} fill="#3b82f6" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex items-center justify-center h-48 text-gray-500">
            <p className="text-sm">{timeseries.isLoading ? 'Loading…' : 'No LLM calls in this range'}</p>
          </div>
        )}
      </div>

      {/* Breakdown */}
      <div className="bg-surface-1 rounded-lg border border-line-soft overflow-hidden">
        <div className="flex items-center justify-between px-6 py-4 border-b border-line-soft">
          <h3 className="text-lg font-semibold text-gray-100">Breakdown</h3>
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-400">By</span>
            <select
              value={groupBy}
              onChange={(e) => setGroupBy(e.target.value as UsageDimension)}
              className="input text-xs !py-1 !w-40"
            >
              {GROUPS.map((g) => (
                <option key={g.value} value={g.value}>{g.label}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-white/[0.06]">
            <thead className="bg-surface-0">
              <tr>
                {[GROUPS.find((g) => g.value === groupBy)?.label, 'Calls', 'Input', 'Output', 'Cached', 'Cost', '', 'Unpriced', 'Errors'].map(
                  (h, i) => (
                    <th
                      key={i}
                      className={`px-6 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider ${i === 0 || i === 6 ? 'text-left' : 'text-right'}`}
                    >
                      {h}
                    </th>
                  )
                )}
              </tr>
            </thead>
            <tbody className="bg-surface-1 divide-y divide-white/[0.06]">
              {groups.length === 0 ? (
                <tr>
                  <td colSpan={9} className="px-6 py-8 text-center text-sm text-gray-500">
                    {summary.isLoading ? 'Loading…' : 'No LLM calls in this range'}
                  </td>
                </tr>
              ) : (
                groups.map((g, i) => (
                  <tr key={i} className="hover:bg-hover">
                    <td className="px-6 py-3 text-sm font-mono text-gray-100 max-w-xs truncate" title={groupLabel(g.key)}>
                      {groupLabel(g.key)}
                    </td>
                    <td className="px-6 py-3 text-sm text-gray-300 text-right">{g.calls.toLocaleString()}</td>
                    <td className="px-6 py-3 text-sm text-gray-300 text-right">{formatTokens(g.prompt_tokens)}</td>
                    <td className="px-6 py-3 text-sm text-gray-300 text-right">{formatTokens(g.completion_tokens)}</td>
                    <td className="px-6 py-3 text-sm text-gray-300 text-right">
                      {formatTokens(g.cache_read_tokens + g.cache_write_tokens)}
                    </td>
                    <td className="px-6 py-3 text-sm font-semibold text-gray-100 text-right">{formatUsd(g.cost_usd)}</td>
                    <td className="px-6 py-3 w-32">
                      <div className="h-2 bg-surface-0 rounded">
                        <div
                          className="h-2 bg-primary-600 rounded"
                          style={{ width: `${maxCost > 0 ? (g.cost_usd / maxCost) * 100 : 0}%` }}
                        />
                      </div>
                    </td>
                    <td className={`px-6 py-3 text-sm text-right ${g.unpriced_calls ? 'text-yellow-400' : 'text-gray-500'}`}>
                      {g.unpriced_calls.toLocaleString()}
                    </td>
                    <td className={`px-6 py-3 text-sm text-right ${g.errors ? 'text-red-400' : 'text-gray-500'}`}>
                      {g.errors.toLocaleString()}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        {summary.data?.truncated && (
          <p className="px-6 py-3 text-xs text-gray-500 border-t border-line-soft">
            Showing the {groups.length} largest groups.
          </p>
        )}
      </div>

      {/* Ratecard (admins) */}
      {ratecard.data && (
        <div className="space-y-4">
          <div className="card">
            <div className="flex items-start justify-between gap-4 mb-4">
              <div>
                <h3 className="text-lg font-semibold text-gray-100">Ratecard</h3>
                <p className="text-sm text-gray-400 mt-1">
                  Calls are priced when they are recorded: from the LLM provider's own pricing first, then the
                  deployment ratecard ({ratecard.data.deployment_models.toLocaleString()} models).
                </p>
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                {urlSource?.configured && (
                  <button
                    onClick={() => refreshMutation.mutate()}
                    disabled={refreshMutation.isPending}
                    className="btn btn-secondary btn-sm"
                  >
                    {refreshMutation.isPending ? 'Fetching…' : 'Fetch URL now'}
                  </button>
                )}
                <button
                  onClick={() => {
                    if (confirm('Price the unpriced calls in this range at the current prices?')) backfillMutation.mutate();
                  }}
                  disabled={backfillMutation.isPending || !totals?.unpriced_calls || !rangeComplete}
                  className="btn btn-secondary btn-sm"
                  title={
                    rangeComplete
                      ? "Price calls recorded without a cost, in the selected range, at today's prices"
                      : 'Set both dates of the custom range first'
                  }
                >
                  {backfillMutation.isPending ? 'Pricing…' : 'Price unpriced calls'}
                </button>
              </div>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              {ratecard.data.sources.map((s) => (
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
          </div>

          <div className="bg-surface-1 rounded-lg border border-line-soft overflow-hidden">
            <div className="px-6 py-4 border-b border-line-soft">
              <h3 className="text-lg font-semibold text-gray-100">
                Unpriced models{' '}
                <span className="text-sm font-normal text-gray-500">(called in the last {ratecard.data.days} days)</span>
              </h3>
              <p className="text-sm text-gray-400 mt-1">
                Give these a price on the LLM provider (or an alias to a priced model), or add them to the deployment
                ratecard. Then price the calls already made with "Price unpriced calls".
              </p>
            </div>
            {ratecard.data.unpriced_models.length === 0 ? (
              <p className="px-6 py-6 text-sm text-gray-500">Every model in use has a price.</p>
            ) : (
              <ModelTable rows={ratecard.data.unpriced_models} showPrice={false} />
            )}
          </div>

          <div className="bg-surface-1 rounded-lg border border-line-soft overflow-hidden">
            <div className="px-6 py-4 border-b border-line-soft">
              <h3 className="text-lg font-semibold text-gray-100">Prices in use</h3>
              <p className="text-sm text-gray-400 mt-1">Per million tokens, as they apply to new calls now.</p>
            </div>
            <ModelTable rows={ratecard.data.models_in_use.filter((m) => m.source)} showPrice />
          </div>
        </div>
      )}
    </div>
  );
}

function ModelTable({
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
