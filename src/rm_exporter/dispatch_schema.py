"""Shared dispatch contract for the HTTP bridge and polling queue."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DispatchLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tracking_number: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    tracking_url: str = Field(default="", max_length=2048)
    label_url: str = Field(default="", max_length=2048)

    @field_validator("tracking_url", "label_url")
    @classmethod
    def http_url(cls, value: str) -> str:
        if value:
            try:
                parsed = urlsplit(value)
                if (
                    parsed.scheme in ("https", "http")
                    and parsed.hostname
                    and not parsed.username
                ):
                    return value
            except ValueError:
                pass
            raise ValueError("URL must be an absolute HTTP(S) URL without credentials")
        return value


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(pattern=r"^order_[A-Za-z0-9]+$", max_length=100)
    # Explicitly excludes label-generation events from this contract.
    status: Literal["despatched"]
    fulfillment_id: str | None = Field(
        default=None, pattern=r"^ful_[A-Za-z0-9]+$", max_length=100
    )
    labels: list[DispatchLabel] | None = Field(default=None, max_length=100)
