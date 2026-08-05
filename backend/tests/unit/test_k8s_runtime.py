"""Unit tests for the k8s sandbox runtime's pure pieces.

Pod manifest hardening and quantity conversion are contract-bearing (they are
the k8s translation of the Docker sandbox hardening in
`_ephemeral_runtime._run_config`); the API-touching lifecycle is exercised
against a real cluster in integration testing.
"""

import pytest

from app.services.executor._k8s_runtime import _k8s_quantity, _pod_manifest


def test_quantity_conversion():
    assert _k8s_quantity("1g") == "1Gi"
    assert _k8s_quantity("500m") == "500Mi"
    assert _k8s_quantity("64k") == "64Ki"
    assert _k8s_quantity("2Gi") == "2Gi"  # already a k8s quantity: passthrough


def test_pod_manifest_hardening():
    m = _pod_manifest("sinas-sbx-e1", "executor:latest", deadline_seconds=600)
    spec = m["spec"]
    assert spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False
    assert spec["activeDeadlineSeconds"] == 600

    c = spec["containers"][0]
    sc = c["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert set(sc["capabilities"]["add"]) == {"CHOWN", "SETUID", "SETGID"}
    assert sc["seccompProfile"] == {"type": "RuntimeDefault"}

    # Sandbox mode must be set — it is what hard-disables input() in the
    # in-container executor.
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env["SINAS_CONTAINER_MODE"] == "sandbox"

    # tmpfs /tmp parity with the Docker sandbox.
    assert spec["volumes"][0]["emptyDir"]["medium"] == "Memory"
    assert c["volumeMounts"][0]["mountPath"] == "/tmp"

    # Resource ceilings present.
    limits = c["resources"]["limits"]
    assert "memory" in limits and "cpu" in limits and "ephemeral-storage" in limits

    # Selectable by the chart's sandbox NetworkPolicy.
    assert m["metadata"]["labels"]["sinas.type"] == "sandbox-executor"


def test_pod_manifest_no_service_account_by_default():
    m = _pod_manifest("n", "img", deadline_seconds=1)
    assert "serviceAccountName" not in m["spec"]
