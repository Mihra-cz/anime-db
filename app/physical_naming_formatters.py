"""Canonical tokens from resolved identity values; no filename parsing or writes."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re

from .physical_naming_components import (
    ComponentDiagnostic, ComponentPreview, SanitizedComponent, component_preview,
)


_EXTENSION = re.compile(r"\.?[A-Za-z0-9]+\Z")
_SUPPLEMENTARY_LABELS = {
    "ova": "OVA", "special": "Special", "preview": "Preview", "op": "OP",
    "ed": "ED", "ncop": "NCOP", "nced": "NCED", "recap": "Recap",
}


@dataclass(frozen=True)
class EpisodeIdentity:
    season: int
    episode: int
    part: int | None = None
    media_part: int | None = None


@dataclass(frozen=True)
class SupplementaryIdentity:
    content_type: str
    season: int | None = None
    part: int | None = None
    ordinal: int | None = None
    recap_position: Decimal | int | None = None
    media_part: int | None = None


@dataclass(frozen=True)
class FormattedComponent(ComponentPreview):
    prefix: SanitizedComponent | None


@dataclass(frozen=True)
class NormalizedExtension:
    original: str
    extension: str | None
    diagnostics: tuple[ComponentDiagnostic, ...]

    @property
    def valid(self) -> bool:
        return self.extension is not None


def normalize_extension(extension: str) -> NormalizedExtension:
    """One separate ASCII suffix, with an optional leading dot; never guessed."""
    if not isinstance(extension, str) or not _EXTENSION.fullmatch(extension):
        return NormalizedExtension(extension, None, (ComponentDiagnostic("invalid_extension", "error"),))
    return NormalizedExtension(extension, "." + extension.lstrip(".").lower(), ())


def _result(preview: str | None, prefix: SanitizedComponent | None = None,
            diagnostics: tuple[ComponentDiagnostic, ...] = ()) -> FormattedComponent:
    measured = component_preview(preview, diagnostics)
    return FormattedComponent(
        measured.preview_text, measured.component, measured.chars, measured.utf8_bytes,
        measured.utf16_units, measured.diagnostics, prefix,
    )


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def _optional_int(value) -> bool:
    return value is None or _positive_int(value)


def _structure(season: int | None, part: int | None) -> str:
    return (f"S{season:02d}" if season is not None else "") + (f"P{part:02d}" if part is not None else "")


def _with_prefix(prefix: SanitizedComponent, suffix: str, extension: str) -> FormattedComponent:
    normalized = normalize_extension(extension)
    if not normalized.valid:
        return _result(None, prefix, normalized.diagnostics)
    # Keep a full preview for byte-overflow prefixes and measure the assembled
    # filename. Other invalid prefixes must never become identity-only names.
    errors = tuple(d for d in prefix.diagnostics if d.severity == "error" and d.code != "component_byte_limit")
    if errors or not prefix.preview_text:
        return _result(None, prefix, errors or (ComponentDiagnostic("invalid_prefix", "error"),))
    diagnostics = tuple(d for d in prefix.diagnostics if d.code != "component_byte_limit")
    return _result(prefix.preview_text + " - " + suffix + normalized.extension, prefix, diagnostics)


def format_root_component(root: SanitizedComponent) -> FormattedComponent:
    return _result(root.preview_text, root, root.diagnostics)


def format_season_directory(season: int) -> FormattedComponent:
    if not _positive_int(season):
        return _result(None, diagnostics=(ComponentDiagnostic("invalid_identity", "error"),))
    return _result(f"Season {season:02d}")


def format_episode_component(
    prefix: SanitizedComponent, identity: EpisodeIdentity, extension: str,
) -> FormattedComponent:
    if not (_positive_int(identity.season) and _positive_int(identity.episode)
            and _optional_int(identity.part) and _optional_int(identity.media_part)):
        return _result(None, prefix, (ComponentDiagnostic("invalid_identity", "error"),))
    suffix = _structure(identity.season, identity.part) + f"E{identity.episode:02d}"
    if identity.media_part is not None:
        suffix += f"-MP{identity.media_part:02d}"
    return _with_prefix(prefix, suffix, extension)


def format_supplementary_component(
    prefix: SanitizedComponent, identity: SupplementaryIdentity, extension: str,
) -> FormattedComponent:
    label = _SUPPLEMENTARY_LABELS.get(identity.content_type)
    if label is None:
        # Film/Bonus/CM/Menu, variants and duplicate disposition remain open.
        return _result(None, prefix, (ComponentDiagnostic("unsupported_identity", "error"),))
    position = identity.recap_position
    is_recap = identity.content_type == "recap"
    if not (
        _optional_int(identity.season) and _optional_int(identity.part)
        and (identity.part is None or identity.season is not None)
        and _optional_int(identity.ordinal) and _optional_int(identity.media_part)
        and (
            identity.season is not None and identity.ordinal is None
            and (type(position) is int or isinstance(position, Decimal))
            and (not isinstance(position, Decimal) or position.is_finite()) and position > 0
            if is_recap else position is None
        )
    ):
        return _result(None, prefix, (ComponentDiagnostic("invalid_identity", "error"),))
    structure = _structure(identity.season, identity.part)
    suffix = (structure + " - " if structure else "") + label
    if is_recap:
        number = format(position, "f") if isinstance(position, Decimal) else str(position)
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        suffix += " " + number
    elif identity.ordinal is not None:
        suffix += f" {identity.ordinal:02d}"
    if identity.media_part is not None:
        suffix += f"-MP{identity.media_part:02d}"
    return _with_prefix(prefix, suffix, extension)
