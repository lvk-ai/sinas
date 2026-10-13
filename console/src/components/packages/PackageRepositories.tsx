import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { AlertTriangle, Check, Code2, ExternalLink, Library, Lock, Plus, RefreshCw, ShieldAlert, Sparkles, Trash2, X } from 'lucide-react';
import { apiClient } from '../../lib/api';
import type { PackageRepository, PackageRepositoryAuthType, PackageRepositoryCreate } from '../../types';
import {
  OFFICIAL_REPOSITORY,
  errorMessage,
  hasOfficialRepository,
  isForbidden,
  timeAgo,
  useAddOfficialRepository,
  useInvalidatePackageData,
  usePackageRepositories,
} from './hooks';
import { LoadError } from './LoadError';

const AUTH_TYPES: { value: PackageRepositoryAuthType; label: string }[] = [
  { value: 'none', label: 'None (public)' },
  { value: 'bearer', label: 'Bearer token' },
  { value: 'header', label: 'Token in a custom header' },
];

// As PackageRepositorySpec.name: no dots, at most 100 characters.
const NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,99}$/;

/** Packages → Repositories: where Sinas finds packages. Admin only. */
export function PackageRepositories() {
  const invalidate = useInvalidatePackageData();
  const [showAddModal, setShowAddModal] = useState(false);
  const { data: repositories, isLoading, error, refetch, isFetching } = usePackageRepositories();
  const addOfficial = useAddOfficialRepository();

  const toggleMutation = useMutation({
    mutationFn: (repo: PackageRepository) =>
      apiClient.updatePackageRepository(repo.name, { is_active: !repo.is_active }),
    onSettled: invalidate,
  });
  const refreshMutation = useMutation({
    mutationFn: (name: string) => apiClient.refreshPackageRepository(name),
    onSettled: invalidate,
  });
  const deleteMutation = useMutation({
    mutationFn: (name: string) => apiClient.deletePackageRepository(name),
    onSettled: invalidate,
  });

  if (isLoading) {
    return (
      <div className="text-center py-12">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600"></div>
      </div>
    );
  }

  if (isForbidden(error)) {
    return (
      <div className="text-center py-12 card">
        <ShieldAlert className="w-12 h-12 text-gray-500 mx-auto mb-3" />
        <h3 className="text-lg font-medium text-gray-100 mb-1">Admins only</h3>
        <p className="text-gray-400">
          Package repositories are managed by administrators. Ask one to add a repository; its packages then show up in the Catalog.
        </p>
      </div>
    );
  }

  // Any other failed read: say so, rather than an empty list that offers to add repositories.
  if (error) {
    return <LoadError what="package repositories" error={error} onRetry={() => refetch()} retrying={isFetching} />;
  }

  const officialAdded = hasOfficialRepository(repositories);

  return (
    <div className="space-y-4">
      {!officialAdded && (
        <div className="card flex items-center gap-4 border border-primary-700/40">
          <Sparkles className="w-8 h-8 text-primary-400 flex-shrink-0" />
          <div className="flex-1 min-w-0">
            <h3 className="font-semibold text-gray-100">Official Sinas packages</h3>
            <p className="text-sm text-gray-400 mt-0.5">
              Add the repository maintained by the Sinas team,{' '}
              <a href={OFFICIAL_REPOSITORY.url} target="_blank" rel="noopener noreferrer" className="font-mono text-primary-400 hover:underline">
                sinas-platform/packages
              </a>
              , to browse and install its packages from the Catalog.
            </p>
          </div>
          <button
            onClick={() => addOfficial.mutate()}
            disabled={addOfficial.isPending}
            className="btn btn-primary flex items-center flex-shrink-0"
          >
            <Plus className="w-4 h-4 mr-2" />
            {addOfficial.isPending ? 'Adding...' : 'Add official repository'}
          </button>
        </div>
      )}

      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-400">
          Sinas fetches each repository's index when it is added, on Refresh, and every few hours after that.
          Nothing installs or updates on its own.
        </p>
        <div className="flex items-center gap-3 flex-shrink-0 ml-4">
          {officialAdded && (
            <span className="flex items-center text-xs text-green-400" title={OFFICIAL_REPOSITORY.url}>
              <Check className="w-4 h-4 mr-1" />
              Official repository added
            </span>
          )}
          <button onClick={() => setShowAddModal(true)} className="btn btn-secondary flex items-center">
            <Plus className="w-4 h-4 mr-2" />
            Add repository
          </button>
        </div>
      </div>

      {repositories && repositories.length > 0 ? (
        <div className="space-y-3">
          {repositories.map((repo) => {
            const refreshing = refreshMutation.isPending && refreshMutation.variables === repo.name;
            const toggling = toggleMutation.isPending && toggleMutation.variables?.name === repo.name;
            return (
              <div key={repo.id} className={`card ${repo.is_active ? '' : 'opacity-70'}`}>
                <div className="flex items-start gap-4">
                  <Library className="w-8 h-8 text-primary-400 flex-shrink-0 mt-0.5" />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <h3 className="font-semibold text-gray-100">{repo.name}</h3>
                      {repo.managed_by && (
                        <span
                          className="flex items-center gap-1 px-2 py-0.5 text-xs bg-blue-900/30 text-blue-400 rounded"
                          title={`Managed by ${repo.managed_by}. Changing it here detaches it from that config.`}
                        >
                          <Code2 className="w-3 h-3" />
                          config
                        </span>
                      )}
                      {repo.auth?.type && repo.auth.type !== 'none' && (
                        <span
                          className="flex items-center gap-1 px-2 py-0.5 text-xs bg-surface-2 text-gray-400 rounded"
                          title={`${repo.auth.type === 'bearer' ? 'Bearer token' : `Header ${repo.auth.header || 'X-Api-Key'}`} from secret ${repo.auth.secret || '(none)'}`}
                        >
                          <Lock className="w-3 h-3" />
                          {repo.auth.secret || repo.auth.type}
                        </span>
                      )}
                      {!repo.is_active && (
                        <span className="px-2 py-0.5 text-xs bg-surface-2 text-gray-400 rounded">Disabled</span>
                      )}
                    </div>
                    <a
                      href={repo.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-xs font-mono text-gray-500 hover:text-primary-400 mt-0.5 break-all"
                    >
                      {repo.url}
                      <ExternalLink className="w-3 h-3 flex-shrink-0" />
                    </a>
                    {repo.description && <p className="text-sm text-gray-400 mt-1">{repo.description}</p>}
                    <div className="flex flex-wrap gap-x-3 gap-y-1 mt-1.5 text-xs">
                      <span className="text-gray-400">
                        {repo.package_count} package{repo.package_count === 1 ? '' : 's'}
                      </span>
                      {repo.last_error ? (
                        <span className="text-red-400 flex items-start gap-1">
                          <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-px" />
                          <span>
                            {/* last_refreshed_at is the last attempt, failed or not. */}
                            Refresh failed{repo.last_refreshed_at ? ` ${timeAgo(repo.last_refreshed_at)}` : ''}: {repo.last_error}
                            {repo.package_count > 0 && (
                              <span className="text-gray-500"> (still using the last good index)</span>
                            )}
                          </span>
                        </span>
                      ) : repo.last_refreshed_at ? (
                        <span className="text-gray-500" title={new Date(repo.last_refreshed_at).toLocaleString()}>
                          Refreshed {timeAgo(repo.last_refreshed_at)}
                        </span>
                      ) : (
                        <span className="text-gray-500">Not refreshed yet</span>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-1 flex-shrink-0">
                    <label
                      className="flex items-center gap-2 mr-2 cursor-pointer text-xs text-gray-400"
                      title={repo.is_active ? 'Disable: its packages leave the catalog' : 'Enable'}
                    >
                      <input
                        type="checkbox"
                        checked={repo.is_active}
                        disabled={toggling}
                        onChange={() => toggleMutation.mutate(repo)}
                        className="w-4 h-4 text-primary-600 border-line rounded"
                      />
                      Enabled
                    </label>
                    <button
                      onClick={() => refreshMutation.mutate(repo.name)}
                      disabled={!repo.is_active || refreshing}
                      className="p-2 text-gray-500 hover:text-primary-400 hover:bg-hover-strong rounded-md transition-colors disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-gray-500"
                      title={repo.is_active ? 'Refresh: fetch the index now' : 'Enable the repository to refresh it'}
                    >
                      <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
                    </button>
                    <button
                      onClick={() => {
                        if (confirm(`Remove repository "${repo.name}"? Packages installed from it stay installed; they just no longer get update notices from it.`)) {
                          deleteMutation.mutate(repo.name);
                        }
                      }}
                      disabled={deleteMutation.isPending && deleteMutation.variables === repo.name}
                      className="p-2 text-gray-500 hover:text-red-600 hover:bg-hover-strong rounded-md transition-colors"
                      title="Remove"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="text-center py-12 card">
          <Library className="w-16 h-16 text-gray-500 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-100 mb-2">No package repositories</h3>
          <p className="text-gray-400">
            A repository is a URL serving an <span className="font-mono">index.json</span> of packages: the official one, or your own.
          </p>
        </div>
      )}

      {showAddModal && <AddRepositoryModal onClose={() => setShowAddModal(false)} onAdded={invalidate} />}
    </div>
  );
}

function AddRepositoryModal({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [form, setForm] = useState({
    name: '',
    url: '',
    description: '',
    authType: 'none' as PackageRepositoryAuthType,
    secret: '',
    header: 'X-Api-Key',
  });

  const { data: secrets } = useQuery({
    queryKey: ['secrets'],
    queryFn: () => apiClient.listSecrets(),
    retry: false,
  });
  // Repository credentials resolve as shared Secrets.
  const sharedSecrets = (secrets || []).filter((s: any) => !s.visibility || s.visibility === 'shared');

  const createMutation = useMutation({
    mutationFn: (data: PackageRepositoryCreate) => apiClient.createPackageRepository(data),
    onSuccess: () => {
      onAdded();
      onClose();
    },
  });

  const nameValid = NAME_PATTERN.test(form.name);
  const needsSecret = form.authType !== 'none';
  const canSubmit =
    nameValid && form.url.trim() !== '' && (!needsSecret || form.secret !== '') && !createMutation.isPending;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    createMutation.mutate({
      name: form.name,
      url: form.url.trim(),
      description: form.description.trim() || undefined,
      auth:
        form.authType === 'none'
          ? { type: 'none' }
          : form.authType === 'bearer'
            ? { type: 'bearer', secret: form.secret }
            : { type: 'header', secret: form.secret, header: form.header.trim() || 'X-Api-Key' },
    });
  };

  return (
    <div className="fixed inset-0 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-surface-1 rounded-lg max-w-xl w-full p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold text-gray-100">Add Package Repository</h2>
          <button onClick={onClose} className="p-1 text-gray-500 hover:text-gray-400">
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="label">Name *</label>
            <input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="internal"
              className="input w-full font-mono"
              autoFocus
            />
            {form.name && !nameValid && (
              <p className="text-xs text-red-400 mt-1">Up to 100 lowercase letters, digits, dashes and underscores; starting with a letter or digit.</p>
            )}
          </div>
          <div>
            <label className="label">URL *</label>
            <input
              value={form.url}
              onChange={(e) => setForm({ ...form, url: e.target.value })}
              placeholder="https://github.com/acme/sinas-packages"
              className="input w-full font-mono"
            />
            <p className="text-xs text-gray-500 mt-1">
              A GitHub repository (optionally <span className="font-mono">/tree/&lt;branch&gt;/&lt;folder&gt;</span>), or any URL serving{' '}
              <span className="font-mono">index.json</span>. Credentials never go in the URL.
            </p>
          </div>
          <div>
            <label className="label">Description</label>
            <input
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              className="input w-full"
            />
          </div>

          <div className="border-t border-line-soft pt-4">
            <h3 className="text-sm font-medium text-gray-300 mb-1">Authentication</h3>
            <p className="text-xs text-gray-500 mb-3">For a private repository: a token kept in a shared Secret, sent with every request.</p>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="label">Auth Type</label>
                <select
                  value={form.authType}
                  onChange={(e) => setForm({ ...form, authType: e.target.value as PackageRepositoryAuthType })}
                  className="input w-full"
                >
                  {AUTH_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                </select>
              </div>
              {needsSecret && (
                <div>
                  <label className="label">Secret *</label>
                  <select
                    value={form.secret}
                    onChange={(e) => setForm({ ...form, secret: e.target.value })}
                    className="input w-full"
                  >
                    <option value="">Select a secret...</option>
                    {sharedSecrets.map((s: any) => <option key={s.name} value={s.name}>{s.name}</option>)}
                  </select>
                  {sharedSecrets.length === 0 && (
                    <p className="text-xs text-gray-500 mt-1">No shared secrets yet; add one under Secrets.</p>
                  )}
                </div>
              )}
            </div>
            {form.authType === 'header' && (
              <div className="w-1/2 mt-4 pr-2">
                <label className="label">Header Name</label>
                <input
                  value={form.header}
                  onChange={(e) => setForm({ ...form, header: e.target.value })}
                  placeholder="X-Api-Key"
                  className="input w-full font-mono"
                />
              </div>
            )}
          </div>

          {createMutation.isError && (
            <div className="p-3 bg-red-900/20 border border-red-900/30 rounded text-sm text-red-400 whitespace-pre-wrap">
              {errorMessage(createMutation.error, 'Adding the repository failed')}
            </div>
          )}

          <div className="flex justify-end gap-3">
            <button type="button" onClick={onClose} className="btn btn-secondary">Cancel</button>
            <button type="submit" disabled={!canSubmit} className="btn btn-primary">
              {createMutation.isPending ? 'Adding and fetching index...' : 'Add Repository'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
