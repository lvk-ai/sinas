import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Code2, Copy, Edit2, Fingerprint, Lock, Plus, ShieldAlert, Trash2, X } from 'lucide-react';
import { API_BASE_URL, apiClient, getApiErrorMessage } from '../lib/api';
import type { IdentityProvider, IdentityProviderCreate, Role, RoleMapping } from '../types';
import { claimMappingProblems, type ClaimMappingRow } from '../lib/attribution';

// As IdentityProviderSpec.name: it is in the redirect URI, so no renames.
const NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,99}$/;
const DEFAULT_SCOPES = 'openid email profile';

const isForbidden = (error: unknown): boolean => (error as any)?.response?.status === 403;

/** Space- or comma-separated input as a list. */
const splitList = (value: string): string[] => value.split(/[\s,]+/).map((v) => v.trim()).filter(Boolean);

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        await navigator.clipboard.writeText(value);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
      className="p-1 text-gray-500 hover:text-primary-400 rounded"
      title="Copy"
    >
      {copied ? <Check className="w-3.5 h-3.5 text-green-400" /> : <Copy className="w-3.5 h-3.5" />}
    </button>
  );
}

function describeRule(rule: RoleMapping): string {
  const when = rule.group != null ? `group "${rule.group}"` : `email @${rule.email_domain}`;
  return `${when} → ${rule.roles.join(', ')}`;
}

/** Admin → Single Sign-On: the OpenID Connect providers people can sign in with. */
export function IdentityProviders() {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<IdentityProvider | 'new' | null>(null);
  const { data: providers, isLoading, error } = useQuery({
    queryKey: ['identity-providers'],
    queryFn: () => apiClient.listIdentityProviders(),
    retry: (failureCount, err) => !isForbidden(err) && failureCount < 1,
  });
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['identity-providers'] });

  const toggleMutation = useMutation({
    mutationFn: (provider: IdentityProvider) =>
      apiClient.updateIdentityProvider(provider.name, { is_active: !provider.is_active }),
    onSettled: invalidate,
  });
  const deleteMutation = useMutation({
    mutationFn: (name: string) => apiClient.deleteIdentityProvider(name),
    onSettled: invalidate,
  });

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-gray-100">Single Sign-On</h1>
          <p className="text-gray-400 mt-1 max-w-3xl">
            Let people sign in with your identity provider (OpenID Connect: Keycloak, Auth0, WorkOS, Entra ID, Okta...).
            Their roles follow the groups they hold there, synced at every sign-in. Password and code sign-in keep
            working, and the superadmin can always use them.
          </p>
        </div>
        {!isForbidden(error) && (
          <button onClick={() => setEditing('new')} className="btn btn-primary flex items-center flex-shrink-0">
            <Plus className="w-4 h-4 mr-2" />
            Add provider
          </button>
        )}
      </div>

      {isLoading ? (
        <div className="text-center py-12">
          <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600"></div>
        </div>
      ) : isForbidden(error) ? (
        <div className="text-center py-12 card">
          <ShieldAlert className="w-12 h-12 text-gray-500 mx-auto mb-3" />
          <h3 className="text-lg font-medium text-gray-100 mb-1">Admins only</h3>
          <p className="text-gray-400">Identity providers are managed by administrators.</p>
        </div>
      ) : error ? (
        <div className="card text-red-400">{getApiErrorMessage(error, 'Loading identity providers failed')}</div>
      ) : providers && providers.length > 0 ? (
        <div className="space-y-3">
          {providers.map((provider) => (
            <div key={provider.id} className={`card ${provider.is_active ? '' : 'opacity-70'}`}>
              <div className="flex items-start gap-4">
                <Fingerprint className="w-8 h-8 text-primary-400 flex-shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0 space-y-1.5">
                  <div className="flex items-center gap-2 flex-wrap">
                    <h3 className="font-semibold text-gray-100">{provider.display_name}</h3>
                    <span className="text-xs font-mono text-gray-500">{provider.name}</span>
                    {provider.managed_by && (
                      <span
                        className="flex items-center gap-1 px-2 py-0.5 text-xs bg-blue-900/30 text-blue-400 rounded"
                        title={`Managed by ${provider.managed_by}. Changing it here detaches it from that config.`}
                      >
                        <Code2 className="w-3 h-3" />
                        config
                      </span>
                    )}
                    {provider.client_secret && (
                      <span className="flex items-center gap-1 px-2 py-0.5 text-xs bg-surface-2 text-gray-400 rounded" title="Client secret from this shared Secret">
                        <Lock className="w-3 h-3" />
                        {provider.client_secret}
                      </span>
                    )}
                    {!provider.is_active && <span className="px-2 py-0.5 text-xs bg-surface-2 text-gray-400 rounded">Disabled</span>}
                  </div>
                  <p className="text-xs font-mono text-gray-500 break-all">
                    {provider.issuer} · client {provider.client_id}
                  </p>
                  <div className="flex items-center gap-1 text-xs">
                    <span className="text-gray-400 whitespace-nowrap">Redirect URI:</span>
                    <span className="font-mono text-gray-300 break-all">{provider.redirect_uri}</span>
                    <CopyButton value={provider.redirect_uri} />
                  </div>
                  {provider.role_mappings.length > 0 && (
                    <ul className="text-xs text-gray-400 space-y-0.5">
                      {provider.role_mappings.map((rule, i) => <li key={i}>{describeRule(rule)}</li>)}
                    </ul>
                  )}
                  {(provider.claim_mappings ?? []).length > 0 && (
                    <ul className="text-xs text-gray-400 space-y-0.5">
                      {(provider.claim_mappings ?? []).map((m, i) => (
                        <li key={i}>
                          Claim <span className="font-mono">{m.claim}</span> → custom field{' '}
                          <span className="font-mono">{m.field}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500">
                    <span>{provider.auto_provision ? 'Creates new users' : 'Existing users only'}</span>
                    {provider.default_role && <span>New users get {provider.default_role}</span>}
                    <span>Sessions last {provider.session_hours}h</span>
                    {provider.allowed_domains.length > 0 && <span>Only {provider.allowed_domains.join(', ')}</span>}
                    {provider.enforce_for_domains.length > 0 && (
                      <span className="text-amber-400">Required for {provider.enforce_for_domains.join(', ')}</span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-1 flex-shrink-0">
                  <label className="flex items-center gap-2 mr-2 cursor-pointer text-xs text-gray-400">
                    <input
                      type="checkbox"
                      checked={provider.is_active}
                      disabled={toggleMutation.isPending && toggleMutation.variables?.name === provider.name}
                      onChange={() => toggleMutation.mutate(provider)}
                      className="w-4 h-4 text-primary-600 border-line rounded"
                    />
                    Enabled
                  </label>
                  <button
                    onClick={() => setEditing(provider)}
                    className="p-2 text-gray-500 hover:text-primary-400 hover:bg-hover-strong rounded-md transition-colors"
                    title="Edit"
                  >
                    <Edit2 className="w-4 h-4" />
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`Remove "${provider.display_name}"? Its users keep their accounts and roles, and sign in another way.`)) {
                        deleteMutation.mutate(provider.name);
                      }
                    }}
                    className="p-2 text-gray-500 hover:text-red-600 hover:bg-hover-strong rounded-md transition-colors"
                    title="Remove"
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
          <Fingerprint className="w-16 h-16 text-gray-500 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-100 mb-2">No identity providers</h3>
          <p className="text-gray-400">Add one to show a "Sign in with ..." button on the login page.</p>
        </div>
      )}

      {editing && (
        <ProviderModal
          provider={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            invalidate();
            setEditing(null);
          }}
        />
      )}
    </div>
  );
}

interface RuleRow {
  kind: 'group' | 'email_domain';
  value: string;
  roles: string[];
}

function toRows(rules: RoleMapping[]): RuleRow[] {
  return rules.map((r) => ({
    kind: r.group != null ? 'group' : 'email_domain',
    value: (r.group ?? r.email_domain) || '',
    roles: [...r.roles],
  }));
}

function ProviderModal({
  provider,
  onClose,
  onSaved,
}: {
  provider: IdentityProvider | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState(() => ({
    name: provider?.name ?? '',
    display_name: provider?.display_name ?? '',
    issuer: provider?.issuer ?? '',
    client_id: provider?.client_id ?? '',
    client_secret: provider?.client_secret ?? '',
    scopes: provider ? provider.scopes.join(' ') : DEFAULT_SCOPES,
    email_claim: provider?.email_claim ?? 'email',
    groups_claim: provider?.groups_claim ?? 'groups',
    allowed_domains: provider?.allowed_domains.join(', ') ?? '',
    enforce_for_domains: provider?.enforce_for_domains.join(', ') ?? '',
    auto_provision: provider?.auto_provision ?? false,
    default_role: provider?.default_role ?? '',
    session_hours: String(provider?.session_hours ?? 12),
    is_active: provider?.is_active ?? true,
  }));
  const [rules, setRules] = useState<RuleRow[]>(() => toRows(provider?.role_mappings ?? []));
  const [claims, setClaims] = useState<ClaimMappingRow[]>(() =>
    (provider?.claim_mappings ?? []).map((m) => ({ claim: m.claim, field: m.field })),
  );
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));

  const { data: secrets } = useQuery({ queryKey: ['secrets'], queryFn: () => apiClient.listSecrets(), retry: false });
  const { data: roles } = useQuery<Role[]>({ queryKey: ['roles'], queryFn: () => apiClient.listRoles(), retry: false });
  // The client secret resolves as a shared Secret.
  const sharedSecrets: { name: string }[] = (secrets || []).filter((s: { visibility?: string }) => !s.visibility || s.visibility === 'shared');
  const roleNames = (roles || []).map((r) => r.name).sort();

  const save = useMutation({
    mutationFn: async () => {
      const payload: Omit<IdentityProviderCreate, 'name'> = {
        display_name: form.display_name.trim(),
        issuer: form.issuer.trim(),
        client_id: form.client_id.trim(),
        client_secret: form.client_secret,  // "" clears it: a public client
        scopes: splitList(form.scopes),
        email_claim: form.email_claim.trim() || 'email',
        groups_claim: form.groups_claim.trim() || 'groups',
        // Every rule is complete by now (ruleProblems blocks saving otherwise).
        role_mappings: rules
          .map((r) => (r.kind === 'group' ? { group: r.value.trim(), roles: r.roles } : { email_domain: r.value.trim(), roles: r.roles })),
        // Complete too (claimProblems blocks saving otherwise).
        claim_mappings: claims.map((c) => ({ claim: c.claim.trim(), field: c.field.trim() })),
        allowed_domains: splitList(form.allowed_domains),
        enforce_for_domains: splitList(form.enforce_for_domains),
        auto_provision: form.auto_provision,
        default_role: form.default_role,
        session_hours: Number(form.session_hours),
        is_active: form.is_active,
      };
      if (provider) return apiClient.updateIdentityProvider(provider.name, payload);
      return apiClient.createIdentityProvider({
        ...payload,
        name: form.name,
        client_secret: form.client_secret || null,
        default_role: form.default_role || null,
      });
    },
    onSuccess: onSaved,
  });

  const nameValid = NAME_PATTERN.test(form.name);
  const hours = Number(form.session_hours);
  // A rule left half-filled is pointed out, never dropped on save.
  const ruleProblems = rules.map((r) => {
    if (!r.value.trim()) return r.kind === 'group' ? 'Enter the group.' : 'Enter the email domain.';
    if (r.roles.length === 0) return 'Pick at least one role.';
    return null;
  });
  const rulesComplete = ruleProblems.every((p) => p === null);
  const claimProblems = claimMappingProblems(claims);
  const claimsComplete = claimProblems.every((p) => p === null);
  const canSubmit =
    rulesComplete && claimsComplete &&
    nameValid && form.display_name.trim() !== '' && form.issuer.trim() !== '' && form.client_id.trim() !== '' &&
    Number.isInteger(hours) && hours >= 1 && !save.isPending;
  const redirectUri = provider?.redirect_uri ?? `${API_BASE_URL}/auth/oidc/${form.name || '<name>'}/callback`;

  const updateRule = (i: number, patch: Partial<RuleRow>) =>
    setRules((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <div className="fixed inset-0 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-surface-1 rounded-lg max-w-2xl w-full p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold text-gray-100">{provider ? `Edit ${provider.display_name}` : 'Add Identity Provider'}</h2>
          <button onClick={onClose} className="p-1 text-gray-500 hover:text-gray-400">
            <X className="w-5 h-5" />
          </button>
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (canSubmit) save.mutate();
          }}
          className="space-y-4"
        >
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Name *</label>
              <input
                value={form.name}
                onChange={(e) => set({ name: e.target.value })}
                disabled={!!provider}
                placeholder="keycloak"
                className="input w-full font-mono disabled:opacity-60"
                autoFocus={!provider}
              />
              {form.name && !nameValid && (
                <p className="text-xs text-red-400 mt-1">Lowercase letters, digits, dashes and underscores.</p>
              )}
            </div>
            <div>
              <label className="label">Button label *</label>
              <input
                value={form.display_name}
                onChange={(e) => set({ display_name: e.target.value })}
                placeholder="Acme SSO"
                className="input w-full"
              />
              <p className="text-xs text-gray-500 mt-1">Shown as "Sign in with ..."</p>
            </div>
          </div>

          <div className="p-3 bg-surface-2 rounded text-xs space-y-1">
            <div className="text-gray-400">Register this redirect URI at the identity provider:</div>
            <div className="flex items-center gap-1">
              <span className="font-mono text-gray-200 break-all">{redirectUri}</span>
              {provider && <CopyButton value={redirectUri} />}
            </div>
            {!provider && <div className="text-gray-500">The exact value is shown on the provider once it's saved.</div>}
          </div>

          <div>
            <label className="label">Issuer URL *</label>
            <input
              value={form.issuer}
              onChange={(e) => set({ issuer: e.target.value })}
              placeholder="https://auth.example.com/realms/acme"
              className="input w-full font-mono"
            />
            <p className="text-xs text-gray-500 mt-1">
              Sinas reads <span className="font-mono">/.well-known/openid-configuration</span> under it.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Client ID *</label>
              <input value={form.client_id} onChange={(e) => set({ client_id: e.target.value })} className="input w-full font-mono" />
            </div>
            <div>
              <label className="label">Client secret</label>
              <select value={form.client_secret} onChange={(e) => set({ client_secret: e.target.value })} className="input w-full">
                <option value="">None (public client)</option>
                {form.client_secret && !sharedSecrets.some((s) => s.name === form.client_secret) && (
                  <option value={form.client_secret}>{form.client_secret}</option>
                )}
                {sharedSecrets.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
              </select>
              <p className="text-xs text-gray-500 mt-1">A shared Secret; add one under Secrets.</p>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="label">Scopes</label>
              <input value={form.scopes} onChange={(e) => set({ scopes: e.target.value })} className="input w-full font-mono" />
            </div>
            <div>
              <label className="label">Email claim</label>
              <input value={form.email_claim} onChange={(e) => set({ email_claim: e.target.value })} className="input w-full font-mono" />
            </div>
            <div>
              <label className="label">Groups claim</label>
              <input
                value={form.groups_claim}
                onChange={(e) => set({ groups_claim: e.target.value })}
                placeholder="groups"
                className="input w-full font-mono"
              />
            </div>
          </div>
          <p className="text-xs text-gray-500 -mt-2">
            Claims are paths: <span className="font-mono">realm_access.roles</span> (Keycloak realm roles), or a namespaced
            claim such as <span className="font-mono">https://example.com/roles</span> (Auth0).
          </p>

          <div className="border-t border-line-soft pt-4">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-medium text-gray-300">Role mappings</h3>
              <button
                type="button"
                onClick={() => setRules((rs) => [...rs, { kind: 'group', value: '', roles: [] }])}
                className="btn btn-secondary text-xs flex items-center"
              >
                <Plus className="w-3 h-3 mr-1" />
                Add rule
              </button>
            </div>
            <p className="text-xs text-gray-500 mb-3">
              At each sign-in the roles a rule grants are added, and roles this provider granted before that no
              longer map are removed. Roles given by hand are never touched.
            </p>
            <div className="space-y-2">
              {rules.map((rule, i) => (
                <div key={i} className="p-3 bg-surface-2 rounded space-y-2">
                  {/* .input is width: 100%; the wrappers size it. */}
                  <div className="flex items-center gap-2">
                    <div className="w-44 flex-shrink-0">
                      <select
                        value={rule.kind}
                        onChange={(e) => updateRule(i, { kind: e.target.value as RuleRow['kind'] })}
                        className="input"
                      >
                        <option value="group">Group is</option>
                        <option value="email_domain">Email domain is</option>
                      </select>
                    </div>
                    <div className="flex-1 min-w-0">
                      <input
                        value={rule.value}
                        onChange={(e) => updateRule(i, { value: e.target.value })}
                        placeholder={rule.kind === 'group' ? 'sinas-admins' : 'example.com'}
                        className="input font-mono"
                      />
                    </div>
                    <button
                      type="button"
                      onClick={() => setRules((rs) => rs.filter((_, j) => j !== i))}
                      className="p-2 text-gray-500 hover:text-red-600"
                      title="Remove rule"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="text-xs text-gray-500 mr-1">grants</span>
                    {rule.roles.map((role) => (
                      <span key={role} className="flex items-center gap-1 px-2 py-1 text-xs bg-surface-1 text-gray-300 rounded">
                        {role}
                        <button
                          type="button"
                          onClick={() => updateRule(i, { roles: rule.roles.filter((r) => r !== role) })}
                          className="text-gray-500 hover:text-red-400"
                        >
                          <X className="w-3 h-3" />
                        </button>
                      </span>
                    ))}
                    <div className="w-36">
                      <select
                        value=""
                        onChange={(e) => e.target.value && updateRule(i, { roles: [...rule.roles, e.target.value] })}
                        className="input text-xs py-1"
                      >
                        <option value="">+ role</option>
                        {roleNames.filter((r) => !rule.roles.includes(r)).map((r) => <option key={r} value={r}>{r}</option>)}
                      </select>
                    </div>
                  </div>
                  {ruleProblems[i] && <p className="text-xs text-red-400">{ruleProblems[i]}</p>}
                </div>
              ))}
            </div>
          </div>

          <div className="border-t border-line-soft pt-4">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-medium text-gray-300">Claim mappings</h3>
              <button
                type="button"
                onClick={() => setClaims((cs) => [...cs, { claim: '', field: '' }])}
                className="btn btn-secondary text-xs flex items-center"
              >
                <Plus className="w-3 h-3 mr-1" />
                Add mapping
              </button>
            </div>
            <p className="text-xs text-gray-500 mb-3">
              At each sign-in a claim is copied into the user's custom field, and the field is removed when the claim
              is gone. Fields no mapping names are never touched. E.g. <span className="font-mono">org_id</span> →{' '}
              <span className="font-mono">orgId</span>, for attribution and usage per customer.
            </p>
            <div className="space-y-2">
              {claims.map((c, i) => (
                <div key={i} className="p-3 bg-surface-2 rounded space-y-1">
                  <div className="flex items-center gap-2">
                    <div className="flex-1 min-w-0">
                      <input
                        value={c.claim}
                        onChange={(e) => setClaims((cs) => cs.map((x, j) => (j === i ? { ...x, claim: e.target.value } : x)))}
                        placeholder="org_id"
                        className="input font-mono"
                        aria-label="Claim"
                      />
                    </div>
                    <span className="text-xs text-gray-500">→</span>
                    <div className="w-48 flex-shrink-0">
                      <input
                        value={c.field}
                        onChange={(e) => setClaims((cs) => cs.map((x, j) => (j === i ? { ...x, field: e.target.value } : x)))}
                        placeholder="orgId"
                        className="input font-mono"
                        aria-label="Custom field"
                      />
                    </div>
                    <button
                      type="button"
                      onClick={() => setClaims((cs) => cs.filter((_, j) => j !== i))}
                      className="p-2 text-gray-500 hover:text-red-600"
                      title="Remove mapping"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                  {claimProblems[i] && <p className="text-xs text-red-400">{claimProblems[i]}</p>}
                </div>
              ))}
            </div>
          </div>

          <div className="border-t border-line-soft pt-4 space-y-4">
            <h3 className="text-sm font-medium text-gray-300">Who may sign in</h3>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="label">Allowed email domains</label>
                <input
                  value={form.allowed_domains}
                  onChange={(e) => set({ allowed_domains: e.target.value })}
                  placeholder="any"
                  className="input w-full font-mono"
                />
              </div>
              <div>
                <label className="label">Required for domains</label>
                <input
                  value={form.enforce_for_domains}
                  onChange={(e) => set({ enforce_for_domains: e.target.value })}
                  placeholder="none"
                  className="input w-full font-mono"
                />
                <p className="text-xs text-gray-500 mt-1">These can't use password or code sign-in (the superadmin always can).</p>
              </div>
            </div>
            <div className="grid grid-cols-3 gap-4 items-end">
              <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer pb-2">
                <input
                  type="checkbox"
                  checked={form.auto_provision}
                  onChange={(e) => set({ auto_provision: e.target.checked })}
                  className="w-4 h-4 text-primary-600 border-line rounded"
                />
                Create unknown users
              </label>
              <div>
                <label className="label">Default role for new users</label>
                <select value={form.default_role} onChange={(e) => set({ default_role: e.target.value })} className="input w-full">
                  <option value="">None</option>
                  {roleNames.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </div>
              <div>
                <label className="label">Session length (hours)</label>
                <input
                  type="number"
                  min={1}
                  value={form.session_hours}
                  onChange={(e) => set({ session_hours: e.target.value })}
                  className="input w-full"
                />
              </div>
            </div>
            <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
              <input
                type="checkbox"
                checked={form.is_active}
                onChange={(e) => set({ is_active: e.target.checked })}
                className="w-4 h-4 text-primary-600 border-line rounded"
              />
              Enabled
            </label>
          </div>

          {!rulesComplete && (
            <p className="text-sm text-red-400">Complete or remove the role rules marked above to save.</p>
          )}
          {!claimsComplete && (
            <p className="text-sm text-red-400">Complete or remove the claim mappings marked above to save.</p>
          )}

          {save.isError && (
            <div className="p-3 bg-red-900/20 border border-red-900/30 rounded text-sm text-red-400 whitespace-pre-wrap">
              {getApiErrorMessage(save.error, 'Saving failed')}
            </div>
          )}

          <div className="flex justify-end gap-3">
            <button type="button" onClick={onClose} className="btn btn-secondary">Cancel</button>
            <button type="submit" disabled={!canSubmit} className="btn btn-primary">
              {save.isPending ? 'Saving...' : provider ? 'Save' : 'Add Provider'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
