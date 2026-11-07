"""Frozen pre-index namespace algorithm (c596fe3), test/benchmark oracle only."""
from collections import defaultdict
from pathlib import PurePosixPath
from app.physical_naming_components import collision_keys
from app.target_planner import relative_locator_safe
from app.target_planner_types import TargetCollision
from app.target_planner_filesystem import _diagnose

def _namespace_collisions(records, directories=()):
    nodes = {guard: defaultdict(list) for guard in ('exact', 'casefold', 'uppercase')}
    for index, record in enumerate(records):
        if record.status == 'SYSTEM_EXCLUDED':
            continue
        path = record.target_relative_path or record.source_relative_path
        if not relative_locator_safe(path):
            continue
        components = PurePosixPath(path).parts
        for length in range(1, len(components)+1):
            raw = '/'.join(components[:length])
            keys = [collision_keys(p) for p in components[:length]]
            for guard, attr in [('exact', 'exact_key'), ('casefold', 'fold_key'), ('uppercase', 'uppercase_guard')]:
                key = tuple(getattr(k, attr) for k in keys)
                nodes[guard][key].append((raw, length == len(components), index))
    # Existing empty directories occupy the namespace too. They are context,
    # not file actions; exact approved parent directories may be reused.
    for path in directories:
        if not relative_locator_safe(path):
            continue
        components = PurePosixPath(path).parts
        for length in range(1, len(components)+1):
            raw = '/'.join(components[:length])
            keys = [collision_keys(p) for p in components[:length]]
            for guard, attr in [('exact', 'exact_key'), ('casefold', 'fold_key'), ('uppercase', 'uppercase_guard')]:
                key = tuple(getattr(k, attr) for k in keys)
                nodes[guard][key].append((raw, False, None))
    collisions = []
    for guard, groups in nodes.items():
        for members in groups.values():
            if not any(index is not None for _, _, index in members):
                continue
            paths = {raw for raw, _, _ in members}
            leaves = {index for _, leaf, index in members if leaf}
            parents = [item for item in members if not item[1]]
            if leaves and parents:
                reason = 'file_directory'
            elif len(leaves) > 1 or len(paths) > 1:
                reason = guard
            else:
                continue
            indices = sorted({index for _, _, index in members if index is not None})
            context_keys = sorted({('filesystem_directory', raw) for raw, _, index in members if index is None})
            collision = TargetCollision(reason, tuple(sorted(paths)),
                tuple((records[i].object_kind, records[i].object_id) for i in indices) + tuple(context_keys))
            if collision not in collisions:
                collisions.append(collision)
            for index in indices:
                records[index] = _diagnose(records[index], 'target_collision:' + reason)
    # Separate collection IDs may never merge into one physical anime root,
    # even when their filenames do not collide. Shared Season owners may.
    root_owners = defaultdict(list)
    for index, record in enumerate(records):
        if record.collection_id is not None and record.target_relative_path and not record.target_relative_path.startswith('Duplicates/'):
            root_owners[PurePosixPath(record.target_relative_path).parts[0]].append(index)
    for root, indices in root_owners.items():
        if len({records[i].collection_id for i in indices}) > 1:
            collisions.append(TargetCollision('collection_root', (root,), tuple((records[i].object_kind, records[i].object_id) for i in indices)))
            for i in indices:
                records[i] = _diagnose(records[i], 'collection_root_collision')
    managed = [r for r in records if r.object_kind in {'video', 'subtitle'} and r.target_relative_path and r.duplicate is None]
    for index, record in enumerate(records):
        if record.auxiliary_namespace:
            key = tuple(collision_keys(p).fold_key for p in PurePosixPath(record.auxiliary_namespace).parts)
            owners = [r for r in managed if tuple(collision_keys(p).fold_key for p in PurePosixPath(r.target_relative_path).parent.parts[:len(key)]) == key]
            if owners:
                records[index] = _diagnose(record, 'auxiliary_managed_namespace_collision', status='REVIEW')
                collisions.append(TargetCollision('auxiliary_namespace', (record.auxiliary_namespace,), tuple(
                    [(record.object_kind, record.object_id)] + [(r.object_kind, r.object_id) for r in owners])))
    return tuple(sorted(collisions, key=lambda c: (c.guard, c.target_paths, repr(c.record_keys))))
