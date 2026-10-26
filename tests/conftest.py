import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo


@pytest.fixture
def scratch_url():
    """The URL of a freshly created database, dropped afterwards.

    Needs a role allowed to CREATE DATABASE, which the compose superuser is.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    name = f"test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_conninfo(url, dbname=name)
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def scratch_conn(scratch_url):
    """A plain connection to the scratch database."""
    with psycopg.connect(scratch_url) as conn:
        yield conn
