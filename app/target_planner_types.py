"""Immutable evidence and derived plan records; none of these are persisted."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .physical_layout import PhysicalLayoutContext
from .physical_naming import PhysicalNamingContext


@dataclass(frozen=True)
class SourceCollection:
    id: int
    locators: tuple[str, ...]


@dataclass(frozen=True)
class TargetTitle:
    id: int
    collection_id: int | None
    kind: str
    season: int | None
    part: int | None
    source_locator: str = ''
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetVideo:
    id: int
    title_id: int | None
    collection_id: int | None
    source: str
    content_type: str
    episode_number: int | None = None
    ordinal: int | None = None
    recap_position: Decimal | int | None = None
    media_part: int | None = None
    variant_id: int | None = None
    variant_label: str | None = None
    release_source: str | None = None
    duplicate_primary_id: int | None = None
    duplicate_validity: str | None = None
    issues: tuple[str, ...] = ()
    # Detached copy of persisted parser evidence, immutable across projection.
    # This is a read-model field, never a new DB column or physical basename.
    source_evidence_filename: str | None = None


@dataclass(frozen=True)
class TargetSubtitle:
    id: int
    source: str
    compatibility: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class UnmatchedSubtitle:
    id: int
    source: str
    match_status: str
    source_evidence_filename: str | None = None


class SideAssetAccounting(str, Enum):
    COMPLETE = 'COMPLETE'
    INCOMPLETE = 'INCOMPLETE'
    UNKNOWN = 'UNKNOWN'


@dataclass(frozen=True)
class SideAssetEvidence:
    source: str
    secondary_video_id: int
    provenance: str
    expected_size: int | None = None
    expected_sha256: str | None = None
    object_kind: str = 'duplicate_side_asset'
    object_id: int | str | None = None
    # Primary archive locator in the verifier's snapshot (post-state Subs
    # target after projection); its approved hash does not require a re-read.
    primary_archive_source: str | None = None
    primary_archive_sha256: str | None = None


@dataclass(frozen=True)
class DuplicateExecutionEvidence:
    """Carried execution facts for a manifest/journal boundary, never DB authority."""
    secondary_video_id: int
    primary_video_id: int
    known_side_assets: tuple[SideAssetEvidence, ...] = ()
    side_asset_accounting: SideAssetAccounting = SideAssetAccounting.UNKNOWN
    provenance: tuple[str, ...] = ()

    def __post_init__(self):
        # Invalid/unrecognised accounting cannot silently become a fourth state.
        object.__setattr__(self, 'side_asset_accounting', SideAssetAccounting(self.side_asset_accounting))


@dataclass(frozen=True)
class PlannerContext:
    naming: PhysicalNamingContext
    layout: PhysicalLayoutContext
    collections: tuple[SourceCollection, ...]
    titles: tuple[TargetTitle, ...]
    videos: tuple[TargetVideo, ...]
    subtitles: tuple[TargetSubtitle, ...]
    unmatched: tuple[UnmatchedSubtitle, ...]
    side_assets: tuple[SideAssetEvidence, ...] = ()
    execution_evidence: tuple[DuplicateExecutionEvidence, ...] = ()


@dataclass(frozen=True)
class ContainerLocator:
    """Derived physical anchor; never a selector or owner identity."""
    object_kind: str
    object_id: int
    target_relative_path: str | None
    status: str
    authority: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class DuplicateDiagnostic:
    secondary_video_id: int
    primary_video_id: int | None
    primary_source_relative_path: str | None
    primary_target_relative_path: str | None
    quarantine_target_relative_path: str | None
    validity: str
    primary_physical_state: str = 'NOT_CHECKED'
    primary_outside_duplicates: bool | None = None
    secondary_in_duplicates: bool | None = None
    side_assets: tuple[str, ...] = ()
    side_asset_accounting: SideAssetAccounting = SideAssetAccounting.UNKNOWN
    accounting_provenance: tuple[str, ...] = ()
    # A snapshot can never grant permission to purge later. The primary must
    # be freshly checked immediately before a future delete operation.
    purge_state: str = 'PRE_EXECUTION_REQUIRED'

    @property
    def side_assets_accounted(self) -> bool:
        """Compatibility read; UNKNOWN and INCOMPLETE both prohibit future delete."""
        return self.side_asset_accounting == SideAssetAccounting.COMPLETE


@dataclass(frozen=True)
class PlanRecord:
    object_kind: str
    object_id: int | str
    source_relative_path: str
    target_relative_path: str | None
    action: str
    status: str
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    authority: tuple[str, ...] = ()
    collection_id: int | None = None
    title_id: int | None = None
    duplicate: DuplicateDiagnostic | None = None
    compatibility: tuple[tuple[int, str], ...] = ()
    auxiliary_namespace: str | None = None
    # Information that requires no action before execution (never a warning).
    info: tuple[str, ...] = ()


@dataclass(frozen=True)
class FilesystemEntry:
    relative_path: str
    kind: str
    size: int | None = None
    mtime_ns: int | None = None
    diagnostic: str | None = None
    content_sha256: str | None = None


@dataclass(frozen=True)
class FilesystemSnapshot:
    root: str
    entries: tuple[FilesystemEntry, ...]
    name_max: int | None
    path_max: int | None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetCollision:
    guard: str
    target_paths: tuple[str, ...]
    record_keys: tuple[tuple[str, int | str], ...]


@dataclass(frozen=True)
class TargetPlan:
    records: tuple[PlanRecord, ...]
    collisions: tuple[TargetCollision, ...]
    counts: tuple[tuple[str, int], ...]
    status_counts: tuple[tuple[str, int], ...]
    filesystem_hash: str | None
    plan_hash: str
    name_max: int | None
    path_max: int | None
    max_component_utf8_bytes: int
    max_nas_target_path_utf8_bytes: int | None
    windows_state: str
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class PostStateProjection:
    context: PlannerContext
    snapshot: FilesystemSnapshot
    locators: tuple[ContainerLocator, ...]
    plan: TargetPlan
    converged: bool
    blockers: tuple[str, ...]
    projection_hash: str
    execution_evidence: tuple[DuplicateExecutionEvidence, ...] = ()
