"""The database this process uses: the SQLite index on a master, the master's
own database over HTTP on an agent (which keeps none). Modules that run in
both roles — uploader, downloader, watcher, logs — import `db` from here."""

import config

if config.ROLE == "agent":
    import remote_db as db  # noqa: F401
else:
    import db  # noqa: F401
