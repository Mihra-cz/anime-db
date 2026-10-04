"""Strict inverse of the supported canonical formatter language.

This is lexical evidence only. Supplementary formatting is non-injective:
``Foo - S01 - OVA`` can carry Season 1 or a root identity with prefix
``Foo - S01``. Every valid decomposition is retained; ordering is never
precedence. Authoritative context resolution belongs to the future P2C layer.

Only pure component primitives are shared with naming. This module does not
use the tolerant source parser, owners, scanner state or external lookups.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
import re
from typing import Literal, TypeAlias

from .physical_naming_components import component_preview, sanitize_component


class CoordinateProvenance(StrEnum):
    CANONICAL = "canonical"


SupplementaryType: TypeAlias = Literal["ova", "special", "preview", "op", "ed", "ncop", "nced"]
ParseStatus: TypeAlias = Literal["unique", "ambiguous", "not_canonical"]


@dataclass(frozen=True)
class StandardEpisodeIdentity:
    season_number: int
    part_number: int | None
    episode_number: int
    kind: Literal["episode"] = field(default="episode", init=False)
    # E is already a canonical Season/Part-local coordinate. It must never be
    # fed to a source-number API for absolute/part_local offset conversion.
    coordinate_provenance: Literal[CoordinateProvenance.CANONICAL] = field(
        default=CoordinateProvenance.CANONICAL, init=False,
    )


@dataclass(frozen=True)
class SupplementaryIdentity:
    season_number: int | None
    part_number: int | None
    supplementary_type: SupplementaryType
    ordinal: int | None
    kind: Literal["supplementary"] = field(default="supplementary", init=False)


@dataclass(frozen=True)
class RecapIdentity:
    season_number: int
    part_number: int | None
    position: Decimal
    kind: Literal["recap"] = field(default="recap", init=False)


LogicalIdentity: TypeAlias = StandardEpisodeIdentity | SupplementaryIdentity | RecapIdentity


@dataclass(frozen=True)
class CanonicalInterpretation:
    prefix: str
    logical_identity: LogicalIdentity
    media_part_number: int | None = None
    extension: str | None = None
    grammar_version: Literal["v1"] = field(default="v1", init=False)


@dataclass(frozen=True)
class CanonicalParseResult:
    alternatives: tuple[CanonicalInterpretation, ...] = ()

    @property
    def status(self) -> ParseStatus:
        if not self.alternatives:
            return "not_canonical"
        return "unique" if len(self.alternatives) == 1 else "ambiguous"


# Exact f"{positive_int:02d}" output: width is a minimum, never extra zeros.
_INTEGER = r"(?:0[1-9]|[1-9][0-9]+)"
_STRUCTURE = rf"S(?P<season>{_INTEGER})(?:P(?P<part>{_INTEGER}))?"
_MP = rf"(?:-MP(?P<mp>{_INTEGER}))?"
_SCOPE = re.compile(_STRUCTURE)
_EPISODE = re.compile(_STRUCTURE + rf"E(?P<episode>{_INTEGER})" + _MP)
_SUPPLEMENTARY = re.compile(
    rf"(?P<label>OVA|Special|Preview|OP|ED|NCOP|NCED)(?: (?P<ordinal>{_INTEGER}))?" + _MP,
)
# The formatter emits fixed Decimal notation, strips fractional trailing zeros
# and requires > 0. Integer leading zeros are never emitted.
_RECAP = re.compile(r"Recap (?P<position>(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?)" + _MP)
_EXTENSION = re.compile(r"\.[a-z0-9]+")


def _optional_number(match: re.Match[str], group: str) -> int | None:
    text = match.group(group)
    return int(text) if text is not None else None


def _valid_prefix(prefix: str) -> bool:
    # Recognize sanitized output without rewriting the supplied evidence.
    return sanitize_component(prefix).component == prefix


def _result(alternatives: list[CanonicalInterpretation]) -> CanonicalParseResult:
    # Distinct decompositions have distinct prefix boundaries. Lexical prefix
    # ordering is solely for deterministic serialization, never a winner rule.
    return CanonicalParseResult(tuple(sorted(alternatives, key=lambda item: item.prefix)))


def parse_canonical_stem(stem: str) -> CanonicalParseResult:
    """Recognize an entire sanitized stem; no extension or source heuristics.

    All supported suffixes end after the last `` - `` boundary. Supplementary
    and Recap may additionally consume exactly one preceding Season/Part scope.
    Fullmatches at these boundaries exhaust the grammar's decompositions.
    """
    if not isinstance(stem, str):
        raise TypeError("Canonical stem must be a string.")
    if not component_preview(stem).valid:
        return CanonicalParseResult()
    prefix, separator, suffix = stem.rpartition(" - ")
    if not separator:
        return CanonicalParseResult()

    if match := _EPISODE.fullmatch(suffix):
        if not _valid_prefix(prefix):
            return CanonicalParseResult()
        return CanonicalParseResult((CanonicalInterpretation(
            prefix,
            StandardEpisodeIdentity(int(match["season"]), _optional_number(match, "part"), int(match["episode"])),
            _optional_number(match, "mp"),
        ),))

    scoped_prefix, scope_separator, scope_text = prefix.rpartition(" - ")
    scope = _SCOPE.fullmatch(scope_text) if scope_separator else None
    if match := _SUPPLEMENTARY.fullmatch(suffix):
        alternatives = []
        content_type: SupplementaryType = match["label"].lower()
        ordinal, mp = _optional_number(match, "ordinal"), _optional_number(match, "mp")
        if _valid_prefix(prefix):
            alternatives.append(CanonicalInterpretation(
                prefix, SupplementaryIdentity(None, None, content_type, ordinal), mp,
            ))
        if scope is not None and _valid_prefix(scoped_prefix):
            alternatives.append(CanonicalInterpretation(
                scoped_prefix,
                SupplementaryIdentity(int(scope["season"]), _optional_number(scope, "part"), content_type, ordinal),
                mp,
            ))
        return _result(alternatives)

    if match := _RECAP.fullmatch(suffix):
        position = Decimal(match["position"])
        if position > 0 and scope is not None and _valid_prefix(scoped_prefix):
            return CanonicalParseResult((CanonicalInterpretation(
                scoped_prefix,
                RecapIdentity(int(scope["season"]), _optional_number(scope, "part"), position),
                _optional_number(match, "mp"),
            ),))
    return CanonicalParseResult()


def parse_canonical_filename(filename: str, *, extension: str) -> CanonicalParseResult:
    """Strip one explicit canonical lowercase extension and parse the stem.

    The caller supplies the dot-prefixed extension: no media allowlist, codec
    guessing or last-dot heuristic (Recap positions contain decimal dots).
    The final filename must also fit the formatter's portable component budget.
    """
    if not isinstance(filename, str):
        raise TypeError("Canonical filename must be a string.")
    if (not isinstance(extension, str) or not _EXTENSION.fullmatch(extension)
            or not filename.endswith(extension) or not component_preview(filename).valid):
        return CanonicalParseResult()
    stem = parse_canonical_stem(filename[:-len(extension)])
    return CanonicalParseResult(tuple(replace(item, extension=extension) for item in stem.alternatives))
