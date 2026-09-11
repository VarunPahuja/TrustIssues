"""Alembic environment. Reads `DATABASE_URL` from the environment (falling
back to `docker-compose.yml`'s default connection string) rather than storing
a connection string in `alembic.ini`, so the same variable
`.env.example`/the running app uses is the only place it's ever set.

`target_metadata` is `app.models.Base.metadata` — every table this branch
defines. `--autogenerate` diffs against this the same way it would against
any other Alembic setup; the one migration in `versions/` was hand-verified,
not blindly accepted from a diff.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

# backend/alembic/env.py -> backend/ -> repo root, so `import app...` and
# `import shared...` both resolve regardless of the process's cwd. Must
# happen before importing app.models below, hence sitting ahead of the
# otherwise-later third-party imports.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_DIR.parent
for path in (_BACKEND_DIR, _REPO_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.config import database_url
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# `app.config.database_url()` is the single reader of DATABASE_URL, so
# migrations and the running app can never disagree about which database they
# are pointed at — including the `postgres://` -> `postgresql://` fix that
# some managed providers make necessary.
config.set_main_option("sqlalchemy.url", database_url())


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
