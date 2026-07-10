import logging

from src import log as app_log


def test_console_log_can_be_disabled_and_reenabled(tmp_path):
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    for handler in original_handlers:
        root.removeHandler(handler)
    try:
        app_log.setup(log_dir=str(tmp_path), console_log=False)
        assert not [
            h
            for h in root.handlers
            if getattr(h, app_log._HANDLER_MARKER, None) == "console"
        ]
        assert (tmp_path / "weibo-monitor.log").exists()

        app_log.setup(log_dir=str(tmp_path), console_log=True)
        assert len(
            [
                h
                for h in root.handlers
                if getattr(h, app_log._HANDLER_MARKER, None) == "console"
            ]
        ) == 1
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)
