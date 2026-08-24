"""Condense a session transcript into something a prompt can hold.

The reading itself lives in mashu.transcript, which is what the worker uses;
this is the hand entry point for looking at one file. Two implementations of
the same parsing would drift, and the one that drifted would be the one nobody
ran.

Usage:
    python tools/condense_session.py TRANSCRIPT.jsonl > condensed.md
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from mashu import transcript  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=pathlib.Path)
    parser.add_argument("-o", "--output", type=pathlib.Path)
    parser.add_argument("--cli", choices=transcript.CLIS, help="skip the format sniff")
    parser.add_argument("--since", type=int, help="only the turns after this record ordinal")
    args = parser.parse_args()

    session = transcript.read(args.transcript, source_cli=args.cli)
    text = transcript.render(session, session.since(args.since))

    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(
            f"{args.transcript.stat().st_size / 1e6:.1f} MB -> "
            f"{len(text.encode()) / 1e3:.0f} kB  {args.output}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
