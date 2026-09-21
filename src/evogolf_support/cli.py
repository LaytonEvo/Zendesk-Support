"""Command line entry point: evogolf <command>."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ZendeskConfig, corpus_path
from .corpus.store import CorpusStore


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )


def cmd_verify(_: argparse.Namespace) -> int:
    from .zendesk.client import ZendeskClient

    config = ZendeskConfig.from_env()
    print(f"Connecting to {config.base_url} …")
    with ZendeskClient(config) as client:
        me = client.verify()
    print(f"  OK - authenticated as {me.get('name')} <{me.get('email')}> "
          f"(role: {me.get('role')})")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .zendesk.export import run_export

    result = run_export(full=args.full, limit=args.limit)
    print(
        f"\nExported {result.tickets} tickets and {result.comments} comments "
        f"({result.users} users){' [resumed]' if result.resumed else ''}."
    )
    if result.errors:
        print(f"  {len(result.errors)} ticket(s) had problems:")
        for err in result.errors[:10]:
            print(f"    - {err}")
    print(f"Corpus: {corpus_path()}")
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    path = corpus_path()
    if not path.exists():
        print(f"No corpus yet at {path}. Run: evogolf export")
        return 1
    with CorpusStore(path) as store:
        for key, value in store.stats().items():
            print(f"{key:>18}: {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evogolf", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("verify", help="check Zendesk credentials").set_defaults(func=cmd_verify)

    export = sub.add_parser("export", help="pull ticket history into the corpus")
    export.add_argument("--full", action="store_true",
                        help="ignore the saved cursor and re-walk from the start")
    export.add_argument("--limit", type=int, default=None,
                        help="stop after N tickets (trial run)")
    export.set_defaults(func=cmd_export)

    sub.add_parser("stats", help="summarise the local corpus").set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
