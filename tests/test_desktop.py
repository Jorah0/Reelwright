"""Desktop shell: closing minimizes to the taskbar (unless turned off), explicit quit, and start-on-boot."""
import os
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402

from reelwright import config, desktop  # noqa: E402


class FakeWindow:
    def __init__(self):
        self.calls = []

    def minimize(self):
        self.calls.append("minimize")

    def restore(self):
        self.calls.append("restore")

    def show(self):
        self.calls.append("show")

    def destroy(self):
        self.calls.append("destroy")


def test_clicking_close_minimizes_instead_of_quitting(monkeypatch):
    w = FakeWindow()
    monkeypatch.setattr(desktop, "_quitting", False)
    config.save({"close_to_taskbar": True})
    assert desktop.on_closing(w) is False and w.calls == ["minimize"]  # False cancels the close in pywebview


def test_close_really_closes_when_turned_off_or_quitting(monkeypatch):
    w = FakeWindow()
    monkeypatch.setattr(desktop, "_quitting", False)
    config.save({"close_to_taskbar": False})
    assert desktop.on_closing(w) is not False and w.calls == []
    config.save({"close_to_taskbar": True})
    monkeypatch.setattr(desktop, "_quitting", True)  # "Quit Reelwright" always exits
    assert desktop.on_closing(w) is not False and w.calls == []


def test_quit_and_show_drive_the_window(monkeypatch):
    w = FakeWindow()
    monkeypatch.setattr(desktop, "WINDOW", w)
    monkeypatch.setattr(desktop, "_quitting", False)
    desktop.show_window()
    desktop.quit_app()
    assert w.calls == ["restore", "show", "destroy"] and desktop._quitting


def test_start_on_boot_writes_and_removes_the_run_entry(monkeypatch):
    reg = {}
    monkeypatch.setattr(desktop, "_run_key_get", lambda: reg.get("Reelwright"))
    monkeypatch.setattr(desktop, "_run_key_set", lambda v: reg.__setitem__("Reelwright", v) if v else reg.pop("Reelwright", None))
    monkeypatch.setattr(desktop, "boot_command", lambda: '"C:\\Apps\\Reelwright\\Reelwright.exe" --minimized')
    assert desktop.start_on_boot() is False
    desktop.set_start_on_boot(True)
    assert desktop.start_on_boot() is True and reg["Reelwright"].endswith("--minimized")
    desktop.set_start_on_boot(False)
    assert desktop.start_on_boot() is False and "Reelwright" not in reg


def test_boot_command_points_at_the_exe_when_packaged(monkeypatch, tmp_path):
    exe = tmp_path / "Reelwright.exe"
    monkeypatch.setattr(desktop.sys, "frozen", True, raising=False)
    monkeypatch.setattr(desktop.sys, "executable", str(exe))
    assert desktop.boot_command() == f'"{exe}" --minimized'


def test_app_routes(monkeypatch):
    from fastapi.testclient import TestClient
    from reelwright.server import app
    reg = {}
    monkeypatch.setattr(desktop, "_run_key_get", lambda: reg.get("Reelwright"))
    monkeypatch.setattr(desktop, "_run_key_set", lambda v: reg.__setitem__("Reelwright", v) if v else reg.pop("Reelwright", None))
    monkeypatch.setattr(desktop, "boot_command", lambda: '"x.exe" --minimized')
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    r = c.put("/api/app", json={"start_on_boot": True, "close_to_taskbar": False}).json()
    assert r["start_on_boot"] is True and r["close_to_taskbar"] is False and reg
    assert c.get("/api/app").json()["start_on_boot"] is True
    monkeypatch.setattr(desktop, "WINDOW", None)
    assert c.post("/api/app/quit").status_code == 400  # browser mode: nothing to quit from the page
    assert c.put("/api/app", json={"start_on_boot": "yes"}).status_code == 400


@pytest.fixture(autouse=True)
def _reset():
    yield
    config.save({"close_to_taskbar": True})
