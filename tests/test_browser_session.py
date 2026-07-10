import stat
import sys
from types import SimpleNamespace

from src import browser_session


async def test_profile_cleanup_happens_inside_lock_and_sandbox_stays_enabled(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    launched = {}

    class FakeContext:
        async def close(self):
            launched["closed"] = True

    class FakeChromium:
        async def launch_persistent_context(self, **kwargs):
            launched.update(kwargs)
            return FakeContext()

    class FakePlaywrightManager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    fake_module = SimpleNamespace(async_playwright=lambda: FakePlaywrightManager())
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_module)

    lock = browser_session._lock_for(profile)

    def assert_locked(path):
        assert path == profile
        assert lock.locked()

    monkeypatch.setattr(browser_session, "_clear_singleton_locks", assert_locked)

    async with browser_session.persistent_context(str(profile)):
        assert lock.locked()

    assert "--no-sandbox" not in launched["args"]
    assert launched["closed"]
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
