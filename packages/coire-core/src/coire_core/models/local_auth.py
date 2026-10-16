"""Local browser identity contracts; credentials never appear in responses."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from coire_core.models.auth import User, normalize_email


class LocalAuthStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    access_enabled: bool


class LocalLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=15, max_length=1024)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)


class LocalSessionIssued(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: User
    expires_at: datetime
