"""Validation of registered metadata (route templates, service names).

Syntax checks reduce, but cannot eliminate, the chance that free text contains sensitive
data; registered templates must still be reviewed manually (see docs/OPERATIONS.md).
"""

from __future__ import annotations

import re

MAX_TEMPLATE_LEN = 200
MAX_SEGMENTS = 20

# Allowlist: path characters plus {param} braces and ':' for custom verbs. This alone
# excludes '%', '?', '#', '@', whitespace, quotes and control characters.
_TEMPLATE_CHARS = re.compile(r"^/[A-Za-z0-9\-._~/{}:]*$")
_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]{0,31})\}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_LONG_DIGITS = re.compile(r"\d{5,}")
_LONG_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()/-]{0,79}$")
_ENVIRONMENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


def template_violations(template: str) -> list[str]:
    codes: list[str] = []
    if not isinstance(template, str) or not template:
        return ["required"]
    if len(template) > MAX_TEMPLATE_LEN:
        return ["too_long"]
    if not template.isascii() or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in template):
        return ["invalid_characters"]
    if _SCHEME.match(template) or "//" in template:
        codes.append("absolute_url")
    if any(ch in template for ch in "?#%"):
        codes.append("query_or_escape")
    if not _TEMPLATE_CHARS.match(template):
        codes.append("invalid_characters")
    segments = [seg for seg in template.split("/")[1:]]
    if len(segments) > MAX_SEGMENTS:
        codes.append("too_many_segments")
    if template != "/" and any(seg == "" for seg in segments[:-1]):
        codes.append("empty_segment")
    # Braces must form well-named parameters only.
    stripped = _PARAM.sub("", template)
    if "{" in stripped or "}" in stripped:
        codes.append("invalid_parameter")
    literal = _PARAM.sub("/", template)
    if "@" in template:
        codes.append("contains_email")
    if _UUID.search(literal):
        codes.append("contains_uuid")
    if _LONG_DIGITS.search(literal):
        codes.append("contains_numeric_id")
    if _LONG_HEX.search(literal):
        codes.append("contains_identifier")
    for seg in literal.split("/"):
        if len(seg) >= 24 and re.search(r"[A-Za-z]", seg) and re.search(r"\d", seg):
            codes.append("contains_identifier")
            break
    return sorted(set(codes))


def service_name_violations(name: str) -> list[str]:
    if not isinstance(name, str) or not name.strip():
        return ["required"]
    if len(name) > 80:
        return ["too_long"]
    if not _SERVICE_NAME.match(name) or name != name.strip():
        return ["invalid_characters"]
    if _UUID.search(name) or _LONG_DIGITS.search(name):
        return ["contains_identifier"]
    return []


def environment_violations(env: str) -> list[str]:
    if not isinstance(env, str) or not env:
        return ["required"]
    if len(env) > 32:
        return ["too_long"]
    if not _ENVIRONMENT.match(env):
        return ["invalid_characters"]
    return []
