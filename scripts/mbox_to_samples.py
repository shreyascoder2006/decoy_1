#!/usr/bin/env python3
"""Turn a Gmail Takeout .mbox into local sample files for benchmark_email_models.py.

Everything runs on this machine and nothing is uploaded. The script prints only
counts, never message text, so you can run it without anyone seeing your mail.

Labelling uses Gmail's own labels (the X-Gmail-Labels header in a Takeout export):
  * Spam                      -> phish/   (Spam is NOT the same as phishing: expect
                                           label noise from ordinary junk mail)
  * Inbox, and not Spam       -> human/
Everything else is skipped. Output goes to mysamples/email/, which is gitignored.

Usage:
    .venv\\Scripts\\python.exe scripts\\mbox_to_samples.py "C:\\path\\to\\All mail.mbox"
    .venv\\Scripts\\python.exe scripts\\mbox_to_samples.py my.mbox --per-class 150 --seed 7
"""

from __future__ import annotations

import argparse
import html
import mailbox
import pathlib
import random
import re
import sys
from email.message import Message

MAX_CHARS = 5000
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t\r\f\v]+")


def labels_of(message: Message) -> set[str]:
    raw = message.get("X-Gmail-Labels", "") or ""
    return {part.strip().strip('"').lower() for part in raw.split(",") if part.strip()}


def body_text(message: Message) -> str:
    """Prefer text/plain; fall back to tag-stripped HTML."""
    plain, rich = [], []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (plain if kind == "text/plain" else rich).append(text)
    if plain:
        return "\n".join(plain)
    stripped = _TAG.sub(" ", html.unescape("\n".join(rich)))
    return stripped


def clean(text: str) -> str:
    lines = [_SPACE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:MAX_CHARS]


def decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        from email.header import decode_header as _decode, make_header  # noqa: PLC0415

        return str(make_header(_decode(value)))
    except Exception:  # noqa: BLE001 - a bad header must not stop the export
        return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mbox", nargs="+", help="one or more Takeout .mbox files (e.g. Inbox and Spam)")
    parser.add_argument("--out", default="mysamples/email")
    parser.add_argument("--per-class", type=int, default=200, help="max samples per class")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    paths = [pathlib.Path(p) for p in args.mbox]
    for path in paths:
        if not path.is_file():
            print(f"not a file: {path}")
            return 1

    buckets: dict[str, list[tuple[str, str]]] = {"human": [], "phish": []}
    seen = skipped = 0
    for path in paths:
        for message in mailbox.mbox(str(path)):
            seen += 1
            labels = labels_of(message)
            if "spam" in labels:
                bucket = "phish"
            elif "inbox" in labels:
                bucket = "human"
            else:
                skipped += 1
                continue
            subject = decode_header(message.get("Subject"))
            text = clean(body_text(message))
            if len(text) < 20:
                skipped += 1
                continue
            buckets[bucket].append((subject, text))

    rng = random.Random(args.seed)
    out = pathlib.Path(args.out)
    for bucket, items in buckets.items():
        rng.shuffle(items)
        chosen = items[: args.per_class]
        folder = out / bucket
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.txt"):
            old.unlink()
        for index, (subject, text) in enumerate(chosen, start=1):
            (folder / f"{bucket}{index:03d}.txt").write_text(f"{subject}\n{text}", encoding="utf-8")
        print(f"{bucket}: wrote {len(chosen)} of {len(items)} usable messages to {folder}")

    print(f"read {seen} messages, skipped {skipped}")
    print("\nNext: .venv\\Scripts\\python.exe scripts\\benchmark_email_models.py --samples-dir " + args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
