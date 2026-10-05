import importlib.util
import io
import json
from pathlib import Path
import plistlib
import struct

import pytest

pytestmark = pytest.mark.macos  # macOS deployment scripts, launchd or Desktop paths


MODULE = Path(__file__).resolve().parents[1] / 'integrations/claude_code/native_preflight.py'
spec = importlib.util.spec_from_file_location('claude_native_preflight', MODULE)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

CLI_ID = '6f1c2d3e-4a5b-4c6d-8e7f-90a1b2c3d4e5'
DESKTOP_ID = 'local_' + CLI_ID


def archive(entry, payload=b'code'):
    header = json.dumps({'files': {'source.js': entry}}).encode()
    padding = b'\0' * (-(len(header) + 4) % 4)
    size = len(header) + len(padding) + 4
    return io.BytesIO(struct.pack('<4I', 4, size + 4, size, len(header)) + header + padding + payload)


def test_packed_source_hash_and_missing_entry_are_distinct():
    result = preflight.read_entries(archive({'offset': '0', 'size': 4}), ['source.js', 'missing.js'])
    assert result == {'source.js': preflight.hashlib.sha256(b'code').hexdigest(), 'missing.js': None}


@pytest.mark.parametrize('entry', [
    {'offset': '0', 'size': 5}, {'offset': '100', 'size': 1},
    {'offset': '-1', 'size': 1}, {'offset': '0', 'size': True},
    {'offset': '0', 'size': 4, 'unpacked': True}, {'link': 'source.js'},
])
def test_invalid_or_external_archive_entries_fail(entry):
    with pytest.raises(ValueError):
        preflight.read_entries(archive(entry), ['source.js'])


@pytest.mark.parametrize('payload', [b'', b'\0' * 16, struct.pack('<4I', 4, 1000, 996, 992)])
def test_invalid_archive_header_fails(payload):
    with pytest.raises(ValueError):
        preflight.read_entries(io.BytesIO(payload), ['source.js'])


def test_metadata_does_not_infer_realtime_idle_or_leak_other_fields(tmp_path):
    app = tmp_path / 'Claude.app/Contents'
    (app / 'Resources').mkdir(parents=True)
    (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': 'unknown-version'}))
    (app / 'Resources/app.asar').write_bytes(archive({'offset': '0', 'size': 4}).getvalue())
    metadata = tmp_path / (DESKTOP_ID + '.json')
    metadata.write_text(json.dumps({'sessionId': DESKTOP_ID, 'cliSessionId': CLI_ID,
        'cwd': str(tmp_path), 'isRunning': False, 'private': 'must-not-appear'}))
    result = preflight.inspect(app.parent, metadata, desktop_id=DESKTOP_ID, cli_id=CLI_ID, workspace=tmp_path)
    assert result['idle'] == 'unknown'
    assert result['archive_state'] == 'unknown'
    assert result['implementation_contract'] == 'unverified'
    assert result['native_provider_ready'] is False
    assert 'must-not-appear' not in json.dumps(result)
    assert 'isRunning' not in result['session']


@pytest.mark.parametrize('field,value', [
    ('sessionId', CLI_ID), ('cliSessionId', 'another'), ('cwd', '/another'),
    ('isArchived', 'false'), ('originCwd', None),
])
def test_mismatched_identity_and_invalid_boundary_types_fail(tmp_path, field, value):
    record = {'sessionId': DESKTOP_ID, 'cliSessionId': CLI_ID, 'cwd': str(tmp_path)}
    record[field] = value
    with pytest.raises(ValueError):
        preflight.session_identity(record, DESKTOP_ID, CLI_ID, tmp_path)


def test_missing_cli_mapping_is_not_inferred_from_desktop_prefix(tmp_path):
    record = {'sessionId': DESKTOP_ID, 'cwd': str(tmp_path)}
    with pytest.raises(ValueError):
        preflight.session_identity(record, DESKTOP_ID, CLI_ID, tmp_path)


def test_recognized_installation_still_does_not_enable_provider(tmp_path, monkeypatch):
    app = tmp_path / 'Contents'
    (app / 'Resources').mkdir(parents=True)
    (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': 'fixture'}))
    (app / 'Resources/app.asar').write_bytes(archive({'offset': '0', 'size': 4}).getvalue())
    monkeypatch.setitem(preflight.CONTRACTS, 'fixture', {'source.js': preflight.hashlib.sha256(b'code').hexdigest()})
    result = preflight.inspect(tmp_path)
    assert result['implementation_contract'] == 'recognized'
    assert result['runtime_tools'] == 'unverified'
    assert result['authorization_transfer'] == 'unverified'
    assert result['native_provider_ready'] is False


def test_same_version_changed_source_is_unverified(tmp_path, monkeypatch):
    app = tmp_path / 'Contents'
    (app / 'Resources').mkdir(parents=True)
    (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': 'fixture'}))
    (app / 'Resources/app.asar').write_bytes(archive({'offset': '0', 'size': 4}).getvalue())
    monkeypatch.setitem(preflight.CONTRACTS, 'fixture', {'source.js': '0' * 64})
    assert preflight.inspect(tmp_path)['implementation_contract'] == 'unverified'


def test_installation_update_during_inspection_fails(tmp_path, monkeypatch):
    app = tmp_path / 'Contents'
    (app / 'Resources').mkdir(parents=True)
    plist = app / 'Info.plist'
    plist.write_bytes(plistlib.dumps({'CFBundleShortVersionString': 'before'}))
    (app / 'Resources/app.asar').write_bytes(archive({'offset': '0', 'size': 4}).getvalue())
    original = preflight.read_entries

    def update_during_read(file, names):
        result = original(file, names)
        plist.write_bytes(plistlib.dumps({'CFBundleShortVersionString': 'after'}))
        return result

    monkeypatch.setattr(preflight, 'read_entries', update_during_read)
    with pytest.raises(ValueError, match='changed during inspection'):
        preflight.inspect(tmp_path)
