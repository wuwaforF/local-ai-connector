"""CLI adapter for the shared read-only Desktop binding inspection."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from local_ai_connector.claude_binding import catalog, inspect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--installation-root', type=Path, default=ROOT)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('catalog')
    check = commands.add_parser('inspect')
    check.add_argument('--desktop-id', required=True)
    check.add_argument('--expected-revision', required=True)
    args = parser.parse_args()
    if not all(path.is_absolute() for path in (args.data, args.home, args.installation_root)):
        parser.error('Use absolute installation, data and home paths')
    try:
        result = (catalog(args.data, args.home) if args.action == 'catalog' else
                  inspect(args.installation_root, args.data, args.home, args.desktop_id, args.expected_revision))
    except (ValueError, OSError):
        result = {'schema_version': 1, 'status': 'prerequisite_error',
                  'message': '无法核对本机安装和会话状态，请检查安装。', 'binding_commit_supported': False}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
