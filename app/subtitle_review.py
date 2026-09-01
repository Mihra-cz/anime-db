from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import json
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import (
    EpisodeNumberDetection, SUPPLEMENTARY_SUBTYPE_TO_FILE_TYPE,
    detect_episode_number, normalize_language,
)
from .external_subtitle_compatibility import confirm_compatible
from .models import ExternalSubtitle, UnresolvedExternalSubtitle, Video

MAX_SUBTITLE_CANDIDATES = 12


@dataclass(frozen=True)
class SubtitleCandidate:
    video: Video
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class UnresolvedSubtitleRow:
    subtitle: UnresolvedExternalSubtitle
    context_label: str
    candidate_scope: str
    candidates: tuple[SubtitleCandidate, ...]
    candidate_count: int
    rejected_count: int


@dataclass(frozen=True)
class SubtitleCandidateIndex:
    """Immutable request-local paths and parser facts for unresolved assets."""

    by_directory: Mapping[PurePosixPath, tuple[Video, ...]]
    by_title_root: Mapping[str, tuple[Video, ...]]
    by_collection_root: Mapping[str, tuple[Video, ...]]
    by_root_folder: Mapping[str, tuple[Video, ...]]
    detections: Mapping[Video, EpisodeNumberDetection]
    normalized_stems: Mapping[Video, str]


def rejected_video_ids(subtitle: UnresolvedExternalSubtitle) -> set[int]:
    try:
        values = json.loads(subtitle.rejected_video_ids_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    return {
        value for value in values
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }


def _store_rejected_video_ids(
    subtitle: UnresolvedExternalSubtitle, video_ids: set[int],
) -> None:
    subtitle.rejected_video_ids_json = json.dumps(sorted(video_ids))


def build_subtitle_candidate_index(
    videos: list[Video],
    *, detections: Mapping[Video, EpisodeNumberDetection] | None = None,
) -> SubtitleCandidateIndex:
    by_directory: dict[PurePosixPath, list[Video]] = {}
    by_title_root: dict[str, list[Video]] = {}
    by_collection_root: dict[str, list[Video]] = {}
    by_root_folder: dict[str, list[Video]] = {}
    for video in videos:
        by_directory.setdefault(
            PurePosixPath(video.relative_path).parent, []
        ).append(video)
        if video.catalog_title is not None:
            by_title_root.setdefault(
                video.catalog_title.relative_root_path, []
            ).append(video)
        if video.catalog_collection is not None:
            by_collection_root.setdefault(
                video.catalog_collection.relative_root_path, []
            ).append(video)
        by_root_folder.setdefault(video.root_folder, []).append(video)
    resolved_detections = dict(detections) if detections is not None else {
        video: detect_episode_number(video.filename) for video in videos
    }
    return SubtitleCandidateIndex(
        by_directory=MappingProxyType({
            key: tuple(values) for key, values in by_directory.items()
        }),
        by_title_root=MappingProxyType({
            key: tuple(values) for key, values in by_title_root.items()
        }),
        by_collection_root=MappingProxyType({
            key: tuple(values) for key, values in by_collection_root.items()
        }),
        by_root_folder=MappingProxyType({
            key: tuple(values) for key, values in by_root_folder.items()
        }),
        detections=MappingProxyType(resolved_detections),
        normalized_stems=MappingProxyType({
            video: _normalized_stem(video.filename) for video in videos
        }),
    )


def _candidate_pool(
    subtitle: UnresolvedExternalSubtitle, index: SubtitleCandidateIndex,
) -> tuple[str, list[Video]]:
    subtitle_path = PurePosixPath(subtitle.relative_path)
    same_directory = index.by_directory.get(subtitle_path.parent, ())
    if same_directory:
        return "stejný adresář", list(same_directory)

    for parent in subtitle_path.parents:
        title_videos = index.by_title_root.get(str(parent))
        if title_videos:
            return "stejná část anime", list(title_videos)

    for parent in subtitle_path.parents:
        collection_videos = index.by_collection_root.get(str(parent))
        if collection_videos:
            return "stejná kolekce", list(collection_videos)

    root_folder = subtitle_path.parts[0] if len(subtitle_path.parts) > 1 else "."
    same_root = list(index.by_root_folder.get(root_folder, ()))
    return ("stejný kořen anime", same_root) if same_root else ("bez kandidátů", [])


def _normalized_stem(filename: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", PurePosixPath(filename).stem.casefold()))


def _rank_candidate(
    subtitle: UnresolvedExternalSubtitle, video: Video, scope: str,
    *, subtitle_number: EpisodeNumberDetection,
    video_number: EpisodeNumberDetection,
    video_stem: str,
) -> SubtitleCandidate:
    subtitle_stem = _normalized_stem(subtitle.filename)
    similarity = SequenceMatcher(None, subtitle_stem, video_stem).ratio()
    score = similarity
    reasons = [scope, f"podobnost názvu {round(similarity * 100)} %"]
    if (
        subtitle_number.sortable_episode_value is not None
        and subtitle_number.sortable_episode_value == video_number.sortable_episode_value
    ):
        score += 0.25
        if "shodný číselný hint" not in scope:
            reasons.append("shodný číselný hint")
    return SubtitleCandidate(video=video, score=score, reasons=tuple(reasons))


def subtitle_candidates(
    subtitle: UnresolvedExternalSubtitle, videos: list[Video],
    *, candidate_index: SubtitleCandidateIndex | None = None,
) -> tuple[str, tuple[SubtitleCandidate, ...], int]:
    index = candidate_index or build_subtitle_candidate_index(videos)
    scope, pool = _candidate_pool(subtitle, index)
    subtitle_number = detect_episode_number(subtitle.filename)
    if subtitle_number.is_supplementary and subtitle_number.supplementary_type:
        expected_file_type = SUPPLEMENTARY_SUBTYPE_TO_FILE_TYPE[
            subtitle_number.supplementary_type
        ]
        same_type = [video for video in pool if video.file_type == expected_file_type]
        if same_type:
            pool = same_type
            scope = f"{scope} · shodný supplementary typ"
    elif subtitle_number.sortable_episode_value is not None:
        same_number = [
            video for video in pool
            if index.detections[video].sortable_episode_value
            == subtitle_number.sortable_episode_value
        ]
        if same_number:
            pool = same_number
            scope = f"{scope} · shodný číselný hint"
    rejected = rejected_video_ids(subtitle)
    ranked = sorted(
        (
            _rank_candidate(
                subtitle, video, scope,
                subtitle_number=subtitle_number,
                video_number=index.detections[video],
                video_stem=index.normalized_stems[video],
            )
            for video in pool
            if video.id not in rejected
        ),
        key=lambda candidate: (
            -candidate.score,
            candidate.video.filename.casefold(),
            candidate.video.id or 0,
        ),
    )
    return scope, tuple(ranked[:MAX_SUBTITLE_CANDIDATES]), len(ranked)


def build_unresolved_subtitle_rows(
    subtitles: list[UnresolvedExternalSubtitle], videos: list[Video],
    *, candidate_index: SubtitleCandidateIndex | None = None,
) -> tuple[UnresolvedSubtitleRow, ...]:
    index = candidate_index or build_subtitle_candidate_index(videos)
    rows = []
    for subtitle in sorted(subtitles, key=lambda item: item.relative_path.casefold()):
        scope, candidates, count = subtitle_candidates(
            subtitle, videos, candidate_index=index,
        )
        if candidates:
            video = candidates[0].video
            if video.catalog_title is not None:
                context = video.catalog_title.local_title
            elif video.catalog_collection is not None:
                context = video.catalog_collection.local_title
            else:
                context = video.root_folder
        else:
            path = PurePosixPath(subtitle.relative_path)
            context = path.parts[0] if len(path.parts) > 1 else "Kořen knihovny"
        rows.append(UnresolvedSubtitleRow(
            subtitle=subtitle,
            context_label=context,
            candidate_scope=scope,
            candidates=candidates,
            candidate_count=count,
            rejected_count=len(rejected_video_ids(subtitle)),
        ))
    return tuple(rows)


def set_subtitle_candidate_rejected(
    subtitle: UnresolvedExternalSubtitle, video_id: int, rejected: bool,
) -> None:
    values = rejected_video_ids(subtitle)
    if rejected:
        values.add(video_id)
    else:
        values.discard(video_id)
    _store_rejected_video_ids(subtitle, values)


def confirm_subtitle_no_match(subtitle: UnresolvedExternalSubtitle) -> None:
    subtitle.status = "confirmed_no_match"


def reopen_subtitle_review(subtitle: UnresolvedExternalSubtitle) -> None:
    subtitle.status = "unresolved"


def manually_link_subtitle(
    session: Session, subtitle: UnresolvedExternalSubtitle, video: Video,
) -> ExternalSubtitle:
    existing = session.scalar(select(ExternalSubtitle).where(
        ExternalSubtitle.relative_path == subtitle.relative_path
    ))
    if existing is not None:
        raise ValueError("Tento fyzický soubor titulků už je přiřazen.")
    linked = ExternalSubtitle(
        relative_path=subtitle.relative_path,
        codec=subtitle.extension.lstrip("."),
        language=subtitle.language,
        normalized_language=normalize_language(
            subtitle.normalized_language or subtitle.language
        ),
        match_method="manual",
    )
    session.add(linked)
    confirm_compatible(session, linked, video)
    session.delete(subtitle)
    return linked


def reopen_manual_subtitle_link(
    session: Session, subtitle: ExternalSubtitle,
) -> UnresolvedExternalSubtitle:
    if subtitle.match_method != "manual":
        raise ValueError("Automatické bezpečné přiřazení nelze vrátit touto akcí.")
    if len(subtitle.compatibilities) > 1:
        raise ValueError(
            "Titulek má další ruční compatibility rozhodnutí. Nejprve je "
            "vraťte na neurčeno."
        )
    unresolved = UnresolvedExternalSubtitle(
        relative_path=subtitle.relative_path,
        filename=PurePosixPath(subtitle.relative_path).name,
        extension=PurePosixPath(subtitle.relative_path).suffix.casefold(),
        language=subtitle.language,
        normalized_language=subtitle.normalized_language,
        status="unresolved",
    )
    session.add(unresolved)
    session.delete(subtitle)
    return unresolved


def can_reopen_manual_subtitle_link(subtitle: ExternalSubtitle) -> bool:
    """Mirror reopen validation for an owner-less physical asset."""
    return subtitle.match_method == "manual" and len(subtitle.compatibilities) <= 1
