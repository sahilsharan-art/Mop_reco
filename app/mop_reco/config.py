"""Load and validate app/config.yaml.

Stage 0 only: this module loads the config, checks its shape is sane, and
resolves source paths on disk. It does not read any source *data* — that's
later stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# app/mop_reco/config.py -> app/mop_reco -> app -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "app" / "config.yaml"

REQUIRED_TOP_LEVEL_KEYS = (
    "base_dir",
    "sources",
    "mop_classification",
    "settlement_cycles",
    "tolerance",
)

REQUIRED_SETTLEMENT_CYCLE_KEYS = ("paytm", "axis", "bajaj", "cms")


class ConfigError(ValueError):
    """Raised when config.yaml is missing, malformed, or breaks a non-negotiable rule."""


def _skip_reason(path: Path) -> Optional[str]:
    """Files that are never data. Returns why, so it can be surfaced as a warning
    rather than silently dropped — a '~$' lock file in particular means the real
    file is open in Excel right now and whatever we read from it may be mid-edit."""
    name = path.name
    if name.startswith("~$"):
        return "Office lock file — the real file may be open and mid-edit in Excel right now"
    if path.suffix.lower() == ".tmp":
        return "temp file"
    if name.startswith("."):
        return "dotfile"
    return None


@dataclass(frozen=True)
class SourceStatus:
    name: str
    configured_path: Optional[str]
    resolved_path: Optional[Path]
    exists: bool
    file_count: int
    files: tuple  # tuple[Path, ...] — top-level files only, no recursion
    warnings: tuple  # tuple[str, ...] — one per skipped file, never silent


@dataclass(frozen=True)
class Config:
    base_dir: Path
    sources: dict  # name -> Optional[Path], already resolved (or None if not configured)
    mop_classification: dict
    settlement_cycles: dict
    tolerance: float
    encodings: dict  # source name -> declared text encoding (only for text-based formats)
    raw: dict  # the untouched parsed YAML, for anything not modelled above yet

    def encoding_for(self, source_name: str) -> Optional[str]:
        """The declared encoding for a source, or None if it doesn't need one
        (e.g. .xlsx/.xlsb carry their own). Never guessed — see io_.read_any."""
        return self.encodings.get(source_name)

    def source_status(self, name: str) -> SourceStatus:
        if name not in self.sources:
            raise ConfigError(f"Unknown source '{name}' — not in config.yaml sources map")
        resolved = self.sources[name]
        if resolved is None:
            return SourceStatus(
                name=name, configured_path=None, resolved_path=None,
                exists=False, file_count=0, files=(), warnings=(),
            )
        exists = resolved.is_dir()
        files = []
        warnings = []
        if exists:
            # Depth-1 discovery (CLAUDE.md section 4): the source root plus its
            # immediate subdirectories, never deeper. Month folders sit one level
            # down ("July", "July'26", "Aug'26"); the folder NAME is never
            # interpreted — it is a container only, and the business date always
            # comes from a data column. A non-recursive scan silently saw only
            # the loose single-day files and would have kept reconciling one day
            # while reporting a month.
            candidates = list(resolved.iterdir())
            for sub in sorted(p for p in candidates if p.is_dir()):
                candidates.extend(sub.iterdir())
            for p in sorted(p for p in candidates if p.is_file()):
                reason = _skip_reason(p)
                if reason is not None:
                    warnings.append(f"{p.relative_to(resolved)}: skipped — {reason}")
                else:
                    files.append(p)
        return SourceStatus(
            name=name,
            configured_path=str(resolved),
            resolved_path=resolved,
            exists=exists,
            file_count=len(files),
            files=tuple(files),
            warnings=tuple(warnings),
        )

    def all_source_statuses(self) -> list:
        return [self.source_status(name) for name in self.sources]


def load_config(path: Optional[Path] = None) -> Config:
    path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} did not parse to a mapping — got {type(raw).__name__}")

    missing = [k for k in REQUIRED_TOP_LEVEL_KEYS if k not in raw]
    if missing:
        raise ConfigError(f"{path} is missing required key(s): {', '.join(missing)}")

    # base_dir is relative to the config file's own project root unless absolute.
    base_dir = Path(raw["base_dir"])
    if not base_dir.is_absolute():
        base_dir = (PROJECT_ROOT / base_dir).resolve()

    sources_raw = raw["sources"]
    if not isinstance(sources_raw, dict) or not sources_raw:
        raise ConfigError(f"{path}: 'sources' must be a non-empty mapping")

    sources = {}
    for name, rel_path in sources_raw.items():
        if rel_path is None:
            sources[name] = None
        elif isinstance(rel_path, str):
            sources[name] = (base_dir / rel_path).resolve()
        else:
            raise ConfigError(
                f"{path}: source '{name}' must be a string path or null, got {rel_path!r}"
            )

    mop_classification = raw["mop_classification"]
    if not isinstance(mop_classification, dict) or not mop_classification:
        raise ConfigError(f"{path}: 'mop_classification' must be a non-empty mapping")
    for mop, entry in mop_classification.items():
        if not isinstance(entry, dict) or "class" not in entry or "reconciled_in" not in entry:
            raise ConfigError(
                f"{path}: mop_classification['{mop}'] must have 'class' and 'reconciled_in'"
            )

    settlement_cycles = raw["settlement_cycles"]
    if not isinstance(settlement_cycles, dict):
        raise ConfigError(f"{path}: 'settlement_cycles' must be a mapping")
    missing_cycles = [k for k in REQUIRED_SETTLEMENT_CYCLE_KEYS if k not in settlement_cycles]
    if missing_cycles:
        raise ConfigError(
            f"{path}: 'settlement_cycles' is missing key(s): {', '.join(missing_cycles)}"
        )

    tolerance = raw["tolerance"]
    if tolerance != 0:
        # CLAUDE.md non-negotiable rule 1: nil tolerance, exact match only.
        raise ConfigError(
            f"{path}: tolerance must be 0 (non-negotiable rule 1) — got {tolerance!r}"
        )

    encodings = raw.get("encodings") or {}
    if not isinstance(encodings, dict):
        raise ConfigError(f"{path}: 'encodings' must be a mapping if present")
    for source_name, enc in encodings.items():
        if not isinstance(enc, str):
            raise ConfigError(f"{path}: encodings['{source_name}'] must be a string, got {enc!r}")

    return Config(
        base_dir=base_dir,
        sources=sources,
        mop_classification=mop_classification,
        settlement_cycles=settlement_cycles,
        tolerance=float(tolerance),
        encodings=encodings,
        raw=raw,
    )
