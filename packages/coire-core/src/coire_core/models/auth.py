"""Identity, API-key, entitlement, and quota wire contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


class AuthScope(StrEnum):
    CHAT = "chat"
    IMAGES = "images"
    IMAGES_EXPLICIT = "images:explicit"
    MCP = "mcp"
    ADMIN = "admin"


class ActorType(StrEnum):
    USER = "user"
    API_KEY = "api_key"
    SERVICE = "service"
    ANONYMOUS = "anonymous"


def normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    local, separator, domain = normalized.partition("@")
    if not separator or not local or "." not in domain or domain.startswith("."):
        raise ValueError("a valid email address is required")
    return normalized


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=120)
    role: UserRole

    _email = field_validator("email")(normalize_email)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    role: UserRole | None = None
    active: bool | None = None


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    active: bool
    entitlements: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class Entitlement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    granted_by: uuid.UUID
    granted_at: datetime
    revoked_at: datetime | None = None


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    scopes: frozenset[AuthScope] = Field(min_length=1)
    requests_per_minute: int = Field(ge=1, le=10_000)
    monthly_budget_tokens: int = Field(ge=1)


class ApiKeyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    scopes: frozenset[AuthScope] | None = Field(default=None, min_length=1)
    requests_per_minute: int | None = Field(default=None, ge=1, le=10_000)
    monthly_budget_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_change(self) -> ApiKeyUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one API key field is required")
        return self


class ApiKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    prefix: str
    scopes: frozenset[AuthScope]
    requests_per_minute: int
    monthly_budget_tokens: int
    tokens_consumed: int = 0
    period_resets_at: datetime
    active: bool
    created_at: datetime
    rotated_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyIssued(ApiKey):
    secret: str = Field(min_length=32)


class QuotaStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_tokens: int
    consumed_tokens: int
    resets_at: datetime


class AuthPrincipal(BaseModel):
    """Request-local verified identity. Presented credential bytes are deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_type: ActorType
    subject: str
    user_id: uuid.UUID | None = None
    role: UserRole | None = None
    scopes: frozenset[AuthScope] = frozenset()
    entitlements: frozenset[str] = frozenset()
    api_key_id: uuid.UUID | None = None
    credential_version: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN or AuthScope.ADMIN in self.scopes
