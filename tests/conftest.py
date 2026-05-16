from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def sample_static_png(fixtures_dir: Path) -> bytes:
    return (fixtures_dir / "sample_static_200x200.png").read_bytes()


@pytest.fixture
def sample_animated_gif(fixtures_dir: Path) -> bytes:
    return (fixtures_dir / "sample_animated_200x200.gif").read_bytes()


@pytest.fixture
def package_detail_json(fixtures_dir: Path) -> str:
    return (fixtures_dir / "package_detail_170660.json").read_text(encoding="utf-8")
