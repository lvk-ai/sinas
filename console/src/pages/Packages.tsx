import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { apiClient } from '../lib/api';
import { Package, Plus, Trash2, Download, Upload, X, Check, ArrowUpCircle, Library, Store, ShieldAlert } from 'lucide-react';
import { useState } from 'react';
import { InstallPackageModal, type InstallTarget } from '../components/packages/InstallPackageModal';
import { LoadError } from '../components/packages/LoadError';
import { isForbidden } from '../components/packages/hooks';
import { PackageCatalog } from '../components/packages/PackageCatalog';
import { PackageRepositories } from '../components/packages/PackageRepositories';
import type { InstalledPackage } from '../types';

type Tab = 'installed' | 'catalog' | 'repositories';
const TABS: Tab[] = ['installed', 'catalog', 'repositories'];

export function Packages() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get('tab') as Tab | null;
  const activeTab: Tab = tabParam && TABS.includes(tabParam) ? tabParam : 'installed';
  const setActiveTab = (tab: Tab) =>
    setSearchParams(tab === 'installed' ? {} : { tab }, { replace: true });

  // The install flow, for pasted YAML or a repository package (install or update).
  const [installTarget, setInstallTarget] = useState<InstallTarget | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createForm, setCreateForm] = useState({ name: '', version: '1.0.0', description: '', author: '', url: '' });
  const [selectedResources, setSelectedResources] = useState<Array<{ type: string; namespace: string; name: string }>>([]);
  const [createdYaml, setCreatedYaml] = useState('');

  const { data: packages, isLoading, error: packagesError, refetch: refetchPackages, isFetching: packagesFetching } = useQuery({
    queryKey: ['packages'],
    queryFn: () => apiClient.listPackages(),
    // No package access is a 403, explained in place below: not retried.
    retry: (failureCount, error) => !isForbidden(error) && failureCount < 1,
  });
  const updatesAvailable = (packages || []).filter((p) => p.update_available).length;

  // Load available resources for create modal
  const { data: agents } = useQuery({ queryKey: ['agents'], queryFn: () => apiClient.listAgents(), enabled: showCreateModal });
  const { data: functions } = useQuery({ queryKey: ['functions'], queryFn: () => apiClient.listFunctions(), enabled: showCreateModal });
  const { data: skills } = useQuery({ queryKey: ['skills'], queryFn: () => apiClient.listSkills(), enabled: showCreateModal });
  const { data: manifests } = useQuery({ queryKey: ['manifests'], queryFn: () => apiClient.listManifests(), enabled: showCreateModal });
  const { data: components } = useQuery({ queryKey: ['components'], queryFn: () => apiClient.listComponents(), enabled: showCreateModal });
  const { data: queries } = useQuery({ queryKey: ['queries'], queryFn: () => apiClient.listQueries(), enabled: showCreateModal });
  const { data: collections } = useQuery({ queryKey: ['collections'], queryFn: () => apiClient.listCollections(), enabled: showCreateModal });
  const { data: webhooks } = useQuery({ queryKey: ['webhooks'], queryFn: () => apiClient.listWebhooks(), enabled: showCreateModal });
  const { data: templates } = useQuery({ queryKey: ['templates'], queryFn: () => apiClient.listTemplates(), enabled: showCreateModal });
  const { data: schedules } = useQuery({ queryKey: ['schedules'], queryFn: () => apiClient.listSchedules(), enabled: showCreateModal });
  const { data: databaseTriggers } = useQuery({ queryKey: ['database-triggers'], queryFn: () => apiClient.listDatabaseTriggers(), enabled: showCreateModal });
  const { data: connectors } = useQuery({ queryKey: ['connectors'], queryFn: () => apiClient.listConnectors(), enabled: showCreateModal });

  const uninstallMutation = useMutation({
    mutationFn: (name: string) => apiClient.uninstallPackage(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['packages'] });
      queryClient.invalidateQueries({ queryKey: ['package-catalog'] });
    },
  });

  const createMutation = useMutation({
    mutationFn: (data: any) => apiClient.createPackageYaml(data),
    onSuccess: (data) => {
      setCreatedYaml(data.yaml);
    },
  });

  /** Update = re-install the repository's latest version under the same install name. */
  const startUpdate = (pkg: InstalledPackage) => {
    if (!pkg.source_repository) return;
    const packageName = pkg.package_name || pkg.name;
    setInstallTarget({
      kind: 'repository',
      repository: pkg.source_repository,
      package: packageName,
      instance: pkg.name !== packageName ? pkg.name : undefined,
      update: { from: pkg.version, to: pkg.latest_version },
    });
  };

  const handleExport = async (name: string) => {
    try {
      const yaml = await apiClient.exportPackage(name);
      const blob = new Blob([yaml], { type: 'text/yaml' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${name}.yaml`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      // Error handled by API client
    }
  };

  const toggleResource = (type: string, namespace: string, name: string) => {
    setSelectedResources(prev => {
      const exists = prev.some(r => r.type === type && r.namespace === namespace && r.name === name);
      if (exists) {
        return prev.filter(r => !(r.type === type && r.namespace === namespace && r.name === name));
      }
      return [...prev, { type, namespace, name }];
    });
  };

  const isSelected = (type: string, namespace: string, name: string) =>
    selectedResources.some(r => r.type === type && r.namespace === namespace && r.name === name);

  const handleCreate = () => {
    if (!createForm.name.trim() || selectedResources.length === 0) return;
    createMutation.mutate({
      name: createForm.name,
      version: createForm.version,
      description: createForm.description || undefined,
      author: createForm.author || undefined,
      url: createForm.url || undefined,
      resources: selectedResources,
    });
  };

  const closeCreateModal = () => {
    setShowCreateModal(false);
    setCreateForm({ name: '', version: '1.0.0', description: '', author: '', url: '' });
    setSelectedResources([]);
    setCreatedYaml('');
    createMutation.reset();
  };

  const downloadCreatedYaml = () => {
    const blob = new Blob([createdYaml], { type: 'text/yaml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${createForm.name}.yaml`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // Build resource sections for create modal
  const resourceSections = [
    { type: 'agent', label: 'Agents', items: agents?.map((a: any) => ({ namespace: a.namespace, name: a.name })) || [] },
    { type: 'function', label: 'Functions', items: functions?.map((f: any) => ({ namespace: f.namespace, name: f.name })) || [] },
    { type: 'skill', label: 'Skills', items: skills?.map((s: any) => ({ namespace: s.namespace, name: s.name })) || [] },
    { type: 'component', label: 'Components', items: components?.map((c: any) => ({ namespace: c.namespace, name: c.name })) || [] },
    { type: 'query', label: 'Queries', items: queries?.map((q: any) => ({ namespace: q.namespace, name: q.name })) || [] },
    { type: 'collection', label: 'Collections', items: collections?.map((c: any) => ({ namespace: c.namespace, name: c.name })) || [] },
    { type: 'manifest', label: 'Manifests', items: manifests?.map((a: any) => ({ namespace: a.namespace, name: a.name })) || [] },
    { type: 'webhook', label: 'Webhooks', items: webhooks?.map((w: any) => ({ namespace: 'default', name: w.path })) || [] },
    { type: 'template', label: 'Templates', items: templates?.map((t: any) => ({ namespace: t.namespace, name: t.name })) || [] },
    { type: 'schedule', label: 'Schedules', items: schedules?.map((s: any) => ({ namespace: 'default', name: s.name })) || [] },
    { type: 'database_trigger', label: 'Database Triggers', items: databaseTriggers?.map((t: any) => ({ namespace: 'default', name: t.name })) || [] },
    { type: 'connector', label: 'Connectors', items: connectors?.map((c: any) => ({ namespace: c.namespace, name: c.name })) || [] },
  ].filter(s => s.items.length > 0);

  const tabClass = (tab: Tab) =>
    `py-4 px-1 border-b-2 font-medium text-sm transition-colors flex items-center ${
      activeTab === tab
        ? 'border-primary-600 text-primary-600'
        : 'border-transparent text-gray-500 hover:text-gray-300 hover:border-line'
    }`;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-gray-100">Packages</h1>
          <p className="text-gray-400 mt-1">Install and manage integration packages</p>
        </div>
        <div className="flex space-x-3">
          <button onClick={() => setShowCreateModal(true)} className="btn btn-secondary flex items-center">
            <Plus className="w-5 h-5 mr-2" />
            Create Package
          </button>
          <button onClick={() => setInstallTarget({ kind: 'yaml' })} className="btn btn-primary flex items-center">
            <Upload className="w-5 h-5 mr-2" />
            Install from YAML
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-line-soft">
        <nav className="-mb-px flex space-x-8">
          <button onClick={() => setActiveTab('installed')} className={tabClass('installed')}>
            <Package className="w-5 h-5 mr-2" />
            Installed
            {updatesAvailable > 0 && (
              <span
                className="ml-2 px-1.5 py-0.5 text-xs rounded bg-amber-900/30 text-amber-400"
                title={`${updatesAvailable} update${updatesAvailable === 1 ? '' : 's'} available`}
              >
                {updatesAvailable}
              </span>
            )}
          </button>
          <button onClick={() => setActiveTab('catalog')} className={tabClass('catalog')}>
            <Store className="w-5 h-5 mr-2" />
            Catalog
          </button>
          <button onClick={() => setActiveTab('repositories')} className={tabClass('repositories')}>
            <Library className="w-5 h-5 mr-2" />
            Repositories
          </button>
        </nav>
      </div>

      {activeTab === 'catalog' && (
        <PackageCatalog
          installed={packages}
          onInstall={setInstallTarget}
          onShowRepositories={() => setActiveTab('repositories')}
        />
      )}

      {activeTab === 'repositories' && <PackageRepositories />}

      {/* Installed packages */}
      {activeTab === 'installed' && (isLoading ? (
        <div className="text-center py-12">
          <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600"></div>
        </div>
      ) : isForbidden(packagesError) ? (
        <div className="text-center py-12 card">
          <ShieldAlert className="w-12 h-12 text-gray-500 mx-auto mb-3" />
          <h3 className="text-lg font-medium text-gray-100 mb-1">No access to packages</h3>
          <p className="text-gray-400">Viewing installed packages needs permission to read packages. Ask an administrator.</p>
        </div>
      ) : packagesError ? (
        <LoadError
          what="installed packages"
          error={packagesError}
          onRetry={() => refetchPackages()}
          retrying={packagesFetching}
        />
      ) : packages && packages.length > 0 ? (
        <div className="space-y-3">
          {packages.map((pkg) => (
            <div key={pkg.id} className="card">
              <div className="flex items-center gap-4">
                <Package className="w-8 h-8 text-primary-400 flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <h3 className="font-semibold text-gray-100">{pkg.name}</h3>
                    <span className="text-xs px-2 py-0.5 bg-primary-900/30 text-primary-300 rounded font-mono">{pkg.version}</span>
                    {pkg.package_name && pkg.package_name !== pkg.name && (
                      <span className="text-xs text-gray-500">instance of <span className="font-mono">{pkg.package_name}</span></span>
                    )}
                    {pkg.update_available && (
                      <span className="flex items-center gap-1 text-xs px-2 py-0.5 bg-amber-900/30 text-amber-400 rounded">
                        <ArrowUpCircle className="w-3 h-3" />
                        Update available → <span className="font-mono">{pkg.latest_version}</span>
                      </span>
                    )}
                  </div>
                  {pkg.description && <p className="text-sm text-gray-400 mt-0.5">{pkg.description}</p>}
                  <div className="flex flex-wrap gap-3 mt-1 text-xs text-gray-500">
                    {pkg.author && <span>by {pkg.author}</span>}
                    <span>Installed {new Date(pkg.installed_at).toLocaleDateString()}</span>
                    {pkg.source_repository && (
                      <span className="flex items-center gap-1" title="The repository this package follows for updates">
                        <Library className="w-3 h-3" />
                        follows <span className="font-mono">{pkg.source_repository}</span>
                        {!pkg.update_available && pkg.latest_version && (
                          <span className="text-green-400 flex items-center gap-0.5 ml-1">
                            <Check className="w-3 h-3" />
                            up to date
                          </span>
                        )}
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-1 flex-shrink-0">
                  {pkg.update_available && pkg.source_repository && (
                    <button onClick={() => startUpdate(pkg)} className="btn btn-primary flex items-center text-sm mr-1">
                      <ArrowUpCircle className="w-4 h-4 mr-1.5" />
                      Update
                    </button>
                  )}
                  <button
                    onClick={() => handleExport(pkg.name)}
                    className="p-2 text-gray-500 hover:text-primary-400 hover:bg-hover-strong rounded-md transition-colors"
                    title="Export YAML"
                  >
                    <Download className="w-4 h-4" />
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`Are you sure you want to uninstall "${pkg.name}"? All resources created by this package will be deleted.`)) {
                        uninstallMutation.mutate(pkg.name);
                      }
                    }}
                    className="p-2 text-gray-500 hover:text-red-600 hover:bg-hover-strong rounded-md transition-colors"
                    disabled={uninstallMutation.isPending}
                    title="Uninstall"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-center py-12 card">
          <Package className="w-16 h-16 text-gray-500 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-100 mb-2">No packages installed</h3>
          <p className="text-gray-400 mb-4">Install integration packages to add agents, functions, and more</p>
          <div className="flex items-center justify-center gap-3">
            <button onClick={() => setActiveTab('catalog')} className="btn btn-primary flex items-center">
              <Store className="w-5 h-5 mr-2" />
              Browse Catalog
            </button>
            <button onClick={() => setInstallTarget({ kind: 'yaml' })} className="btn btn-secondary flex items-center">
              <Upload className="w-5 h-5 mr-2" />
              Install from YAML
            </button>
          </div>
        </div>
      ))}

      {/* Install / update flow */}
      {installTarget && <InstallPackageModal target={installTarget} onClose={() => setInstallTarget(null)} />}

      {/* Create Package Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <div className="bg-surface-1 rounded-lg max-w-3xl w-full p-6 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-semibold text-gray-100">Create Package</h2>
              <button onClick={closeCreateModal} className="p-1 text-gray-500 hover:text-gray-400">
                <X className="w-5 h-5" />
              </button>
            </div>

            {!createdYaml ? (
              <div className="space-y-4">
                {/* Metadata */}
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-300 mb-1">Name *</label>
                    <input
                      value={createForm.name}
                      onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })}
                      placeholder="my-package"
                      className="input w-full"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-300 mb-1">Version</label>
                    <input
                      value={createForm.version}
                      onChange={(e) => setCreateForm({ ...createForm, version: e.target.value })}
                      className="input w-full"
                    />
                  </div>
                  <div className="col-span-2">
                    <label className="block text-sm font-medium text-gray-300 mb-1">Description</label>
                    <input
                      value={createForm.description}
                      onChange={(e) => setCreateForm({ ...createForm, description: e.target.value })}
                      placeholder="What does this package do?"
                      className="input w-full"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-300 mb-1">Author</label>
                    <input
                      value={createForm.author}
                      onChange={(e) => setCreateForm({ ...createForm, author: e.target.value })}
                      className="input w-full"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-300 mb-1">URL</label>
                    <input
                      value={createForm.url}
                      onChange={(e) => setCreateForm({ ...createForm, url: e.target.value })}
                      placeholder="https://github.com/..."
                      className="input w-full"
                    />
                  </div>
                </div>

                {/* Resource Picker */}
                <div className="border-t border-line-soft pt-4">
                  <h3 className="text-sm font-medium text-gray-300 mb-3">
                    Select Resources ({selectedResources.length} selected)
                  </h3>
                  <div className="space-y-3 max-h-72 overflow-y-auto">
                    {resourceSections.map(section => (
                      <div key={section.type}>
                        <h4 className="text-xs font-semibold text-gray-500 uppercase mb-1">{section.label}</h4>
                        <div className="flex flex-wrap gap-2">
                          {section.items.map((item: any) => {
                            const selected = isSelected(section.type, item.namespace, item.name);
                            return (
                              <button
                                key={`${section.type}-${item.namespace}-${item.name}`}
                                onClick={() => toggleResource(section.type, item.namespace, item.name)}
                                className={`px-3 py-1.5 rounded text-sm font-mono transition-colors ${
                                  selected
                                    ? 'bg-primary-900/30 text-primary-300 border border-primary-700'
                                    : 'bg-surface-0 text-gray-400 border border-line-soft hover:border-line'
                                }`}
                              >
                                {item.namespace}/{item.name}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    ))}
                    {resourceSections.length === 0 && (
                      <p className="text-sm text-gray-500">No resources available</p>
                    )}
                  </div>
                </div>

                <div className="flex justify-end gap-3">
                  <button onClick={closeCreateModal} className="btn btn-secondary">Cancel</button>
                  <button
                    onClick={handleCreate}
                    disabled={!createForm.name.trim() || selectedResources.length === 0 || createMutation.isPending}
                    className="btn btn-primary"
                  >
                    {createMutation.isPending ? 'Creating...' : 'Create Package'}
                  </button>
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                <div className="p-3 bg-green-900/20 border border-green-900/30 rounded">
                  <div className="flex items-center gap-2">
                    <Check className="w-5 h-5 text-green-400" />
                    <span className="text-sm font-medium text-green-300">Package YAML created</span>
                  </div>
                </div>

                <div className="bg-gray-900 rounded-lg p-4 overflow-x-auto max-h-80">
                  <pre className="text-sm text-gray-100 font-mono whitespace-pre-wrap">{createdYaml}</pre>
                </div>

                <div className="flex justify-between">
                  <button
                    onClick={() => { navigator.clipboard.writeText(createdYaml); }}
                    className="btn btn-secondary text-sm"
                  >
                    Copy to Clipboard
                  </button>
                  <div className="flex gap-2">
                    <button onClick={downloadCreatedYaml} className="btn btn-secondary flex items-center text-sm">
                      <Download className="w-4 h-4 mr-2" />
                      Download
                    </button>
                    <button onClick={closeCreateModal} className="btn btn-primary text-sm">Done</button>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
