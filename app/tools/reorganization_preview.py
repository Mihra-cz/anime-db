"""Request-scoped RO planner worker. Does not import the web application."""
import json
import sys

from ..reorganization_web import build_preview
from ..reorganization_archive_review import ArchiveReviewConflict, ArchiveReviewUnavailable


def main():
    if len(sys.argv) not in (5, 6):
        raise SystemExit('Expected database, library root, require_mount, access timeout and optional archive decision.')
    try:
        result = build_preview(sys.argv[1], sys.argv[2], require_mount=sys.argv[3] == "1",
            access_timeout=float(sys.argv[4]), decision=json.loads(sys.argv[5]) if len(sys.argv) == 6 else None)
    except (ArchiveReviewConflict, ArchiveReviewUnavailable) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=True))
        raise SystemExit(4 if isinstance(exc, ArchiveReviewUnavailable) else 3)
    print(json.dumps(result, ensure_ascii=True, default=str))


if __name__ == '__main__':
    main()
