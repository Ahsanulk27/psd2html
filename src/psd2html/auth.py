"""Shared-secret check for the API.

Palette's server is the only intended caller. It sends the secret in the
`X-Psd2html-Key` header. With no secret configured the API is open, which is
fine on a developer's machine and refused in production (`PSD2HTML_ENV`).
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException

HEADER = "X-Psd2html-Key"


def configured_secret() -> str | None:
    secret = (os.environ.get("PSD2HTML_SHARED_SECRET") or "").strip()
    return secret or None


def check_startup() -> None:
    if os.environ.get("PSD2HTML_ENV", "development").lower() == "production" and not configured_secret():
        raise RuntimeError("PSD2HTML_SHARED_SECRET must be set when PSD2HTML_ENV=production.")


def require_key(x_psd2html_key: str | None = Header(default=None, alias=HEADER)) -> None:
    secret = configured_secret()
    if secret is None:
        return
    if not x_psd2html_key or not hmac.compare_digest(x_psd2html_key, secret):
        raise HTTPException(401, "Missing or wrong API key.")
