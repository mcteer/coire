"""Shared FastAPI dependencies.

Declared as `Annotated` aliases rather than call-in-default so route signatures stay clean and
the dependency is named once. `CurrentPrincipal` lives in `auth.py` alongside the seam it
depends on.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import get_session
from coire_core.settings import Settings, get_settings

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
