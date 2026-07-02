import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def timeline_data():
    with open(FIXTURES / "timeline_1644027280_page_1.json", encoding="utf-8") as f:
        return json.load(f)
