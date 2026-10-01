"""Pure layout candidates, queues and badges; preview text is never authority."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Mapping
from urllib.parse import urlencode

from .naming_review import NamingBadge, NamingReviewContext
from .physical_layout import (
    LAYOUT_TITLE_TYPES, PhysicalLayoutContext, PhysicalLayoutResolution,
    create_basis_snapshot, resolve_physical_layout,
)
from .physical_naming import resolve_physical_name
from .physical_naming_components import sanitize_component
from .physical_layout_types import SHARED_CONTENT_LAYOUTS


LAYOUT_LABELS = {
    'own_folder': 'Vlastní složka', 'shared_ova': 'OVA/',
    'shared_specials': 'Specials/', 'direct_season': 'Přímo v Season',
    'extras_openings_endings': 'Extras/Openings & Endings/',
    'extras_promo': 'Extras/Promo/', 'extras_bonus': 'Extras/Bonus/',
    'extras_menus': 'Extras/Menus/',
}


@dataclass(frozen=True)
class LayoutEvidence:
    title_id: int
    local_title: str
    collection_label: str
    metadata_romaji: str | None
    physical_count: int
    media_part_count: int
    variant_count: int
    duplicate_count: int


@dataclass(frozen=True)
class LayoutReviewContext:
    physical: PhysicalLayoutContext
    naming: NamingReviewContext
    evidence: Mapping[int, LayoutEvidence]


@dataclass(frozen=True)
class LayoutCandidate:
    layout_kind: str
    label: str
    recommended: bool
    explanation: str
    attachment_preview: str
    folder_preview: str | None
    source: str
    valid: bool
    preview: str | None


@dataclass(frozen=True)
class LayoutReviewUnit:
    title_id: int
    collection_id: int | None
    evidence: LayoutEvidence
    resolution: PhysicalLayoutResolution
    candidates: tuple[LayoutCandidate, ...]
    actionable: bool
    fingerprint: str
    attachment_label: str
    physical_naming: str | None
    naming_warning: bool
    reason_messages: tuple[str, ...]
    state_label: str
    layout_label: str | None

    @property
    def physical_count(self):
        return self.evidence.physical_count

    @property
    def can_reconfirm(self):
        # Same applicability rule the POST enforces; never offer a dead action.
        choice = self.resolution.choice
        return (choice is not None and self.resolution.basis_matches is False
                and any(c.layout_kind == choice.layout_kind and c.valid for c in self.candidates))


@dataclass(frozen=True)
class LayoutReviewCounts:
    actionable: int
    strong: int
    optional: int
    ambiguity: int
    derived: int
    direct_season: int


@dataclass(frozen=True)
class LayoutReviewIndex:
    context: LayoutReviewContext
    units: Mapping[int, LayoutReviewUnit]
    pending: tuple[LayoutReviewUnit, ...]
    counts: LayoutReviewCounts


def _candidates(context, title, resolution):
    name = resolve_physical_name(context.naming.physical, 'title', title.id)
    component = sanitize_component(name.effective_text) if name.effective_text else None
    naming_ready = name.ready and component is not None and component.valid
    attachment = f'Season {title.attachment.season_number:02d}' if title.attachment.kind == 'season' else 'Root'
    kinds = set()
    direct = resolution.review_class == 'DIRECT_SEASON'
    if title.hierarchy_type in LAYOUT_TITLE_TYPES and title.structural_valid:
        if title.own_metadata_identity:
            kinds.add('own_folder')
        if direct:
            kinds.add('direct_season')
        else:
            kinds.update(SHARED_CONTENT_LAYOUTS[k] for k in title.content_types
                         if k in SHARED_CONTENT_LAYOUTS and not (k == 'preview' and title.hierarchy_type == 'preview'))
            if title.interview_evidence and set(title.content_types) == {'special'}:
                kinds.add('extras_bonus')
    recommended = None
    if title.logical_count is not None:
        if direct:
            recommended = 'direct_season'
        elif title.interview_evidence and set(title.content_types) == {'special'}:
            # Approved interview grouping uses Bonus; it remains an explicit
            # choice and does not reclassify Special content or hierarchy.
            recommended = 'extras_bonus'
        elif resolution.review_class == 'OWN_FOLDER_STRONG':
            recommended = 'own_folder'
        elif len(kinds - {'own_folder'}) == 1:
            recommended = next(iter(kinds - {'own_folder'}))
    candidates = []
    for kind in LAYOUT_LABELS:
        if kind not in kinds:
            continue
        own = kind == 'own_folder'
        valid = bool(naming_ready) if own else True
        folder = component.component if own and valid else None
        grouping = folder + '/' if folder else LAYOUT_LABELS[kind]
        preview = None if own and not valid else (
            attachment + '/' if kind == 'direct_season' else attachment + '/\n└── ' + grouping
        )
        explanation = ('Samostatné dílo s vlastní potvrzenou metadata identitou.' if own
                       else 'Schválené seskupení podle aktuálního typu a strukturálního kontextu.')
        if own and not valid:
            explanation = 'Nejprve vyřeš fyzický název v záložce Názvy. Náhled vlastní složky není bezpečně dostupný.'
        candidates.append(LayoutCandidate(kind, LAYOUT_LABELS[kind], kind == recommended,
            explanation, attachment, folder, 'Physical Naming + Hierarchie' if own else 'Layout kontrakt + Hierarchie', valid, preview))
    return tuple(candidates), name, attachment


def _fingerprint(context, title, resolution, candidates):
    choice = resolution.choice
    payload = {
        'version': 1, 'basis': json.loads(create_basis_snapshot(context.physical, title.id)),
        'choice': None if choice is None else {'id': choice.id, 'kind': choice.layout_kind, 'basis': choice.basis_snapshot_json},
        # Applicability can change without the persisted basis changing (e.g.
        # interview release evidence or an invalid naming dependency). Text,
        # filenames, exact physical counts and timestamps are not identities.
        'candidates': [(c.layout_kind, c.valid) for c in candidates],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def build_layout_review(context: LayoutReviewContext) -> LayoutReviewIndex:
    units = {}
    for title in context.physical.titles.values():
        if title.hierarchy_type not in LAYOUT_TITLE_TYPES and title.id not in context.physical.choices:
            continue
        result = resolve_physical_layout(context.physical, title.id)
        candidates, naming, attachment = _candidates(context, title, result)
        reasons = []
        if result.basis_matches is False:
            reasons.append('Kontext fyzického rozložení se od posledního potvrzení změnil.')
        if result.requires_human_decision:
            if not candidates:
                # Layout cannot resolve this item; name the domain that can.
                if title.hierarchy_type not in LAYOUT_TITLE_TYPES:
                    reasons.append('Část už není doplňkovým obsahem. Uložené rozložení lze jen vrátit zpět.')
                elif title.structural_valid:
                    reasons.append('Pro současný typ obsahu a zařazení není použitelné žádné schválené rozložení. Uprav je v Hierarchii.')
            elif title.logical_count is None:
                reasons.append('Logickou identitu nelze bezpečně určit.')
            elif result.review_class == 'OWN_FOLDER_STRONG':
                reasons.append('Vlastní metadata identita a více souvisejících položek: doporučena vlastní složka. Rozhodnutí potvrď výslovně.')
            elif result.review_class == 'OWN_FOLDER_OPTIONAL':
                reasons.append('Samostatná metadata identita: vyber vlastní složku nebo praktičtější sdílené rozložení.')
            else:
                reasons.append('Evidence připouští více seskupení nebo nestačí k bezpečnému odvození rozložení.')
        if not title.structural_valid:
            reasons.append('Nejprve vyřeš authoritative zařazení v Hierarchii.')
        choice = result.choice
        state = 'K vyřízení' if result.requires_human_decision else 'Potvrzené člověkem' if choice else 'Odvozené'
        kind = choice.layout_kind if choice else result.effective_layout_kind
        units[title.id] = LayoutReviewUnit(title.id, title.collection_id, context.evidence[title.id], result,
            candidates, result.requires_human_decision, _fingerprint(context, title, result, candidates),
            attachment, naming.effective_text, any(c.layout_kind == 'own_folder' and not c.valid for c in candidates),
            tuple(reasons), state, LAYOUT_LABELS.get(kind))
    units = dict(sorted(units.items(), key=lambda pair: (
        pair[1].evidence.collection_label.casefold(),
        pair[1].resolution.attachment.season_number or 0,
        pair[1].resolution.owner_title.hierarchy_type, pair[0],
    )))
    pending = tuple(u for u in units.values() if u.actionable)
    strong = sum(u.resolution.review_class == 'OWN_FOLDER_STRONG' for u in pending)
    optional = sum(u.resolution.review_class == 'OWN_FOLDER_OPTIONAL' for u in pending)
    counts = LayoutReviewCounts(len(pending), strong, optional, len(pending) - strong - optional,
        sum(u.resolution.authority == 'derived_default' for u in units.values()),
        sum(u.resolution.effective_layout_kind == 'direct_season' for u in units.values()))
    return LayoutReviewIndex(context, units, pending, counts)


def filter_layout_units(index, *, status='pending', q='', collection_id=None, title_id=None):
    if status not in {'pending', 'own', 'shared', 'confirmed', 'all'}:
        raise ValueError('Neznámý filtr rozložení.')
    query = q.strip().casefold()
    result = []
    for unit in index.units.values():
        if collection_id is not None and unit.collection_id != collection_id:
            continue
        if title_id is not None and unit.title_id != title_id:
            continue
        choice = unit.resolution.choice
        kind = choice.layout_kind if choice else unit.resolution.effective_layout_kind
        if status == 'pending' and not unit.actionable:
            continue
        if status == 'confirmed' and choice is None:
            continue
        if status == 'own' and not (kind == 'own_folder' or (unit.actionable and any(c.layout_kind == 'own_folder' for c in unit.candidates))):
            continue
        if status == 'shared' and not (kind and kind != 'own_folder' or unit.actionable and any(c.layout_kind != 'own_folder' for c in unit.candidates)):
            continue
        if query and query not in ' '.join((unit.evidence.collection_label, unit.evidence.local_title, unit.evidence.metadata_romaji or '', unit.physical_naming or '')).casefold():
            continue
        result.append(unit)
    return tuple(result)


def review_tabs(naming_index, layout_index, *, active, collection_id=None, title_id=None):
    from .naming_review import filter_naming_units
    scope = {k: v for k, v in {'collection_id': collection_id, 'title_id': title_id}.items() if v is not None}
    suffix = '?' + urlencode(scope) if scope else ''
    return (
        ('names', 'Názvy', '/naming-review' + suffix, len(filter_naming_units(naming_index, **scope)), active == 'names'),
        ('layout', 'Rozložení', '/naming-review/layout' + suffix, len(filter_layout_units(layout_index, **scope)), active == 'layout'),
    )


def aggregate_naming_badge(naming_badge, layout_index, *, collection_id=None, title_id=None):
    pending = filter_layout_units(layout_index, collection_id=collection_id, title_id=title_id)
    if not pending:
        return naming_badge
    scope = {'title_id': title_id} if title_id is not None else {'collection_id': collection_id}
    if collection_id is not None: scope['collection_id'] = collection_id
    severity = 'error' if naming_badge.severity == 'error' else 'warning'
    return NamingBadge('problem' if severity == 'error' else 'review',
        'Pojmenování: ' + ('problém' if severity == 'error' else 'kontrola') + ' · Rozložení', severity, naming_badge.review_count + len(pending),
        '/naming-review/layout?' + urlencode(scope))
