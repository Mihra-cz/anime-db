"""Portable components are derived text, never catalog authority."""
import importlib
import importlib.util
from itertools import product
import unicodedata

import pytest


def component_api():
    assert importlib.util.find_spec("app.physical_naming_components") is not None, (
        "V6 canonical component sanitizer foundation is missing"
    )
    return importlib.import_module("app.physical_naming_components")


@pytest.mark.parametrize("raw,expected", [
    ("Bananya: Fushigi na Nakamatachi", "Bananya - Fushigi na Nakamatachi"),
    ("Ore wo Suki nano wa Omae dake ka yo: Oretachi no Game Set", "Ore wo Suki nano wa Omae dake ka yo - Oretachi no Game Set"),
    ("Kizumonogatari I: Tekketsu-hen", "Kizumonogatari I - Tekketsu-hen"),
    ("Re:Zero", "Re-Zero"), ("Fate/Grand Order", "Fate-Grand Order"),
    ("A / B", "A - B"), ("A\\B", "A-B"), ("A\\ B", "A - B"),
    ("A|B", "A-B"), ("A::B", "A-B"), ("A :: B", "A - B"),
    ("A:/\\|B", "A-B"), ("A: /B", "A - B"),
    ("A?B", "A B"), ("A*B", "A B"), ("A\"B", "A'B"), ("A<B>", "A(B)"),
    ("SPY×FAMILY", "SPY×FAMILY"), ("Cafe\u0301", "Café"),
    ("👩\u200d💻", "👩\u200d💻"), ("日本語", "日本語"),
    ("Ａ：Ｂ", "Ａ：Ｂ"), ("A\u200cB", "A\u200cB"),
    (" ! ' , ; ( ) [ ] & + … – ", "! ' , ; ( ) [ ] & + … –"),
    ("  name   title  ", "name title"), ("A\u00a0\u3000B", "A B"),
    ("A\u200bB", "A B"), (".hidden", "_.hidden"),
    ("name. . ", "name"), ("A..B", "A..B"),
    ("Show (J23)", "Show (J23)"),
])
def test_sanitized_examples_preserve_meaning(raw, expected):
    api = component_api()
    result = api.sanitize_component(raw)
    assert result.valid and result.component == expected
    assert result.original == raw and result.preview_text == expected
    assert result.policy_id == "v1"
    assert result.unicode_version == unicodedata.unidata_version
    assert result.chars == len(expected)
    assert result.utf8_bytes == len(expected.encode("utf-8"))
    assert result.utf16_units == len(expected.encode("utf-16-le")) // 2
    assert result.changed == (raw != expected)


@pytest.mark.parametrize("device", [
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *[prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"],
])
@pytest.mark.parametrize("suffix", ["", ".txt", ".tar.gz", " .txt"])
def test_device_names_escape_once_including_extension_stems(device, suffix):
    api = component_api()
    raw = device.lower() + suffix
    result = api.sanitize_component(raw)
    assert result.component == "_" + raw
    assert api.sanitize_component(result.component).component == result.component
    assert "escaped_device_name" in {t.code for t in result.transformations}


@pytest.mark.parametrize("raw", ["_COM1", "COM10", "LPT10", "CONtext"])
def test_non_device_names_are_not_escaped(raw):
    assert component_api().sanitize_component(raw).component == raw


@pytest.mark.parametrize("raw", ["a\x00B", "a\tB", "a\nB", "a\rB", "a\x7fB", "a\x85B", "a\u2028B", "a\u2029B", "a\ud800B"])
def test_invalid_logical_input_fails_without_silent_removal(raw):
    result = component_api().sanitize_component(raw)
    assert not result.valid and result.component is None and result.preview_text is None
    assert [d.code for d in result.diagnostics] == ["invalid_logical_input"]
    assert result.chars is None and result.utf8_bytes is None


@pytest.mark.parametrize("raw", ["", "   ", ".", "..", " - _ . ", ":/\\|?*< >\"", "\u200b", "\u200d", "\u200c", "\ufeff", "\u0301", "\ufe0f"])
def test_empty_separator_and_invisible_only_input_never_create_placeholder(raw):
    result = component_api().sanitize_component(raw)
    assert not result.valid and result.component is None
    assert "empty_or_separator_only" in {d.code for d in result.diagnostics}


@pytest.mark.parametrize("non_content", ["\u115f", "\u1160", "\u3164", "\uffa0", "\u2800", "\u093e"])
@pytest.mark.parametrize("surrounding", ["", " :/\u200d", ". "])
def test_fillers_and_base_less_marks_cannot_be_the_only_human_content(non_content, surrounding):
    api = component_api()
    result = api.sanitize_component(non_content + surrounding)
    assert not result.valid and result.component is None
    assert "empty_or_separator_only" in {d.code for d in result.diagnostics}
    # Content validation does not remove a legitimate Unicode sequence.
    text = "日本" + non_content + "語"
    with_content = api.sanitize_component(text)
    assert with_content.valid and with_content.component == text
    assert api.sanitize_component(with_content.component).component == text


@pytest.mark.parametrize("raw,nfc", [("≮", "≮"), ("≯", "≯")])
def test_content_check_uses_nfc_projection_of_canonically_equivalent_input(raw, nfc):
    api = component_api()
    assert unicodedata.normalize("NFC", raw) == nfc
    composed, decomposed = api.sanitize_component(nfc), api.sanitize_component(raw)
    assert composed.valid and composed.component == nfc
    assert (decomposed.valid, decomposed.component, decomposed.diagnostics) == (True, nfc, composed.diagnostics)


@pytest.mark.parametrize("raw,valid,bytes_", [("a"*255, True, 255), ("a"*256, False, 256), ("あ"*85, True, 255), ("あ"*85+"a", False, 256)])
def test_utf8_hard_limit_preserves_full_preview(raw, valid, bytes_):
    result = component_api().sanitize_component(raw)
    assert result.valid is valid and result.preview_text == raw
    assert result.utf8_bytes == bytes_
    if not valid:
        error = next(d for d in result.diagnostics if d.code == "component_byte_limit")
        assert (error.severity, error.actual_utf8_bytes, error.max_utf8_bytes, error.overflow_bytes) == ("error", 256, 255, 1)


def test_readability_is_derived_review_not_hard_failure():
    api = component_api()
    assert not api.sanitize_component("a"*70).requires_readability_review
    result = api.sanitize_component("a"*71)
    assert result.valid and result.requires_readability_review
    assert not any(d.severity == "error" for d in result.diagnostics)


def test_repeated_existing_hyphens_are_preserved_with_info():
    result = component_api().sanitize_component("A: - B")
    assert result.component == "A - - B" and result.valid
    assert [(d.code, d.severity) for d in result.diagnostics] == [("repeated_separator", "info")]


def test_results_and_transformation_order_are_deterministic():
    api = component_api()
    raw = "  Cafe\u0301\u00a0: A? . "
    first = api.sanitize_component(raw)
    assert first == api.sanitize_component(raw)
    assert [t.code for t in first.transformations] == [
        "normalized_nfc", "normalized_unicode_space", "replaced_separator",
        "replaced_question", "collapsed_spaces", "trimmed_spaces", "removed_trailing_dots",
    ]
    with pytest.raises(ValueError):
        api.sanitize_component("Name", policy_id="future-policy")


def test_property_style_outputs_are_portable_and_idempotent():
    api = component_api()
    for left, middle, right in product(("A", "日本", ".CON", "Café", "👩\u200d💻"), (":", "/", "\\", "|", ":/", " :: ", "?", "*", "<", "\"", "\u00a0", "\u200b"), ("B", " .", "", "!", "\u0301")):
        result = api.sanitize_component(left+middle+right)
        if not result.valid:
            continue
        component = result.component
        assert component and not any(c in '<>:"/\\|?*' for c in component)
        assert component[-1] not in " ." and not component.startswith(".")
        assert unicodedata.normalize("NFC", component) == component
        assert api.sanitize_component(component).component == component


@pytest.mark.parametrize("left,right,collide", [("A", "a", True), ("Café", "Cafe\u0301", True), ("I", "ı", True), ("ß", "SS", True), ("Ａ", "A", False), ("Alpha", "Beta", False)])
def test_collision_guards_match_without_renaming(left, right, collide):
    api = component_api()
    a, b = api.collision_keys(left), api.collision_keys(right)
    assert a.exact_key == left and b.exact_key == right
    assert api.collision_guards_match(a, b) is collide
    assert api.collision_keys(left) == a


@pytest.mark.parametrize("units", [239, 240, 241])
def test_windows_absolute_path_budget_is_soft_and_exact(units):
    path = "X:\\" + "a"*(units-3)
    result = component_api().evaluate_windows_path_budget(path)
    assert result.utf16_units == units and result.target_units == 240
    assert result.overflow_units == max(0, units-240)
    assert result.within_target is (units <= 240)
    assert result.requires_shorter_name is (units > 240)


@pytest.mark.parametrize("ascii_chars,units", [(234, 239), (235, 240), (236, 241)])
def test_path_budget_counts_non_bmp_character_as_surrogate_pair(ascii_chars, units):
    # len() would report one unit less and accept the 241-unit path.
    path = "X:\\" + "a"*ascii_chars + "\U0001f600"
    result = component_api().evaluate_windows_path_budget(path)
    assert (result.utf16_units, result.overflow_units, result.requires_shorter_name) == (
        units, max(0, units-240), units > 240,
    )


def test_path_budget_counts_utf16_and_does_not_guess_base_path():
    api = component_api()
    path = "\\\\NAS\\very-long-share\\" + "👩\u200d💻"*50
    result = api.evaluate_windows_path_budget(path)
    assert result.utf16_units == len(path.encode("utf-16-le"))//2
    assert result.requires_shorter_name
    with pytest.raises(ValueError):
        api.evaluate_windows_path_budget("Anime\\file.mkv")
    with pytest.raises(ValueError):
        api.evaluate_windows_path_budget("X:\\a\ud800")
    with pytest.raises(ValueError):
        api.evaluate_windows_path_budget("X:\\a", target_units=0)
