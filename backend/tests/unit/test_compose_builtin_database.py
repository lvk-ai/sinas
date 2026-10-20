"""BUILTIN_DATABASE_ENABLED=false must stop compose creating sinas_data (#192).

The postgres init script creates it on a fresh volume and the scheduler on
every boot; both must see the same resolved value (shell or .env), or one of
them creates it anyway.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(not (REPO / "docker-compose.yml").is_file(), reason="deployment files not in this checkout")
def test_every_creator_of_sinas_data_gets_the_flag():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    services = compose["services"]
    for name in ("postgres", "scheduler", "backend"):
        assert services[name]["environment"]["BUILTIN_DATABASE_ENABLED"] == "${BUILTIN_DATABASE_ENABLED:-true}", name
    mounts = services["postgres"]["volumes"]
    assert any(m.startswith("./backend/init-postgres.sh:/docker-entrypoint-initdb.d/") for m in mounts)
    script = (REPO / "backend/init-postgres.sh").read_text()
    assert "BUILTIN_DATABASE_ENABLED" in script and "CREATE DATABASE sinas_data" in script
