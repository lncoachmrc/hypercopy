from __future__ import annotations

import ast
import fnmatch
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


@dataclass(frozen=True)
class ModuleFile:
    module: str
    path: Path
    is_package: bool


def _module_index() -> dict[str, ModuleFile]:
    modules: dict[str, ModuleFile] = {}
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(BACKEND_ROOT).with_suffix("")
        parts = list(relative.parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        module = ".".join(parts)
        modules[module] = ModuleFile(module=module, path=path, is_package=is_package)
    return modules


def _resolve_from_base(current: str, module: str | None, level: int) -> str:
    if level == 0:
        return module or ""

    package = current.split(".")[:-1]
    keep = len(package) - (level - 1)
    if keep < 0:
        return ""
    base_parts = package[:keep]
    if module:
        base_parts.extend(module.split("."))
    return ".".join(base_parts)


def _local_imports(
    current: str,
    source: str,
    modules: dict[str, ModuleFile],
) -> set[str]:
    tree = ast.parse(source)
    discovered: set[str] = set()

    def add(module: str) -> None:
        target = modules.get(module)
        if target is not None and not target.is_package:
            discovered.add(module)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    add(alias.name)
            continue

        if isinstance(node, ast.ImportFrom):
            base = _resolve_from_base(current, node.module, node.level)
            if base.startswith("app."):
                add(base)
                for alias in node.names:
                    if alias.name != "*":
                        add(f"{base}.{alias.name}")
            continue

        if not isinstance(node, ast.Call) or not node.args:
            continue

        is_import_module = (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
            and node.func.attr == "import_module"
        ) or (
            isinstance(node.func, ast.Name)
            and node.func.id in {"import_module", "__import__"}
        )
        if not is_import_module:
            continue

        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value.startswith("app."):
                add(first.value)

    return discovered


def _transitive_app_modules(entrypoint: str) -> dict[str, ModuleFile]:
    modules = _module_index()
    assert entrypoint in modules, f"entrypoint module missing: {entrypoint}"

    visited: set[str] = set()
    pending = [entrypoint]

    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)

        module_file = modules[current]
        source = module_file.path.read_text(encoding="utf-8")
        for imported in sorted(_local_imports(current, source, modules)):
            if imported not in visited:
                pending.append(imported)

    return {module: modules[module] for module in sorted(visited)}


def _watch_patterns(config_name: str) -> list[str]:
    config_path = BACKEND_ROOT / config_name
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    return list(config["build"]["watchPatterns"])


def _repository_path(path: Path) -> str:
    return "/backend/" + path.relative_to(BACKEND_ROOT).as_posix()


def watch_pattern_covers_path(repository_path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/**"):
            if repository_path.startswith(pattern[:-2]):
                return True
        elif fnmatch.fnmatchcase(repository_path, pattern):
            return True
    return False


@pytest.mark.parametrize(
    ("entrypoint", "config_name"),
    [
        ("app.workers.resilient_execution_worker", "railway.worker.toml"),
        ("app.workers.watcher", "railway.watcher.toml"),
        ("app.main", "railway.toml"),
    ],
)
def test_runtime_import_graph_is_covered_by_railway_watch_patterns(
    entrypoint: str,
    config_name: str,
) -> None:
    modules = _transitive_app_modules(entrypoint)
    patterns = _watch_patterns(config_name)

    uncovered = sorted(
        _repository_path(module.path)
        for module in modules.values()
        if not watch_pattern_covers_path(_repository_path(module.path), patterns)
    )

    assert uncovered == [], (
        f"{config_name} does not watch every app.* runtime dependency reachable "
        f"from {entrypoint}:\n" + "\n".join(f"  - {path}" for path in uncovered)
    )


def test_worker_watch_policy_covers_all_services_and_security_modules() -> None:
    patterns = set(_watch_patterns("railway.worker.toml"))

    required = {
        "/backend/app/services/**",
        "/backend/app/security/**",
    }
    missing = sorted(required - patterns)

    assert missing == [], (
        "railway.worker.toml must keep directory-level coverage for worker "
        "services and security dependencies:\n"
        + "\n".join(f"  - {pattern}" for pattern in missing)
    )
