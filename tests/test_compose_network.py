"""Offline checks of the resolved Compose model; never starts containers."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def model(tmp_path, *files):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is required")
    for name in ("compose.yaml", *files):
        shutil.copy(ROOT / name, tmp_path / name)
    (tmp_path / ".env").write_text("NOTION_TOKEN=app-placeholder\n")
    (tmp_path / ".env.tailscale").write_text("TS_AUTHKEY=test-only-placeholder\n")
    cmd = ["docker", "compose"]
    for name in ("compose.yaml", *files):
        cmd += ["-f", name]
    cmd += ["config", "--format", "json"]
    env = dict(os.environ, TS_AUTHKEY="test-only-placeholder")
    result = subprocess.run(cmd, cwd=tmp_path, env=env, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_reports_uses_started_tagged_persistent_sidecar(tmp_path):
    config = model(tmp_path)
    app = config["services"]["notion-reports"]
    sidecar = config["services"]["tailscale"]
    assert app["network_mode"] == "service:tailscale"
    assert app["depends_on"]["tailscale"]["condition"] == "service_started"
    assert app["depends_on"]["tailscale"]["restart"] is True
    assert sidecar["image"].startswith("tailscale/tailscale:") and "@sha256:" in sidecar["image"]
    assert sidecar["environment"]["TS_EXTRA_ARGS"] == "--advertise-tags=tag:t6-reports"
    assert sidecar["environment"]["TS_AUTHKEY"] == "test-only-placeholder"
    assert sidecar["environment"]["TS_USERSPACE"] == "false"
    assert sidecar["environment"]["TS_HOSTNAME"] == "t6-reports"
    assert sidecar["environment"]["TS_STATE_DIR"] == "/var/lib/tailscale"
    assert sidecar["restart"] == "unless-stopped"
    assert "NET_ADMIN" in sidecar["cap_add"]
    assert any(device["source"] == "/dev/net/tun" and device["target"] == "/dev/net/tun" for device in sidecar["devices"])
    assert any(v["target"] == "/var/lib/tailscale" and v["type"] == "volume" for v in sidecar["volumes"])
    assert "ports" not in sidecar and "ports" not in app


def test_enrollment_key_is_sidecar_only_and_optional_after_enrollment(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is required")
    shutil.copy(ROOT / "compose.yaml", tmp_path / "compose.yaml")
    (tmp_path / ".env").write_text("NOTION_TOKEN=app-placeholder\n")
    key_file = tmp_path / ".env.tailscale"
    key_file.write_text("TS_AUTHKEY=test-only-placeholder\n")
    env = {k: v for k, v in os.environ.items() if k != "TS_AUTHKEY"}
    cmd = ["docker", "compose", "config", "--format", "json"]
    enrolled = subprocess.run(cmd, cwd=tmp_path, env=env, text=True, capture_output=True)
    assert enrolled.returncode == 0, enrolled.stderr
    config = json.loads(enrolled.stdout)
    assert config["services"]["tailscale"]["environment"]["TS_AUTHKEY"] == "test-only-placeholder"
    assert "TS_AUTHKEY" not in config["services"]["notion-reports"]["environment"]
    key_file.unlink()
    after = subprocess.run(cmd, cwd=tmp_path, env=env, text=True, capture_output=True)
    assert after.returncode == 0, after.stderr
    assert "TS_AUTHKEY" not in json.loads(after.stdout)["services"]["tailscale"]["environment"]


def test_external_sixto_network_is_attached_to_sidecar_not_app(tmp_path):
    config = model(tmp_path, "compose.sixto.example.yaml")
    app = config["services"]["notion-reports"]
    sidecar = config["services"]["tailscale"]
    assert "networks" not in app
    assert "sixto" in sidecar["networks"]
    assert "notion-reports" in sidecar["networks"]["sixto"]["aliases"]
    assert config["networks"]["sixto"]["external"] is True
    assert config["networks"]["sixto"]["name"] == "sixto"
