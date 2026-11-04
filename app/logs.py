import collections
import logging
import logging.handlers

import config
from store import db

logger = logging.getLogger("coldvault")
logger.setLevel(logging.DEBUG)

_file = logging.handlers.RotatingFileHandler(config.LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5)
_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
logger.addHandler(_file)

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
logger.addHandler(_console)

# On an agent, events wait here until the agent sends them to the master's log
# (shown there prefixed with the agent's name). Bounded: if the master is away
# for long, the oldest are dropped — they're still in the agent's own log file.
outbox = collections.deque(maxlen=5000)


def log_event(level, category, message, detail=None):
    """Log to the rotating file, stdout and the events table (shown in the web UI)."""
    line = f"[{category}] {message}"
    if detail:
        line += f" | {detail}"
    getattr(logger, level.lower(), logger.info)(line)
    if config.ROLE == "agent":
        if level != "DEBUG":
            outbox.append({"ts": db.now(), "level": level, "category": category,
                           "message": message, "detail": detail})
        return
    try:
        db.insert_event(level, category, message, detail)
    except Exception:
        logger.exception("failed to write event to db")
