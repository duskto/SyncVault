"""Portable logical names; never use these names as server storage paths."""

import re
import unicodedata

RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
}


def logical_path(value: str) -> str:
    if not value or len(value.encode("utf-8")) > 1024 or value.startswith("/") or "\\" in value:
        raise ValueError("路径必须是使用 / 的非空相对路径")
    for part in value.split("/"):
        if (
            part in ("", ".", "..")
            or len(part.encode("utf-8")) > 255
            or part.endswith((" ", "."))
            or any(ord(c) < 32 or c in '<>:"|?*\x7f' for c in part)
            or part.split(".")[0].upper() in RESERVED
        ):
            raise ValueError(f"不可移植的路径分量: {part!r}")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("路径必须采用 Unicode NFC 规范形式")
    return value


def path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def namespace(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("namespace 必须为 1–64 个字母、数字或 _.-，并以字母或数字开头")
    return value
