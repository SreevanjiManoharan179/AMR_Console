"""Load the named-location registry used by typed commands to named places.

The exact schema of config/named_locations.yaml was not visible to the author
of this module, so the loader accepts the common shapes and fails loudly on
anything else. Integration step: run `python3 -m grounding_calibration.registry <path>` on
the real file and confirm it prints the four approved locations and their
aliases before trusting any result.

Accepted shapes (top level):
  {locations: [...]} / {named_locations: ...} / {entries: ...} / {places: ...}
  a mapping of location keys to location data (non-location keys skipped)
  a list of location mappings
Each location may carry: name / id / key, aliases (list or string),
and a pose as x/y, pose: {x, y} or [x, y], position: {x, y}, or xy: [x, y].
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable

import yaml

NON_LOCATION_KEYS = {
    "approval_status", "approved", "approved_by", "approved_at", "status",
    "frame_id", "frame", "map", "map_id", "map_file", "map_name", "version",
    "units", "notes", "note", "metadata", "description", "proposed_by",
    "updated", "updated_at", "created", "created_at", "source",
}


@dataclass(frozen=True)
class Entry:
    key: str                 # the identifier as written in the file
    name: str                # display name: lower case, spaces for _ and -
    aliases: tuple[str, ...]
    x: float | None = None
    y: float | None = None

    @property
    def variants(self) -> tuple[str, ...]:
        """The name and every alias, each a way the user may refer to it."""
        out = [self.name]
        for a in self.aliases:
            if a not in out:
                out.append(a)
        return tuple(out)


def display_name(key: Any) -> str:
    return re.sub(r"[_\-]+", " ", str(key)).strip().lower()


def _as_aliases(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    return tuple(display_name(v) for v in value if str(v).strip())


def _pose(d: Any) -> tuple[float | None, float | None]:
    if isinstance(d, (list, tuple)) and len(d) >= 2 and all(isinstance(v, (int, float)) for v in d[:2]):
        return float(d[0]), float(d[1])
    if not isinstance(d, dict):
        return None, None
    if "x" in d and "y" in d:
        return float(d["x"]), float(d["y"])
    for k in ("pose", "position", "xy", "goal"):
        if k in d:
            x, y = _pose(d[k])
            if x is not None:
                return x, y
    return None, None


def _entry(key: Any, val: Any) -> Entry:
    if isinstance(val, dict):
        name = val.get("name") or val.get("label") or key
        aliases = _as_aliases(val.get("aliases") or val.get("alias") or val.get("synonyms"))
    else:
        name, aliases = key, ()
    x, y = _pose(val)
    return Entry(key=str(key), name=display_name(name), aliases=aliases, x=x, y=y)


def parse_registry(data: Any) -> list[Entry]:
    candidates: Any = None
    if isinstance(data, dict):
        for k in ("locations", "named_locations", "entries", "places"):
            if k in data:
                candidates = data[k]
                break
        if candidates is None:
            candidates = {k: v for k, v in data.items()
                          if str(k) not in NON_LOCATION_KEYS and isinstance(v, (dict, list))}
    elif isinstance(data, list):
        candidates = data
    entries: list[Entry] = []
    if isinstance(candidates, dict):
        for key, val in candidates.items():
            if str(key) in NON_LOCATION_KEYS:
                continue
            entries.append(_entry(key, val))
    elif isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict):
                raise ValueError(f"registry list item is not a mapping: {item!r}")
            key = item.get("name") or item.get("id") or item.get("key")
            if key is None:
                raise ValueError(f"registry item has no name/id/key: {item!r}")
            entries.append(_entry(key, item))
    else:
        raise ValueError("unrecognised registry structure; extend parse_registry for this schema")
    if not entries:
        raise ValueError("registry holds no locations")
    names = [e.name for e in entries]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate location names: {names}")
    return entries


def load_registry(path: str) -> list[Entry]:
    with open(path, encoding="utf-8") as f:
        return parse_registry(yaml.safe_load(f))


def describe(entries: Iterable[Entry]) -> str:
    lines = []
    for e in entries:
        pose = f"({e.x:g}, {e.y:g})" if e.x is not None else "(no pose read)"
        alias = ", ".join(e.aliases) if e.aliases else "none"
        lines.append(f"{e.name:<20} key={e.key:<16} pose={pose:<14} aliases: {alias}")
    return "\n".join(lines)


if __name__ == "__main__":  # integration check on the real file
    if len(sys.argv) != 2:
        sys.exit("usage: python3 -m grounding_calibration.registry <path/to/named_locations.yaml>")
    print(describe(load_registry(sys.argv[1])))
