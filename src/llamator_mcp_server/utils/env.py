from __future__ import annotations

import json
from typing import Any


def parse_system_prompts(raw: str) -> tuple[str, ...] | None:
    """
    Parse system prompts from an env value.

    Accepts JSON array (preferred) or a newline-separated string.

    :param raw: Environment raw value.
    :return: A tuple of prompts or None.
    :raises ValueError: If JSON does not represent a list of strings.
    """
    val: str = str(raw).strip()
    if not val:
        return None

    if val.startswith("["):
        parsed: Any
        try:
            parsed = json.loads(val)
        except json.JSONDecodeError as e:
            raise ValueError("Invalid JSON for system prompts.") from e
        if not isinstance(parsed, list):
            raise ValueError("System prompts JSON must be an array.")
        if any(not isinstance(x, str) for x in parsed):
            raise ValueError("System prompts JSON must be an array of strings.")
        cleaned_json: list[str] = [x.strip() for x in parsed if x.strip()]
        return tuple(cleaned_json) or None

    parts: list[str] = [p.strip() for p in val.splitlines() if p.strip()]
    return tuple(parts) or None
