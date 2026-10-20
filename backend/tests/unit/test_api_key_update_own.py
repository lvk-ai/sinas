"""Users can edit their own API keys (#72): the default role grants it, and the
registry lists the action so the role editor can grant it too."""
from app.core.permission_registry import PERMISSION_REGISTRY
from app.core.permissions import DEFAULT_ROLE_PERMISSIONS, check_permission


def test_users_role_may_update_own_api_keys():
    users = DEFAULT_ROLE_PERMISSIONS["Users"]
    assert check_permission(users, "sinas.api_keys.update:own")
    # Own keys only: another user's key still needs update:all
    assert not check_permission(users, "sinas.api_keys.update:all")


def test_registry_lists_update_for_api_keys():
    entry = next(e for e in PERMISSION_REGISTRY if e["resource"] == "api_keys")
    assert entry["actions"] == ["create", "read", "update", "delete", "assign_context"]
