from pathlib import Path


def test_api_watch_patterns_include_reconcile_service() -> None:
    railway_toml = Path(__file__).resolve().parents[2] / "railway.toml"
    text = railway_toml.read_text(encoding="utf-8")

    assert '"/backend/app/services/reconcile.py"' in text
