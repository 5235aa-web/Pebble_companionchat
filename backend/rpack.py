"""rpack.py - Pure-Python RisuAI RPack substitution & `.risum` binary container parser.

No internal project dependencies so both `charx.py` and `risum.py` can import it
safely without circular imports.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any, Dict, List, Tuple

MAX_RISUM_JSON_BYTES = 32 * 1024 * 1024
MAX_RISUM_SINGLE_ASSET_BYTES = 256 * 1024 * 1024
MAX_RISUM_ASSETS_COUNT = 10000
RPACK_MAGIC = 111
RPACK_VERSION = 0

# Official 512-byte RPack substitution table from kwaroran/RisuAI (`rpack_map.bin`):
# bytes 0..255 = encode map, bytes 256..511 = decode map.
_RPACK_MAP_B64 = (
    "xA0eC70rP1X8RW71ZlNPGuC7MJSGumu/QVBvm+/etxBhFyDfMomonW2ryZAADF2v0sFW5RZkkYJl"
    "dJfKI9ZS0f+0oOgvilg4WmAZlknb18g7PkNLpWNHqmopkvQVz2I0eNMdPOIFjipXDhvNTC3yQCwl"
    "eUgPsnq1p2w35px7VH7+h9yaAuQzouuxLgPdmaaw59WIGIN89r7hXJ/DIUYfCE7QdhJf7v2PROqj"
    "XosoCTWeacwKx4UHrUrzd+ln1NqEgJO2TXP6JyZ/BMb78XI5UcI2qWis+O3FucvOdaQ9gdlCcByV"
    "EbzYjJj5WaET9xR9s+xxwOON8AGuWzEGJCI6uCz3hIvJZfu2n66zAy0BaXQf5KPs7lw0IZNKD2ri"
    "YgKeIpz9PPxxx8atWWcFcG2KRBL6JIZfr9F6R87+UGPdUQZvGOBSqAmdVnNMuFNsw6AOGc8+DX4H"
    "MmhG6kj5mS6rpEkgXlU1OAy807FYFnkoChrh8s3EOduiumBydn2V73/IwN43lL+1FIGSJUWs5/Vm"
    "pys2WsET40s66I2DG3wnsJpC64eq3FSOeCbSVynUt/gvj4l18EF3wh7/2BUR5QSXF/Mx0JsA18q0"
    "Tyo72bJr2l2hPzBhvZE9Tubfvk2CjB0jEJhk9IUze5BDu6mI8dalHPbMbrlbC5bt1enFywimgEA="
)
_RPACK_MAP = base64.b64decode(_RPACK_MAP_B64)
_ENCODE_TABLE = _RPACK_MAP[:256]
_DECODE_TABLE = _RPACK_MAP[256:512]


class RisumError(ValueError):
    """Raised when a `.risum` module archive is malformed or oversized."""


def decode_rpack(data: bytes) -> bytes:
    """Apply the RPack decode substitution map to *data*."""
    return data.translate(_DECODE_TABLE)


def encode_rpack(data: bytes) -> bytes:
    """Apply the RPack encode substitution map to *data*."""
    return data.translate(_ENCODE_TABLE)


def parse_risum_bytes(raw: bytes) -> Tuple[Dict[str, Any], List[bytes]]:
    """Parse a binary `.risum` or plain JSON `module.json` payload.

    Returns ``(module_dict, decoded_asset_blobs)``.
    """
    if not raw:
        raise RisumError("Empty .risum payload")

    stripped = raw.lstrip()
    if stripped.startswith(b"{"):
        try:
            obj = json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RisumError(f"Invalid module JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise RisumError("Module JSON must be an object")
        mod = obj.get("module") if obj.get("type") == "risuModule" else obj
        if not isinstance(mod, dict):
            raise RisumError("Invalid risuModule payload")
        return mod, []

    if len(raw) < 6:
        raise RisumError("Truncated .risum header")
    if raw[0] != RPACK_MAGIC or raw[1] != RPACK_VERSION:
        raise RisumError("Invalid .risum magic header or unsupported version")

    pos = 2
    main_len = struct.unpack_from("<I", raw, pos)[0]
    pos += 4
    if main_len == 0 or main_len > MAX_RISUM_JSON_BYTES or pos + main_len > len(raw):
        raise RisumError("Invalid .risum main block length")

    main_json_bytes = decode_rpack(raw[pos : pos + main_len])
    pos += main_len
    try:
        envelope = json.loads(main_json_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RisumError(f"Corrupt .risum module metadata: {exc}") from exc

    if not isinstance(envelope, dict) or envelope.get("type") != "risuModule":
        raise RisumError("Archive is not a risuModule")
    module = envelope.get("module")
    if not isinstance(module, dict):
        raise RisumError("Missing module object in .risum")

    assets: List[bytes] = []
    while pos < len(raw):
        mark = raw[pos]
        pos += 1
        if mark == 0:
            break
        if mark != 1:
            raise RisumError(f"Unexpected .risum asset marker {mark}")
        if pos + 4 > len(raw):
            raise RisumError("Truncated .risum asset length")
        asset_len = struct.unpack_from("<I", raw, pos)[0]
        pos += 4
        if asset_len > MAX_RISUM_SINGLE_ASSET_BYTES or pos + asset_len > len(raw):
            raise RisumError("Invalid .risum asset size")
        if len(assets) >= MAX_RISUM_ASSETS_COUNT:
            raise RisumError("Too many assets in .risum module")
        assets.append(decode_rpack(raw[pos : pos + asset_len]))
        pos += asset_len

    return module, assets
