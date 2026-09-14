from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"


class UserPrincipal(BaseModel):
    """由服务端认证层构造的不可变用户身份。"""

    model_config = ConfigDict(frozen=True)

    user_id: str
    username: str
    role: UserRole


class AuthenticatedSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    principal: UserPrincipal
    csrf_hash: str
    absolute_expires_at: int


class AuthTokens(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_token: str
    csrf_token: str
    absolute_expires_at: int

