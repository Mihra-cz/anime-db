#!/usr/bin/env python3
"""LAB adapter for the shared approved deployment and host start mechanism.

Production CLI keeps its anime-db profile. Here the project and entrypoint are
explicitly LAB; release identity, source/derived SHA and bind validation remain
the real shared checks. Never falls back to stack/approved-v6 or live Compose.
"""
import argparse
from pathlib import Path
import sys

from reorganization_autostart import autostart, compose, load_boot
from reorganization_deployment import ensure_bindings
from reorganization_coordinator import run_command

CONFIG = Path('/srv/animedb-v6-lab/stack/reorganization-coordinator.json')
PROJECT = 'animedb-v6-lab'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'down', 'compose'))
    parser.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        deployment, config = load_boot(CONFIG, verify_live=args.action in ('start', 'compose'), project_name=PROJECT)
        if args.action == 'compose':
            ensure_bindings(deployment.bindings)
            print(run_command(compose(deployment) + args.args), end='')
        elif args.action == 'down':
            if args.args:
                raise ValueError('unexpected LAB down arguments')
            # Release drift must not prevent stopping the approved LAB stack.
            # Do not recreate anchors while tearing it down.
            print(run_command(compose(deployment) + ['down']), end='')
        else:
            if args.args:
                raise ValueError('unexpected host start arguments')
            print(autostart(deployment, config, args.action, project_name=PROJECT), end='')
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print('STOP: ' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
