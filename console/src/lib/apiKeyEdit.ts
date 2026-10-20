// Editing an API key in place (PATCH /api-keys/{id}): who may, and the body
// to send. Pure functions (tested in tests/api-key-edit.test.mjs).
//
// The backend (app/api/v1/endpoints/api_keys.py) lets a caller holding
// sinas.api_keys.update:all edit any key, and one holding update:own edit
// their own. Only the name, the explicit permissions and the linked roles
// can change; a field left out of the body stays as it is, and a permission
// dict or role list that is sent REPLACES the previous one.

/** The editable part of a key, as listed. */
export interface EditableAPIKey {
  name: string;
  permissions: Record<string, boolean>;
  roles?: Array<{ id: string }>;
}

/** The dialog's state. */
export interface APIKeyDraft {
  name: string;
  permissions: Record<string, boolean>;
  role_ids: string[];
}

/** PATCH /api-keys/{id}. */
export interface APIKeyUpdate {
  name?: string;
  permissions?: Record<string, boolean>;
  role_ids?: string[];
}

/** The dialog's starting state: the key as it is. */
export function draftFromKey(key: EditableAPIKey): APIKeyDraft {
  return {
    name: key.name,
    permissions: { ...(key.permissions ?? {}) },
    role_ids: (key.roles ?? []).map((role) => role.id),
  };
}

function samePermissions(a: Record<string, boolean>, b: Record<string, boolean>): boolean {
  const aKeys = Object.keys(a);
  if (aKeys.length !== Object.keys(b).length) return false;
  return aKeys.every((perm) => Object.prototype.hasOwnProperty.call(b, perm) && b[perm] === a[perm]);
}

function sameIds(a: string[], b: string[]): boolean {
  const setA = new Set(a);
  const setB = new Set(b);
  return setA.size === setB.size && [...setA].every((id) => setB.has(id));
}

/**
 * The PATCH body: only what changed, so a field the editor could not show
 * (roles, when the caller may not list them) is never overwritten. Empty
 * when nothing changed. A blank name is left out (the API refuses it).
 */
export function apiKeyUpdatePayload(key: EditableAPIKey, draft: APIKeyDraft): APIKeyUpdate {
  const original = draftFromKey(key);
  const payload: APIKeyUpdate = {};
  const name = draft.name.trim();
  if (name && name !== original.name) payload.name = name;
  if (!samePermissions(original.permissions, draft.permissions)) {
    payload.permissions = { ...draft.permissions };
  }
  if (!sameIds(original.role_ids, draft.role_ids)) {
    payload.role_ids = [...new Set(draft.role_ids)];
  }
  return payload;
}

export function isEmptyUpdate(payload: APIKeyUpdate): boolean {
  return Object.keys(payload).length === 0;
}

/** What the caller holds, from /auth/check-permissions. */
export interface APIKeyUpdateAccess {
  userId: string | null | undefined;
  updateAll: boolean;
  updateOwn: boolean;
}

/**
 * Whether to offer Edit on a key: the same check the PATCH endpoint makes
 * (update:all for any key, update:own for the caller's own), and only on a
 * key still in use: a revoked key cannot be brought back, so there is
 * nothing to edit.
 */
export function canEditAPIKey(
  key: { user_id: string; is_active: boolean },
  access: APIKeyUpdateAccess,
): boolean {
  if (!key.is_active) return false;
  if (access.updateAll) return true;
  return access.updateOwn && !!access.userId && key.user_id === access.userId;
}

/** A role as the picker shows it. */
export interface PickerRole {
  id: string;
  name: string;
  description?: string | null;
}

/**
 * The roles to offer: those the caller can list, plus every role the key is
 * linked to that the list leaves out. GET /roles returns only the caller's
 * own memberships without roles.read:all, and at most one page, so a linked
 * role can be missing from it; it must still show, ticked, so it can be
 * unlinked.
 */
export function rolesForPicker(listed: PickerRole[], linked: PickerRole[] = []): PickerRole[] {
  const seen = new Set(listed.map((role) => role.id));
  const missing = linked.filter((role) => !seen.has(role.id));
  return [...listed, ...missing.map((role) => ({ id: role.id, name: role.name, description: null }))];
}
