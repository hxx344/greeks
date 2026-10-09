"""Prepare a complete candidate configuration without rewriting existing settings."""
from __future__ import annotations

import io
import os
from pathlib import Path
import sys

from dotenv import dotenv_values


def merged_config(content: str, template: str) -> tuple[str, list[str]]:
    # The same parser as runtime handles quotes, multiline secrets and bare keys.
    # Presence, including an intentionally blank value, preserves the user's setting.
    present = {key.upper() for key in dotenv_values(stream=io.StringIO(content), interpolate=False)}
    defaults = dotenv_values(stream=io.StringIO(template), interpolate=False)
    additions = {key: value for key, value in defaults.items() if key.upper() not in present and value is not None}
    if not additions:
        return content, []
    separator = "" if not content or content.endswith("\n") else "\n"
    lines = "\n".join(f"{key}={value}" for key, value in additions.items())
    return f"{content}{separator}\n# Missing strategy settings added by the installer.\n{lines}\n", list(additions)


def prepare_config(source: Path, destination: Path) -> list[str]:
    content = source.read_bytes().decode("utf-8")
    template = Path(__file__).with_name("strategy.env").read_text(encoding="utf-8")
    merged, added = merged_config(content, template)
    # Exclusive creation inside the installer's private staging directory.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(merged.encode("utf-8"))
    return added


if __name__ == "__main__":
    try:
        added = prepare_config(Path(sys.argv[1]), Path(sys.argv[2]))
        print(", ".join(added) if added else "none")
    except Exception as exc:
        # Never echo source values or parser exception details containing credentials.
        print(f"Configuration preparation failed ({type(exc).__name__}).", file=sys.stderr)
        raise SystemExit(1) from None
