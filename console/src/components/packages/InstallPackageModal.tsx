import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Check, Eye, GitBranch, Package as PackageIcon, RefreshCw, X } from 'lucide-react';
import { apiClient } from '../../lib/api';
import {
  BLOCKED_LABEL,
  dependencyPayload,
  installWarnings,
  planSummary,
  sentValues,
  variableDefaults,
} from '../../lib/packageDependencies';
import type { ApplyChange, DependencyBreak, DependencyPlanEntry, PackageSource } from '../../types';
import { errorMessage } from './hooks';

// Kinds a package never deletes on uninstall (deleted_with_package = False
// in their backend appliers): taking one over doesn't make uninstall remove it.
const KEPT_ON_UNINSTALL = new Set(['dependencies', 'secrets', 'llmProviders', 'databaseConnections', 'packageRepositories']);

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
      /** Set for another instance of a multi-instance package: install names
       * already in use, which the new one must not take (that would update
       * the existing install instead). */
      takenNames?: string[];
    };

type InstallStep = 'input' | 'preview' | 'installing' | 'done';


const sortedEntries = (values: Record<string, any> | undefined) =>
  Object.keys(values ?? {}).sort().map((k) => [k, values![k]]);

type PreviewRequest = {
  variables?: Record<string, any>;
  instance?: string;
  adoptUnmanaged?: boolean;
  dependencyVariables?: Record<string, Record<string, any>>;
  allowBreakingDependents?: boolean;
};

/** The values, install name and choices a preview (or an install) is for,
 * as one comparable key: Confirm installs exactly what the shown preview
 * was for. */
const requestKey = (r: PreviewRequest) =>
  JSON.stringify([
    sortedEntries(r.variables),
    (r.instance ?? '').trim(),
    !!r.adoptUnmanaged,
    sortedEntries(r.dependencyVariables).map(([k, v]) => [k, sortedEntries(v)]),
    !!r.allowBreakingDependents,
  ]);

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
  // What the preview on screen was for (requestKey of its values and name).
  const [previewedKey, setPreviewedKey] = useState<string | null>(null);
  // Take over resources the package declares that were edited by hand.
  const [adoptUnmanaged, setAdoptUnmanaged] = useState(false);
  // Values for the packages the install installs first, by install name.
  const [dependencyValues, setDependencyValues] = useState<Record<string, Record<string, any>>>({});
  // Upgrade although installed packages requiring this one don't accept the version.
  const [allowBreaking, setAllowBreaking] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Any edit to what would be installed withdraws the consent: it was given
  // for the permissions a preview of other values showed.
  const setVariable = (name: string, value: any) => {
    setVariableValues((prev) => ({ ...prev, [name]: value }));
    setAcceptedBroad(null);
  };
  const setDependencyVariable = (installName: string, name: string, value: any) => {
    setDependencyValues((prev) => ({ ...prev, [installName]: { ...prev[installName], [name]: value } }));
    setAcceptedBroad(null);
  };
  const changeInstanceName = (value: string) => {
    setInstanceName(value);
    setAcceptedBroad(null);
  };

  const source = (): PackageSource =>
    target.kind === 'repository'
      ? { repository: target.repository, package: target.package }
      : { source: yamlInput };

  const previewMutation = useMutation({
    mutationFn: ({ variables, instance, adoptUnmanaged, dependencyVariables, allowBreakingDependents }: PreviewRequest) =>
      apiClient.previewPackage(source(), {
        variables,
        instance,
        adoptUnmanaged: adoptUnmanaged || undefined,
        dependencyVariables,
        allowBreakingDependents: allowBreakingDependents || undefined,
      }),
    onSuccess: (data, request) => {
      setPreviewResult(data);
      setPreviewedKey(requestKey(request));
      setAcceptedBroad(null);
      // Pre-fill variable defaults (a preview with them is still to be run:
      // until then the form differs from what was previewed)
      if (data.variables?.length) {
        const defaults = variableDefaults(data.variables);
        setVariableValues(prev => ({ ...defaults, ...prev }));
      }
      // The same for each package the install would install first.
      const toInstall = planSummary(data.dependencies).toInstall.filter((d) => d.variables?.length);
      if (toInstall.length) {
        setDependencyValues((prev) => {
          const next = { ...prev };
          for (const d of toInstall) next[d.install_name] = { ...variableDefaults(d.variables), ...prev[d.install_name] };
          return next;
        });
      }
      setInstallStep('preview');
    },
  });

  const installMutation = useMutation({
    mutationFn: ({ variables, instance, adoptUnmanaged, dependencyVariables, allowBreakingDependents }: PreviewRequest) =>
      apiClient.installPackage(source(), {
        variables,
        instance,
        allowBroadRolePermissions: allowBroadRolePermissions || undefined,
        adoptUnmanaged: adoptUnmanaged || undefined,
        dependencyVariables,
        allowBreakingDependents: allowBreakingDependents || undefined,
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

  /** The values to send (a blank secret is left out, see sentValues). */
  const payloadValues = (): Record<string, any> | undefined =>
    sentValues(variableValues, previewResult?.variables);

  /** What to send: these values, for the packages the shown plan installs. */
  const currentRequest = (
    overrides: Partial<Pick<PreviewRequest, 'adoptUnmanaged' | 'allowBreakingDependents'>> = {},
  ): PreviewRequest => ({
    variables: payloadValues(),
    instance: instanceName.trim() || undefined,
    adoptUnmanaged,
    dependencyVariables: dependencyPayload(dependencyValues, previewResult?.dependencies),
    allowBreakingDependents: allowBreaking,
    ...overrides,
  });

  const runPreview = (overrides: Partial<Pick<PreviewRequest, 'adoptUnmanaged' | 'allowBreakingDependents'>> = {}) => {
    previewMutation.mutate(currentRequest(overrides));
  };

  // Taking over hand-edited resources changes what the install does to
  // them: show it, by previewing again with the choice.
  const changeAdoptUnmanaged = (adopt: boolean) => {
    setAdoptUnmanaged(adopt);
    runPreview({ adoptUnmanaged: adopt });
  };
  // Accepting broken dependents changes the verdict: preview again with it.
  const changeAllowBreaking = (allow: boolean) => {
    setAllowBreaking(allow);
    runPreview({ allowBreakingDependents: allow });
  };

  // A repository package has nothing to paste: preview it straight away.
  // Once, also under StrictMode's mount-unmount-mount: the first run's timer
  // is cleared, and the mutation starts after the remount, so the observer
  // that shows its progress is the one subscribed (a mutation started before
  // the simulated unmount left the button "Previewing..." for good).
  useEffect(() => {
    if (!fromRepository) return;
    const timer = setTimeout(() => runPreview(), 0);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      setYamlInput(ev.target?.result as string);
      setAcceptedBroad(null);
      setAdoptUnmanaged(false);
    };
    reader.readAsText(file);
  };

  const handlePreview = () => {
    if (fromRepository || yamlInput.trim()) runPreview();
  };

  // The preview (its changes, warnings, broad permissions) is for one set of
  // values and one install name; after any edit it no longer describes what
  // Confirm would install.
  const previewStale = !!previewResult && previewedKey !== requestKey(currentRequest());
  const instanceStale =
    !!previewResult?.multi_instance &&
    (instanceName.trim() || previewResult.package_name) !== previewResult.instance;
  // Whether values are missing is the server's call, not ours: the preview
  // resolves variables by the install's rules (an existing secret reused, a
  // default, even an empty one) and fails when one is missing. So a
  // successful preview for exactly these values is the gate.
  const previewFailed = !!previewResult && (previewResult.success === false || (previewResult.errors || []).length > 0);

  const plan = planSummary(previewResult?.dependencies);
  const breaks: DependencyBreak[] = previewResult?.breaks || [];
  // The consent covers every package the install installs: the ones it
  // installs first too, their broad permissions named after them.
  // Every warning is shown, the dependencies' named after them.
  const { all: allWarnings, broad: broadRoleWarnings } = installWarnings(
    previewResult?.dependencies,
    previewResult?.warnings,
  );
  // Consent covers exactly the permissions this preview showed, for exactly
  // its values: a new preview or any edit withdraws it.
  const broadKey = `${previewedKey}|${JSON.stringify(broadRoleWarnings)}`;
  const allowBroadRolePermissions = broadRoleWarnings.length > 0 && acceptedBroad === broadKey;

  // Resources this preview skips because they were edited by hand (or, when
  // previewed with adoptUnmanaged, takes over): the server marks them.
  const handEdited: ApplyChange[] = (previewResult?.changes || []).filter(
    (c: ApplyChange) => c.reason === 'edited_by_hand' || c.reason === 'adopted',
  );
  const keptOnUninstall = handEdited.filter((c) => KEPT_ON_UNINSTALL.has(c.resourceType));
  const deletedOnUninstall = handEdited.length - keptOnUninstall.length;
  const uninstallNote =
    keptOnUninstall.length === 0
      ? 'and uninstalling the package deletes them.'
      : deletedOnUninstall === 0
        ? 'and uninstalling the package leaves them installed.'
        : `and uninstalling the package deletes ${deletedOnUninstall} of them; the other ` +
          `${keptOnUninstall.length} (${[...new Set(keptOnUninstall.map((c) => c.resourceType))].join(', ')}) stay installed.`;

  const takenNames = target.kind === 'repository' ? target.takenNames : undefined;
  const effectiveName = instanceName.trim() || (target.kind === 'repository' ? target.package : '');
  const nameTaken = !!takenNames && takenNames.includes(effectiveName);

  const blockedBecause = nameTaken
    ? `'${effectiveName}' is already installed: choose a new install name`
    : previewStale
      ? 'Preview with these values first'
      : plan.blocked.length > 0
        ? 'Its required packages can\'t all be installed'
      : previewFailed
        ? 'The preview failed: fix the values, then preview again'
        : broadRoleWarnings.length > 0 && !allowBroadRolePermissions
          ? 'Accept the broad role permissions first'
          : null;

  const handleInstall = () => {
    if (blockedBecause) return;
    setInstallStep('installing');
    installMutation.mutate(currentRequest());
  };

  const title =
    target.kind === 'yaml'
      ? 'Install Package'
      : target.takenNames
        ? `Install another ${target.package}`
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
                onChange={(e) => { setYamlInput(e.target.value); setAcceptedBroad(null); setAdoptUnmanaged(false); }}
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
            {(previewResult.dependencies?.length ?? 0) > 0 && (
              <DependencyPlan entries={previewResult.dependencies} />
            )}

            {breaks.length > 0 && (
              <div className="p-3 bg-red-900/20 border border-red-900/30 rounded">
                <div className="flex items-center gap-2 mb-1">
                  <AlertTriangle className="w-4 h-4 text-red-400" />
                  <span className="text-sm font-medium text-red-300">Packages that require this one</span>
                </div>
                {breaks.map((b) => (
                  <p key={`${b.dependent}-${b.requires}`} className="text-sm text-red-400/90">
                    <span className="font-mono">{b.dependent}</span> requires{' '}
                    <span className="font-mono">{b.requires}{b.version ? ` ${b.version}` : ''}</span>, which this version doesn't meet.
                  </p>
                ))}
                <label className="flex items-start gap-2 mt-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={allowBreaking}
                    disabled={previewMutation.isPending}
                    onChange={(e) => changeAllowBreaking(e.target.checked)}
                    className="w-4 h-4 mt-0.5 text-primary-600 border-line rounded"
                  />
                  <span className="text-sm text-gray-300">
                    Install anyway. Those packages stay installed but no longer get the version they ask for;
                    updating them first is safer.
                  </span>
                </label>
              </div>
            )}

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

            {allWarnings.length > 0 && (
              <div className="p-3 bg-yellow-900/20 border border-yellow-900/30 rounded">
                <div className="flex items-center gap-2 mb-1">
                  <AlertTriangle className="w-4 h-4 text-yellow-400" />
                  <span className="text-sm font-medium text-yellow-300">Warnings</span>
                </div>
                {allWarnings.map((w, i) => (
                  <p key={i} className="text-sm text-yellow-400/80">{w}</p>
                ))}
                {broadRoleWarnings.length > 0 && (
                  <label className="flex items-start gap-2 mt-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={allowBroadRolePermissions}
                      // Consent is for what this preview showed: not while the values differ from it.
                      disabled={previewStale}
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

            {/* Resources edited by hand: skipped, unless the user takes them over */}
            {(handEdited.length > 0 || adoptUnmanaged) && (
              <div className="p-3 bg-surface-2 border border-line-soft rounded">
                <label className="flex items-start gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={adoptUnmanaged}
                    disabled={previewMutation.isPending}
                    onChange={(e) => changeAdoptUnmanaged(e.target.checked)}
                    className="w-4 h-4 mt-0.5 text-primary-600 border-line rounded"
                  />
                  <span>
                    <span className="block text-sm text-gray-200">
                      Take over {handEdited.length} resource{handEdited.length === 1 ? '' : 's'} edited by hand
                    </span>
                    <span className="block text-xs text-gray-400 mt-0.5">
                      The package overwrites their hand edits, {uninstallNote}
                    </span>
                  </span>
                </label>
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
                        onChange={(e) => changeInstanceName(e.target.value)}
                        className="flex-1 px-2.5 py-1.5 text-sm font-mono bg-surface-0 border border-line rounded text-gray-200 placeholder-gray-600"
                      />
                      <button onClick={handlePreview} className="btn btn-secondary text-sm" disabled={previewMutation.isPending}>
                        Re-preview
                      </button>
                    </div>
                    {instanceStale && (
                      <p className="text-xs text-amber-400 mt-2">
                        The name changed since this preview. Re-preview before installing.
                      </p>
                    )}
                  </>
                )}
              </div>
            )}

            {/* Configuration of each package the install installs first */}
            {plan.toInstall
              .filter((d) => d.variables && d.variables.length > 0)
              .map((d) => (
                <div key={d.install_name} className="p-3 bg-surface-2 border border-line-soft rounded">
                  <h3 className="text-sm font-medium text-gray-200 mb-1">
                    Configuration: <span className="font-mono">{d.install_name}</span>
                  </h3>
                  <p className="text-xs text-gray-500 mb-3">
                    Required package, installed first ({d.package_name} {d.version}).
                  </p>
                  <VariableFields
                    declarations={d.variables!}
                    values={dependencyValues[d.install_name] ?? {}}
                    onChange={(name, value) => setDependencyVariable(d.install_name, name, value)}
                  />
                </div>
              ))}

            {/* Variable form (if package requires input) */}
            {previewResult.variables && previewResult.variables.length > 0 && (
              <div className="p-3 bg-surface-2 border border-line-soft rounded">
                <h3 className="text-sm font-medium text-gray-200 mb-3">
                  Configuration{plan.toInstall.length > 0 && <>: <span className="font-mono">{previewResult.instance}</span></>}
                </h3>
                <VariableFields declarations={previewResult.variables} values={variableValues} onChange={setVariable} />
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
              <div className="flex items-center gap-2">
                {(previewStale || previewFailed) && (
                  <button
                    onClick={handlePreview}
                    disabled={previewMutation.isPending}
                    className="btn btn-secondary flex items-center"
                  >
                    <Eye className="w-4 h-4 mr-2" />
                    {previewMutation.isPending ? 'Previewing...' : 'Preview with these values'}
                  </button>
                )}
                <button
                  onClick={handleInstall}
                  className="btn btn-primary flex items-center"
                  disabled={!!blockedBecause}
                  title={blockedBecause ?? undefined}
                >
                  <Check className="w-4 h-4 mr-2" />
                  {isUpdate ? 'Confirm Update' : 'Confirm Install'}
                </button>
              </div>
            </div>
            {blockedBecause && (
              <p className="text-xs text-amber-400 text-right">{blockedBecause}.</p>
            )}
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
                {(installResult.dependencies || []).length > 0 && (
                  <p className="text-sm text-gray-400 mt-1">
                    Also installed:{' '}
                    {installResult.dependencies.map((d: any, i: number) => (
                      <span key={d.package.name}>
                        {i > 0 && ', '}
                        <span className="font-mono">{d.package.name}</span> v{d.package.version}
                      </span>
                    ))}
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

/** What the package requires: already installed, installed first, or why
 * it can't be. */
function DependencyPlan({ entries }: { entries: DependencyPlanEntry[] }) {
  const { toInstall, satisfied, blocked } = planSummary(entries);
  return (
    <div className="p-3 bg-surface-2 border border-line-soft rounded space-y-2">
      <h3 className="text-sm font-medium text-gray-200 flex items-center gap-2">
        <GitBranch className="w-4 h-4 text-gray-400" />
        Required packages
      </h3>
      {toInstall.map((d) => (
        <div key={d.install_name} className="text-sm">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-green-900/30 text-green-400">Also installs</span>
            <PackageIcon className="w-3.5 h-3.5 text-gray-500" />
            <span className="font-mono text-gray-200">{d.install_name}</span>
            <span className="font-mono text-gray-400">{d.version}</span>
            {d.repository && <span className="text-xs text-gray-500">from <span className="font-mono">{d.repository}</span></span>}
            {d.install_name !== d.package_name && (
              <span className="text-xs text-gray-500">instance of <span className="font-mono">{d.package_name}</span></span>
            )}
          </div>
          <p className="text-xs text-gray-500 ml-1 mt-0.5">
            Required by {d.required_by.join(', ')}
            {d.preview && d.preview.success && ` · ${d.preview.changes.filter((c) => c.action !== 'unchanged').length} resource change(s)`}
          </p>
          {d.preview && !d.preview.success && (d.preview.errors || []).map((e, i) => (
            <p key={i} className="text-xs text-red-400/90 ml-1">{e}</p>
          ))}
        </div>
      ))}
      {satisfied.map((d) => (
        <div key={d.install_name} className="flex items-center gap-2 text-sm flex-wrap">
          <span className="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs font-medium bg-surface-0 text-gray-400">
            <Check className="w-3 h-3" />
            Installed
          </span>
          <span className="font-mono text-gray-300">{d.install_name}</span>
          <span className="font-mono text-gray-500">{d.version}</span>
        </div>
      ))}
      {blocked.map((d) => (
        <div key={d.install_name} className="text-sm">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-red-900/30 text-red-400">
              {BLOCKED_LABEL[d.status] ?? d.status}
            </span>
            <span className="font-mono text-gray-200">{d.install_name}</span>
          </div>
          {d.message && <p className="text-xs text-red-400/90 ml-1 mt-0.5">{d.message}</p>}
        </div>
      ))}
    </div>
  );
}

/** A package's install-time variables as form fields. */
function VariableFields({
  declarations,
  values,
  onChange,
}: {
  declarations: any[];
  values: Record<string, any>;
  onChange: (name: string, value: any) => void;
}) {
  return (
    <div className="space-y-3">
      {declarations.map((v: any) => (
        <div key={v.name}>
          <label className="block text-xs font-medium text-gray-400 mb-1">
            {v.description || v.name}
            {v.required && !v.default && <span className="text-red-400 ml-1">*</span>}
          </label>

          {v.type === 'boolean' ? (
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={values[v.name] ?? v.default ?? false}
                onChange={(e) => onChange(v.name, e.target.checked)}
                className="w-4 h-4 text-primary-600 border-line rounded"
              />
              <span className="text-sm text-gray-300 font-mono">{v.name}</span>
            </label>
          ) : v.type === 'enum' && v.choices ? (
            <select
              value={values[v.name] ?? v.default ?? ''}
              onChange={(e) => onChange(v.name, e.target.value)}
              className="w-full px-2.5 py-1.5 text-sm bg-surface-0 border border-line rounded text-gray-200"
            >
              <option value="">Select...</option>
              {v.choices.map((c: string) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          ) : v.type === 'resource_ref' && v.resource === 'llm_providers' ? (
            <LLMProviderSelect value={values[v.name] ?? ''} onChange={(val) => onChange(v.name, val)} />
          ) : v.type === 'resource_ref' && v.resource === 'database_connections' ? (
            <DatabaseConnectionSelect value={values[v.name] ?? ''} onChange={(val) => onChange(v.name, val)} />
          ) : (
            <input
              type={v.type === 'secret' ? 'password' : 'text'}
              value={values[v.name] ?? v.default ?? ''}
              placeholder={v.example || (v.type === 'secret' ? 'Leave blank to keep an existing secret' : `Enter ${v.name}`)}
              onChange={(e) => onChange(v.name, e.target.value)}
              className="w-full px-2.5 py-1.5 text-sm font-mono bg-surface-0 border border-line rounded text-gray-200 placeholder-gray-600"
            />
          )}
        </div>
      ))}
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
