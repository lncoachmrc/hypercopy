import tomllib
from pathlib import Path

from test_railway_runtime_watch_coverage import watch_pattern_covers_path


def test_api_watch_patterns_include_reconcile_service() -> None:
    """A reconcile.py change must redeploy the API via watch-pattern coverage.

    The protected guarantee is unchanged; it is expressed as coverage rather
    than requiring one particular literal watch-pattern form.
    """

    railway_toml = Path(__file__).resolve().parents[2] / "railway.toml"
    with railway_toml.open("rb") as handle:
        config = tomllib.load(handle)

    patterns = list(config["build"]["watchPatterns"])
    assert watch_pattern_covers_path(
        "/backend/app/services/reconcile.py",
        patterns,
    )
