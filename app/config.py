import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Load .env if present (Docker's env_file already sets env; this covers bare-metal runs).
    Existing environment variables always win."""
    for cand in (os.path.join(os.getcwd(), ".env"),
                 os.path.join(BASE_DIR, "..", ".env")):
        if os.path.exists(cand):
            with open(cand) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


_load_dotenv()


def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_int(name, default):
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def env_bool(name, default=True):
    return env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def _csv(name, default=""):
    return [x.strip() for x in env(name, default).split(",") if x.strip()]


BUCKET = env("COLDVAULT_BUCKET")
PREFIX = env("COLDVAULT_PREFIX").strip("/")
STORAGE_CLASS = env("COLDVAULT_STORAGE_CLASS", "DEEP_ARCHIVE")

WATCH_DIRS = _csv("COLDVAULT_WATCH_DIRS", "/media")
BROWSE_ROOTS = WATCH_DIRS + _csv("COLDVAULT_BROWSE_ROOTS")
CANARY_NAME = env("COLDVAULT_CANARY", "coldvault.canary")
AUTO_UPLOAD = env_bool("COLDVAULT_AUTO_UPLOAD", True)
WATCH_INTERVAL = max(2, env_int("COLDVAULT_WATCH_INTERVAL", 10))

UPLOAD_WORKERS = max(1, env_int("COLDVAULT_UPLOAD_WORKERS", 2))
DEDUPE = env_bool("COLDVAULT_DEDUPE", True)
MULTIPART_THRESHOLD = env_int("COLDVAULT_MULTIPART_THRESHOLD_MB", 512) * 1024 * 1024
PART_SIZE = max(5, env_int("COLDVAULT_PART_SIZE_MB", 256)) * 1024 * 1024
PART_WORKERS = max(1, env_int("COLDVAULT_PART_WORKERS", 4))

RESTORE_POLL = max(60, env_int("COLDVAULT_RESTORE_POLL_MINUTES", 60) * 60)

DOWNLOAD_DIR = env("COLDVAULT_DOWNLOAD_DIR", "/downloads")
DOWNLOAD_WORKERS = max(1, env_int("COLDVAULT_DOWNLOAD_WORKERS", 2))
DOWNLOAD_PART_WORKERS = max(1, env_int("COLDVAULT_DOWNLOAD_PART_WORKERS", 4))
# Objects larger than this are fetched with parallel ranged GETs
DOWNLOAD_PART_SIZE = max(5, env_int("COLDVAULT_DOWNLOAD_PART_SIZE_MB", 256)) * 1024 * 1024
PORT = env_int("COLDVAULT_PORT", 9999)

DATA_DIR = env("COLDVAULT_DATA_DIR", "/data")
TMP_DIR = os.path.join(DATA_DIR, "tmp")
DB_PATH = os.path.join(DATA_DIR, "coldvault.db")
LOG_PATH = os.path.join(DATA_DIR, "coldvault.log")

EXCLUDES = _csv(
    "COLDVAULT_EXCLUDE",
    ".Trashes,.Trash-*,.Spotlight-V100,System Volume Information,"
    ".fseventsd,.TemporaryItems,.DS_Store,._*,lost+found,$RECYCLE.BIN,.hidden",
)

os.makedirs(TMP_DIR, exist_ok=True)
