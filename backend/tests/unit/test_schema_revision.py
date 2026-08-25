from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.schema import EXPECTED_REVISION


def test_expected_revision_matches_alembic_head():
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / 'alembic.ini'))
    config.set_main_option('script_location', str(backend_root / 'alembic'))
    script = ScriptDirectory.from_config(config)

    assert script.get_current_head() == EXPECTED_REVISION
