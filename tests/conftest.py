import pytest


@pytest.fixture(autouse=True)
def _isolated_flex_preview_dir(tmp_path_factory, monkeypatch):
    """REPORT_DRY_RUN writes the Flex preview (PNG + JSON) to disk. Point it
    at a throw-away directory so no test ever drops files into the repo."""
    monkeypatch.setenv(
        "REPORT_PREVIEW_DIR", str(tmp_path_factory.mktemp("flex_preview"))
    )
