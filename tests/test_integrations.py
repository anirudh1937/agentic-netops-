"""Tests for the /integrations endpoint used by the dashboard overview.

The endpoint aggregates per-vendor connectivity + inventory plus backend
(agent/MCP) status. Tests inject fake connectors to exercise each branch
without touching a live device and without leaking credentials.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.inventory import InterfaceInfo


client = TestClient(app)

# Sandbox host/credential variable names read by each vendor connector.
_SANDBOX_KEYS = tuple(
    f"{vendor}_SANDBOX_{name}"
    for vendor in ("CISCO", "JUNIPER", "ARISTA")
    for name in ("HOST", "USERNAME", "PASSWORD")
)


class _FakeInventory:
    def __init__(self, hostname: str = "core-1"):
        self.hostname = hostname
        self.interfaces = [
            InterfaceInfo(name="ge-0/0/0", status="up"),
            InterfaceInfo(name="ge-0/0/1", status="up"),
        ]


class _FakeConnector:
    """Minimal SandboxConnector stand-in used to exercise each branch."""

    def __init__(self, host="https://fake.device", reachable=True, hostname="core-1"):
        self._device = type("Device", (), {"host": host})()
        self._reachable = reachable
        self._hostname = hostname

    @property
    def host(self) -> str:
        return self._device.host

    def connectivity_check(self) -> bool:
        if not self._reachable:
            raise RuntimeError("Device connectivity check failed.")
        return True

    def fetch_inventory(self) -> _FakeInventory:
        inv = _FakeInventory(hostname=self._hostname)
        return inv


def _patch_make_connector(monkeypatch, connector):
    # The /integrations endpoint now resolves the connector from the current
    # process environment, so the stand-in must accept (and ignore) the
    # ``environ`` argument while still returning the fake connector. The
    # endpoint's branch behaviour is unchanged.
    monkeypatch.setattr(
        "app.main.make_connector",
        lambda vendor, environ=None: connector,
    )


def _clear_sandbox_env(monkeypatch):
    for var in _SANDBOX_KEYS:
        monkeypatch.delenv(var, raising=False)


def test_integrations_reports_all_vendors_configured_false(monkeypatch):
    # Nothing configured: every vendor is reported as not configured with an
    # honest error, never a false healthy status.
    _clear_sandbox_env(monkeypatch)

    response = client.get("/integrations")
    body = response.json()

    assert response.status_code == 200
    vendors = {v["vendor"]: v for v in body["vendors"]}
    assert set(vendors) == {"cisco", "juniper", "arista"}
    for entry in vendors.values():
        assert entry["configured"] is False
        assert entry["reachable"] is None
        assert entry["hostname"] is None
        assert entry["error"]  # honest error message surfaced
        # Credentials must never appear in the error body.
        assert "password" not in entry["error"].lower()


def test_integrations_backend_and_backends_ok():
    body = client.get("/integrations").json()

    assert body["backend"]["status"] == "ok"
    assert body["backend"]["service"] == "agentic-netops"
    assert body["generated_at"]
    assert body["mcp"]["available"] is True


def test_integrations_configurable_vendor_returns_inventory(monkeypatch):
    connector = _FakeConnector(reachable=True, hostname="core-1")
    _patch_make_connector(monkeypatch, connector)

    body = client.get("/integrations").json()
    cisco = next(v for v in body["vendors"] if v["vendor"] == "cisco")

    assert cisco["configured"] is True
    assert cisco["reachable"] is True
    assert cisco["hostname"] == "core-1"
    assert cisco["host"] == "https://fake.device"
    assert cisco["interface_count"] == 2
    assert cisco["interfaces"] == ["ge-0/0/0", "ge-0/0/1"]
    assert cisco["error"] is None
    assert cisco["last_checked"]


def test_integrations_unreachable_vendor_reports_error(monkeypatch):
    connector = _FakeConnector(reachable=False)
    _patch_make_connector(monkeypatch, connector)

    body = client.get("/integrations").json()
    cisco = next(v for v in body["vendors"] if v["vendor"] == "cisco")

    assert cisco["configured"] is True
    assert cisco["reachable"] is False
    assert "failed" in (cisco["error"] or "").lower()
    assert "core-1" not in (cisco["hostname"] or "")


def test_integrations_inventory_read_failure(monkeypatch):
    class _BoomConnector(_FakeConnector):
        def fetch_inventory(self):
            raise RuntimeError("device inventory could not be read")

    _patch_make_connector(monkeypatch, _BoomConnector())

    body = client.get("/integrations").json()
    cisco = next(v for v in body["vendors"] if v["vendor"] == "cisco")

    assert cisco["reachable"] is True
    assert cisco["hostname"] is None
    assert "inventory" in (cisco["error"] or "").lower()


def test_integrations_agent_reflects_config(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    body = client.get("/integrations").json()
    assert body["agent"]["configured"] is False
    assert body["agent"]["reachable"] is None

    monkeypatch.setenv("GEMINI_API_KEY", "test-key-do-not-leak")
    body = client.get("/integrations").json()
    assert body["agent"]["configured"] is True
    assert body["agent"]["reachable"] is None