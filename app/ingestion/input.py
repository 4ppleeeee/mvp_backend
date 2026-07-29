"""Normalize pasted share text into a single public URL."""

import re


_HTTP_URL = re.compile(r"(https?://[^\s，。；、]+)", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;)]}\"'，。；、）】》」』"


def extract_first_http_url(raw_text: str) -> str:
    """Extract the first URL from either a direct URL or a share message."""

    match = _HTTP_URL.search(raw_text)
    if match is None:
        raise ValueError("input must include an HTTP or HTTPS URL")
    url = match.group(1).rstrip(_TRAILING_PUNCTUATION)
    if not url:
        raise ValueError("input must include an HTTP or HTTPS URL")
    return url
