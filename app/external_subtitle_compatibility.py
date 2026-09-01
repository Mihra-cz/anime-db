from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import EpisodeNumberDetection
from .catalog_video_presentation import (
    video_variant_display_for_video,
    video_variant_group_technical_details,
)
from .models import (
    ExternalSubtitle,
    ExternalSubtitleCompatibility,
    Video,
    utc_now,
)
from .numbering import (
    LogicalEpisodeIdentity,
    is_nonprimary_duplicate_video,
    logical_episode_identity,
)
from .subtitles import safe_subtitle_matches


AUTOMATIC_MATCH = "automatic_match"
CONFIRMED_COMPATIBLE = "confirmed_compatible"
CONFIRMED_INCOMPATIBLE = "confirmed_incompatible"
COMPATIBILITY_STATUSES = frozenset({
    AUTOMATIC_MATCH,
    CONFIRMED_COMPATIBLE,
    CONFIRMED_INCOMPATIBLE,
})
HUMAN_COMPATIBILITY_STATUSES = frozenset({
    CONFIRMED_COMPATIBLE,
    CONFIRMED_INCOMPATIBLE,
})

MATCH_METHOD_FILENAME = "filename"
MATCH_METHOD_MANUAL = "manual"
MATCH_METHOD_LEGACY_BACKFILL = "legacy_backfill"
COMPATIBILITY_MATCH_METHODS = frozenset({
    MATCH_METHOD_FILENAME,
    MATCH_METHOD_MANUAL,
    MATCH_METHOD_LEGACY_BACKFILL,
})

STATUS_LABELS = {
    None: "Neurčeno",
    AUTOMATIC_MATCH: "Automaticky přiřazeno",
    CONFIRMED_COMPATIBLE: "Ručně potvrzeno kompatibilní",
    CONFIRMED_INCOMPATIBLE: "Ručně potvrzeno nekompatibilní",
}
MATCH_METHOD_LABELS = {
    MATCH_METHOD_FILENAME: "Automatické přiřazení podle názvu",
    MATCH_METHOD_MANUAL: "Ruční rozhodnutí",
    MATCH_METHOD_LEGACY_BACKFILL: "Historická automatická vazba",
}
MANUAL_DECISION_LABELS = {
    "unknown": "Bez ručního rozhodnutí",
    CONFIRMED_COMPATIBLE: "Potvrdit kompatibilitu",
    CONFIRMED_INCOMPATIBLE: "Potvrdit nekompatibilitu",
}
POSITIVE_COMPATIBILITY_STATUSES = frozenset({
    AUTOMATIC_MATCH,
    CONFIRMED_COMPATIBLE,
})


@dataclass(frozen=True)
class CompatibilityCandidate:
    video: Video
    compatibility: ExternalSubtitleCompatibility | None
    eligible: bool
    episode_label: str
    variant_label: str
    variant_details: tuple[str, ...]

    @property
    def status(self) -> str | None:
        return self.compatibility.status if self.compatibility is not None else None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


@dataclass(frozen=True)
class SubtitleCompatibilityPresentation:
    subtitle: ExternalSubtitle
    candidates: tuple[CompatibilityCandidate, ...]


@dataclass(frozen=True)
class CompatibilityDecisionPreview:
    subtitle: ExternalSubtitle
    target: CompatibilityCandidate
    decision: str
    current_status: str | None
    resulting_status: str | None
    note: str | None
    fingerprint: str

    @property
    def current_label(self) -> str:
        return STATUS_LABELS[self.current_status]

    @property
    def resulting_label(self) -> str:
        return STATUS_LABELS[self.resulting_status]

    @property
    def decision_label(self) -> str:
        return MANUAL_DECISION_LABELS[self.decision]


@dataclass(frozen=True)
class CompatibilityCandidateIndex:
    """One request-scoped index for all compatibility candidate lookups."""

    videos: tuple[Video, ...]
    videos_by_id: Mapping[int, Video]
    identity_by_video_id: Mapping[int, LogicalEpisodeIdentity | None]
    videos_by_identity: Mapping[LogicalEpisodeIdentity, tuple[Video, ...]]


@dataclass(frozen=True)
class VideoExternalSubtitleState:
    """Read-only compatibility projection for one physical Video."""

    compatible_subtitles: tuple[ExternalSubtitle, ...]
    incompatible_subtitles: tuple[ExternalSubtitle, ...]
    unknown_candidate_subtitles: tuple[ExternalSubtitle, ...]


def compatibility_status_label(status: str | None) -> str:
    if status not in STATUS_LABELS:
        return status or STATUS_LABELS[None]
    return STATUS_LABELS[status]


def compatibility_match_method_label(method: str) -> str:
    return MATCH_METHOD_LABELS.get(method, method)


def external_subtitle_compatibility_status(
    subtitle: ExternalSubtitle,
    video: Video,
) -> str | None:
    row = get_compatibility(subtitle, video)
    return row.status if row is not None else None


def external_subtitle_is_shared(subtitle: ExternalSubtitle) -> bool:
    """Return whether one physical asset has relationships to multiple Videos."""
    return len({row.video_id for row in subtitle.compatibilities}) > 1


def effective_external_subtitle_compatibilities_for_video(
    video: Video,
) -> tuple[ExternalSubtitleCompatibility, ...]:
    """Return positive association authority; the legacy owner is irrelevant."""
    return tuple(sorted(
        (
            row for row in video.external_subtitle_compatibilities
            if row.status in POSITIVE_COMPATIBILITY_STATUSES
        ),
        key=lambda row: (
            row.external_subtitle.relative_path.casefold(),
            row.external_subtitle_id or 0,
        ),
    ))


def effective_external_subtitles_for_video(
    video: Video,
) -> tuple[ExternalSubtitle, ...]:
    """Resolve physical assets positively compatible with one Video.

    No association is unknown, and ``confirmed_incompatible`` is negative
    human authority. Physical assets have no owning Video.
    """
    assets: dict[tuple[str, int | str], ExternalSubtitle] = {}
    for row in effective_external_subtitle_compatibilities_for_video(video):
        subtitle = row.external_subtitle
        key = (
            "id", subtitle.id
        ) if subtitle.id is not None else ("object", str(id(subtitle)))
        assets[key] = subtitle
    return tuple(sorted(
        assets.values(), key=lambda item: (item.relative_path.casefold(), item.id or 0)
    ))


def _same_pair(
    row: ExternalSubtitleCompatibility,
    subtitle: ExternalSubtitle,
    video: Video,
) -> bool:
    subtitle_matches = (
        row.external_subtitle_id == subtitle.id
        if subtitle.id is not None else
        row.__dict__.get("external_subtitle") is subtitle
    )
    video_matches = (
        row.video_id == video.id
        if video.id is not None else row.__dict__.get("video") is video
    )
    return subtitle_matches and video_matches


def get_compatibility(
    subtitle: ExternalSubtitle,
    video: Video,
) -> ExternalSubtitleCompatibility | None:
    """Return the explicit pair row; no row is the only unknown state."""
    return next(
        (row for row in subtitle.compatibilities if _same_pair(row, subtitle, video)),
        None,
    )


def _stored_compatibility(
    session: Session,
    subtitle: ExternalSubtitle,
    video: Video,
) -> ExternalSubtitleCompatibility | None:
    in_memory = get_compatibility(subtitle, video)
    if in_memory is not None or subtitle.id is None or video.id is None:
        return in_memory
    return session.scalar(select(ExternalSubtitleCompatibility).where(
        ExternalSubtitleCompatibility.external_subtitle_id == subtitle.id,
        ExternalSubtitleCompatibility.video_id == video.id,
    ))


def _new_pair(
    subtitle: ExternalSubtitle,
    video: Video,
    *,
    status: str,
    match_method: str,
    verified_at: datetime | None,
    note: str | None,
) -> ExternalSubtitleCompatibility:
    row = ExternalSubtitleCompatibility(
        external_subtitle=subtitle,
        video=video,
        status=status,
        match_method=match_method,
        verified_at=verified_at,
        note=note,
    )
    return row


def _remove_pair(
    session: Session,
    subtitle: ExternalSubtitle,
    row: ExternalSubtitleCompatibility,
) -> None:
    """Delete a pair and keep the already-loaded asset collection truthful."""
    if row in subtitle.compatibilities:
        subtitle.compatibilities.remove(row)
    else:
        session.delete(row)


def synchronize_automatic_match(
    session: Session,
    subtitle: ExternalSubtitle,
    video: Video,
) -> ExternalSubtitleCompatibility:
    """Synchronize only scanner-owned evidence and preserve all human rows."""
    row = _stored_compatibility(session, subtitle, video)
    if row is None:
        row = _new_pair(
            subtitle,
            video,
            status=AUTOMATIC_MATCH,
            match_method=MATCH_METHOD_FILENAME,
            verified_at=None,
            note=None,
        )
        session.add(row)
    elif row.status == AUTOMATIC_MATCH:
        row.match_method = MATCH_METHOD_FILENAME
        row.verified_at = None

    # Scanner owns automatic evidence only. A unique match can replace stale
    # automatic rows, but never a confirmed compatible/incompatible decision.
    target_id = video.id
    for other in list(subtitle.compatibilities):
        if (
            other is not row
            and other.status == AUTOMATIC_MATCH
            and (target_id is None or other.video_id != target_id)
        ):
            _remove_pair(session, subtitle, other)
    return row


def remove_automatic_matches(
    session: Session,
    subtitle: ExternalSubtitle,
) -> None:
    """Remove stale scanner evidence while preserving every human decision."""
    for row in list(subtitle.compatibilities):
        if row.status == AUTOMATIC_MATCH:
            _remove_pair(session, subtitle, row)


def backfill_legacy_external_subtitle_compatibilities(
    session: Session,
    legacy_links: Iterable[tuple[int, int | None, str]],
) -> int:
    """Convert captured legacy owners to compatibility authority once.

    ``legacy_links`` is read from the old table before its ``video_id`` column
    is removed. Existing association rows always win, so human authority and
    its timestamp/note are never overwritten.
    """
    valid_video_ids = set(session.scalars(select(Video.id)))
    existing_pairs = {
        (row.external_subtitle_id, row.video_id)
        for row in session.scalars(select(ExternalSubtitleCompatibility))
    }
    created = 0
    verified_at = utc_now()
    valid_subtitle_ids = set(session.scalars(select(ExternalSubtitle.id)))
    for subtitle_id, video_id, asset_match_method in sorted(
        legacy_links, key=lambda item: (item[0], item[1] or 0, item[2])
    ):
        pair = (subtitle_id, video_id)
        if (
            subtitle_id not in valid_subtitle_ids
            or video_id is None
            or video_id not in valid_video_ids
            or pair in existing_pairs
        ):
            continue
        is_manual = asset_match_method == "manual"
        session.add(ExternalSubtitleCompatibility(
            external_subtitle_id=subtitle_id,
            video_id=video_id,
            status=CONFIRMED_COMPATIBLE if is_manual else AUTOMATIC_MATCH,
            match_method=(
                MATCH_METHOD_MANUAL if is_manual else MATCH_METHOD_LEGACY_BACKFILL
            ),
            verified_at=verified_at if is_manual else None,
            note=None,
        ))
        existing_pairs.add(pair)
        created += 1
    return created


def consolidate_legacy_external_subtitle_assets(session: Session) -> int:
    """Collapse historical per-video rows into one physical-path asset.

    Old databases used ``(video_id, relative_path)`` as their unique key. The
    compatibility backfill runs first, so every former owner is retained as an
    M:N relationship before duplicate physical rows are removed.
    """
    assets_by_path: dict[str, list[ExternalSubtitle]] = defaultdict(list)
    for subtitle in session.scalars(
        select(ExternalSubtitle).order_by(ExternalSubtitle.id)
    ):
        assets_by_path[subtitle.relative_path].append(subtitle)

    removed = 0
    for relative_path, assets in assets_by_path.items():
        if len(assets) < 2:
            continue
        keeper = min(
            assets,
            key=lambda item: (
                item.match_method != "manual",
                item.manual_language is None,
                item.id or 0,
            ),
        )
        for duplicate in assets:
            if duplicate is keeper:
                continue
            existing_by_video = {
                row.video_id: row for row in keeper.compatibilities
            }
            for row in list(duplicate.compatibilities):
                existing = existing_by_video.get(row.video_id)
                if existing is None:
                    row.external_subtitle = keeper
                    existing_by_video[row.video_id] = row
                    continue
                existing_human = existing.status in HUMAN_COMPATIBILITY_STATUSES
                row_human = row.status in HUMAN_COMPATIBILITY_STATUSES
                if existing_human and row_human and existing.status != row.status:
                    raise ValueError(
                        "Historický fyzický subtitle asset má pro stejné video "
                        "protichůdná ruční compatibility rozhodnutí: "
                        f"{relative_path}"
                    )
                if row_human and not existing_human:
                    session.delete(existing)
                    session.flush()
                    row.external_subtitle = keeper
                    existing_by_video[row.video_id] = row
                else:
                    session.delete(row)
            if duplicate.manual_language is not None:
                if (
                    keeper.manual_language is not None
                    and keeper.manual_language != duplicate.manual_language
                ):
                    raise ValueError(
                        "Historický fyzický subtitle asset má protichůdné ruční "
                        f"jazykové override: {relative_path}"
                    )
                keeper.manual_language = duplicate.manual_language
            session.flush()
            session.delete(duplicate)
            session.flush()
            removed += 1
    return removed


def _normalize_note(note: str | None) -> str | None:
    normalized = (note or "").strip()
    if len(normalized) > 1000:
        raise ValueError("Poznámka ke kompatibilitě smí mít nejvýše 1000 znaků.")
    return normalized or None


def _confirm(
    session: Session,
    subtitle: ExternalSubtitle,
    video: Video,
    status: str,
    *,
    note: str | None = None,
    verified_at: datetime | None = None,
) -> ExternalSubtitleCompatibility:
    if status not in HUMAN_COMPATIBILITY_STATUSES:
        raise ValueError("Neplatný ruční stav kompatibility titulků.")
    normalized_note = _normalize_note(note)
    row = _stored_compatibility(session, subtitle, video)
    if row is None:
        row = _new_pair(
            subtitle,
            video,
            status=status,
            match_method=MATCH_METHOD_MANUAL,
            verified_at=verified_at or utc_now(),
            note=normalized_note,
        )
        session.add(row)
        return row
    if (
        row.status == status
        and row.match_method == MATCH_METHOD_MANUAL
        and row.note == normalized_note
        and row.verified_at is not None
    ):
        return row
    row.status = status
    row.match_method = MATCH_METHOD_MANUAL
    row.verified_at = verified_at or utc_now()
    row.note = normalized_note
    return row


def confirm_compatible(
    session: Session,
    subtitle: ExternalSubtitle,
    video: Video,
    *,
    note: str | None = None,
    verified_at: datetime | None = None,
) -> ExternalSubtitleCompatibility:
    return _confirm(
        session,
        subtitle,
        video,
        CONFIRMED_COMPATIBLE,
        note=note,
        verified_at=verified_at,
    )


def confirm_incompatible(
    session: Session,
    subtitle: ExternalSubtitle,
    video: Video,
    *,
    note: str | None = None,
    verified_at: datetime | None = None,
) -> ExternalSubtitleCompatibility:
    return _confirm(
        session,
        subtitle,
        video,
        CONFIRMED_INCOMPATIBLE,
        note=note,
        verified_at=verified_at,
    )


def automatic_evidence_target(
    subtitle: ExternalSubtitle,
    videos: Iterable[Video],
) -> Video | None:
    """Re-evaluate the current scanner filename rule from persisted paths."""
    subtitle_path = PurePosixPath(subtitle.relative_path)
    candidates = [
        video for video in videos
        if PurePosixPath(video.relative_path).parent == subtitle_path.parent
    ]
    paths = [Path(video.relative_path) for video in candidates]
    _method, matched_paths = safe_subtitle_matches(
        paths, Path(subtitle.relative_path)
    )
    if len(matched_paths) != 1:
        return None
    matched = matched_paths[0].as_posix()
    return next((video for video in candidates if video.relative_path == matched), None)


def clear_manual_decision(
    session: Session,
    subtitle: ExternalSubtitle,
    video: Video,
    *,
    videos: Iterable[Video],
) -> ExternalSubtitleCompatibility | None:
    """Restore current automatic evidence, otherwise restore no-row unknown."""
    row = _stored_compatibility(session, subtitle, video)
    if row is None:
        return None
    evidence = automatic_evidence_target(subtitle, videos)
    if evidence is not None and evidence.id == video.id:
        row.status = AUTOMATIC_MATCH
        row.match_method = MATCH_METHOD_FILENAME
        row.verified_at = None
        row.note = None
        return row
    _remove_pair(session, subtitle, row)
    return None


def _candidate_sort_key(video: Video) -> tuple:
    return (
        video.season_episode_number is None,
        video.season_episode_number or 0,
        (video.video_variant_group.manual_label.casefold()
         if video.__dict__.get("video_variant_group") is not None else ""),
        video.filename.casefold(),
        video.id or 0,
    )


def build_compatibility_candidate_index(
    videos: Iterable[Video],
    *, detections: Mapping[Video, EpisodeNumberDetection] | None = None,
) -> CompatibilityCandidateIndex:
    """Calculate central logical identities once for the whole request."""
    known = tuple(videos)
    videos_by_id = {
        video.id: video for video in known if video.id is not None
    }
    identity_by_video_id: dict[int, LogicalEpisodeIdentity | None] = {}
    identity_buckets: dict[LogicalEpisodeIdentity, list[Video]] = {}
    for video_id, video in videos_by_id.items():
        identity = logical_episode_identity(
            video,
            detection=detections.get(video) if detections is not None else None,
        )
        identity_by_video_id[video_id] = identity
        if identity is not None:
            identity_buckets.setdefault(identity, []).append(video)
    return CompatibilityCandidateIndex(
        videos=known,
        videos_by_id=videos_by_id,
        identity_by_video_id=identity_by_video_id,
        videos_by_identity={
            identity: tuple(bucket)
            for identity, bucket in identity_buckets.items()
        },
    )


def candidate_variant_videos(
    subtitle: ExternalSubtitle,
    videos: Iterable[Video] | CompatibilityCandidateIndex,
) -> tuple[CompatibilityCandidate, ...]:
    """Return same-logical-episode targets without inferring compatibility."""
    index = (
        videos
        if isinstance(videos, CompatibilityCandidateIndex)
        else build_compatibility_candidate_index(videos)
    )
    compat_by_video_id = {
        row.video_id: row for row in subtitle.compatibilities
        if row.video_id is not None
    }
    anchor_video_ids = sorted(
        compat_by_video_id,
        key=lambda video_id: (
            compat_by_video_id[video_id].status not in POSITIVE_COMPATIBILITY_STATUSES,
            compat_by_video_id[video_id].status == CONFIRMED_INCOMPATIBLE,
            video_id,
        ),
    )
    anchor = (
        index.videos_by_id.get(anchor_video_ids[0])
        if anchor_video_ids else None
    )
    if anchor is None:
        return ()

    anchor_identity = index.identity_by_video_id.get(anchor.id)
    eligible: list[Video]
    if anchor_identity is None:
        eligible = [anchor]
    else:
        eligible = [
            video for video in index.videos_by_identity.get(anchor_identity, ())
            if (
                not is_nonprimary_duplicate_video(video)
                or video.id == anchor.id
                or video.id in compat_by_video_id
            )
        ]
    eligible_ids = {video.id for video in eligible}
    visible = {video.id: video for video in eligible if video.id is not None}
    for video_id in compat_by_video_id:
        if video_id in index.videos_by_id:
            visible.setdefault(video_id, index.videos_by_id[video_id])

    return tuple(
        CompatibilityCandidate(
            video=video,
            compatibility=compat_by_video_id.get(video.id),
            eligible=video.id in eligible_ids,
            episode_label=(
                f"E{video.season_episode_number:02d}"
                if video.season_episode_number is not None else "Bez canonical epizody"
            ),
            variant_label=(
                video_variant_display_for_video(video, include_unassigned=True)
                or "Varianta neurčena"
            ),
            variant_details=video_variant_group_technical_details(
                video.__dict__.get("video_variant_group")
            ),
        )
        for video in sorted(visible.values(), key=_candidate_sort_key)
    )


def build_video_external_subtitle_states(
    videos: Iterable[Video],
    *,
    candidate_index: CompatibilityCandidateIndex | None = None,
) -> dict[int, VideoExternalSubtitleState]:
    """Build compatible, incompatible and unknown candidate assets in one pass.

    Positive and negative facts come directly from M:N authority. Unknown
    candidates use the same bounded logical-episode scope as the manual
    compatibility workflow and never imply compatibility.
    """
    index = candidate_index or build_compatibility_candidate_index(videos)
    compatible: dict[int, dict[int, ExternalSubtitle]] = defaultdict(dict)
    incompatible: dict[int, dict[int, ExternalSubtitle]] = defaultdict(dict)
    unknown: dict[int, dict[int, ExternalSubtitle]] = defaultdict(dict)

    for video_id, video in index.videos_by_id.items():
        for row in video.external_subtitle_compatibilities:
            subtitle = row.external_subtitle
            if subtitle.id is None:
                continue
            if row.status in POSITIVE_COMPATIBILITY_STATUSES:
                compatible[video_id][subtitle.id] = subtitle
            elif row.status == CONFIRMED_INCOMPATIBLE:
                incompatible[video_id][subtitle.id] = subtitle

    subtitles = {
        row.external_subtitle.id: row.external_subtitle
        for video in index.videos
        for row in video.external_subtitle_compatibilities
        if row.external_subtitle.id is not None
    }
    for subtitle in subtitles.values():
        for candidate in candidate_variant_videos(subtitle, index):
            video_id = candidate.video.id
            if (
                video_id is not None
                and candidate.eligible
                and candidate.compatibility is None
            ):
                unknown[video_id][subtitle.id] = subtitle

    def ordered(items: dict[int, ExternalSubtitle]) -> tuple[ExternalSubtitle, ...]:
        return tuple(sorted(
            items.values(),
            key=lambda subtitle: (
                subtitle.relative_path.casefold(), subtitle.id or 0
            ),
        ))

    return {
        video_id: VideoExternalSubtitleState(
            compatible_subtitles=ordered(compatible[video_id]),
            incompatible_subtitles=ordered(incompatible[video_id]),
            unknown_candidate_subtitles=ordered(unknown[video_id]),
        )
        for video_id in index.videos_by_id
    }


def build_compatibility_presentations(
    videos: Iterable[Video],
    *,
    presentation_videos: Iterable[Video] | None = None,
    presentation_subtitles: Iterable[ExternalSubtitle] | None = None,
    candidate_index: CompatibilityCandidateIndex | None = None,
) -> dict[int, SubtitleCompatibilityPresentation]:
    index = candidate_index or build_compatibility_candidate_index(videos)
    if presentation_subtitles is not None:
        subtitles = {
            subtitle.id: subtitle
            for subtitle in presentation_subtitles
            if subtitle.id is not None
        }
        rows = {}
        for subtitle in subtitles.values():
            rows[subtitle.id] = SubtitleCompatibilityPresentation(
                subtitle=subtitle,
                candidates=candidate_variant_videos(subtitle, index),
            )
        return rows

    owners = (
        index.videos
        if presentation_videos is None else tuple(presentation_videos)
    )
    rows = {}
    subtitles = {
        relation.external_subtitle.id: relation.external_subtitle
        for video in owners
        for relation in video.external_subtitle_compatibilities
        if relation.external_subtitle.id is not None
    }
    for subtitle in subtitles.values():
        rows[subtitle.id] = SubtitleCompatibilityPresentation(
            subtitle=subtitle,
            candidates=candidate_variant_videos(subtitle, index),
        )
    return rows


def _require_candidate(
    subtitle: ExternalSubtitle,
    video_id: int,
    videos: Iterable[Video],
) -> CompatibilityCandidate:
    candidate = next(
        (
            candidate for candidate in candidate_variant_videos(subtitle, videos)
            if candidate.video.id == video_id
        ),
        None,
    )
    if candidate is None or not candidate.eligible:
        raise ValueError(
            "Vybrané video není bezpečný candidate stejné logické epizody a title."
        )
    return candidate


def _decision_result_status(
    subtitle: ExternalSubtitle,
    candidate: CompatibilityCandidate,
    decision: str,
    videos: Iterable[Video],
) -> str | None:
    if decision in HUMAN_COMPATIBILITY_STATUSES:
        return decision
    evidence = automatic_evidence_target(subtitle, videos)
    return (
        AUTOMATIC_MATCH
        if evidence is not None and evidence.id == candidate.video.id else None
    )


def _decision_fingerprint(
    subtitle: ExternalSubtitle,
    videos: Iterable[Video],
    *,
    video_id: int,
    decision: str,
    note: str | None,
) -> str:
    known = tuple(videos)
    payload = {
        "subtitle": (
            subtitle.id,
            subtitle.relative_path,
            subtitle.match_method,
            subtitle.language,
            subtitle.normalized_language,
            subtitle.manual_language,
        ),
        "compatibilities": sorted(
            (
                row.id,
                row.video_id,
                row.status,
                row.match_method,
                row.verified_at.isoformat() if row.verified_at else None,
                row.note,
            )
            for row in subtitle.compatibilities
        ),
        "videos": sorted(
            (
                video.id,
                video.catalog_title_id,
                video.season_episode_number,
                video.video_variant_group_id,
                video.duplicate_of_video_id,
                video.duplicate_primary_missing,
                video.relative_path,
            )
            for video in known
        ),
        "proposal": (video_id, decision, note),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def preview_compatibility_decision(
    subtitle: ExternalSubtitle,
    video_id: int,
    decision: str,
    *,
    note: str | None,
    videos: Iterable[Video],
) -> CompatibilityDecisionPreview:
    normalized_decision = (decision or "").strip().casefold()
    if normalized_decision not in {*HUMAN_COMPATIBILITY_STATUSES, "unknown"}:
        raise ValueError("Neplatné rozhodnutí o kompatibilitě titulků.")
    normalized_note = _normalize_note(note)
    known = tuple(videos)
    candidate = _require_candidate(subtitle, int(video_id), known)
    current_status = candidate.status
    result_status = _decision_result_status(
        subtitle, candidate, normalized_decision, known
    )
    return CompatibilityDecisionPreview(
        subtitle=subtitle,
        target=candidate,
        decision=normalized_decision,
        current_status=current_status,
        resulting_status=result_status,
        note=normalized_note,
        fingerprint=_decision_fingerprint(
            subtitle,
            known,
            video_id=int(video_id),
            decision=normalized_decision,
            note=normalized_note,
        ),
    )


def apply_compatibility_decision(
    session: Session,
    subtitle: ExternalSubtitle,
    video_id: int,
    decision: str,
    *,
    note: str | None,
    videos: Iterable[Video],
    expected_fingerprint: str,
) -> CompatibilityDecisionPreview:
    known = tuple(videos)
    preview = preview_compatibility_decision(
        subtitle, video_id, decision, note=note, videos=known
    )
    if not expected_fingerprint or preview.fingerprint != expected_fingerprint:
        raise ValueError(
            "Náhled kompatibility už neodpovídá aktuálnímu stavu. Načtěte jej znovu."
        )
    target = session.get(Video, video_id)
    if target is None:
        raise ValueError("Vybrané video už neexistuje.")
    if preview.decision == CONFIRMED_COMPATIBLE:
        confirm_compatible(session, subtitle, target, note=preview.note)
    elif preview.decision == CONFIRMED_INCOMPATIBLE:
        confirm_incompatible(session, subtitle, target, note=preview.note)
    else:
        clear_manual_decision(session, subtitle, target, videos=known)
    session.flush()
    return preview
