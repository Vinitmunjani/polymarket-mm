"""
Structured logging setup using structlog.

Logs to file (JSON) always. Console output is controlled
separately so the dashboard can suppress it.
"""

import os
import logging
import structlog


def setup_logging(level: str = "INFO", log_file: str = "logs/bot.log",
                  console: bool = True, json_logs: bool = True):
    """Configure structured logging for the bot."""
    # Ensure log directory exists
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    handlers = []

    # File handler — always active, gets everything
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(file_handler)

    # Console handler — can be suppressed later by raising root level
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, level.upper()))
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(console_handler)

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=handlers,
        format="%(message)s",
        force=True,
    )

    # Configure structlog to route through stdlib logging
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """Get a named structured logger."""
    return structlog.get_logger(name)


def suppress_console():
    """Suppress console output (for dashboard mode). File logging continues."""
    for handler in logging.root.handlers:
        if isinstance(handler, logging.StreamHandler) and \
           not isinstance(handler, logging.FileHandler):
            handler.setLevel(logging.CRITICAL)


def restore_console(level: str = "INFO"):
    """Restore console output."""
    for handler in logging.root.handlers:
        if isinstance(handler, logging.StreamHandler) and \
           not isinstance(handler, logging.FileHandler):
            handler.setLevel(getattr(logging, level.upper()))
