import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
GOOD = EXAMPLES / "contracts" / "retail.yml"
BROKEN = EXAMPLES / "contracts" / "broken.yml"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_sample_db", EXAMPLES / "build_sample_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def retail_db(tmp_path_factory) -> Path:
    """Fresh copy of the deterministic sample DB, built from the example script."""
    return _load_builder().build(tmp_path_factory.mktemp("db") / "retail.sqlite")
