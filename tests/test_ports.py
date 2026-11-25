"""Reelwright's own port block (38765 main, 38766 OBS receiver, 38810 phone remote), clear of other local apps such as
Lodestar on 8765. Old installs move off the old defaults once; custom ports are left alone."""
import importlib.util
import json
import os
import tempfile
from pathlib import Path

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402

from reelwright import accounts, config, desktop, mcp_server, platforms, relay, remote, server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """A throwaway settings.json: the migration tests never touch the suite's real settings."""
    f = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", f)
    return f


def test_defaults_are_the_new_block():
    assert (config.DEFAULT_PORT, config.DEFAULT_LAN_PORT, config.DEFAULT_REMOTE_PORT) == (38765, 38766, 38810)
    assert config.DEFAULTS["lan_port"] == 38766 and config.DEFAULTS["remote_port"] == 38810
    assert relay.DISCOVERY_PORT == config.DEFAULT_LAN_PORT  # the standalone relay script can't import config
    assert config.DEFAULTS["gsi_port"] == 3010  # games are configured to it: unchanged
    assert max(config.app_ports(config.DEFAULT_PORT)) < 49152 and config.DEFAULT_REMOTE_PORT < 49152  # below Windows' dynamic range
    if not os.environ.get("REELWRIGHT_PORT"):
        assert server.PORT == 38765


def test_fallback_scan_range_skips_the_lan_and_remote_ports(monkeypatch):
    assert config.app_ports(38765) == [38765, *range(38767, 38800)]
    monkeypatch.delenv("REELWRIGHT_PORT", raising=False)
    assert mcp_server._ports() == config.app_ports(38765)  # the MCP server looks where desktop.pick_port puts the app
    monkeypatch.setenv("REELWRIGHT_PORT", "40000")
    assert mcp_server._ports()[0] == 40000  # the env override keeps working
    busy, mine = {38765, 38767}, set()
    monkeypatch.setattr(desktop, "_port_in_use", lambda p: p in busy or p in mine)
    monkeypatch.setattr(desktop, "_is_reelwright", lambda p: p in mine)
    assert desktop.pick_port(38765) == (38768, False)


def test_remote_refuses_the_new_main_range_and_the_lan_port():
    for bad in (38765, 38766, 38780, 38799, config.load()["lan_port"]):
        with pytest.raises(ValueError):
            remote.check_port(bad)
    assert remote.check_port(38810) == 38810 and remote.check_port(38800) == 38800
    assert remote.MAIN_PORTS == range(38765, 38800)


def test_mcp_config_only_pins_ports_outside_the_scanned_range():
    assert "env" not in mcp_server.claude_config(38770)["mcpServers"]["reelwright"] or \
        "REELWRIGHT_PORT" not in mcp_server.claude_config(38770)["mcpServers"]["reelwright"]["env"]
    assert mcp_server.claude_config(8765)["mcpServers"]["reelwright"]["env"]["REELWRIGHT_PORT"] == "8765"


def test_migration_moves_only_old_default_ports_once(settings_file):
    phone = {"id": "d1", "name": "Phone", "secret": "s", "created": 1, "paired_at": 1}
    settings_file.write_text(json.dumps({"lan_port": 8766, "remote_port": 8810, "clip_count": 5, "remote_devices": [phone]}), "utf-8")
    moved = config.migrate_ports()
    s = config.load()
    assert (s["lan_port"], s["remote_port"], s["clip_count"]) == (38766, 38810, 5) and len(moved) == 2
    assert s["ports_migrated"] and s["notice_tiktok_redirect"] and s["notice_remote_rescan"]
    config.save({"lan_port": 8766})  # chosen by the user after the move: never moved again
    assert config.migrate_ports() == [] and config.load()["lan_port"] == 8766


def test_migration_leaves_custom_ports_alone(settings_file):
    settings_file.write_text(json.dumps({"lan_port": 9000, "remote_port": 9100, "remote_devices": [{"id": "d1", "secret": "s"}]}), "utf-8")
    assert config.migrate_ports() == []
    s = config.load()
    assert (s["lan_port"], s["remote_port"], s["ports_migrated"]) == (9000, 9100, True)
    assert not s["notice_remote_rescan"]  # the phones' address didn't change


def test_no_phone_notice_without_paired_phones(settings_file):
    settings_file.write_text(json.dumps({"remote_port": 8810}), "utf-8")
    assert config.migrate_ports() == ["remote_port 8810 -> 38810"]
    s = config.load()
    assert s["remote_port"] == 38810 and not s["notice_remote_rescan"] and s["notice_tiktok_redirect"]


def test_fresh_install_gets_no_notices(settings_file):
    assert config.migrate_ports() == []
    s = config.load()
    assert s["ports_migrated"] and not s["notice_tiktok_redirect"] and not s["notice_remote_rescan"]


def test_relay_notice_only_for_installs_that_used_the_receiver(settings_file, monkeypatch):
    """Relay scripts already on other PCs still discover on 8766: an install that received clips is told to update them."""
    settings_file.write_text(json.dumps({"lan_port": 8766, "lan_receive": True}), "utf-8")
    config.migrate_ports()
    assert config.load()["notice_lan_relay"]
    from reelwright import lan, obs
    monkeypatch.setattr(lan, "status", lambda: {})
    assert obs.lan_status()["relay_notice"]
    config.save({"notice_lan_relay": False})  # the OBS page's "Got it"
    assert not obs.lan_status()["relay_notice"] and config.migrate_ports() == [] and not config.load()["notice_lan_relay"]


@pytest.mark.parametrize("raw", [{"lan_port": 8766}, {"lan_port": 8766, "lan_token": "made-by-opening-the-page"},
                                 {"lan_port": 9000, "lan_receive": True}, {}])
def test_no_relay_notice_when_the_receiver_was_unused_or_its_port_stays(settings_file, raw):
    settings_file.write_text(json.dumps(raw), "utf-8")
    config.migrate_ports()
    assert not config.load()["notice_lan_relay"]


def test_tiktok_redirect_uses_the_running_port_and_warns_on_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(accounts, "LEGACY_CONN", tmp_path / "connections.json")
    monkeypatch.setattr(accounts, "LEGACY_YT", tmp_path / "youtube_token.json")
    monkeypatch.setattr(platforms, "CONN_FILE", tmp_path / "connections.json")
    platforms.tiktok_set_app("key", "secret")
    monkeypatch.setattr(server, "PORT", config.DEFAULT_PORT)
    st = server.connections()["tiktok"]
    assert st["redirect_uri"] == "http://localhost:38765/oauth/tiktok/callback" and not st["port_warning"]
    monkeypatch.setattr(server, "PORT", 38770)  # another program held 38765: desktop.pick_port moved the app
    st = server.connections()["tiktok"]
    assert st["redirect_uri"] == "http://localhost:38770/oauth/tiktok/callback" and "38770" in st["port_warning"]
    url = server.tiktok_connect({})["url"]
    assert "localhost%3A38770" in url


def test_guide_shows_the_new_redirect_uri():
    guide = (ROOT / "reelwright" / "static" / "guide" / "connect.html").read_text("utf-8")
    assert f"Under Redirect URI add exactly <code>{platforms.tiktok_redirect_uri(38765)}</code>" in guide
    assert guide.count("localhost:8765") == 1  # only in the "replace the old URI" notice
    assert "<code>38810</code>" in guide and "<code>8810</code>" not in guide


def _update_exe():
    spec = importlib.util.spec_from_file_location("update_exe", ROOT / "scripts" / "update_exe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_update_exe_finds_the_app_on_the_new_range_and_an_old_build_on_8765(monkeypatch):
    ux = _update_exe()
    up = {38768}
    monkeypatch.setattr(desktop, "_port_in_use", lambda p: p in up)
    monkeypatch.setattr(ux, "_get", lambda path, port: [] if port in up else None)
    assert ux._app_port() == 38768
    up.clear()
    up.add(8765)  # the build still running from before the move (rebuilding must still wait for its jobs)
    assert ux._app_port() == 8765
