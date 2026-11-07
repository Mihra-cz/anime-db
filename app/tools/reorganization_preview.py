"""Request-scoped RO planner worker. Does not import the web application."""
import json
import sys

from ..reorganization_web import build_preview


def main():
    if len(sys.argv) != 5:
        raise SystemExit('Expected database, library root, require_mount and access timeout.')
    print(json.dumps(build_preview(sys.argv[1], sys.argv[2], require_mount=sys.argv[3] == "1", access_timeout=float(sys.argv[4])), ensure_ascii=True, default=str))


if __name__ == '__main__':
    main()
