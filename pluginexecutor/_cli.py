from __future__ import annotations

import argparse
import signal
import sys
from typing import Optional, Sequence

import yaml

from ._config import load_config
from ._executor import PluginExecutor


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute Naemon-compatible plugins on a fixed schedule.")
    parser.add_argument("config", help="path to YAML config file")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    executor = PluginExecutor(config)

    try:
        signal.signal(signal.SIGINT, lambda _signum, _frame: executor.stop())
        signal.signal(signal.SIGTERM, lambda _signum, _frame: executor.stop())
    except ValueError:
        pass

    executor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
