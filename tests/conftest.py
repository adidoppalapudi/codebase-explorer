import shutil
from pathlib import Path

import pytest

from explorer.indexer import index_repo

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A writable copy of the sample repository."""
    target = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, target)
    return target


@pytest.fixture
def db(repo: Path, tmp_path: Path) -> Path:
    """The sample repository, indexed."""
    db_path = tmp_path / "index.db"
    index_repo(repo, db_path)
    return db_path
