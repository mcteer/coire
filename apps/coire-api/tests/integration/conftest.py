"""Training persistence fixtures never connect to a developer's production database."""

from collections.abc import Iterator

import pytest
from training_postgres import disposable_postgres


@pytest.fixture(scope="session")
def training_postgres_url() -> Iterator[str]:
    with disposable_postgres() as url:
        yield url
