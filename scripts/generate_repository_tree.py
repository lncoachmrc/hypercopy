#!/usr/bin/env python3
"""Render the exact Git-tracked repository tree for audit and incident response."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8")


def repository_root() -> Path:
    return Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())


def tracked_files(repo: Path) -> list[str]:
    raw = _git(repo, "ls-files", "-z")
    return sorted(path for path in raw.split("\0") if path)


def render_tree(repo: Path) -> str:
    tree: dict[str, object] = {}
    for tracked_path in tracked_files(repo):
        parts = Path(tracked_path).parts
        node = tree
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise RuntimeError(f"Path collision while rendering repository tree: {tracked_path}")
            node = child
        node[parts[-1]] = None

    lines = [f"{repo.name}/"]

    def walk(node: dict[str, object], prefix: str) -> None:
        entries = sorted(
            node.items(),
            key=lambda item: (not isinstance(item[1], dict), item[0].casefold(), item[0]),
        )
        for index, (name, child) in enumerate(entries):
            last = index == len(entries) - 1
            connector = "└── " if last else "├── "
            is_directory = isinstance(child, dict)
            lines.append(f"{prefix}{connector}{name}{'/' if is_directory else ''}")
            if is_directory:
                walk(child, prefix + ("    " if last else "│   "))

    walk(tree, "")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic tree from the repository Git index."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the generated tree to this path instead of stdout.",
    )
    args = parser.parse_args()

    repo = repository_root()
    rendered = render_tree(repo)
    if args.output is None:
        print(rendered, end="")
    else:
        output = args.output if args.output.is_absolute() else repo / args.output
        output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
