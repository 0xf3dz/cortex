from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    meta_access_token: str = Field(min_length=1)
    meta_app_secret: str = Field(min_length=1)
    meta_verify_token: str = Field(min_length=16)
    whatsapp_phone_number_id: str = Field(min_length=1)
    whatsapp_graph_api_version: str = Field(pattern=r"^v[1-9]\d*\.\d+$")
    allowed_whatsapp_wa_id: str = Field(min_length=5)
    database_path: Path
    webhook_max_body_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)
    embedding_model: str = Field(default="intfloat/multilingual-e5-small", min_length=1)
    user_timezone: str = Field(default="Australia/Brisbane", min_length=1)
    worker_enabled: bool = True
    worker_poll_interval_seconds: float = Field(default=0.25, gt=0, le=60)
    worker_stale_after_seconds: int = Field(default=300, ge=10, le=86_400)

    @field_validator(
        "meta_access_token",
        "meta_app_secret",
        "meta_verify_token",
        "whatsapp_phone_number_id",
        "whatsapp_graph_api_version",
        "allowed_whatsapp_wa_id",
    )
    @classmethod
    def reject_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("must not contain surrounding whitespace")
        return value

    @field_validator("whatsapp_phone_number_id", "allowed_whatsapp_wa_id")
    @classmethod
    def validate_ascii_digits(cls, value: str) -> str:
        if not value.isascii() or not value.isdigit():
            raise ValueError("must contain only ASCII digits")
        return value

    @field_validator("user_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("must be a valid IANA time zone") from error
        return value
