"""Validated, atomic-ready edits for title-scoped Media Edit workflows."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .catalog import (
    MANUAL_LANGUAGE_CHOICES, language_display_label, manual_hardsub_state,
    set_audio_track_manual_language, set_internal_subtitle_manual_language,
    set_manual_hardsub,
)
from .media_check import build_media_check_evaluation, set_czsk_availability_manual
from .models import ExternalSubtitleCompatibility, Video


HARDSUB_LABELS = {
    "unknown": "neověřeno", "none": "žádný · ověřeno",
    "cs": "CZ", "sk": "SK", "both": "CZ i SK",
}
AVAILABILITY_LABELS = {
    "clear": "Neurčeno", "seeking": "Sháním", "unavailable": "Neexistují",
}
_LANGUAGE_VALUES = {value for value, _ in MANUAL_LANGUAGE_CHOICES}


@dataclass(frozen=True)
class MediaEditValidationError(ValueError):
    """Human-readable refusal with enough context to safely correct the form."""

    rejected: str
    reason: str
    problem_items: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    requested_changes: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.rejected} {self.reason}".strip()


def hardsub_mode(video: Video) -> str:
    if manual_hardsub_state(video) != "yes":
        return "none" if manual_hardsub_state(video) == "no" else "unknown"
    if video.manual_hardsub_cs and video.manual_hardsub_sk:
        return "both"
    return "cs" if video.manual_hardsub_cs else "sk"


def load_media_videos(session: Session, video_ids: tuple[int, ...]) -> list[Video]:
    if (
        not video_ids
        or len(set(video_ids)) != len(video_ids)
        or any(value < 1 for value in video_ids)
    ):
        raise MediaEditValidationError(
            "Výběr videí byl odmítnut.",
            "Vyberte alespoň jedno existující video a každé vyberte pouze jednou.",
            next_steps=("Upravte výběr videí a odešlete formulář znovu.",),
        )
    videos = list(session.scalars(select(Video).options(
        selectinload(Video.catalog_title),
        selectinload(Video.audio_tracks),
        selectinload(Video.internal_subtitles),
        selectinload(Video.external_subtitle_compatibilities).joinedload(
            ExternalSubtitleCompatibility.external_subtitle
        ),
    ).where(Video.id.in_(video_ids)).order_by(Video.id)).all())
    if len(videos) != len(video_ids):
        missing = sorted(set(video_ids) - {video.id for video in videos})
        raise MediaEditValidationError(
            "Výběr videí byl odmítnut.",
            "Některá vybraná videa už neexistují.",
            tuple(f"Video ID {video_id}" for video_id in missing),
            ("Obnovte stránku a zkontrolujte aktuální výběr.",),
        )
    return videos


def _video_label(video: Video) -> str:
    number = video.season_episode_number
    identity = f"E{number:02d}" if number is not None else "Video"
    return f"{identity} · {video.filename}"


def _language_label(value: str) -> str:
    return (
        "automaticky"
        if value == "auto"
        else language_display_label(value, include_name=True)
    )


def _requested_bulk_changes(
    *, bulk_audio: str, bulk_internal_subtitle: str,
    hardsub: str, availability: str,
) -> tuple[str, ...]:
    requested = []
    if bulk_audio:
        requested.append(f"Audio → {_language_label(bulk_audio)}")
    if bulk_internal_subtitle:
        requested.append(
            f"Interní titulky → {_language_label(bulk_internal_subtitle)}"
        )
    if hardsub:
        requested.append(f"Hardsub → {HARDSUB_LABELS.get(hardsub, hardsub)}")
    if availability:
        requested.append(
            f"CZ/SK dostupnost → {AVAILABILITY_LABELS.get(availability, availability)}"
        )
    return tuple(requested)


def _track_count_label(count: int, kind: str) -> str:
    suffix = "stopa" if count == 1 else "stopy" if count in {2, 3, 4} else "stop"
    return f"{count} {kind} {suffix}"


def _validate_bulk_track_cardinality(
    videos: list[Video], *, bulk_audio: str, bulk_internal_subtitle: str,
    requested_changes: tuple[str, ...],
) -> None:
    problems: list[str] = []
    ambiguous_axes: list[str] = []
    if bulk_audio:
        ambiguous = [video for video in videos if len(video.audio_tracks) != 1]
        if ambiguous:
            ambiguous_axes.append("jazyka audia")
            problems.extend(
                f"{_video_label(video)} — "
                f"{_track_count_label(len(video.audio_tracks), 'audio')}"
                for video in ambiguous
            )
    if bulk_internal_subtitle:
        ambiguous = [
            video for video in videos if len(video.internal_subtitles) != 1
        ]
        if ambiguous:
            ambiguous_axes.append("jazyka interních titulků")
            problems.extend(
                f"{_video_label(video)} — "
                f"{_track_count_label(len(video.internal_subtitles), 'interní subtitle')}"
                for video in ambiguous
            )
    if problems:
        axes = " a ".join(ambiguous_axes)
        raise MediaEditValidationError(
            "Žádná část hromadné změny nebyla uložena.",
            f"Hromadná změna {axes} není jednoznačná, protože každé "
            "vybrané video nemá právě jednu odpovídající stopu.",
            tuple(problems),
            (
                "Odeberte problematická videa z výběru,",
                "nastavte problematickou vlastnost na „Neměnit“,",
                "nebo upravte jednotlivé stopy v detailu videa.",
            ),
            requested_changes,
        )


def apply_media_edits(
    videos: list[Video], *, audio_by_track: dict[int, str] | None = None,
    internal_subtitle_by_track: dict[int, str] | None = None,
    bulk_audio: str = "", bulk_internal_subtitle: str = "",
    hardsub: str = "", availability: str = "", allow_empty: bool = False,
) -> list[str]:
    """Validate all requested axes, then stage them for one caller-owned commit."""
    requested = _requested_bulk_changes(
        bulk_audio=bulk_audio,
        bulk_internal_subtitle=bulk_internal_subtitle,
        hardsub=hardsub,
        availability=availability,
    )
    if hardsub and hardsub not in HARDSUB_LABELS:
        raise MediaEditValidationError(
            "Změna hardsubu byla odmítnuta.",
            f"Hodnota „{hardsub}“ není podporovaný stav hardsubu.",
            next_steps=("Vyberte některou z nabízených hodnot.",),
            requested_changes=requested,
        )
    if availability and availability not in AVAILABILITY_LABELS:
        raise MediaEditValidationError(
            "Změna dostupnosti CZ/SK byla odmítnuta.",
            f"Hodnota „{availability}“ není podporovaný stav.",
            next_steps=("Vyberte Neurčeno, Sháním nebo Neexistují.",),
            requested_changes=requested,
        )
    allowed_languages = {"auto"} | _LANGUAGE_VALUES
    if bulk_audio and bulk_audio not in allowed_languages:
        raise MediaEditValidationError(
            "Hromadná změna audia byla odmítnuta.",
            f"Jazyk „{bulk_audio}“ není podporovaný.",
            next_steps=("Vyberte nabízený jazyk, Automaticky nebo Neměnit.",),
            requested_changes=requested,
        )
    if bulk_internal_subtitle and bulk_internal_subtitle not in allowed_languages:
        raise MediaEditValidationError(
            "Hromadná změna interních titulků byla odmítnuta.",
            f"Jazyk „{bulk_internal_subtitle}“ není podporovaný.",
            next_steps=("Vyberte nabízený jazyk, Automaticky nebo Neměnit.",),
            requested_changes=requested,
        )
    if audio_by_track is not None and bulk_audio:
        raise MediaEditValidationError(
            "Změna audia byla odmítnuta.",
            "Jednotlivé a hromadné nastavení audia nelze kombinovat.",
            next_steps=("Použijte pouze jeden způsob úpravy.",),
        )
    if internal_subtitle_by_track is not None and bulk_internal_subtitle:
        raise MediaEditValidationError(
            "Změna interních titulků byla odmítnuta.",
            "Jednotlivé a hromadné nastavení interních titulků nelze kombinovat.",
            next_steps=("Použijte pouze jeden způsob úpravy.",),
        )

    all_audio = {
        track.id: (video, track)
        for video in videos for track in video.audio_tracks
    }
    all_internal = {
        track.id: (video, track)
        for video in videos for track in video.internal_subtitles
    }
    if audio_by_track is not None and set(audio_by_track) != set(all_audio):
        raise MediaEditValidationError(
            "Změna audia nebyla uložena.",
            "Seznam audio stop se od načtení stránky změnil.",
            next_steps=("Obnovte stránku a zkontrolujte aktuální stopy.",),
        )
    if (
        internal_subtitle_by_track is not None
        and set(internal_subtitle_by_track) != set(all_internal)
    ):
        raise MediaEditValidationError(
            "Změna interních titulků nebyla uložena.",
            "Seznam interních subtitle stop se od načtení stránky změnil.",
            next_steps=("Obnovte stránku a zkontrolujte aktuální stopy.",),
        )
    _validate_bulk_track_cardinality(
        videos,
        bulk_audio=bulk_audio,
        bulk_internal_subtitle=bulk_internal_subtitle,
        requested_changes=requested,
    )

    changes: list[str] = []
    for track_id, raw in (audio_by_track or {}).items():
        video, track = all_audio[track_id]
        desired = "" if raw == "auto" else raw
        before = track.manual_language
        try:
            set_audio_track_manual_language(track, desired)
        except ValueError as exc:
            raise MediaEditValidationError(
                f"Jazyk audio stopy Stream {track.stream_index} nebyl uložen.",
                f"Hodnota „{raw}“ není podporovaný jazyk.",
                (_video_label(video),),
                ("Vyberte nabízenou hodnotu nebo „automaticky“.",),
            ) from exc
        if track.manual_language != before:
            changes.append(
                f"{_video_label(video)} · Audio stream {track.stream_index}: "
                f"{before or 'automaticky'} → "
                f"{track.manual_language or 'automaticky'}"
            )
    for track_id, raw in (internal_subtitle_by_track or {}).items():
        video, track = all_internal[track_id]
        desired = "" if raw == "auto" else raw
        before = track.manual_language
        try:
            set_internal_subtitle_manual_language(track, desired)
        except ValueError as exc:
            raise MediaEditValidationError(
                f"Jazyk interní subtitle stopy Stream {track.stream_index} "
                "nebyl uložen.",
                f"Hodnota „{raw}“ není podporovaný jazyk.",
                (_video_label(video),),
                ("Vyberte nabízenou hodnotu nebo „automaticky“.",),
            ) from exc
        if track.manual_language != before:
            changes.append(
                f"{_video_label(video)} · Interní titulky Stream "
                f"{track.stream_index}: {before or 'automaticky'} → "
                f"{track.manual_language or 'automaticky'}"
            )

    if bulk_audio:
        for video in videos:
            track = video.audio_tracks[0]
            before = track.manual_language
            set_audio_track_manual_language(
                track, "" if bulk_audio == "auto" else bulk_audio,
            )
            if track.manual_language != before:
                changes.append(
                    f"{_video_label(video)} · Audio stream "
                    f"{track.stream_index}: {before or 'automaticky'} → "
                    f"{track.manual_language or 'automaticky'}"
                )
    if bulk_internal_subtitle:
        for video in videos:
            track = video.internal_subtitles[0]
            before = track.manual_language
            set_internal_subtitle_manual_language(
                track,
                "" if bulk_internal_subtitle == "auto"
                else bulk_internal_subtitle,
            )
            if track.manual_language != before:
                changes.append(
                    f"{_video_label(video)} · Interní titulky Stream "
                    f"{track.stream_index}: {before or 'automaticky'} → "
                    f"{track.manual_language or 'automaticky'}"
                )

    for video in videos:
        if hardsub and hardsub != hardsub_mode(video):
            before = hardsub_mode(video)
            set_manual_hardsub(video, hardsub)
            changes.append(
                f"{_video_label(video)} · Hardsub: "
                f"{HARDSUB_LABELS[before]} → {HARDSUB_LABELS[hardsub]}"
            )
        if availability:
            desired = None if availability == "clear" else availability
            if desired != video.czsk_availability_manual:
                if desired == "unavailable":
                    evaluation = build_media_check_evaluation(video)
                    if evaluation.factual.has_cs_or_sk:
                        raise MediaEditValidationError(
                            "Změna CZ/SK dostupnosti nebyla uložena.",
                            "Video obsahuje skutečnou pozitivní CZ/SK evidenci, "
                            "proto je nelze označit jako „Neexistují“.",
                            (_video_label(video),),
                            (
                                "Ponechte stav Neurčeno nebo opravte konkrétní "
                                "factual evidenci.",
                            ),
                            requested,
                        )
                    if not evaluation.subtitle_required:
                        raise MediaEditValidationError(
                            "Změna CZ/SK dostupnosti nebyla uložena.",
                            "Pro tento obsah nejsou titulky podle Media Check "
                            "semantics požadované.",
                            (_video_label(video),),
                            ("Ponechte stav Neurčeno.",),
                            requested,
                        )
                before = video.czsk_availability_manual or "clear"
                set_czsk_availability_manual(video, desired)
                changes.append(
                    f"{_video_label(video)} · CZ/SK dostupnost: "
                    f"{AVAILABILITY_LABELS[before]} → "
                    f"{AVAILABILITY_LABELS[availability]}"
                )
    if not changes and not allow_empty:
        raise MediaEditValidationError(
            "Změny nebyly uloženy.",
            "Formulář neobsahuje žádnou změněnou hodnotu.",
            next_steps=(
                "Změňte alespoň jednu hodnotu, nebo se vraťte bez uložení.",
            ),
            requested_changes=requested,
        )
    return changes
