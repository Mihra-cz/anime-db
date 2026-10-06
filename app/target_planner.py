"""Pure CURRENT → TARGET projection. IDs own objects; paths locate them.

There is no scanner routing, canonical filename parser, persistence or executor
here. Filesystem evidence and target namespace checks are separate preflight.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import PurePosixPath
import re

from .hierarchy_types import MAIN_CONTENT_PART_TYPES
from .physical_layout import resolve_physical_layout
from .physical_naming import resolve_physical_name
from .physical_naming_components import collision_keys, sanitize_component
from .physical_naming_formatters import (
    EpisodeIdentity, SupplementaryIdentity, format_episode_component,
    format_supplementary_component, format_variant_component,
    format_season_directory, normalize_extension,
)
from .target_planner_types import (
    DuplicateDiagnostic, PlanRecord, PlannerContext, TargetTitle, TargetVideo,
)

_SHARED_PATHS = {
    'shared_ova': ('OVA',), 'shared_specials': ('Specials',),
    'direct_season': (), 'extras_openings_endings': ('Extras', 'Openings & Endings'),
    'extras_promo': ('Extras', 'Promo'), 'extras_bonus': ('Extras', 'Bonus'),
    'extras_menus': ('Extras', 'Menu'),
}
_AVAILABLE_EDGES = frozenset({'automatic_match', 'confirmed_compatible'})


def relative_locator_safe(path: str) -> bool:
    return bool(path) and not path.startswith('/') and '\\' not in path and all(
        part not in ('', '.', '..') for part in path.split('/')
    )


def root_targets(context: PlannerContext) -> dict[int, tuple[str | None, tuple[str, ...], str]]:
    result = {}
    for collection in context.collections:
        if collection.id not in context.naming.collections:
            result[collection.id] = (None, ('collection_naming_unavailable',), 'unavailable')
            continue
        name = resolve_physical_name(context.naming, 'collection', collection.id)
        component = sanitize_component(name.effective_text) if name.effective_text else None
        issues = name.diagnostics + (tuple(d.code for d in component.diagnostics if d.severity == 'error') if component else ())
        if component and component.valid and collision_keys(component.component).fold_key in {'#recycle', 'duplicates', 'subs'}:
            issues += ('reserved_library_namespace',)
        result[collection.id] = (
            component.component if name.ready and component and component.valid and not issues else None,
            issues or (() if component and component.valid else ('collection_naming_unavailable',)), name.authority,
        )
    return result


def source_collection(context: PlannerContext, source: str) -> int | None:
    """Associate owner-less inventory by explicit source locators, never Video."""
    if not relative_locator_safe(source):
        return None
    candidates = []
    for collection in context.collections:
        for locator in collection.locators:
            if relative_locator_safe(locator) and (source == locator or source.startswith(locator + '/')):
                candidates.append((len(PurePosixPath(locator).parts), collection.id))
    if not candidates:
        return None
    longest = max(length for length, _ in candidates)
    owners = {id for length, id in candidates if length == longest}
    return next(iter(owners)) if len(owners) == 1 else None


def _video_target(context: PlannerContext, video: TargetVideo, title: TargetTitle | None, roots) -> PlanRecord:
    blockers = list(video.issues)
    authority = (f'video:{video.id}', f'title:{video.title_id}', f'collection:{video.collection_id}')
    root, root_issues, naming_authority = roots.get(video.collection_id, (None, ('collection_unavailable',), 'unavailable'))
    blockers.extend(root_issues)
    authority += ('root_naming:' + naming_authority,)
    if not relative_locator_safe(video.source):
        blockers.append('unsafe_source_locator')
    if title is None or title.collection_id != video.collection_id:
        blockers.append('video_owner_unavailable_or_conflicting')
    else:
        blockers.extend(title.issues)
    if title is None or title.id not in context.naming.titles:
        return PlanRecord('video', video.id, video.source, None, 'REVIEW', 'REVIEW', blockers=tuple(sorted(set(blockers))), authority=authority)
    prefix = resolve_physical_name(context.naming, 'title', title.id)
    blockers.extend(prefix.diagnostics)
    authority += ('title_naming:' + prefix.authority,)
    if not prefix.ready:
        blockers.append('title_naming_unavailable')
    parent = PurePosixPath(root or '')
    if title.season is not None:
        directory = format_season_directory(title.season)
        if directory.valid:
            parent /= directory.component
        else:
            blockers.append('invalid_season')
    if video.content_type == 'episode' and title.season is None:
        blockers.append('episode_season_unavailable')
    layout = None
    if title.kind not in {'season', 'part', 'cour', 'title', 'film'} or title.id in context.layout.choices:
        layout = resolve_physical_layout(context.layout, title.id)
        authority += ('layout:' + layout.authority, 'layout_kind:' + str(layout.effective_layout_kind))
        if layout.effective_layout_kind is None:
            blockers.extend(layout.diagnostics or ('layout_unavailable',))
        elif layout.effective_layout_kind == 'own_folder':
            own = sanitize_component(prefix.effective_text or '')
            if own.valid:
                parent /= own.component
            else:
                blockers.append('own_folder_naming_unavailable')
        else:
            grouping = _SHARED_PATHS[layout.effective_layout_kind]
            # D01–D02 final CM/Menu namespaces refine the approved shared kinds;
            # stored kinds and their confirmation basis remain unchanged.
            if video.content_type == 'cm' and layout.effective_layout_kind == 'extras_promo':
                grouping = ('Extras', 'CM')
            parent = parent.joinpath(*grouping)
    if prefix.effective_text:
        component = sanitize_component(prefix.effective_text)
        extension = PurePosixPath(video.source).suffix
        if video.content_type == 'episode':
            formatted = format_episode_component(component, EpisodeIdentity(title.season, video.episode_number, title.part, video.media_part), extension)
        else:
            formatted = format_supplementary_component(component, SupplementaryIdentity(
                video.content_type, title.season, title.part, video.ordinal, video.recap_position, video.media_part), extension)
        if video.variant_id is not None:
            authority += (f'variant:{video.variant_id}',)
            label = sanitize_component(video.variant_label) if video.variant_label is not None else None
            if label is None or not label.valid:
                blockers.append('variant_label_unavailable')
            else:
                formatted = format_variant_component(formatted, label)
        if not formatted.valid:
            blockers.extend(d.code for d in formatted.diagnostics if d.severity == 'error')
        filename = formatted.component
    else:
        filename = None
    blockers = tuple(sorted(set(blockers)))
    target = str(parent / filename) if filename and root and not blockers else None
    hard = any(code in blockers for code in ('component_byte_limit', 'invalid_component', 'invalid_logical_input', 'unsafe_source_locator', 'invalid_extension'))
    return PlanRecord('video', video.id, video.source, target,
        'REVIEW' if not target else 'KEEP' if target == video.source else 'MOVE',
        'BLOCKED' if hard else 'REVIEW' if blockers else 'READY', blockers=blockers,
        authority=authority, collection_id=video.collection_id, title_id=video.title_id)


def _subtitle_selection(subtitle, videos, records):
    ids = tuple(id for id, status in subtitle.compatibility if status in _AVAILABLE_EDGES)
    candidates = [videos[id] for id in ids if id in videos]
    if len(candidates) != len(ids) or not candidates:
        return None
    if any(not records[v.id].target_relative_path for v in candidates):
        return None
    if len(candidates) == 1:
        return candidates[0]
    # D05 needs one known logical identity and exactly one explicit BD lane.
    identities = {(v.collection_id, v.title_id, v.content_type, v.episode_number, v.ordinal, v.recap_position, v.media_part) for v in candidates}
    bd = [v for v in candidates if v.variant_id is not None and v.release_source == 'bd']
    tv = [v for v in candidates if v.variant_id is not None and v.release_source == 'tv']
    if (len(candidates) == 2 and len(identities) == 1 and len(bd) == len(tv) == 1
        and bd[0].variant_id != tv[0].variant_id
        and all(v.duplicate_primary_id is None for v in candidates)):
        return bd[0]
    return None


def _continuation_index(context, videos, records, titles):
    series = defaultdict(list)
    for subtitle in context.subtitles:
        chosen = _subtitle_selection(subtitle, videos, records)
        if (chosen is None or chosen.content_type != 'episode' or chosen.media_part is not None
            or chosen.variant_id is not None or chosen.duplicate_primary_id is not None
            or records[chosen.id].action == 'QUARANTINE'):
            continue
        if (chosen.id, 'confirmed_compatible') not in subtitle.compatibility:
            continue
        source = PurePosixPath(subtitle.source)
        for match in re.finditer(r'\d+', source.stem):
            if int(match[0]) == chosen.episode_number:
                key = (str(source.parent), source.stem[:match.start()], source.stem[match.end():], source.suffix.lower())
                series[key].append((chosen.episode_number, chosen, records[chosen.id], match[0]))
    result = {}
    for key, items in series.items():
        positions = sorted({n for n, _, _, _ in items})
        identities = {(v.title_id, v.collection_id) for _, v, _, _ in items}
        targets = {str(PurePosixPath(r.target_relative_path).parent) for _, _, r, _ in items}
        if not (len(positions) >= 3 and positions == list(range(positions[0], positions[-1]+1))
                and len(items) == len(positions) and len(identities) == 1 and len(targets) == 1):
            continue
        title_id, collection_id = next(iter(identities))
        # Another Season/Part context, or any Video at/after the next position,
        # means the orphan may belong elsewhere or contradict confirmed_no_match.
        contexts = {t.id for t in titles.values() if t.collection_id == collection_id and t.kind in MAIN_CONTENT_PART_TYPES}
        contexts |= {v.title_id for v in videos.values() if v.collection_id == collection_id and v.content_type == 'episode'}
        numbers = [v.episode_number for v in videos.values() if v.title_id == title_id and v.content_type == 'episode']
        if contexts != {title_id} or None in numbers or max(numbers) != positions[-1]:
            continue
        # Zero padding is part of the exact source pattern, not just the value.
        width = max((len(text) for _, _, _, text in items if text.startswith('0')), default=0)
        if all(text == str(n).zfill(width) for n, _, _, text in items):
            result[key] = (positions[-1], items[-1][1], next(iter(targets)), width)
    return result


def project_targets(context: PlannerContext) -> tuple[PlanRecord, ...]:
    """Project managed rows without filesystem access, SQL or mutations."""
    titles = {t.id: t for t in context.titles}
    videos = {v.id: v for v in context.videos}
    roots = root_targets(context)
    records = {v.id: _video_target(context, v, titles.get(v.title_id), roots) for v in context.videos}
    # Duplicate disposition uses the PRIMARY projection; secondary naming is
    # preserved even if it has no independent naming/numbering authority.
    for video in context.videos:
        if video.duplicate_primary_id is None and video.duplicate_validity is None:
            continue
        primary = videos.get(video.duplicate_primary_id)
        primary_record = records.get(video.duplicate_primary_id)
        validity = video.duplicate_validity or 'unknown'
        target = None
        blockers = []
        if validity != 'valid' or primary is None or primary.duplicate_primary_id is not None:
            blockers.append('duplicate_relation_' + validity)
        elif primary_record is None or not primary_record.target_relative_path:
            blockers.append('duplicate_primary_target_unavailable')
        elif not relative_locator_safe(video.source):
            blockers.append('unsafe_source_locator')
        else:
            target = str(PurePosixPath('Duplicates') / PurePosixPath(primary_record.target_relative_path).parent / PurePosixPath(video.source).name)
        diagnostic = DuplicateDiagnostic(video.id, video.duplicate_primary_id, primary.source if primary else None,
            primary_record.target_relative_path if primary_record else None, target, validity)
        records[video.id] = PlanRecord('video', video.id, video.source, target,
            'QUARANTINE' if target else 'REVIEW', 'READY' if target else 'REVIEW', blockers=tuple(blockers),
            authority=(f'video:{video.id}', f'confirmed_duplicate_primary:{video.duplicate_primary_id}'),
            collection_id=video.collection_id, title_id=video.title_id, duplicate=diagnostic)
    result = list(records.values())
    for subtitle in context.subtitles:
        selected = _subtitle_selection(subtitle, videos, records)
        extension = normalize_extension(PurePosixPath(subtitle.source).suffix)
        target = None
        diagnostic = None
        if selected and extension.valid and relative_locator_safe(subtitle.source):
            owner = records[selected.id]
            target = str(PurePosixPath(owner.target_relative_path).with_suffix(extension.extension))
            diagnostic = owner.duplicate
        result.append(PlanRecord('subtitle', subtitle.id, subtitle.source, target,
            'QUARANTINE' if diagnostic and target else 'KEEP' if target == subtitle.source else 'MOVE' if target else 'REVIEW',
            'READY' if target else 'REVIEW', blockers=() if target else ('subtitle_placement_unresolved',),
            authority=(f'subtitle:{subtitle.id}', 'explicit_compatibility') + ((f'physical_sidecar_video:{selected.id}',) if selected else ()),
            collection_id=selected.collection_id if selected else None, title_id=selected.title_id if selected else None,
            duplicate=diagnostic, compatibility=subtitle.compatibility))
    continuation = _continuation_index(context, videos, records, titles)
    for subtitle in context.unmatched:
        collection_id = source_collection(context, subtitle.source)
        root = roots.get(collection_id, (None, (), 'unavailable'))[0]
        target = str(PurePosixPath(root) / PurePosixPath(subtitle.source).name) if root else None
        authority = (f'unresolved_subtitle:{subtitle.id}', 'source_collection_locator')
        if subtitle.match_status == 'confirmed_no_match' and root:
            source = PurePosixPath(subtitle.source)
            matches = []
            for match in re.finditer(r'\d+', source.stem):
                key = (str(source.parent), source.stem[:match.start()], source.stem[match.end():], source.suffix.lower())
                known = continuation.get(key)
                if known and match[0] == str(known[0] + 1).zfill(known[3]):
                    _, video, parent, _ = known
                    title = titles[video.title_id]
                    prefix = resolve_physical_name(context.naming, 'title', title.id)
                    formatted = format_episode_component(sanitize_component(prefix.effective_text), EpisodeIdentity(title.season, int(match[0]), title.part), source.suffix)
                    if formatted.valid:
                        matches.append(str(PurePosixPath(parent) / formatted.component))
            if len(set(matches)) == 1:
                target = matches[0]
                authority += ('confirmed_series_continuation',)
        kind = 'confirmed_no_match' if subtitle.match_status == 'confirmed_no_match' else 'unresolved_subtitle'
        if kind == 'unresolved_subtitle':
            target = None
        result.append(PlanRecord(kind, subtitle.id, subtitle.source, target,
            'KEEP' if target == subtitle.source else 'MOVE' if target else 'REVIEW', 'WARNING' if target else 'REVIEW',
            warnings=('subtitle_present_video_missing',) if kind == 'confirmed_no_match' else (),
            blockers=() if target else ('subtitle_scope_unresolved',), authority=authority, collection_id=collection_id))
    return tuple(sorted(result, key=lambda r: (r.object_kind, str(r.object_id).zfill(12), r.source_relative_path)))
