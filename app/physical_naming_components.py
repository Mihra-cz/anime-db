"""Pure portable-component projections; human naming snapshots remain unchanged."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath
import re
from typing import Literal
import unicodedata


COMPONENT_POLICY_ID = "v1"
MAX_COMPONENT_UTF8_BYTES = 255
READABILITY_REVIEW_CHARS = 70
WINDOWS_PATH_TARGET_UNITS = 240
_INVALID_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})
_FORBIDDEN = frozenset('<>:"/\\|?*')
_SEPARATOR_RUN = re.compile(r"[:/\\|](?: *[:/\\|])*")
_REPEATED_SEPARATOR = re.compile(r"(?:^| )-(?: +-)+(?: |$)")
_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}) | frozenset(
    prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"
)
# Hangul fillers are ignorable letters (Lo); Braille blank is a symbol (So).
# Neither counts as human content, but both remain intact within real names.
_NON_CONTENT_BLANKS = frozenset("\u115f\u1160\u3164\uffa0\u2800")


@dataclass(frozen=True)
class ComponentDiagnostic:
    code: str
    severity: Literal["error", "info", "warning"]
    actual_utf8_bytes: int | None = None
    max_utf8_bytes: int | None = None
    overflow_bytes: int | None = None


@dataclass(frozen=True)
class ComponentTransformation:
    code: str
    count: int = 1
    original: str | None = None
    replacement: str | None = None


@dataclass(frozen=True)
class ComponentPreview:
    preview_text: str | None
    component: str | None
    chars: int | None
    utf8_bytes: int | None
    utf16_units: int | None
    diagnostics: tuple[ComponentDiagnostic, ...]

    @property
    def valid(self) -> bool:
        return self.component is not None


@dataclass(frozen=True)
class SanitizedComponent(ComponentPreview):
    policy_id: str
    unicode_version: str
    original: str
    changed: bool
    transformations: tuple[ComponentTransformation, ...]

    @property
    def requires_readability_review(self) -> bool:
        return self.chars is not None and self.chars > READABILITY_REVIEW_CHARS


def _logical_invalid(text: str) -> bool:
    return any(unicodedata.category(char) in _INVALID_CATEGORIES for char in text)


def _device_name(text: str) -> bool:
    return text.split(".", 1)[0].rstrip(" .").upper() in _DEVICE_NAMES


def component_preview(
    text: str | None, diagnostics: tuple[ComponentDiagnostic, ...] = (),
) -> ComponentPreview:
    """Measure/check assembled text without rewriting generated identity tokens."""
    # Always measure the final component, rather than reusing a prefix budget.
    diagnostics = tuple(d for d in diagnostics if d.code != "component_byte_limit")
    if text is None:
        return ComponentPreview(None, None, None, None, None, diagnostics)
    if _logical_invalid(text):
        return ComponentPreview(None, None, None, None, None, diagnostics + (
            ComponentDiagnostic("invalid_logical_input", "error"),
        ))
    errors = list(diagnostics)
    if not text and not any(d.code == "empty_or_separator_only" for d in errors):
        errors.append(ComponentDiagnostic("empty_or_separator_only", "error"))
    if text and (any(char in _FORBIDDEN for char in text) or text[-1] in " ."
                 or text.startswith(".") or _device_name(text)):
        errors.append(ComponentDiagnostic("invalid_component", "error"))
    if _REPEATED_SEPARATOR.search(text) and not any(d.code == "repeated_separator" for d in errors):
        errors.append(ComponentDiagnostic("repeated_separator", "info"))
    byte_count = len(text.encode("utf-8"))
    if byte_count > MAX_COMPONENT_UTF8_BYTES:
        errors.append(ComponentDiagnostic(
            "component_byte_limit", "error", byte_count,
            MAX_COMPONENT_UTF8_BYTES, byte_count - MAX_COMPONENT_UTF8_BYTES,
        ))
    return ComponentPreview(
        text, None if any(d.severity == "error" for d in errors) else text,
        len(text), byte_count, len(text.encode("utf-16-le")) // 2, tuple(errors),
    )


def sanitize_component(raw_text: str, policy_id: str = COMPONENT_POLICY_ID) -> SanitizedComponent:
    """Sanitize human text only; no authority, identity, path or collision decisions."""
    if policy_id != COMPONENT_POLICY_ID:
        raise ValueError("Unsupported component policy.")
    if not isinstance(raw_text, str):
        raise TypeError("Human component text must be a string.")
    transformations = []
    if _logical_invalid(raw_text):
        preview = component_preview(None, (ComponentDiagnostic("invalid_logical_input", "error"),))
    else:
        text = normalized = unicodedata.normalize("NFC", raw_text)
        if text != raw_text:
            transformations.append(ComponentTransformation("normalized_nfc"))
        space_count = sum(
            (unicodedata.category(char) == "Zs" and char != " ") or char == "\u200b"
            for char in text
        )
        if space_count:
            transformations.append(ComponentTransformation("normalized_unicode_space", space_count, replacement=" "))
        text = "".join(" " if unicodedata.category(char) == "Zs" or char == "\u200b" else char for char in text)
        source = text
        def replace_separator(match: re.Match) -> str:
            # Spaces inside a delimiter run also preserve separated syntax.
            spaced = (" " in match[0] or source[match.start()-1:match.start()] == " "
                      or source[match.end():match.end()+1] == " ")
            replacement = " - " if spaced else "-"
            transformations.append(ComponentTransformation("replaced_separator", original=match[0], replacement=replacement))
            return replacement
        text = _SEPARATOR_RUN.sub(replace_separator, text)
        for char, replacement, code in (
            ("?", " ", "replaced_question"), ("*", " ", "replaced_asterisk"),
            ("<", "(", "replaced_angle_bracket"), (">", ")", "replaced_angle_bracket"),
            ('"', "'", "replaced_quote"),
        ):
            count = text.count(char)
            if count:
                transformations.append(ComponentTransformation(code, count, char, replacement))
                text = text.replace(char, replacement)
        collapsed = re.sub(" +", " ", text)
        if collapsed != text:
            transformations.append(ComponentTransformation("collapsed_spaces", len(text)-len(collapsed)))
        text = collapsed
        trimmed = text.strip(" ")
        if trimmed != text:
            transformations.append(ComponentTransformation("trimmed_spaces", len(text)-len(trimmed)))
        text = trimmed
        trimmed = text.rstrip(" .")
        if trimmed != text:
            transformations.append(ComponentTransformation("removed_trailing_dots", len(text)-len(trimmed)))
        text = trimmed
        # Replacement punctuation must not manufacture a human name. Marks and
        # format controls alone have no visible base, but remain within names.
        has_content = any(
            char not in _FORBIDDEN and char not in " .-_" and char not in _NON_CONTENT_BLANKS
            and unicodedata.category(char) not in {"Zs", "Cf", "Mn", "Mc", "Me"}
            for char in normalized
        )
        diagnostics = () if has_content else (ComponentDiagnostic("empty_or_separator_only", "error"),)
        if text and _device_name(text):
            text = "_" + text
            transformations.append(ComponentTransformation("escaped_device_name"))
        if text.startswith("."):
            text = "_" + text
            transformations.append(ComponentTransformation("escaped_leading_dot"))
        preview = component_preview(text, diagnostics)
    return SanitizedComponent(
        preview.preview_text, preview.component, preview.chars, preview.utf8_bytes,
        preview.utf16_units, preview.diagnostics, policy_id, unicodedata.unidata_version,
        raw_text, preview.preview_text is not None and preview.preview_text != raw_text,
        tuple(transformations),
    )


@dataclass(frozen=True)
class ComponentCollisionKeys:
    exact_key: str
    fold_key: str
    uppercase_guard: str


def collision_keys(component: str) -> ComponentCollisionKeys:
    """Conservative Unicode guards; these do not emulate a target filesystem."""
    return ComponentCollisionKeys(
        component,
        unicodedata.normalize("NFC", unicodedata.normalize("NFD", component).casefold()),
        unicodedata.normalize("NFC", component.upper()),
    )


def collision_guards_match(left: ComponentCollisionKeys, right: ComponentCollisionKeys) -> bool:
    """Caller must establish the same parent namespace; no traversal lives here."""
    return (left.exact_key == right.exact_key or left.fold_key == right.fold_key
            or left.uppercase_guard == right.uppercase_guard)


@dataclass(frozen=True)
class WindowsPathBudget:
    absolute_path: str
    utf16_units: int
    target_units: int
    overflow_units: int
    within_target: bool
    requires_shorter_name: bool


def evaluate_windows_path_budget(
    absolute_path: str, target_units: int = WINDOWS_PATH_TARGET_UNITS,
) -> WindowsPathBudget:
    """Soft portability target, requiring an explicit absolute client path."""
    if not isinstance(absolute_path, str) or _logical_invalid(absolute_path) or not PureWindowsPath(absolute_path).is_absolute():
        raise ValueError("Provide an absolute Windows client path without invalid logical characters.")
    if type(target_units) is not int or target_units <= 0:
        raise ValueError("Windows path target must be a positive integer.")
    units = len(absolute_path.encode("utf-16-le")) // 2
    overflow = max(0, units - target_units)
    return WindowsPathBudget(absolute_path, units, target_units, overflow, not overflow, bool(overflow))
