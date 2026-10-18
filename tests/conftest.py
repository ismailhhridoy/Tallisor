import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Tests never touch the user's real workspace (custom adapters, saved runs).
os.environ["TALLISOR_WORKSPACE"] = tempfile.mkdtemp(prefix="md-test-")
sys.path.insert(0, str(ROOT))

from tallisor.session import Session  # noqa: E402

SAMPLE = ROOT / "sample_data" / "northwind"


@pytest.fixture(scope="session", autouse=True)
def sample_data():
    if not (SAMPLE / "gold.json").exists():
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample.py")], check=True)
    return SAMPLE


@pytest.fixture
def session():
    return Session.from_dir(SAMPLE).run()


@pytest.fixture
def gold():
    return json.loads((SAMPLE / "gold.json").read_text())["cases"]
