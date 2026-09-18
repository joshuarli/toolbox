from __future__ import annotations

import re


def profile_arg(value: str, *, command: str) -> str:
    if not value.startswith("@") or len(value) == 1:
        raise ValueError(f"{command} expects @profile")
    username = value[1:]
    if not re.fullmatch(r"[A-Za-z0-9._]+", username):
        raise ValueError(f"{command} expects @profile")
    return username
