import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Archive, Eye, Loader2, Play, AlertTriangle, CheckCircle2, XCircle } from 'lucide-react';
import { apiClient } from '../lib/api';
import { describeRetention, formatBytes, plural, runSummary, skipLabel } from '../lib/retention';
import type { RetentionPreview, RetentionRun } from '../types';

function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : '—';
}

function apiError(error: unknown): string {
  const detail = (error as any)?.response?.data?.detail;
  return typeof detail === 'string' ? detail : 'The request failed.';
}

const STATUS_STYLE: Record<RetentionRun['status'], string> = {
  queued: 'text-gray-300',
  running: 'text-blue-400',
  completed: 'text-green-400',
  partial: 'text-yellow-400',
  failed: 'text-red-400',
  refused: 'text-red-400',
};

function RunLine({ run }: { run: RetentionRun }) {
  return (
    <div className="space-y-1 text-sm">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={`font-medium ${STATUS_STYLE[run.status] ?? 'text-gray-300'}`}>{run.status}</span>
        <span className="text-gray-400">
          {run.trigger === 'schedule' ? 'scheduled' : run.trigger === 'api' ? `run now${run.actor_email ? ` by ${run.actor_email}` : ''}` : 'command line'}
        </span>
        <span className="text-gray-500">
          {run.finished_at ? `finished ${when(run.finished_at)}` : `started ${when(run.started_at ?? run.created_at)}`}
        </span>
      </div>
      {run.counts && (
        <p className="text-gray-300">
          {runSummary(run.counts)}
          {run.counts.bytes_archived > 0 && <span className="text-gray-500"> · {formatBytes(run.counts.bytes_archived)} archived</span>}
        </p>
      )}
      {run.counts && Object.keys(run.counts.skipped).length > 0 && (
        <p className="text-xs text-gray-500">
          Skipped: {Object.entries(run.counts.skipped).map(([r, n]) => `${n} ${skipLabel(r)}`).join(', ')}
        </p>
      )}
      {run.counts && Object.keys(run.counts.failed).length > 0 && (
        <p className="text-xs text-red-400">
          Failed (kept, retried next run): {Object.entries(run.counts.failed).map(([stage, n]) => `${n} at ${stage}`).join(', ')}
        </p>
      )}
      {run.error && <p className="text-xs text-red-400">{run.error}</p>}
    </div>
  );
}

function PreviewTable({ preview }: { preview: RetentionPreview }) {
  if (preview.agents.length === 0) {
    return <p className="text-sm text-gray-400">No chats are past their retention period right now.</p>;
  }
  return (
    <div className="space-y-2">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-400 border-b border-line">
              <th className="py-2 pr-4 font-medium">Agent</th>
              <th className="py-2 pr-4 font-medium">Retention</th>
              <th className="py-2 pr-4 font-medium text-right">Chats</th>
              <th className="py-2 pr-4 font-medium text-right">Messages</th>
              <th className="py-2 font-medium">Left alone</th>
            </tr>
          </thead>
          <tbody>
            {preview.agents.map((a) => (
              <tr key={`${a.agent}-${a.retention_days}-${a.source}`} className="border-b border-line/50">
                <td className="py-2 pr-4 text-gray-200 font-mono text-xs">{a.agent}</td>
                <td className="py-2 pr-4 text-gray-300">
                  {plural(a.retention_days, 'day')}
                  <span className="text-gray-500"> ({a.source === 'agent' ? 'agent' : 'default'})</span>
                </td>
                <td className="py-2 pr-4 text-right text-gray-200">{a.chats.toLocaleString('en-US')}</td>
                <td className="py-2 pr-4 text-right text-gray-200">{a.messages.toLocaleString('en-US')}</td>
                <td className="py-2 text-xs text-gray-400">
                  {Object.entries(a.blocked).map(([r, n]) => `${n} ${skipLabel(r)}`).join(', ') || '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-sm text-gray-300">
        A run now would {preview.would_archive ? 'archive and delete' : 'delete (no archive destination)'}{' '}
        {plural(preview.totals.chats, 'chat')} ({plural(preview.totals.messages, 'message')}).
      </p>
      {preview.notes.map((n) => (
        <p key={n} className="text-xs text-gray-500">{n}</p>
      ))}
    </div>
  );
}

/** System page: chat retention policy, destination, last run, preview, Run now. Admins only. */
export function RetentionPanel() {
  const queryClient = useQueryClient();
  const [preview, setPreview] = useState<RetentionPreview | null>(null);

  const { data: status, error, isLoading } = useQuery({
    queryKey: ['retention-status'],
    queryFn: () => apiClient.getRetentionStatus(),
    retry: false,
    refetchInterval: (query) => (query.state.data?.in_progress ? 5000 : 60000),
  });

  const previewMutation = useMutation({
    mutationFn: () => apiClient.previewRetention(),
    onSuccess: (data) => setPreview(data),
  });

  const runMutation = useMutation({
    mutationFn: () => apiClient.runRetention(),
    onSuccess: () => {
      setPreview(null); // what it showed is about to change
      queryClient.invalidateQueries({ queryKey: ['retention-status'] });
    },
  });

  if ((error as any)?.response?.status === 403) return null; // not an admin
  if (isLoading) {
    return (
      <div className="card flex items-center gap-2 text-gray-400 text-sm">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading chat retention…
      </div>
    );
  }
  if (error || !status) {
    return (
      <div className="card text-sm text-red-400">Chat retention status is unavailable: {apiError(error)}</div>
    );
  }

  const { policy, destination } = status;
  const confirmRun = () => {
    const what = preview
      ? `${plural(preview.totals.chats, 'chat')} (${plural(preview.totals.messages, 'message')}) as of the last preview`
      : 'every chat past its retention period';
    const how = destination.configured ? 'archived and then permanently deleted from the database' : 'permanently deleted (there is no archive destination)';
    if (window.confirm(`Run chat retention now?\n\n${what} will be ${how}. This can't be undone from the console.`)) {
      runMutation.mutate();
    }
  };

  return (
    <div className="card">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div className="flex items-center gap-2">
          <Archive className="w-5 h-5 text-gray-400" />
          <h2 className="text-lg font-semibold text-gray-100">Chat Retention</h2>
          <span
            className={`text-xs px-2 py-0.5 rounded-full ${policy.enabled ? 'bg-green-900/30 text-green-400' : 'bg-surface-2 text-gray-400'}`}
          >
            {policy.enabled ? 'On' : 'Off'}
          </span>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn btn-secondary flex items-center gap-1.5"
            onClick={() => previewMutation.mutate()}
            disabled={previewMutation.isPending}
          >
            {previewMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Eye className="w-4 h-4" />}
            Preview
          </button>
          <button
            type="button"
            className="btn btn-danger flex items-center gap-1.5"
            onClick={confirmRun}
            disabled={!!status.refusal || !!status.in_progress || runMutation.isPending}
            title={status.refusal ?? (status.in_progress ? 'A run is already queued or running' : undefined)}
          >
            {runMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
            Run now
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-3 text-sm">
        <div>
          <p className="text-gray-400">Default retention</p>
          <p className="text-gray-200">
            {policy.default_days ? `${plural(policy.default_days, 'day')} after the last activity` : 'Keep forever'}
          </p>
        </div>
        <div>
          <p className="text-gray-400">Action</p>
          <p className="text-gray-200">
            {policy.action === 'archive' ? 'Archive, then delete' : 'Delete (archive first if a destination is set)'}
          </p>
        </div>
        <div>
          <p className="text-gray-400">Schedule (UTC)</p>
          <p className="text-gray-200 font-mono text-xs mt-0.5">{policy.schedule}</p>
          {status.schedule_error ? (
            <p className="text-xs text-red-400">{status.schedule_error}</p>
          ) : (
            <p className="text-xs text-gray-500">Next run: {policy.enabled ? when(status.next_run) : 'none (retention is off)'}</p>
          )}
        </div>
        <div>
          <p className="text-gray-400">Archive destination</p>
          {destination.configured ? (
            <>
              <p className="text-gray-200 font-mono text-xs mt-0.5 break-all">
                {destination.bucket}/{destination.prefix}
              </p>
              <p className="text-xs text-gray-500">
                {destination.endpoint}
                {destination.region ? ` · ${destination.region}` : ''} · {destination.sse_kms ? 'SSE-KMS' : 'bucket default encryption'} ·{' '}
                {destination.credentials}
              </p>
            </>
          ) : (
            <p className="text-gray-200">Not configured</p>
          )}
        </div>
        {status.agent_overrides.length > 0 && (
          <div className="md:col-span-2">
            <p className="text-gray-400">Agent overrides</p>
            <p className="text-gray-300 text-xs mt-0.5">
              {status.agent_overrides.map((o) => `${o.agent}: ${describeRetention(o.retention_days, policy.default_days).toLowerCase()}`).join(' · ')}
            </p>
          </div>
        )}
      </div>

      {status.refusal && (
        <div
          className={`mt-4 p-3 rounded-lg text-sm flex gap-2 ${policy.enabled ? 'bg-red-900/20 border border-red-800/30 text-red-400' : 'bg-surface-1 border border-line text-gray-400'}`}
        >
          <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <span>{status.refusal} Nothing is archived or deleted.</span>
        </div>
      )}

      <div className="mt-4 border-t border-line pt-4 space-y-3">
        {status.in_progress && (
          <div>
            <p className="text-gray-400 text-sm mb-1 flex items-center gap-1.5">
              <Loader2 className="w-3.5 h-3.5 animate-spin" /> In progress
            </p>
            <RunLine run={status.in_progress} />
          </div>
        )}
        <div>
          <p className="text-gray-400 text-sm mb-1">Last run</p>
          {status.last_run ? <RunLine run={status.last_run} /> : <p className="text-sm text-gray-500">No run yet.</p>}
        </div>
      </div>

      {runMutation.isSuccess && status.last_run?.id !== runMutation.data.run_id && (
        <p className="mt-3 text-sm text-green-400 flex items-center gap-1.5">
          <CheckCircle2 className="w-4 h-4" /> Run queued. It shows here once a worker picks it up.
        </p>
      )}
      {runMutation.isError && (
        <p className="mt-3 text-sm text-red-400 flex items-center gap-1.5">
          <XCircle className="w-4 h-4" /> {apiError(runMutation.error)}
        </p>
      )}

      {(preview || previewMutation.isError) && (
        <div className="mt-4 border-t border-line pt-4">
          <p className="text-gray-400 text-sm mb-2">
            Preview{preview ? ` (as of ${when(preview.as_of)}; nothing was changed)` : ''}
          </p>
          {previewMutation.isError ? (
            <p className="text-sm text-red-400">{apiError(previewMutation.error)}</p>
          ) : (
            preview && <PreviewTable preview={preview} />
          )}
        </div>
      )}
    </div>
  );
}
