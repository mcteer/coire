"""Agent image entry point.

Feature 000 builds this image but never runs it on core (FR-017): core hosts no user harness.
The Pydantic AI harness with its coding/general/image profiles is feature 010.
"""

from __future__ import annotations

import sys

from coire_agent import __version__


def main() -> None:
    sys.stdout.write(f"coire-agent {__version__}\n")


if __name__ == "__main__":
    main()
