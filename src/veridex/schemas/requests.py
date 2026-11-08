from pydantic import BaseModel, EmailStr, Field, field_validator

from veridex.auth import BCRYPT_MAX_PASSWORD_BYTES
from veridex.config import settings


class AnalyzeRequest(BaseModel):
    """Request body for the analyze endpoint."""

    page_content: str = Field(
        min_length=1,
        max_length=settings.max_page_bytes,
        description="Raw HTML or text content of the product page.",
    )
    url: str = Field(default="", description="URL of the product page.")
    flags: list[str] = Field(
        default_factory=list, description="Names of optional skills to activate in addition to the default set."
    )


class CreateAccountRequest(BaseModel):
    """Request body for account creation."""

    username: str = Field(min_length=3, max_length=50, description="Unique username.")
    password: str = Field(min_length=8, description="Plain-text password (hashed server-side).")
    contact_email: EmailStr = Field(description="Contact email address.")

    @field_validator("password")
    @classmethod
    def _password_fits_bcrypt(cls, password: str) -> str:
        if len(password.encode()) > BCRYPT_MAX_PASSWORD_BYTES:
            raise ValueError(f"Password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes.")
        return password
