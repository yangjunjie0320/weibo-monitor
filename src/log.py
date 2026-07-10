import logging
import logging.handlers
import os
from pathlib import Path

_HANDLER_MARKER = "_weibo_monitor_handler"


def setup(
    level: str = "INFO",
    log_dir: str = "logs",
    *,
    console_log: bool = True,
) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fmt.converter = __import__("time").gmtime  # UTC timestamps

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    managed_streams = [
        handler
        for handler in root.handlers
        if getattr(handler, _HANDLER_MARKER, None) == "console"
    ]
    if console_log and not managed_streams:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        setattr(stream, _HANDLER_MARKER, "console")
        root.addHandler(stream)
    elif not console_log:
        for stream in managed_streams:
            root.removeHandler(stream)
            stream.close()

    log_path = str(Path(log_dir, "weibo-monitor.log").resolve())
    managed_files = [
        handler
        for handler in root.handlers
        if getattr(handler, _HANDLER_MARKER, None) == "file"
        and getattr(handler, "baseFilename", None) == log_path
    ]
    if not managed_files:
        file_handler = logging.handlers.RotatingFileHandler(
            filename=os.path.join(log_dir, "weibo-monitor.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        setattr(file_handler, _HANDLER_MARKER, "file")
        root.addHandler(file_handler)
