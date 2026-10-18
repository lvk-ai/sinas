import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ArrowUpCircle, BookOpen, Check, GitBranch, Library, Package, Plus, Search, ShieldAlert, Upload } from 'lucide-react';
import { apiClient } from '../../lib/api';
import { formatRequirement } from '../../lib/packageDependencies';
import type { InstalledPackage, PackageCatalogEntry } from '../../types';
import type { InstallTarget } from './InstallPackageModal';
import { isForbidden, useAddOfficialRepository, usePackageRepositories } from './hooks';
import { LoadError } from './LoadError';

/** Packages → Catalog: every package the active repositories offer. */
export function PackageCatalog({
  installed,
  onInstall,
  onShowRepositories,
}: {
  installed: InstalledPackage[] | undefined;
  onInstall: (target: InstallTarget) => void;
  onShowRepositories: () => void;
}) {
  const [search, setSearch] = useState('');
  const { data: catalog, isLoading, error: catalogError, refetch, isFetching } = useQuery({
    queryKey: ['package-catalog'],
    queryFn: () => apiClient.getPackageCatalog(),
    retry: (failureCount, error) => !isForbidden(error) && failureCount < 1,
  });
  const { data: repositories, error: repositoriesError } = usePackageRepositories();
  const addOfficial = useAddOfficialRepository();
  const canManageRepositories = !isForbidden(repositoriesError);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!catalog) return [];
    if (!q) return catalog;
    return catalog.filter((e) =>
      [e.name, e.description, e.repository, e.author].some((v) => v?.toLowerCase().includes(q)),
    );
  }, [catalog, search]);

  if (isLoading) {
    return (
      <div className="text-center py-12">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600"></div>
      </div>
    );
  }

  if (isForbidden(catalogError)) {
    return (
      <div className="text-center py-12 card">
        <ShieldAlert className="w-12 h-12 text-gray-500 mx-auto mb-3" />
        <h3 className="text-lg font-medium text-gray-100 mb-1">No access to packages</h3>
        <p className="text-gray-400">Viewing the catalog needs permission to read packages. Ask an administrator.</p>
      </div>
    );
  }

  if (catalogError) {
    return <LoadError what="the catalog" error={catalogError} onRetry={() => refetch()} retrying={isFetching} />;
  }

  if (!catalog || catalog.length === 0) {
    const noRepositories = canManageRepositories && repositories !== undefined && repositories.length === 0;
    return (
      <div className="text-center py-12 card">
        <Library className="w-16 h-16 text-gray-500 mx-auto mb-4" />
        {noRepositories ? (
          <>
            <h3 className="text-lg font-medium text-gray-100 mb-2">No package repositories yet</h3>
            <p className="text-gray-400 mb-4 max-w-xl mx-auto">
              The catalog lists the packages your repositories offer. Add the official repository maintained by the
              Sinas team to get started, or add your own under Repositories.
            </p>
            <div className="flex items-center justify-center gap-3">
              <button
                onClick={() => addOfficial.mutate()}
                disabled={addOfficial.isPending}
                className="btn btn-primary flex items-center"
              >
                <Plus className="w-4 h-4 mr-2" />
                {addOfficial.isPending ? 'Adding...' : 'Add official repository'}
              </button>
              <button onClick={onShowRepositories} className="btn btn-secondary">
                Repositories
              </button>
            </div>
          </>
        ) : (
          <>
            <h3 className="text-lg font-medium text-gray-100 mb-2">No packages on offer</h3>
            <p className="text-gray-400 mb-4 max-w-xl mx-auto">
              {canManageRepositories
                ? 'None of your enabled repositories lists any packages. Check their status under Repositories.'
                : 'No package repository offers packages yet. An administrator can add one.'}
            </p>
            {canManageRepositories && (
              <button onClick={onShowRepositories} className="btn btn-secondary">
                Repositories
              </button>
            )}
          </>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="relative">
        <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-gray-500 pointer-events-none" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search packages by name, description, repository or author..."
          className="input w-full !pl-11"
        />
      </div>

      {filtered.length === 0 ? (
        <div className="text-center py-8 text-gray-400">No packages match "{search}".</div>
      ) : (
        <div className="space-y-3">
          {filtered.map((entry) => (
            <CatalogCard
              key={`${entry.repository}/${entry.name}`}
              entry={entry}
              installed={installed}
              onInstall={onInstall}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** records-2, records-3, ...: the first one not taken. Install names are
 * lowercase letters, digits and dashes, at most 63 characters. */
function nextInstanceName(packageName: string, taken: string[]): string {
  const base = packageName.toLowerCase().replace(/[^a-z0-9-]/g, '-').slice(0, 55);
  for (let n = 2; ; n++) {
    const candidate = `${base}-${n}`;
    if (!taken.includes(candidate)) return candidate;
  }
}

function CatalogCard({
  entry,
  installed,
  onInstall,
}: {
  entry: PackageCatalogEntry;
  installed: InstalledPackage[] | undefined;
  onInstall: (target: InstallTarget) => void;
}) {
  // Installed under this name, but following another repository (or none):
  // installing from here would replace it.
  const installedElsewhere =
    entry.installed.length === 0
      ? (installed || []).filter(
          (p) => (p.package_name || p.name) === entry.name && p.source_repository !== entry.repository,
        )
      : [];

  const update = (install: PackageCatalogEntry['installed'][number]) =>
    onInstall({
      kind: 'repository',
      repository: entry.repository,
      package: entry.name,
      instance: install.name !== entry.name ? install.name : undefined,
      update: { from: install.version, to: entry.version },
    });
  // One install: its Update is the card's button. Several (named instances):
  // each badge carries its own.
  const single = entry.installed.length === 1 ? entry.installed[0] : null;
  // Every install name in use: this package's installs, and anything else
  // installed (an install name is unique across packages).
  const takenNames = Array.from(
    new Set([...entry.installed.map((i) => i.name), ...(installed || []).map((p) => p.name)]),
  );

  return (
    <div className="card">
      <div className="flex items-start gap-4">
        <Package className="w-8 h-8 text-primary-400 flex-shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="font-semibold text-gray-100">{entry.name}</h3>
            <span className="text-xs px-2 py-0.5 bg-primary-900/30 text-primary-300 rounded font-mono">{entry.version}</span>
            <span className="flex items-center gap-1 text-xs px-2 py-0.5 bg-surface-2 text-gray-400 rounded" title="Repository">
              <Library className="w-3 h-3" />
              {entry.repository}
            </span>
            {entry.readme_url && (
              <a
                href={entry.readme_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 text-xs text-primary-400 hover:underline"
              >
                <BookOpen className="w-3 h-3" />
                Readme
              </a>
            )}
          </div>
          {entry.description && <p className="text-sm text-gray-400 mt-0.5">{entry.description}</p>}
          {entry.author && <p className="text-xs text-gray-500 mt-1">by {entry.author}</p>}
          {(entry.requires?.length ?? 0) > 0 && (
            <p
              className="flex items-center gap-1.5 flex-wrap text-xs text-gray-500 mt-1"
              title="Installed first when missing, from the active repositories"
            >
              <GitBranch className="w-3 h-3" />
              Requires
              {entry.requires!.map((r) => {
                const target = r.instance || r.name;
                const present = (installed || []).some((p) => p.name === target);
                return (
                  <span
                    key={target}
                    className="px-1.5 py-0.5 rounded font-mono bg-surface-2 text-gray-300"
                    title={present ? 'Installed' : 'Installed first when you install this package'}
                  >
                    {formatRequirement(r)}
                    {present && <Check className="w-3 h-3 inline ml-1 text-green-400" />}
                  </span>
                );
              })}
            </p>
          )}

          {(entry.installed.length > 0 || installedElsewhere.length > 0) && (
            <div className="flex flex-wrap gap-2 mt-2">
              {entry.installed.map((install) =>
                install.update_available ? (
                  <span
                    key={install.name}
                    className="flex items-center gap-1.5 text-xs px-2 py-0.5 bg-amber-900/30 text-amber-400 rounded"
                  >
                    <ArrowUpCircle className="w-3 h-3" />
                    {install.name !== entry.name && <span className="font-mono">{install.name}:</span>}
                    Update available <span className="font-mono">{install.version} → {entry.version}</span>
                    {!single && (
                      <button onClick={() => update(install)} className="ml-1 font-medium underline hover:no-underline">
                        Update
                      </button>
                    )}
                  </span>
                ) : (
                  <span
                    key={install.name}
                    className="flex items-center gap-1 text-xs px-2 py-0.5 bg-green-900/30 text-green-400 rounded"
                  >
                    <Check className="w-3 h-3" />
                    {install.name !== entry.name && <span className="font-mono">{install.name}:</span>}
                    Installed <span className="font-mono">{install.version}</span>
                  </span>
                ),
              )}
              {installedElsewhere.map((p) => (
                <span
                  key={p.id}
                  className="text-xs px-2 py-0.5 bg-surface-2 text-gray-400 rounded"
                  title="Installing from this repository replaces it, and it follows this repository from then on"
                >
                  Installed {p.version}
                  {p.source_repository ? ` from ${p.source_repository}` : ' (pasted YAML)'}
                </span>
              ))}
            </div>
          )}
        </div>
        {entry.installed.length === 0 ? (
          <button
            onClick={() => onInstall({ kind: 'repository', repository: entry.repository, package: entry.name })}
            className="btn btn-primary flex items-center flex-shrink-0"
          >
            <Upload className="w-4 h-4 mr-2" />
            Install
          </button>
        ) : (
          <div className="flex flex-col items-end gap-2 flex-shrink-0">
            {single?.update_available && (
              <button onClick={() => update(single)} className="btn btn-primary flex items-center">
                <ArrowUpCircle className="w-4 h-4 mr-2" />
                Update
              </button>
            )}
            {entry.multi_instance && (
              // A new install name next to the existing ones; the flow's
              // "Install as" field starts at a free one.
              <button
                onClick={() =>
                  onInstall({
                    kind: 'repository',
                    repository: entry.repository,
                    package: entry.name,
                    instance: nextInstanceName(entry.name, takenNames),
                    takenNames,
                  })
                }
                className="btn btn-secondary flex items-center"
              >
                <Plus className="w-4 h-4 mr-2" />
                Install another
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
