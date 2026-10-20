import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import { useState } from 'react';
import { apiClient } from '../lib/api';
import {
  apiKeyUpdatePayload,
  draftFromKey,
  isEmptyUpdate,
  rolesForPicker,
  type APIKeyDraft,
} from '../lib/apiKeyEdit';
import { useToast } from '../lib/toast-context';
import type { APIKey } from '../types';
import { APIKeyRolePicker } from './APIKeyRolePicker';
import { PermissionEditor } from './PermissionEditor';

// A 422 carries a list of validation errors, not a message: show those generically.
function errorDetail(error: unknown): string | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string' ? detail : null;
}

interface EditAPIKeyModalProps {
  apiKey: APIKey;
  /** Whether the key belongs to the signed-in user (else an admin edits another's). */
  isOwnKey: boolean;
  onClose: () => void;
}

/**
 * Edit an API key's name, explicit permissions and linked roles in place
 * (PATCH /api-keys/{id}); the key value stays the same. Only what changed is
 * sent, so roles the caller may not list are left untouched.
 */
export function EditAPIKeyModal({ apiKey, isOwnKey, onClose }: EditAPIKeyModalProps) {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [draft, setDraft] = useState<APIKeyDraft>(() => draftFromKey(apiKey));

  // Non-admins may lack roles.read: the linked roles are then shown, not edited.
  const { data: listedRoles } = useQuery({
    queryKey: ['roles'],
    queryFn: () => apiClient.listRoles(),
    retry: false,
  });

  // Linked roles the list leaves out (it holds only the caller's own roles
  // without roles.read:all) are still offered, so they can be unlinked.
  const roles = listedRoles ? rolesForPicker(listedRoles, apiKey.roles) : undefined;
  const payload = apiKeyUpdatePayload(apiKey, draft);
  const capHint = isOwnKey ? 'your own' : "the owner's";

  const updateMutation = useMutation({
    mutationFn: () => apiClient.updateAPIKey(apiKey.id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apiKeys'] });
      showToast('API key updated', 'success');
      onClose();
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (draft.name.trim() && !isEmptyUpdate(payload)) updateMutation.mutate();
  };

  return (
    <>
      <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50" onClick={onClose} />
      <div className="fixed inset-0 flex items-center justify-center z-50 p-4 pointer-events-none">
        <div className="bg-surface-1 rounded-lg max-w-2xl w-full max-h-[90vh] overflow-y-auto pointer-events-auto">
          <div className="sticky top-0 bg-surface-1 border-b border-line-soft p-6">
            <div className="flex items-center justify-between">
              <div className="min-w-0">
                <h2 className="text-xl font-semibold text-gray-100">Edit API Key</h2>
                <p className="text-sm text-gray-400 font-mono mt-1 truncate">
                  {apiKey.key_prefix}***
                  {!isOwnKey && apiKey.user_email && (
                    <span className="font-sans text-gray-500"> ({apiKey.user_email})</span>
                  )}
                </p>
              </div>
              <button onClick={onClose} className="text-gray-500 hover:text-gray-400" aria-label="Close">
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>

          <form onSubmit={handleSubmit} className="p-6 space-y-6">
            <div className="rounded-lg border border-line-soft bg-surface-0 px-4 py-3 text-sm text-gray-300">
              The key's effective permissions are its explicit permissions plus those of its linked
              roles, always capped by {capHint} live permissions. Changes apply on the key's next
              request; the key value stays the same.
            </div>

            {apiKey.context && Object.keys(apiKey.context).length > 0 && (
              <div>
                <span className="block text-sm font-medium text-gray-300 mb-2">Attribution context</span>
                <div className="flex flex-wrap gap-1">
                  {Object.entries(apiKey.context).map(([field, value]) => (
                    <span key={field} className="px-2 py-0.5 bg-surface-2 text-gray-300 text-xs rounded font-mono">
                      {field}={String(value)}
                    </span>
                  ))}
                </div>
                <p className="text-xs text-gray-500 mt-1">
                  Taken from the session that created the key, and fixed: it can't be edited here.
                </p>
              </div>
            )}

            <div>
              <label htmlFor="edit-key-name" className="block text-sm font-medium text-gray-300 mb-2">
                Key Name *
              </label>
              <input
                id="edit-key-name"
                type="text"
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                required
                className="input"
                autoFocus
              />
              <p className="text-xs text-gray-500 mt-1">
                {apiKey.expires_at
                  ? `Expires ${new Date(apiKey.expires_at).toLocaleDateString('en-GB')}. `
                  : 'No expiry. '}
                The expiry can't be changed; create a new key for a different one.
              </p>
            </div>

            {roles && roles.length > 0 ? (
              <APIKeyRolePicker
                roles={roles}
                value={draft.role_ids}
                onChange={(role_ids) => setDraft({ ...draft, role_ids })}
                capHint={capHint}
              />
            ) : (
              apiKey.roles &&
              apiKey.roles.length > 0 && (
                <div>
                  <label className="block text-sm font-medium text-gray-300 mb-2">Linked Roles</label>
                  <div className="flex flex-wrap gap-1">
                    {apiKey.roles.map((role) => (
                      <span key={role.id} className="px-2 py-0.5 bg-purple-900/30 text-purple-300 text-xs rounded">
                        {role.name}
                      </span>
                    ))}
                  </div>
                  <p className="text-xs text-gray-500 mt-1">
                    Changing linked roles needs permission to list roles.
                  </p>
                </div>
              )
            )}

            <PermissionEditor
              mode="dict"
              label="Permissions"
              value={draft.permissions}
              onChange={(permissions) => setDraft({ ...draft, permissions })}
            />

            {updateMutation.isError && (
              <div className="p-3 bg-red-900/20 border border-red-800/30 rounded-lg text-sm text-red-400">
                {errorDetail(updateMutation.error) || 'Failed to update API key. Please try again.'}
              </div>
            )}

            <div className="flex justify-end space-x-3 pt-4 border-t border-line-soft">
              <button
                type="button"
                onClick={onClose}
                className="btn btn-secondary"
                disabled={updateMutation.isPending}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={updateMutation.isPending || !draft.name.trim() || isEmptyUpdate(payload)}
              >
                {updateMutation.isPending ? 'Saving...' : 'Save Changes'}
              </button>
            </div>
          </form>
        </div>
      </div>
    </>
  );
}
