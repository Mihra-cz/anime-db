"""Test-only Linux syscall measurement, including native reads and descendants.

Run under the project interpreter, e.g.:
python tests/v6_media_io_probe.py --output /tmp/v6-probe --counts 2,4,8,16
Requires installed strace. No dependency install, production startup or network.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

VIDEO = re.compile(r'\.(?:mkv|mp4|m4v|avi)(?: \(deleted\))?$', re.I)


def read_events(paths):
    events = []
    for path in paths:
        pending = None
        with path.open() as source:
            for raw in source:
                stamp, _, body = raw.partition(' ')
                try:
                    timestamp = float(stamp)
                except ValueError:
                    continue
                body = body.strip()
                if '<unfinished ...>' in body:
                    pending = timestamp, body.split('<unfinished ...>')[0]
                    continue
                if body.startswith('<... ') and ' resumed>' in body:
                    if pending is None:
                        raise ValueError('resumed syscall has no observed entry')
                    timestamp, prefix = pending
                    body = prefix + body.split(' resumed>', 1)[1]
                    pending = None
                events.append((timestamp, path.name, body))
    return sorted(events)


def summarize(paths):
    phase, root = None, None
    cases = {}
    for timestamp, pid, body in read_events(paths):
        marker = re.search(r'"V6_MEDIA_PHASE (\w+) ([^"\\]+)\\n"', body)
        if marker and body.startswith('write('):
            phase, root = marker.groups()
            cases.setdefault(root, {'media_read_bytes': 0, 'media_read_calls': 0,
                'media_mapped_bytes': 0, 'media_copy_bytes': 0,
                'process_read_syscall_bytes': 0, 'events': []})
            continue
        if phase not in {'EXECUTION', 'RO_STARTUP'} or root is None:
            continue
        returned = re.search(r'= (\d+)\s*$', body)
        read = re.match(r'(read|pread64|readv|preadv|preadv2)\(\d+<([^>]+)>', body)
        case = cases[root]
        if re.match(r'(read|pread64|readv|preadv|preadv2)\(', body) and returned:
            case['process_read_syscall_bytes'] += int(returned[1])
        if read and VIDEO.search(read[2]) and read[2].startswith(root + '/'):
            if returned is None:
                if '= -1 ' in body:
                    continue
                raise ValueError('unparsed media read result: ' + body)
            count = int(returned[1])
            case['media_read_bytes'] += count
            case['media_read_calls'] += 1
            case['events'].append({'pid': pid, 'phase': phase, 'call': read[1], 'bytes': count})
        if body.startswith('mmap(') and '= 0x' in body:
            mapping = re.match(r'mmap\([^,]*, (\d+),.*?\d+<([^>]+)>', body)
            if mapping and mapping[2].startswith(root + '/') and VIDEO.search(mapping[2]):
                case['media_mapped_bytes'] += int(mapping[1])
        if body.startswith(('sendfile(', 'copy_file_range(')):
            paths = re.findall(r'\d+<([^>]+)>', body)
            if any(name.startswith(root + '/') and VIDEO.search(name) for name in paths) and returned:
                case['media_copy_bytes'] += int(returned[1])
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--counts', default='2,4,8,16')
    parser.add_argument('--mode', choices=['constant', 'growing'])
    parser.add_argument('--file-bytes', type=int, default=65536)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    trace = args.output / 'syscalls'
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', ANIME_PATH='/tmp', DATABASE_URL='sqlite://',
        METADATA_DOWNLOAD_ARTWORK='false', V6_SCALING_COUNTS=args.counts,
        V6_SCALING_FILE_BYTES=str(args.file_bytes), V6_SCALING_METRICS_PATH=str(args.output / 'metrics.json'))
    command = ['strace', '-ff', '-ttt', '-yy', '-s', '256', '-o', str(trace), '-e',
        'trace=read,pread64,readv,preadv,preadv2,mmap,sendfile,copy_file_range,write',
        sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-p', 'no:faulthandler',
        'tests/test_v6_execution_scaling.py', '-q', '-s', '--tb=short']
    if args.mode:
        command.extend(['-k', args.mode])
    with (args.output / 'pytest.log').open('w') as log:
        result = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT)
    cases = summarize(args.output.glob('syscalls.*'))
    report = {'pytest_exit_code': result.returncode, 'cases': cases,
        'scope': 'EXECUTION and RO_STARTUP only; fixture creation and RO-view copy excluded',
        'syscalls': 'read,pread64,readv,preadv,preadv2,mmap,sendfile,copy_file_range; strace follows descendants'}
    (args.output / 'io.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if result.returncode:
        return result.returncode
    if not cases or any(case['media_read_bytes'] or case['media_mapped_bytes'] or case['media_copy_bytes']
            for case in cases.values()):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
