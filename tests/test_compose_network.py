"""Offline checks of the resolved Compose model; never starts containers."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = "compose.tailscale.yaml"
# Stand-in for the untracked production override, which attaches reports to `sixto`.
EXISTING_OVERRIDE = (
    "services:\n  notion-reports:\n    networks: [sixto]\n"
    "networks:\n  sixto:\n    external: true\n"
)


def model(tmp_path, *files, key_file=True):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is required")
    for name in ("compose.yaml", OVERLAY):
        shutil.copy(ROOT / name, tmp_path / name)
    (tmp_path / "compose.override.yaml").write_text(EXISTING_OVERRIDE)
    (tmp_path / ".env").write_text("NOTION_TOKEN=app-placeholder\n")
    if key_file:
        (tmp_path / ".env.tailscale").write_text("TS_AUTHKEY=test-only-placeholder\n")
    cmd = ["docker", "compose"]
    for name in files:
        cmd += ["-f", name]
    cmd += ["config", "--format", "json"]
    env = {k: v for k, v in os.environ.items() if k not in {"TS_AUTHKEY", "COMPOSE_FILE"}}
    result = subprocess.run(cmd, cwd=tmp_path, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_default_deploy_is_unchanged_and_keeps_existing_override(tmp_path):
    # Plain `docker compose up` (compose.yaml + compose.override.yaml) must keep working.
    config = model(tmp_path, "compose.yaml", "compose.override.yaml")
    app = config["services"]["notion-reports"]
    assert "tailscale" not in config["services"]
    assert not app.get("network_mode")
    assert "sixto" in app["networks"]
    assert "tailscale_state" not in config.get("volumes", {})


def test_overlay_adds_tagged_persistent_sidecar(tmp_path):
    config = model(tmp_path, "compose.yaml", OVERLAY)
    app = config["services"]["notion-reports"]
    sidecar = config["services"]["tailscale"]
    assert app["network_mode"] == "service:tailscale"
    assert app["depends_on"]["tailscale"]["condition"] == "service_started"
    assert app["depends_on"]["tailscale"]["restart"] is True
    assert sidecar["image"].startswith("tailscale/tailscale:") and "@sha256:" in sidecar["image"]
    assert sidecar["environment"]["TS_EXTRA_ARGS"] == "--advertise-tags=tag:t6-reports"
    assert sidecar["environment"]["TS_USERSPACE"] == "false"
    assert sidecar["environment"]["TS_HOSTNAME"] == "t6-reports"
    assert sidecar["environment"]["TS_STATE_DIR"] == "/var/lib/tailscale"
    assert sidecar["restart"] == "unless-stopped"
    assert "NET_ADMIN" in sidecar["cap_add"]
    assert any(device["source"] == "/dev/net/tun" and device["target"] == "/dev/net/tun" for device in sidecar["devices"])
    assert any(v["target"] == "/var/lib/tailscale" and v["type"] == "volume" for v in sidecar["volumes"])
    assert "ports" not in sidecar and "ports" not in app


def test_overlay_moves_sixto_network_from_app_to_sidecar(tmp_path):
    # The overlay goes last, after the existing override, and must merge with it.
    config = model(tmp_path, "compose.yaml", "compose.override.yaml", OVERLAY)
    app = config["services"]["notion-reports"]
    sidecar = config["services"]["tailscale"]
    assert app["network_mode"] == "service:tailscale"
    assert not app.get("networks")
    assert "notion-reports" in sidecar["networks"]["sixto"]["aliases"]
    assert config["networks"]["sixto"]["external"] is True
    assert config["networks"]["sixto"]["name"] == "sixto"


def test_enrollment_key_is_sidecar_only_and_optional_after_enrollment(tmp_path):
    config = model(tmp_path, "compose.yaml", OVERLAY)
    assert config["services"]["tailscale"]["environment"]["TS_AUTHKEY"] == "test-only-placeholder"
    assert "TS_AUTHKEY" not in config["services"]["notion-reports"]["environment"]
    (tmp_path / ".env.tailscale").unlink()
    after = model(tmp_path, "compose.yaml", OVERLAY, key_file=False)
    assert "TS_AUTHKEY" not in after["services"]["tailscale"]["environment"]


def test_sidecar_secret_excluded_from_docker_build_context():
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env.*" in patterns or ".env.tailscale" in patterns
