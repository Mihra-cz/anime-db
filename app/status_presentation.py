"""Shared read-only labels and visual severity for existing workflow states."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class StatusBadge:
    key: str
    label: str
    severity: str


HIERARCHY_BADGES = {
    "verified": StatusBadge("verified", "Ověřeno", "verified"),
    "automatic": StatusBadge("automatic_ok", "Auto OK", "success"),
    "review_required": StatusBadge("review_required", "Vyžaduje kontrolu", "warning"),
    "conflict": StatusBadge("conflict", "Konflikt", "error"),
}
LONG_EPISODE_SET_BADGE = StatusBadge(
    "long_episode_set", "Zvláštně dlouhá sada epizod", "info",
)
METADATA_BADGES = {
    "resolved": StatusBadge("metadata_resolved", "Metadata OK", "success"),
    "missing": StatusBadge("metadata_missing", "Metadata chybí", "warning"),
    "confirmed": StatusBadge("metadata_confirmed", "Metadata potvrzena", "success"),
    "not_required": StatusBadge("metadata_not_required", "Metadata nejsou vyžadována", "info"),
}
MEDIA_BADGES = {
    "error": StatusBadge("media_error", "Média: problém", "error"),
    "warning": StatusBadge("media_review", "Média: kontrola", "warning"),
    "info": StatusBadge("media_info", "Média: informace", "info"),
    "success": StatusBadge("media_ok", "Média OK", "success"),
}


def media_collection_badge(evaluations: Iterable[object]) -> StatusBadge:
    """Summarize canonical Media Check evaluations, excluding duplicate copies."""
    severities = [
        severity
        for evaluation in evaluations if evaluation.completion_required
        for severity in (evaluation.subtitle_severity, evaluation.audio_severity)
    ]
    for severity in ("error", "warning", "info", "success"):
        if severity in severities:
            return MEDIA_BADGES[severity]
    return MEDIA_BADGES["info"]
