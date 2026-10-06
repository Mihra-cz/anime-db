"""Only already resolved identity axes can become canonical tokens."""
from decimal import Decimal, localcontext
import importlib
import importlib.util

import pytest


def formatter_api():
    assert importlib.util.find_spec("app.physical_naming_formatters") is not None, (
        "V6 canonical formatter foundation is missing"
    )
    return importlib.import_module("app.physical_naming_formatters"), importlib.import_module("app.physical_naming_components")


@pytest.mark.parametrize("season", [1, 2, 100])
def test_root_and_season_directory_have_no_additional_semantic_transform(season):
    api, components = formatter_api()
    root = components.sanitize_component("Re:Zero")
    assert api.format_root_component(root).component == "Re-Zero"
    assert api.format_season_directory(season).component == f"Season {season:02d}"


@pytest.mark.parametrize("part,mp,expected", [
    (None, None, "Title Part 2 - S01E03.mkv"),
    (2, None, "Title Part 2 - S01P02E03.mkv"),
    (2, 1, "Title Part 2 - S01P02E03-MP01.mkv"),
    (None, 2, "Title Part 2 - S01E03-MP02.mkv"),
])
def test_episode_formats_only_explicit_identity_axes(part, mp, expected):
    api, components = formatter_api()
    identity = api.EpisodeIdentity(season=1, episode=3, part=part, media_part=mp)
    result = api.format_episode_component(components.sanitize_component("Title Part 2"), identity, ".MKV")
    assert result.valid and result.component == expected
    assert identity.part == part and identity.episode == 3


def test_high_numbers_are_not_truncated():
    api, components = formatter_api()
    result = api.format_episode_component(components.sanitize_component("Show"), api.EpisodeIdentity(100, 123, part=101, media_part=102), "MP4")
    assert result.component == "Show - S100P101E123-MP102.mp4"


@pytest.mark.parametrize("field,value", [("season", None), ("season", 0), ("season", True), ("episode", None), ("episode", 0), ("episode", 1.5), ("part", 0), ("part", "2"), ("media_part", -1)])
def test_missing_or_invalid_episode_identity_never_gets_fallback(field, value):
    api, components = formatter_api()
    fields = dict(season=1, episode=1, part=None, media_part=None)
    fields[field] = value
    result = api.format_episode_component(components.sanitize_component("Show S99E99"), api.EpisodeIdentity(**fields), ".mkv")
    assert not result.valid and result.component is None
    assert "invalid_identity" in {d.code for d in result.diagnostics}


@pytest.mark.parametrize("raw,expected", [(".MKV", ".mkv"), ("MP4", ".mp4"), (".m4v", ".m4v")])
def test_extension_is_separate_and_lowercase(raw, expected):
    api, _ = formatter_api()
    result = api.normalize_extension(raw)
    assert result.valid and result.extension == expected and result.original == raw


@pytest.mark.parametrize("extension", ["", ".", "../mkv", "a\\mkv", ".mkv ", ".mkv\n", "mkv:stream", ".tar.gz", "ＭＫＶ"])
def test_invalid_extension_is_not_guessed(extension):
    api, components = formatter_api()
    result = api.format_episode_component(components.sanitize_component("Show.mkv"), api.EpisodeIdentity(1, 1), extension)
    assert not result.valid and result.component is None
    assert "invalid_extension" in {d.code for d in result.diagnostics}


@pytest.mark.parametrize("kind,label", [("ova", "OVA"), ("special", "Special"), ("preview", "Preview"), ("op", "OP"), ("ed", "ED"), ("ncop", "NCOP"), ("nced", "NCED")])
@pytest.mark.parametrize("ordinal", [None, 1])
def test_supported_supplementary_labels_preserve_none_ordinal(kind, label, ordinal):
    api, components = formatter_api()
    result = api.format_supplementary_component(components.sanitize_component("Show"), api.SupplementaryIdentity(kind, season=2, ordinal=ordinal), ".MKV")
    assert result.component == "Show - S02 - " + label + (" 01" if ordinal is not None else "") + ".mkv"


def test_root_supplementary_does_not_invent_season_or_ordinal():
    api, components = formatter_api()
    result = api.format_supplementary_component(components.sanitize_component("Show"), api.SupplementaryIdentity("ova"), ".mkv")
    assert result.component == "Show - OVA.mkv"


def test_season_only_parent_prefix_does_not_invent_hierarchy_part():
    api, components = formatter_api()
    identity = api.SupplementaryIdentity("ncop", season=2, part=None, media_part=1)
    result = api.format_supplementary_component(components.sanitize_component("Slime 2nd Season Part 2"), identity, ".mkv")
    assert result.component == "Slime 2nd Season Part 2 - S02 - NCOP-MP01.mkv"
    assert identity.part is None and identity.ordinal is None
    with_part = api.format_supplementary_component(components.sanitize_component("Show"), api.SupplementaryIdentity("ova", season=1, part=2, ordinal=1, media_part=1), ".mkv")
    assert with_part.component == "Show - S01P02 - OVA 01-MP01.mkv"


@pytest.mark.parametrize("position,text", [(Decimal("12.5"), "12.5"), (Decimal("24.25"), "24.25"), (Decimal("12.500"), "12.5"), (Decimal("1.25E1"), "12.5"), (12, "12")])
def test_recap_exact_serialization_does_not_depend_on_decimal_context(position, text):
    api, components = formatter_api()
    with localcontext() as context:
        context.prec = 2
        result = api.format_supplementary_component(components.sanitize_component("Show"), api.SupplementaryIdentity("recap", season=3, recap_position=position), ".mkv")
    assert result.component == f"Show - S03 - Recap {text}.mkv"


@pytest.mark.parametrize("fields", [
    dict(content_type="recap", season=None, recap_position=Decimal("12.5")),
    dict(content_type="recap", season=1, recap_position=12.5),
    dict(content_type="recap", season=1, recap_position=Decimal("NaN")),
    dict(content_type="recap", season=1, recap_position=Decimal("Infinity")),
    dict(content_type="recap", season=1, recap_position=Decimal("-1")),
    dict(content_type="recap", season=1, recap_position=None),
    dict(content_type="recap", season=1, recap_position=Decimal("12.5"), ordinal=1),
    dict(content_type="ova", ordinal=0), dict(content_type="ova", ordinal=True),
    dict(content_type="ova", recap_position=Decimal("12.5")),
    dict(content_type="ova", part=1),
])
def test_invalid_supplementary_axes_fail_without_reinterpretation(fields):
    api, components = formatter_api()
    result = api.format_supplementary_component(components.sanitize_component("Show"), api.SupplementaryIdentity(**fields), ".mkv")
    assert not result.valid and result.component is None
    assert "invalid_identity" in {d.code for d in result.diagnostics}


@pytest.mark.parametrize("kind", ["other", "episode", "unknown"])
def test_open_grammar_is_explicitly_unsupported(kind):
    api, components = formatter_api()
    result = api.format_supplementary_component(components.sanitize_component("Show"), api.SupplementaryIdentity(kind, season=1), ".mkv")
    assert not result.valid and result.component is None
    assert "unsupported_identity" in {d.code for d in result.diagnostics}


@pytest.mark.parametrize("part,mp,suffix_bytes", [(None, None, 13), (2, None, 16), (2, 1, 21)])
@pytest.mark.parametrize("overflow", [0, 1])
def test_final_filename_limit_includes_identity_mp_and_extension(part, mp, suffix_bytes, overflow):
    api, components = formatter_api()
    raw = "a"*(255-suffix_bytes+overflow)
    result = api.format_episode_component(components.sanitize_component(raw), api.EpisodeIdentity(1, 1, part=part, media_part=mp), ".MKV")
    assert result.utf8_bytes == 255+overflow and len(result.preview_text.encode("utf-8")) == 255+overflow
    assert result.valid is (overflow == 0)
    assert (result.component is None) is bool(overflow)
    if overflow:
        error = next(d for d in result.diagnostics if d.code == "component_byte_limit")
        assert (error.actual_utf8_bytes, error.max_utf8_bytes, error.overflow_bytes) == (256, 255, 1)


def test_overlong_prefix_reports_assembled_overflow_without_truncation():
    api, components = formatter_api()
    result = api.format_episode_component(components.sanitize_component("a"*256), api.EpisodeIdentity(1, 1), ".mkv")
    assert not result.valid and result.component is None
    assert result.preview_text == "a"*256 + " - S01E01.mkv"
    assert result.utf8_bytes == 269
    assert next(d for d in result.diagnostics if d.code == "component_byte_limit").overflow_bytes == 14


def test_empty_invalid_prefix_cannot_turn_into_identity_only_placeholder():
    api, components = formatter_api()
    result = api.format_episode_component(components.sanitize_component("///"), api.EpisodeIdentity(1, 1), ".mkv")
    assert not result.valid and result.component is None and result.preview_text is None


def test_formatted_results_and_collisions_are_derived_without_auto_suffixes():
    api, components = formatter_api()
    prefix = components.sanitize_component("A/B")
    identity = api.EpisodeIdentity(1, 1)
    first = api.format_episode_component(prefix, identity, ".mkv")
    second = api.format_episode_component(components.sanitize_component("A:B"), identity, ".mkv")
    assert first == api.format_episode_component(prefix, identity, ".mkv")
    assert first.component == second.component == "A-B - S01E01.mkv"
    assert components.collision_guards_match(components.collision_keys(first.component), components.collision_keys(second.component))


@pytest.mark.parametrize("kind,label", [("film", "Film"), ("bonus", "Bonus"), ("cm", "CM"), ("menu", "Menu")])
@pytest.mark.parametrize("season,ordinal,mp,suffix", [
    (None, None, None, ""), (1, 2, 1, " 02-MP01"),
])
def test_d01_final_labels_do_not_invent_ordinals(kind, label, season, ordinal, mp, suffix):
    api, components = formatter_api()
    result = api.format_supplementary_component(components.sanitize_component("Show"),
        api.SupplementaryIdentity(kind, season=season, ordinal=ordinal, media_part=mp), ".MKV")
    assert result.component == "Show - " + ("S01 - " if season else "") + label + suffix + ".mkv"


@pytest.mark.parametrize("raw,want", [
    ("BD", "BD"), ("TV", "TV"), ("Director's cut", "Director's cut"),
    ('日本語: café/版?', '日本語 - café-版'),
])
def test_variant_suffix_uses_sanitized_manual_label(raw, want):
    api, components = formatter_api()
    base = api.format_episode_component(components.sanitize_component("Show"), api.EpisodeIdentity(1, 1), ".mkv")
    assert hasattr(api, "format_variant_component"), "D03 manual-label formatter missing"
    assert api.format_variant_component(base, components.sanitize_component(raw)).component == f"Show - S01E01 [{want}].mkv"


def test_variant_suffix_is_measured_in_final_filename_budget():
    api, components = formatter_api()
    base = api.format_episode_component(components.sanitize_component("a" * 237), api.EpisodeIdentity(1, 1), ".mkv")
    assert hasattr(api, "format_variant_component"), "D03 manual-label formatter missing"
    result = api.format_variant_component(base, components.sanitize_component("BD"))
    assert result.component == "a" * 237 + " - S01E01 [BD].mkv"
    overflow = api.format_variant_component(base, components.sanitize_component("BDX"))
    assert overflow.component is None and overflow.utf8_bytes == 256
