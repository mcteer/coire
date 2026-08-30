"""The authentication seam.

Feature 000 ships **no** authentication on control-plane routes — a deliberate, time-boxed
exception to Principle IV recorded in `docs/adr/0001-defer-auth-and-edge-until-external-traffic.md`.

This module exists so feature 007 can wire real authentication in **without restructuring**:
every route already depends on `require_principal`, so 007 replaces this function's body and no
route signature changes. It is deliberately *not* a placeholder that pretends to authenticate —
a check that always succeeds looks like protection and is worse than none.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, ConfigDict


class PrincipalKind(StrEnum):
    ANONYMOUS = "anonymous"
    USER = "user"  # issued by feature 007
    SERVICE = "service"  # issued by feature 007


class Principal(BaseModel):
    """Who is making a request. Until feature 007 this is always anonymous."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PrincipalKind = PrincipalKind.ANONYMOUS
    subject: str | None = None
    scopes: frozenset[str] = frozenset()

    @property
    def is_admin(self) -> bool:
        """No principal is an admin in feature 000; roles arrive with feature 007."""
        return False


ANONYMOUS = Principal()


async def require_principal() -> Principal:
    """Resolve the caller.

    Feature 007 replaces this body with Cloudflare Access assertion validation and API-key
    lookup. Until then it returns the anonymous principal and the platform is reachable only
    on core's loopback and the unrouted mesh.
    """
    return ANONYMOUS


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]
