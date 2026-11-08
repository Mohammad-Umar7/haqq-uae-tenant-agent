from pathlib import Path

import pytest

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "sample_contract.txt"


@pytest.fixture(scope="session")
def sample_text() -> str:
    from agent.contract import validate_contract_text

    return validate_contract_text(SAMPLE.read_text(encoding="utf-8"))
