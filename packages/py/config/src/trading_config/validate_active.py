"""Startup validation entry point (IMP-0105).

Loads and validates every active config pointer under a config root. Any
problem is reported and the process exits non-zero — startup must fail closed,
never continue on defaults or partial config.

Usage:
    uv run python -m trading_config.validate_active [--root PATH] [--environment ENV]
"""

import argparse
import sys
from pathlib import Path

from trading_contracts.enums import Environment

from trading_config.loader import ConfigLoader


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("config"))
    parser.add_argument(
        "--environment",
        action="append",
        choices=[member.value for member in Environment],
        help="restrict validation to specific environment(s); default is all",
    )
    args = parser.parse_args(argv[1:])

    if not args.root.is_dir():
        print(f"config root not found: {args.root}")
        return 2

    environments = (
        [Environment(value) for value in args.environment] if args.environment else None
    )
    loader = ConfigLoader(args.root)
    problems = loader.validate_active(environments)

    checked = sum(
        len(loader.iter_pointer_files(env))
        for env in (environments if environments is not None else list(Environment))
    )
    for problem in problems:
        print(problem)
    if problems:
        print(f"FAIL: {len(problems)} active config problem(s).")
        return 1
    print(f"OK: {checked} active config(s) validated.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
