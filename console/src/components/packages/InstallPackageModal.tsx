import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Check, Eye, RefreshCw, X } from 'lucide-react';
import { apiClient } from '../../lib/api';
import type { PackageSource } from '../../types';
import { errorMessage } from './hooks';

/** What the install flow installs: pasted YAML, or a package from a
 * repository (a fresh install from the catalog, or an update of an installed
 * package, which re-installs under the same install name). */
export type InstallTarget =
  | { kind: 'yaml' }
  | {
      kind: 'repository';
      repository: string;
      package: string;
      /** Install name to install (or update) under; the package's own name if left out. */
      instance?: string;
      /** Set for an update: the installed version and the one it moves to. */
      update?: { from: string; to?: string | null };
    };

type InstallStep = 'input' | 'preview' | 'installing' | 'done';

const BROAD_ROLE_WARNING = 'Broad role permission';

/** Preview → variables → confirm → install, for either kind of target. */
export function InstallPackageModal({ target, onClose }: { target: InstallTarget; onClose: () => void }) {
  const queryClient = useQueryClient();
  const fromRepository = target.kind === 'repository';
  const isUpdate = target.kind === 'repository' && !!target.update;
  const [installStep, setInstallStep] = useState<InstallStep>('input');
  const [yamlInput, setYamlInput] = useState('');
  const [previewResult, setPreviewResult] = useState<any>(null);
  const [installResult, setInstallResult] = useState<any>(null);
  const [variableValues, setVariableValues] = useState<Record<string, any>>({});
  // Install name for packages declaring multiInstance; empty = the package's own name.
  const [instanceName, setInstanceName] = useState(target.kind === 'repository' ? target.instance ?? '' : '');
  // The broad role permissions the user accepted (as broadKey below), if any.
  const [acceptedBroad, setAcceptedBroad] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const source = (): PackageSource =>
    target.kind === 'repository'
      ? { repository: target.repository, package: target.package }
      : { source: yamlInput };

  const previewMutation = useMutation({
    mutationFn: ({ variables, instance }: { variables?: Record<string, any>; instance?: string }) =>
      apiClient.previewPackage(source(), { variables, instance }),
    onSuccess: (data) => {
      setPreviewResult(data);
      // Pre-fill variable defaults
      if (data.variables?.length) {
        const defaults: Record<string, any> = {};
        for (const v of data.variables) {
          if (v.default !== undefined && v.default !== null) {
            defaults[v.name] = v.default;
          }
        }
        setVariableValues(prev => ({ ...defaults, ...prev }));
      }
      setInstallStep('preview');
    },
  });

  const installMutation = useMutation({
    mutationFn: ({ variables, instance }: { variables?: Record<string, any>; instance?: string }) =>
      apiClient.installPackage(source(), {
        variables,
        instance,
        allowBroadRolePermissions: allowBroadRolePermissions || undefined,
      }),
    onSuccess: (data) => {
      setInstallResult(data);
      setInstallStep('done');
    },
    // Back to the preview, where the error shows and the values are kept.
    onError: () => setInstallStep('preview'),
    onSettled: () => {
      // Installed versions and update badges, in the list and the catalog.
      queryClient.invalidateQueries({ queryKey: ['packages'] });
      queryClient.invalidateQueries({ queryKey: ['package-catalog'] });
    },
  });

  const runPreview = () => {
    const vars = Object.keys(variableValues).length > 0 ? variableValues : undefined;
    previewMutation.mutate({ variables: vars, instance: instanceName.trim() || undefined });
  };

  // A repository package has nothing to paste: preview it straight away
  // (once, also under StrictMode's double effect run).
  const autoPreviewed = useRef(false);
  useEffect(() => {
    if (fromRepository && !autoPreviewed.current) {
      autoPreviewed.current = true;
      runPreview();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      setYamlInput(ev.target?.result as string);
      setAcceptedBroad(null);
    };
    reader.readAsText(file);
  };

  const handlePreview = () => {
    if (fromRepository || yamlInput.trim()) runPreview();
  };

  // The preview (and its warnings) is for one install name; after editing the
  // name it no longer describes what Confirm would install.
  const previewStale =
    !!previewResult?.multi_instance &&
    (instanceName.trim() || previewResult.package_name) !== previewResult.instance;

  const broadRoleWarnings: string[] = (previewResult?.warnings || []).filter((w: string) =>
    w.startsWith(BROAD_ROLE_WARNING),
  );
  // Consent covers exactly the permissions it was given for: a preview that
  // lists others (another package, another version) needs it again.
  const broadKey = JSON.stringify(broadRoleWarnings);
  const allowBroadRolePermissions = broadRoleWarnings.length > 0 && acceptedBroad === broadKey;

  const handleInstall = () => {
    if (previewStale) return;
    setInstallStep('installing');
    const vars = Object.keys(variableValues).length > 0 ? variableValues : undefined;
    installMutation.mutate({ variables: vars, instance: instanceName.trim() || undefined });
  };

  const title =
    target.kind === 'yaml'
      ? 'Install Package'
      : `${isUpdate ? 'Update' : 'Install'} ${target.instance || target.package}`;

  const applyFailed = installResult?.apply && installResult.apply.success === false;

  return (
    <div className="fixed inset-0 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-surface-1 rounded-lg max-w-2xl w-full p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-xl font-semibold text-gray-100">{title}</h2>
            {target.kind === 'repository' && (
              <p className="text-xs text-gray-500 mt-0.5">
                <span className="font-mono">{target.package}</span> from repository{' '}
                <span className="font-mono">{target.repository}</span>
                {target.update && (
                  <>
                    {' · '}
                    <span className="font-mono">{target.update.from}</span>
                    {target.update.to && (
                      <>
                        {' → '}
                        <span className="font-mono text-amber-400">{target.update.to}</span>
                      </>
                    )}
                  </>
                )}
              </p>
            )}
          </div>
          <button onClick={onClose} className="p-1 text-gray-500 hover:text-gray-400">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Step 1: Input (pasted YAML), or fetching the package (repository) */}
        {installStep === 'input' && target.kind === 'yaml' && (
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-2">Package YAML</label>
              <textarea
                value={yamlInput}
                onChange={(e) => { setYamlInput(e.target.value); setAcceptedBroad(null); }}
                placeholder="Paste SinasPackage YAML here..."
                className="input w-full h-64 font-mono text-sm"
              />
            </div>
            <div className="flex items-center gap-3">
              <input ref={fileInputRef} type="file" accept=".yaml,.yml" onChange={handleFileUpload} className="hidden" />
              <button onClick={() => fileInputRef.current?.click()} className="btn btn-secondary text-sm">
                Upload File
              </button>
              <div className="flex-1" />
              <button
                onClick={handlePreview}
                disabled={!yamlInput.trim() || previewMutation.isPending}
                className="btn btn-primary flex items-center"
              >
                <Eye className="w-4 h-4 mr-2" />
                {previewMutation.isPending ? 'Previewing...' : 'Preview'}
              </button>
            </div>
            {previewMutation.isError && (
              <div className="p-3 bg-red-900/20 border border-red-900/30 rounded text-sm text-red-400 whitespace-pre-wrap">
                {errorMessage(previewMutation.error, 'Preview failed')}
              </div>
            )}
          </div>
        )}

        {installStep === 'input' && target.kind === 'repository' && (
          <div className="space-y-4">
            {previewMutation.isError ? (
              <>
                <div className="p-3 bg-red-900/20 border border-red-900/30 rounded text-sm text-red-400 whitespace-pre-wrap">
                  {errorMessage(previewMutation.error, 'Preview failed')}
                </div>
                <div className="flex justify-end gap-3">
                  <button onClick={onClose} className="btn btn-secondary">Cancel</button>
                  <button onClick={handlePreview} className="btn btn-primary flex items-center">
                    <RefreshCw className="w-4 h-4 mr-2" />
                    Try again
                  </button>
                </div>
              </>
            ) : (
              <div className="flex items-center justify-center py-8">
                <div className="inline-block animate-spin rounded-full h-6 w-6 border-b-2 border-primary-600 mr-3"></div>
                <span className="text-gray-400">Fetching and previewing {target.package}...</span>
              </div>
            )}
          </div>
        )}

        {/* Step 2: Preview */}
        {installStep === 'preview' && previewResult && (
          <div className="space-y-4">
            <div className="p-3 bg-blue-900/20 border border-blue-900/30 rounded">
              <h3 className="text-sm font-medium text-blue-300 mb-2">Preview - Changes to apply:</h3>
              {previewResult.changes && previewResult.changes.length > 0 ? (
                <div className="space-y-1">
                  {previewResult.changes.map((change: any, i: number) => (
                    <div key={i} className="flex items-center gap-2 text-sm">
                      <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                        change.action === 'create' ? 'bg-green-900/30 text-green-400' :
                        change.action === 'update' ? 'bg-yellow-900/30 text-yellow-400' :
                        change.action === 'delete' ? 'bg-red-900/30 text-red-400' :
                        'bg-gray-800 text-gray-400'
                      }`}>
                        {change.action}
                      </span>
                      <span className="text-gray-300">{change.resourceType}</span>
                      <span className="text-gray-500 font-mono">{change.resourceName}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-gray-400">No changes detected</p>
              )}
            </div>

            {previewResult.errors && previewResult.errors.length > 0 && (
              <div className="p-3 bg-red-900/20 border border-red-900/30 rounded">
                <div className="flex items-center gap-2 mb-1">
                  <AlertTriangle className="w-4 h-4 text-red-400" />
                  <span className="text-sm font-medium text-red-300">Errors</span>
                </div>
                {previewResult.errors.map((e: any, i: number) => (
                  <p key={i} className="text-sm text-red-400/90">{typeof e === 'string' ? e : e.message || JSON.stringify(e)}</p>
                ))}
              </div>
            )}

            {previewResult.warnings && previewResult.warnings.length > 0 && (
              <div className="p-3 bg-yellow-900/20 border border-yellow-900/30 rounded">
                <div className="flex items-center gap-2 mb-1">
                  <AlertTriangle className="w-4 h-4 text-yellow-400" />
                  <span className="text-sm font-medium text-yellow-300">Warnings</span>
                </div>
                {previewResult.warnings.map((w: string, i: number) => (
                  <p key={i} className="text-sm text-yellow-400/80">{w}</p>
                ))}
                {broadRoleWarnings.length > 0 && (
                  <label className="flex items-start gap-2 mt-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={allowBroadRolePermissions}
                      onChange={(e) => setAcceptedBroad(e.target.checked ? broadKey : null)}
                      className="w-4 h-4 mt-0.5 text-primary-600 border-line rounded"
                    />
                    <span className="text-sm text-gray-300">
                      Accept the broad role permissions listed above. They reach outside this package's own namespaces.
                    </span>
                  </label>
                )}
              </div>
            )}

            {/* Install name (packages that may be installed more than once) */}
            {previewResult.multi_instance && (
              <div className="p-3 bg-surface-2 border border-line-soft rounded">
                <h3 className="text-sm font-medium text-gray-200 mb-1">{isUpdate ? 'Updating' : 'Install as'}</h3>
                {isUpdate ? (
                  <p className="text-xs text-gray-400">
                    The install named <span className="font-mono">{previewResult.instance}</span>.
                  </p>
                ) : (
                  <>
                    <p className="text-xs text-gray-400 mb-2">
                      This package can be installed more than once. Each install needs its own name
                      (lowercase, dashes); the preview above is for <span className="font-mono">{previewResult.instance}</span>.
                    </p>
                    <div className="flex gap-2">
                      <input
                        type="text"
                        value={instanceName}
                        placeholder={previewResult.package_name || previewResult.instance}
                        onChange={(e) => setInstanceName(e.target.value)}
                        className="flex-1 px-2.5 py-1.5 text-sm font-mono bg-surface-0 border border-line rounded text-gray-200 placeholder-gray-600"
                      />
                      <button onClick={handlePreview} className="btn btn-secondary text-sm" disabled={previewMutation.isPending}>
                        Re-preview
                      </button>
                    </div>
                    {previewStale && (
                      <p className="text-xs text-amber-400 mt-2">
                        The name changed since this preview. Re-preview before installing.
                      </p>
                    )}
                  </>
                )}
              </div>
            )}

            {/* Variable form (if package requires input) */}
            {previewResult.variables && previewResult.variables.length > 0 && (
              <div className="p-3 bg-surface-2 border border-line-soft rounded">
                <h3 className="text-sm font-medium text-gray-200 mb-3">Configuration</h3>
                <div className="space-y-3">
                  {previewResult.variables.map((v: any) => (
                    <div key={v.name}>
                      <label className="block text-xs font-medium text-gray-400 mb-1">
                        {v.description || v.name}
                        {v.required && !v.default && <span className="text-red-400 ml-1">*</span>}
                      </label>

                      {v.type === 'boolean' ? (
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={variableValues[v.name] ?? v.default ?? false}
                            onChange={(e) => setVariableValues(prev => ({ ...prev, [v.name]: e.target.checked }))}
                            className="w-4 h-4 text-primary-600 border-line rounded"
                          />
                          <span className="text-sm text-gray-300 font-mono">{v.name}</span>
                        </label>
                      ) : v.type === 'enum' && v.choices ? (
                        <select
                          value={variableValues[v.name] ?? v.default ?? ''}
                          onChange={(e) => setVariableValues(prev => ({ ...prev, [v.name]: e.target.value }))}
                          className="w-full px-2.5 py-1.5 text-sm bg-surface-0 border border-line rounded text-gray-200"
                        >
                          <option value="">Select...</option>
                          {v.choices.map((c: string) => (
                            <option key={c} value={c}>{c}</option>
                          ))}
                        </select>
                      ) : v.type === 'resource_ref' && v.resource === 'llm_providers' ? (
                        <LLMProviderSelect
                          value={variableValues[v.name] ?? ''}
                          onChange={(val) => setVariableValues(prev => ({ ...prev, [v.name]: val }))}
                        />
                      ) : v.type === 'resource_ref' && v.resource === 'database_connections' ? (
                        <DatabaseConnectionSelect
                          value={variableValues[v.name] ?? ''}
                          onChange={(val) => setVariableValues(prev => ({ ...prev, [v.name]: val }))}
                        />
                      ) : (
                        <input
                          type={v.type === 'secret' ? 'password' : 'text'}
                          value={variableValues[v.name] ?? v.default ?? ''}
                          placeholder={v.example || `Enter ${v.name}`}
                          onChange={(e) => setVariableValues(prev => ({ ...prev, [v.name]: e.target.value }))}
                          className="w-full px-2.5 py-1.5 text-sm font-mono bg-surface-0 border border-line rounded text-gray-200 placeholder-gray-600"
                        />
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {previewMutation.isError && (
              <div className="p-3 bg-red-900/20 border border-red-900/30 rounded text-sm text-red-400 whitespace-pre-wrap">
                {errorMessage(previewMutation.error, 'Preview failed')}
              </div>
            )}

            {installMutation.isError && (
              <div className="p-3 bg-red-900/20 border border-red-900/30 rounded text-sm text-red-400 whitespace-pre-wrap">
                {errorMessage(installMutation.error, 'Install failed')}
              </div>
            )}

            <div className="flex justify-between">
              {fromRepository ? (
                <button onClick={onClose} className="btn btn-secondary">
                  Cancel
                </button>
              ) : (
                <button onClick={() => setInstallStep('input')} className="btn btn-secondary">
                  Back
                </button>
              )}
              <button
                onClick={handleInstall}
                className="btn btn-primary flex items-center"
                disabled={previewStale || (broadRoleWarnings.length > 0 && !allowBroadRolePermissions)}
                title={
                  previewStale
                    ? 'Re-preview with the new install name first'
                    : broadRoleWarnings.length > 0 && !allowBroadRolePermissions
                      ? 'Accept the broad role permissions first'
                      : undefined
                }
              >
                <Check className="w-4 h-4 mr-2" />
                {isUpdate ? 'Confirm Update' : 'Confirm Install'}
              </button>
            </div>
          </div>
        )}

        {/* Step 3: Installing */}
        {installStep === 'installing' && (
          <div className="flex items-center justify-center py-8">
            <div className="inline-block animate-spin rounded-full h-6 w-6 border-b-2 border-primary-600 mr-3"></div>
            <span className="text-gray-400">{isUpdate ? 'Updating package...' : 'Installing package...'}</span>
          </div>
        )}

        {/* Step 4: Done */}
        {installStep === 'done' && installResult && (
          <div className="space-y-4">
            {applyFailed ? (
              <div className="p-3 bg-red-900/20 border border-red-900/30 rounded">
                <div className="flex items-center gap-2 mb-2">
                  <AlertTriangle className="w-5 h-5 text-red-400" />
                  <span className="text-sm font-medium text-red-300">{isUpdate ? 'Update failed' : 'Install failed'}</span>
                </div>
                {(installResult.apply.errors || []).map((e: any, i: number) => (
                  <p key={i} className="text-sm text-red-400/90">{typeof e === 'string' ? e : e.message || JSON.stringify(e)}</p>
                ))}
              </div>
            ) : (
              <div className="p-3 bg-green-900/20 border border-green-900/30 rounded">
                <div className="flex items-center gap-2 mb-2">
                  <Check className="w-5 h-5 text-green-400" />
                  <span className="text-sm font-medium text-green-300">
                    {isUpdate ? 'Package updated successfully' : 'Package installed successfully'}
                  </span>
                </div>
                {installResult.package && (
                  <p className="text-sm text-gray-400">
                    {installResult.package.name} v{installResult.package.version}
                  </p>
                )}
              </div>
            )}

            {installResult.apply?.changes && installResult.apply.changes.length > 0 && (
              <div className="space-y-1">
                {installResult.apply.changes.filter((c: any) => c.action !== 'unchanged').map((change: any, i: number) => (
                  <div key={i} className="flex items-center gap-2 text-sm">
                    <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                      change.action === 'create' ? 'bg-green-900/30 text-green-400' :
                      change.action === 'delete' ? 'bg-red-900/30 text-red-400' :
                      'bg-yellow-900/30 text-yellow-400'
                    }`}>
                      {change.action}
                    </span>
                    <span className="text-gray-300">{change.resourceType}</span>
                    <span className="text-gray-500 font-mono">{change.resourceName}</span>
                  </div>
                ))}
              </div>
            )}

            <div className="flex justify-end">
              <button onClick={onClose} className="btn btn-primary">Done</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** Async dropdown for LLM provider selection in package variables */
function LLMProviderSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const { data: providers } = useQuery({
    queryKey: ['llm-providers'],
    queryFn: () => apiClient.listLLMProviders(),
  });

  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full px-2.5 py-1.5 text-sm bg-surface-0 border border-line rounded text-gray-200"
    >
      <option value="">Select LLM provider...</option>
      {(providers || []).map((p: any) => (
        <option key={p.id} value={p.name}>{p.name} ({p.provider_type})</option>
      ))}
    </select>
  );
}

/** Async dropdown for database connection selection in package variables */
function DatabaseConnectionSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const { data: connections } = useQuery({
    queryKey: ['databaseConnections'],
    queryFn: () => apiClient.listDatabaseConnections(),
  });

  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full px-2.5 py-1.5 text-sm bg-surface-0 border border-line rounded text-gray-200"
    >
      <option value="">Select database connection...</option>
      {(connections || []).map((c: any) => (
        <option key={c.id} value={c.name}>{c.name}</option>
      ))}
    </select>
  );
}
