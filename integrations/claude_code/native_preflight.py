"""Inspect installed native contracts and explicit session metadata without host calls."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import struct
import sys
from uuid import UUID


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from local_ai_connector.claude_binding import session_identity

CONTRACTS = {
    '2.16120.0': {
        '.vite/build/index.chunk-Des3qCSt.js': 'b8bcaa41364f5e95b4518a5624d6d8a4d15cd5facf5ecef7d27648a21997c431',
        '.vite/build/index.chunk-Nnw-A1di.js': 'da4c8b196c12fbf126bcda64537860c41d71d8e9864603a515a88ed32a781b0a',
    },
}


def read_entries(file, names):
    """Read only packed source entries; malformed archives must not imply support."""
    length = file.seek(0, 2)
    file.seek(0)
    prefix = file.read(16)
    if len(prefix) != 16:
        raise ValueError('Truncated ASAR header')
    size_pickle, block_size, header_size, json_size = struct.unpack('<4I', prefix)
    if (size_pickle != 4 or header_size + 4 != block_size or json_size <= 0
            or json_size > 8 * 1024 * 1024 or json_size + 4 > header_size
            or 8 + block_size > length):
        raise ValueError('Invalid ASAR header lengths')
    header = json.loads(file.read(json_size))
    result = {}
    for name in names:
        entry = header
        for part in name.split('/'):
            if not isinstance(entry, dict) or not isinstance(entry.get('files'), dict):
                raise ValueError('Invalid ASAR entry tree')
            entry = entry['files'].get(part)
        if entry is None:
            result[name] = None
            continue
        if (not isinstance(entry, dict) or 'link' in entry or 'unpacked' in entry
                or type(entry.get('size')) is not int or entry['size'] < 0
                or not isinstance(entry.get('offset'), str) or not entry['offset'].isdecimal()):
            raise ValueError('Expected a packed ASAR source entry')
        offset, size = 8 + block_size + int(entry['offset']), entry['size']
        if offset > length or size > length - offset:
            raise ValueError('ASAR source entry exceeds archive bounds')
        file.seek(offset)
        result[name] = hashlib.sha256(file.read(size)).hexdigest()
    return result


def inspect(app, metadata=None, *, desktop_id=None, cli_id=None, workspace=None):
    plist_path = app / 'Contents/Info.plist'
    before_plist = plist_path.read_bytes()
    plist = plistlib.loads(before_plist)
    version = plist['CFBundleShortVersionString']
    if not isinstance(version, str) or not version:
        raise ValueError('Invalid installed version')
    asar = app / 'Contents/Resources/app.asar'
    before = asar.stat()
    expected = CONTRACTS.get(version, {})
    with asar.open('rb') as file:
        hashes = read_entries(file, expected)
    after = asar.stat()
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or plist_path.read_bytes() != before_plist):
        raise ValueError('Claude installation changed during inspection; retry after update completes')
    recognized = bool(expected) and hashes == expected
    result = {
        'version': version, 'asar_size': before.st_size,
        'implementation_contract': 'recognized' if recognized else 'unverified',
        'entry_sha256': hashes,
        'runtime_tools': 'unverified', 'authorization_transfer': 'unverified',
        'native_provider_ready': False,
        'idle': 'unknown',
    }
    if metadata is not None:
        if desktop_id is None or cli_id is None or workspace is None:
            raise ValueError('Metadata inspection requires both expected IDs and workspace')
        if metadata.name != desktop_id + '.json':
            raise ValueError('Metadata filename must name the expected Desktop identity')
        result['session'] = session_identity(json.loads(metadata.read_bytes()), desktop_id, cli_id, workspace)
        result['archive_state'] = ('archived' if result['session']['isArchived'] else 'active') if 'isArchived' in result['session'] else 'unknown'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=Path('/Applications/Claude.app'))
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--desktop-id')
    parser.add_argument('--cli-id')
    parser.add_argument('--workspace', type=Path)
    args = parser.parse_args()
    if args.metadata is None and any(value is not None for value in (args.desktop_id, args.cli_id, args.workspace)):
        parser.error('Identity arguments require --metadata')
    result = inspect(args.app, args.metadata, desktop_id=args.desktop_id, cli_id=args.cli_id, workspace=args.workspace)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['implementation_contract'] == 'recognized' else 2


if __name__ == '__main__':
    raise SystemExit(main())
