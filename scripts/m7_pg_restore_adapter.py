"""Route pg_restore verification and restore modes into one quarantine container."""

from __future__ import annotations

import os
import re
import sys
from typing import Final

_CONTAINER_ID: Final = re.compile(r"^[0-9a-f]{12,64}$")


def command(container_id: str, arguments: tuple[str, ...]) -> tuple[str, ...]:
    if _CONTAINER_ID.fullmatch(container_id) is None:
        raise ValueError("PostgreSQL restore container identity is invalid")
    prefix = ("docker", "exec", "-i", container_id, "pg_restore")
    if "--list" in arguments:
        return (*prefix, *arguments)
    return (*prefix, "-U", "ratereplay", "-d", "ratereplay", *arguments)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(2)
    executable = command(sys.argv[1], tuple(sys.argv[2:]))
    os.execvp(executable[0], executable)  # noqa: S606  # nosec B606


if __name__ == "__main__":
    main()
