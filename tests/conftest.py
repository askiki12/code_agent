import pytest


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)
