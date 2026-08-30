"""Reading the node's secrets from the macOS System keychain.

Two secrets live on a Studio and nowhere else:

  * `coire-node-token` — the per-node bearer the control plane presents and the agent checks
    (feature 000 FR-013; a static token until feature 005 issues real ones, ADR-0001);
  * `coire-hf-token` — the Hugging Face credential. Spec FR-005 puts it *only* here, so the
    node agent is the one component that can talk to Hugging Face at all.

They are in the **System** keychain, not the login keychain, because the agent is a
LaunchDaemon that starts before anyone logs in and the login keychain is still locked at that
point (feature 000 research R6).

A missing item is a warning, never a failure. An agent that exits because the Hugging Face
token is absent could not serve health either, and most of what it does needs no credential:
ungated repositories pull fine without one.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from pydantic import SecretStr

logger = logging.getLogger(__name__)

SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"
NODE_TOKEN_ITEM = "coire-node-token"
HF_TOKEN_ITEM = "coire-hf-token"
_TIMEOUT_S = 10.0


def read_item(service: str, *, keychain: str = SYSTEM_KEYCHAIN) -> SecretStr:
    """Read one generic-password item, or an empty secret when it is not there.

    Never raises and never logs the value. `security` writes the password to stdout with a
    trailing newline, which is stripped — a token with a stray newline fails a
    constant-time comparison in a way that is very hard to see in a log.
    """
    security = shutil.which("security")
    if security is None:  # not macOS: containers and CI supply secrets by environment
        return SecretStr("")
    try:
        proc = subprocess.run(
            [security, "find-generic-password", "-w", "-s", service, keychain],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("could not read keychain item %s: %s", service, exc)
        return SecretStr("")
    if proc.returncode != 0:
        return SecretStr("")
    return SecretStr(proc.stdout.strip())


def load_node_secrets(settings: object) -> None:
    """Fill `node_token` and `hf_token` on `settings` from the System keychain.

    Anything already configured — an environment variable in a container, a mounted file —
    wins, so the Linux test image and the integration suite need no keychain at all. Only an
    empty value is filled in.

    `hf_token` is deliberately *not* exported into this process's environment: it is passed
    only into the acquisition worker's environment when one is spawned (see `jobs.py`), so an
    unrelated library in the agent cannot pick it up from `os.environ`.
    """
    from coire_core.settings import Settings

    assert isinstance(settings, Settings)

    if not settings.node_token.get_secret_value():
        token = read_item(NODE_TOKEN_ITEM)
        if token.get_secret_value():
            settings.node_token = token
            logger.info("node token loaded from the System keychain")
        else:
            logger.error(
                "no node token: %s is absent from %s and NODE_TOKEN is unset. The agent will "
                "refuse every authenticated request and cannot register. Store it with: "
                "sudo security add-generic-password -a coire -s %s -w '<token>' %s",
                NODE_TOKEN_ITEM,
                SYSTEM_KEYCHAIN,
                NODE_TOKEN_ITEM,
                SYSTEM_KEYCHAIN,
            )

    if not settings.hf_token.get_secret_value():
        hf = read_item(HF_TOKEN_ITEM)
        if hf.get_secret_value():
            settings.hf_token = hf
            logger.info("Hugging Face token loaded from the System keychain")
        else:
            logger.warning(
                "no Hugging Face token (%s absent from %s): ungated repositories still pull, "
                "gated ones will be refused with a gating error",
                HF_TOKEN_ITEM,
                SYSTEM_KEYCHAIN,
            )
