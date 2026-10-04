"""Strict formatter inverse: lexical evidence, never owner/source resolution."""
from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext
import importlib
import importlib.util
from itertools import product
from pathlib import Path
import subprocess
import sys

import pytest

from app import physical_naming_components as components
from app import physical_naming_formatters as formatter


LABELS = (
    ("ova", "OVA"), ("special", "Special"), ("preview", "Preview"),
    ("op", "OP"), ("ed", "ED"), ("ncop", "NCOP"), ("nced", "NCED"),
)
PREFIXES = (
    "Anime S2", "Project P2", "E3", "Part 2", "Season 2", "OVA", "Recap",
    "MP01", "Foo S02P02E03 Bar", "Foo - S01", "Foo - S01P02",
)


def parser_api():
    assert importlib.util.find_spec("app.canonical_filename") is not None, (
        "P2B pure canonical filename parser core is missing"
    )
    return importlib.import_module("app.canonical_filename")


@pytest.mark.parametrize("suffix,season,part,episode,mp", [
    ("S01E01", 1, None, 1, None),
    ("S02P02E03", 2, 2, 3, None),
    ("S01E05-MP01", 1, None, 5, 1),
    ("S01E05-MP02", 1, None, 5, 2),
    ("S02P02E03-MP01", 2, 2, 3, 1),
    ("S100P101E123-MP102", 100, 101, 123, 102),
])
def test_standard_part_and_media_part_are_separate_axes(suffix, season, part, episode, mp):
    api = parser_api()
    result = api.parse_canonical_filename(f"Foo - {suffix}.mkv", extension=".mkv")
    assert result.status == "unique"
    (parsed,) = result.alternatives
    assert parsed.prefix == "Foo"
    assert parsed.extension == ".mkv"
    assert parsed.grammar_version == "v1"
    assert parsed.media_part_number == mp
    assert parsed.logical_identity == api.StandardEpisodeIdentity(season, part, episode)
    assert parsed.logical_identity.kind == "episode"
    assert parsed.logical_identity.coordinate_provenance is api.CoordinateProvenance.CANONICAL


def test_media_parts_share_logical_identity():
    api = parser_api()
    first, = api.parse_canonical_stem("Foo - S01E05-MP01").alternatives
    second, = api.parse_canonical_stem("Foo - S01E05-MP02").alternatives
    assert first.logical_identity == second.logical_identity
    assert (first.media_part_number, second.media_part_number) == (1, 2)


def test_supplementary_media_parts_share_each_lexical_identity():
    api = parser_api()
    results = [api.parse_canonical_stem(f"Show - S02 - OVA 01-MP{mp:02d}") for mp in (1, 2)]
    assert all(result.status == "ambiguous" for result in results)
    for result, mp in zip(results, (1, 2), strict=True):
        assert {(p.prefix, p.logical_identity) for p in result.alternatives} == {
            ("Show", api.SupplementaryIdentity(2, None, "ova", 1)),
            ("Show - S02", api.SupplementaryIdentity(None, None, "ova", 1)),
        }
        assert all(p.media_part_number == mp for p in result.alternatives)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_prefix_tokens_are_evidence_only_and_standard_suffix_is_unique(prefix):
    api = parser_api()
    result = api.parse_canonical_stem(prefix + " - S02P02E03-MP01")
    assert result.status == "unique"
    parsed, = result.alternatives
    assert parsed.prefix == prefix
    assert parsed.logical_identity == api.StandardEpisodeIdentity(2, 2, 3)
    assert parsed.media_part_number == 1
    assert api.parse_canonical_stem(prefix).status == "not_canonical"


@pytest.mark.parametrize("kind,label", LABELS)
@pytest.mark.parametrize("ordinal,number", [(None, ""), (1, " 01"), (2, " 02"), (100, " 100")])
def test_root_supplementary_preserves_optional_ordinal(kind, label, ordinal, number):
    api = parser_api()
    result = api.parse_canonical_stem("Foo - " + label + number)
    assert result.status == "unique"
    parsed, = result.alternatives
    assert parsed.logical_identity == api.SupplementaryIdentity(None, None, kind, ordinal)
    assert parsed.logical_identity.kind == "supplementary"
    assert parsed.media_part_number is None


@pytest.mark.parametrize("kind,label", LABELS)
@pytest.mark.parametrize("scope,season,part", [("S01", 1, None), ("S01P02", 1, 2), ("S100P101", 100, 101)])
def test_scoped_supplementary_returns_every_lexical_alternative_without_precedence(kind, label, scope, season, part):
    api = parser_api()
    stem = f"Foo - {scope} - {label} 01-MP01"
    result = api.parse_canonical_stem(stem)
    assert result.status == "ambiguous"
    assert isinstance(result.alternatives, tuple)
    assert [(p.prefix, p.logical_identity, p.media_part_number) for p in result.alternatives] == [
        ("Foo", api.SupplementaryIdentity(season, part, kind, 1), 1),
        (f"Foo - {scope}", api.SupplementaryIdentity(None, None, kind, 1), 1),
    ]
    assert result == api.parse_canonical_stem(stem)


def test_singleton_season_ova_has_no_implicit_ordinal():
    api = parser_api()
    result = api.parse_canonical_filename("Foo - S01 - OVA.mkv", extension=".mkv")
    assert result.status == "ambiguous"
    assert {p.logical_identity for p in result.alternatives} == {
        api.SupplementaryIdentity(1, None, "ova", None),
        api.SupplementaryIdentity(None, None, "ova", None),
    }
    assert all(p.extension == ".mkv" for p in result.alternatives)


def test_only_immediately_preceding_scope_can_be_a_supplementary_suffix():
    api = parser_api()
    result = api.parse_canonical_stem("Foo - S01 - S02P03 - OVA")
    assert result.status == "ambiguous"
    assert [(p.prefix, p.logical_identity) for p in result.alternatives] == [
        ("Foo - S01", api.SupplementaryIdentity(2, 3, "ova", None)),
        ("Foo - S01 - S02P03", api.SupplementaryIdentity(None, None, "ova", None)),
    ]


@pytest.mark.parametrize("scope", ["s01", "S1", "S001", "S00", "P02", "S01P00", "S01p02"])
def test_invalid_scope_can_still_be_literal_root_prefix(scope):
    api = parser_api()
    result = api.parse_canonical_stem(f"Foo - {scope} - OVA")
    assert result.status == "unique"
    parsed, = result.alternatives
    assert parsed.prefix == f"Foo - {scope}"
    assert parsed.logical_identity == api.SupplementaryIdentity(None, None, "ova", None)


@pytest.mark.parametrize("text", ["12", "12.5", "24.25", "0.5", "12.05", "100", "0.00001"])
@pytest.mark.parametrize("scope,season,part", [("S01", 1, None), ("S02P02", 2, 2)])
def test_recap_is_exact_decimal_with_unique_season_scope(text, scope, season, part):
    api = parser_api()
    with localcontext() as context:
        context.prec = 1
        result = api.parse_canonical_stem(f"Foo - {scope} - Recap {text}-MP02")
    assert result.status == "unique"
    parsed, = result.alternatives
    assert parsed.logical_identity == api.RecapIdentity(season, part, Decimal(text))
    assert parsed.logical_identity.kind == "recap"
    assert isinstance(parsed.logical_identity.position, Decimal)
    assert parsed.media_part_number == 2


@pytest.mark.parametrize("text", [
    "12.500", "12.0", "12.050", "01", "1.0", "1e1", "1E+1", "12,5",
    "-1", "+1", ".5", "5.", "NaN", "sNaN", "Infinity", "0", "0.0", "00.5",
])
def test_recap_rejects_non_formatter_decimal_spellings(text):
    api = parser_api()
    assert api.parse_canonical_stem("Foo - S01 - Recap " + text).status == "not_canonical"


@pytest.mark.parametrize("stem", [
    "Foo - s01E01", "Foo - S01e01", "Foo-S01E01", "Foo -S01E01", "Foo- S01E01",
    "Foo  - S01E01", "Foo -  S01E01", "Foo - S1E01", "Foo - S01E1", "Foo - S001E01",
    "Foo - S01E001", "Foo - S099E01", "Foo - S00E01", "Foo - S01P00E01", "Foo - S01E00",
    "Foo - S-1E01", "Foo - S01P-1E01", "Foo - S01E-1", "Foo - S01P1E01", "Foo - S01P001E01",
    "Foo - S01E01-MP00", "Foo - S01E01-MP1", "Foo - S01E01-MP001", "Foo - S01E01-mp01",
    "Foo - S01E01-MP-1", "Foo - S01E01-MP01-MP02", "Foo - S01E01 MP01", "Foo - S01E01-junk",
    "Foo - S01E01 trailing", "Foo - S01E01\n", "Foo - S01E01.mkv", "S01E01", " - S01E01",
    "", "Foo - OVA 1", "Foo - OVA 001", "Foo - OVA 00", "Foo - OVA -1", "Foo - OVA 01 junk",
    "Foo - ova", "Foo - SPECIAL", "Foo - Preview01", "Foo - OVA-MP00", "Foo - OVA-MP01-MP02",
    "Foo - Recap 12.5", "Foo - S1 - Recap 12.5", "Foo - S01P00 - Recap 12.5",
    " - S01 - Recap 12.5", " - OVA", " / - S01E01", "Foo/Bar - S01E01", "Foo:Bar - S01E01",
    "Foo  Bar - S01E01", " Foo - S01E01", "Foo. - S01E01", ".Foo - S01E01", "CON - S01E01",
    "Cafe\u0301 - S01E01", "Foo\u00a0Bar - S01E01", "Foo\u200bBar - S01E01", "Foo\ud800 - S01E01",
    "Foo - S０１E01", "Foo - S01E٠١", "a" * 255 + " - S01E01",
])
def test_malformed_or_unsanitized_whole_stem_is_not_canonical(stem):
    api = parser_api()
    result = api.parse_canonical_stem(stem)
    assert result.status == "not_canonical"
    assert result.alternatives == ()


@pytest.mark.parametrize("label", ["ONA", "Film", "Bonus", "CM", "Menu", "Other", "PV", "OAD"])
def test_legacy_or_deferred_labels_are_not_canonical_identities(label):
    api = parser_api()
    for suffix in (label, label + " 01", "S01 - " + label, "S01 - " + label + " 01-MP01"):
        assert api.parse_canonical_stem("Foo - " + suffix).status == "not_canonical"


@pytest.mark.parametrize("raw", ["Re:Zero", "A / B", "_CON", "日本語 café", "Foo.mkv", "Foo - S01P02"])
def test_prefix_is_exact_sanitized_presentation_text(raw):
    api = parser_api()
    sanitized = components.sanitize_component(raw)
    output = formatter.format_episode_component(sanitized, formatter.EpisodeIdentity(2, 1), ".mkv")
    parsed, = api.parse_canonical_filename(output.component, extension=".mkv").alternatives
    assert parsed.prefix == sanitized.component
    assert parsed.logical_identity.episode_number == 1


@pytest.mark.parametrize("extension", [".mkv", ".mp4", ".m4v", ".avi", ".srt", ".ass", ".xyz", ".123"])
def test_filename_adapter_uses_explicit_lowercase_extension_without_allowlist(extension):
    api = parser_api()
    result = api.parse_canonical_filename("Foo - S01E01" + extension, extension=extension)
    parsed, = result.alternatives
    assert parsed.extension == extension
    stem, = api.parse_canonical_stem("Foo - S01E01").alternatives
    assert stem.extension is None
    assert stem.logical_identity == parsed.logical_identity


@pytest.mark.parametrize("filename,extension", [
    ("Foo - S01E01", ".mkv"), ("Foo - S01E01.", "."), ("Foo - S01E01.MKV", ".MKV"),
    ("Foo - S01E01.MKV", ".mkv"), ("Foo - S01E01.mkv", "mkv"), ("Foo - S01E01.mkv", ""),
    ("Foo - S01E01.mkv ", ".mkv"), ("Foo - S01E01.mkv\n", ".mkv"),
    ("Foo - S01E01.mkv.mp4", ".mp4"), ("Foo - S01E01.mkv.mkv", ".mkv"),
    ("Foo - S01E01..mkv", ".mkv"), ("Foo - S01E01.tar.gz", ".tar.gz"),
    ("Foo - S01 - Recap 12.5", ".mkv"), ("Foo - S01E01.mkv", ".mp4"),
    ("Foo - S01E01.ＭＫＶ", ".ＭＫＶ"),
])
def test_filename_adapter_rejects_missing_or_noncanonical_explicit_extension(filename, extension):
    api = parser_api()
    assert api.parse_canonical_filename(filename, extension=extension).status == "not_canonical"


def test_decimal_dot_is_not_heuristically_stripped_as_an_extension():
    api = parser_api()
    result = api.parse_canonical_stem("Foo - S01 - Recap 12.5")
    parsed, = result.alternatives
    assert parsed.logical_identity.position == Decimal("12.5")
    with pytest.raises(TypeError):
        api.parse_canonical_filename("Foo - S01 - Recap 12.5")
    # Numeric extensions really are formatter output; only the caller selects one.
    integer, = api.parse_canonical_filename("Foo - S01 - Recap 12.5", extension=".5").alternatives
    assert integer.logical_identity.position == Decimal(12)
    assert integer.extension == ".5"


def test_filename_byte_budget_includes_extension():
    api = parser_api()
    prefix = "a" * (255 - len(" - S01E01.mkv"))
    assert api.parse_canonical_filename(prefix + " - S01E01.mkv", extension=".mkv").status == "unique"
    assert api.parse_canonical_filename(prefix + "a - S01E01.mkv", extension=".mkv").status == "not_canonical"


def test_results_and_nested_identities_are_immutable_and_hashable():
    api = parser_api()
    result = api.parse_canonical_stem("Foo - S01P02 - OVA 01-MP01")
    alternative, _ = result.alternatives
    with pytest.raises(FrozenInstanceError):
        result.alternatives = ()
    with pytest.raises(FrozenInstanceError):
        alternative.prefix = "Other"
    with pytest.raises(FrozenInstanceError):
        alternative.logical_identity.ordinal = 2
    with pytest.raises(FrozenInstanceError):
        alternative.grammar_version = "v2"
    assert hash(result) == hash(api.parse_canonical_stem("Foo - S01P02 - OVA 01-MP01"))


@pytest.mark.parametrize("season,episode", product([1, 2, 99, 100, 101, 123], repeat=2))
@pytest.mark.parametrize("part,mp", [(None, None), (2, None), (None, 102), (101, 102)])
def test_episode_formatter_roundtrip_is_always_unique(season, episode, part, mp):
    api = parser_api()
    expected = api.StandardEpisodeIdentity(season, part, episode)
    for prefix in PREFIXES:
        output = formatter.format_episode_component(
            components.sanitize_component(prefix), formatter.EpisodeIdentity(season, episode, part, mp), ".MP4",
        )
        assert output.valid
        result = api.parse_canonical_filename(output.component, extension=".mp4")
        assert result.status == "unique"
        parsed, = result.alternatives
        assert (parsed.prefix, parsed.logical_identity, parsed.media_part_number) == (prefix, expected, mp)


@pytest.mark.parametrize("kind,label", LABELS)
@pytest.mark.parametrize("season,part", [(None, None), (1, None), (100, 101)])
@pytest.mark.parametrize("ordinal", [None, 1, 99, 100])
def test_supplementary_formatter_identity_occurs_exactly_once_among_alternatives(kind, label, season, part, ordinal):
    api = parser_api()
    expected = api.SupplementaryIdentity(season, part, kind, ordinal)
    for prefix, mp in product(PREFIXES, [None, 1, 102]):
        output = formatter.format_supplementary_component(
            components.sanitize_component(prefix), formatter.SupplementaryIdentity(kind, season, part, ordinal, media_part=mp), ".MKV",
        )
        assert output.valid
        result = api.parse_canonical_filename(output.component, extension=".mkv")
        assert result.status in {"unique", "ambiguous"}
        assert sum(
            p.prefix == prefix and p.logical_identity == expected and p.media_part_number == mp
            for p in result.alternatives
        ) == 1


@pytest.mark.parametrize("position", [12, Decimal("12.500"), Decimal("24.25"), Decimal("0.5"), Decimal("12.050"), Decimal("1E+2")])
@pytest.mark.parametrize("part,mp", [(None, None), (2, 1), (101, 102)])
def test_recap_formatter_roundtrip_preserves_decimal_value(position, part, mp):
    api = parser_api()
    output = formatter.format_supplementary_component(
        components.sanitize_component("Foo - S01"),
        formatter.SupplementaryIdentity("recap", 2, part, recap_position=position, media_part=mp), ".mkv",
    )
    assert output.valid
    parsed, = api.parse_canonical_filename(output.component, extension=".mkv").alternatives
    assert parsed.logical_identity == api.RecapIdentity(2, part, Decimal(position))
    assert parsed.prefix == "Foo - S01"
    assert parsed.media_part_number == mp


def test_formatter_noninjectivity_is_represented_not_resolved():
    api = parser_api()
    first = formatter.format_supplementary_component(
        components.sanitize_component("Foo"), formatter.SupplementaryIdentity("ova", season=1), ".mkv",
    )
    second = formatter.format_supplementary_component(
        components.sanitize_component("Foo - S01"), formatter.SupplementaryIdentity("ova"), ".mkv",
    )
    assert first.component == second.component == "Foo - S01 - OVA.mkv"
    result = api.parse_canonical_filename(first.component, extension=".mkv")
    assert result.status == "ambiguous"
    assert len(result.alternatives) == 2


def test_core_cold_import_needs_only_standard_library():
    api = parser_api()
    repository = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-S", "-c", (
            f"import sys; sys.path.insert(0, {repository!r}); "
            "from app.canonical_filename import parse_canonical_stem; "
            "assert parse_canonical_stem('Foo - S01E01').status == 'unique'; "
            "assert not any(name == forbidden or name.startswith(forbidden + '.') "
            "for name in sys.modules for forbidden in ('sqlalchemy', 'httpx', 'app.models', 'app.catalog', 'app.scanner'))"
        )], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_thousands_of_parses_are_deterministic_without_external_lookup(monkeypatch):
    api = parser_api()
    import builtins
    import os
    import socket
    import sqlite3

    def forbidden(*args, **kwargs):
        raise AssertionError("Canonical parser attempted an external lookup")

    filenames = tuple(
        f"Show {i} - S02P02E01-MP01.mkv" if i % 2 else f"Show {i} - S02 - OVA 01.mkv"
        for i in range(5000)
    )
    with monkeypatch.context() as guard:
        for owner, name in (
            (builtins, "open"), (os, "stat"), (os, "scandir"), (os, "listdir"),
            (socket, "socket"), (sqlite3, "connect"),
        ):
            guard.setattr(owner, name, forbidden)
        first = tuple(api.parse_canonical_filename(name, extension=".mkv") for name in filenames)
        second = tuple(api.parse_canonical_filename(name, extension=".mkv") for name in filenames)
    assert first == second
    assert sum(result.status == "unique" for result in first) == 2500
    assert sum(result.status == "ambiguous" for result in first) == 2500
