import type { Role } from '../types';

interface APIKeyRolePickerProps {
  roles: Role[];
  value: string[];
  onChange: (roleIds: string[]) => void;
  /** Whose live permissions cap the key: the creator's own, or the key owner's. */
  capHint: string;
}

/** The roles an API key is linked to (create and edit dialogs). */
export function APIKeyRolePicker({ roles, value, onChange, capHint }: APIKeyRolePickerProps) {
  return (
    <div>
      <label className="block text-sm font-medium text-gray-300 mb-2">
        Linked Roles (Optional)
      </label>
      <div className="max-h-40 overflow-y-auto border border-line rounded-lg divide-y divide-line-soft">
        {roles.map((role) => (
          <label
            key={role.id}
            className="flex items-center gap-2 px-3 py-2 text-sm text-gray-300 cursor-pointer hover:bg-surface-0"
          >
            <input
              type="checkbox"
              checked={value.includes(role.id)}
              onChange={(e) =>
                onChange(
                  e.target.checked ? [...value, role.id] : value.filter((id) => id !== role.id)
                )
              }
              className="rounded border-line text-primary-600 focus:ring-primary-500"
            />
            <span>{role.name}</span>
            {role.description && (
              <span className="text-xs text-gray-500 truncate">{role.description}</span>
            )}
          </label>
        ))}
      </div>
      <p className="text-xs text-gray-500 mt-1">
        The key follows linked roles as they change (no re-minting), always capped by {capHint}{' '}
        live permissions.
      </p>
    </div>
  );
}
