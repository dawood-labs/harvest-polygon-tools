import pytest


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    """Every test keeps the last-run records in its own temp folder (never the repo or the real home)."""
    d = tmp_path / "state"
    monkeypatch.setenv("HARVEST_TOOLS_STATE_DIR", str(d))
    monkeypatch.setattr("preprocess_classified._NOTED", set(), raising=False)
    yield d
