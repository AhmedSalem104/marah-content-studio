import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('sync_notion', Path(__file__).resolve().parents[1] / 'automation/scripts/sync_notion.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class FixtureClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def api(self, path, body=None):
        self.calls.append((path, body))
        base = path.split('?')[0]
        if base == 'pages/root':
            return {'id': 'root', 'properties': {'Title': {'id': 'title', 'type': 'title'}},
                    'cover': {'type': 'file', 'file': {'url': 'https://host/asset?secret=signed', 'expiry_time': 'tomorrow'}}}
        if base.startswith('pages/root/properties/'):
            return {'results': [{'title': 'complete'}], 'has_more': False}
        if base == 'blocks/root/children':
            if 'start_cursor=next' in path:
                return {'results': [{'id': 'ref', 'type': 'link_to_page', 'link_to_page': {'page_id': 'outside'}}], 'has_more': False}
            return {'results': [{'id': 'db', 'type': 'child_database'}], 'has_more': True, 'next_cursor': 'next'}
        if base == 'databases/db':
            return {'id': 'db', 'data_sources': [{'id': 'source'}]}
        if base == 'data_sources/source':
            return {'id': 'source'}
        if base == 'data_sources/source/query':
            if body.get('start_cursor'):
                return {'results': [], 'has_more': False}
            return {'results': [{'id': 'record'}], 'has_more': True, 'next_cursor': 'last'}
        if base == 'pages/record':
            return {'id': 'record', 'properties': {}}
        if base == 'blocks/record/children':
            if self.fail:
                raise RuntimeError('failed fixture')
            return {'results': [{'id': 'unknown', 'type': 'unsupported'}], 'has_more': False}
        raise AssertionError('Unexpected or out-of-scope request: ' + path)

    def asset(self, url):
        return b'fixture image'


class MirrorTests(unittest.TestCase):
    def test_paginated_scoped_snapshot_sanitizes_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'current'
            client = FixtureClient()
            sync.run({'output': str(output), 'root_page': 'root', 'databases': ['db'], 'api_version': '2025-09-03'}, client)
            manifest = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(manifest['pages'], ['record', 'root'])
            self.assertEqual(manifest['unsupported_blocks'], ['unknown'])
            self.assertTrue(manifest['partial'])
            self.assertEqual(len(manifest['references_not_followed']), 1)
            text = '\n'.join(p.read_text() for p in output.rglob('*.json'))
            self.assertNotIn('secret=signed', text)
            self.assertNotIn('expiry_time', text)
            self.assertEqual(len(list((output / 'assets').iterdir())), 1)
            self.assertTrue(any(body and body.get('start_cursor') == 'last' for _, body in client.calls))
            self.assertFalse(any('outside' in path for path, _ in client.calls))

    def test_failure_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'current'
            output.mkdir()
            (output / 'previous.txt').write_text('previous snapshot')
            with self.assertRaises(RuntimeError):
                sync.run({'output': str(output), 'root_page': 'root', 'databases': [], 'api_version': '2025-09-03'}, FixtureClient(fail=True))
            self.assertEqual((output / 'previous.txt').read_text(), 'previous snapshot')
            self.assertEqual(list(Path(temp).iterdir()), [output])

    def test_external_links_are_not_downloaded(self):
        with tempfile.TemporaryDirectory() as temp:
            mirror = sync.Mirror(FixtureClient(), temp)
            value = {'type': 'external', 'external': {'url': 'https://example.com/video'}}
            self.assertEqual(mirror.sanitize(value), value)
            self.assertEqual(mirror.assets, {})

    def test_volatile_request_ids_and_person_emails_do_not_change_snapshot(self):
        class VolatileClient(FixtureClient):
            def __init__(self, request_id):
                super().__init__()
                self.request_id = request_id

            def api(self, path, body=None):
                response = super().api(path, body)
                response['request_id'] = self.request_id
                if path == 'pages/root':
                    response['created_by'] = {'object': 'user', 'type': 'person',
                                              'person': {'email': 'private@example.com'}}
                return response

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'current'
            config = {'output': str(output), 'root_page': 'root', 'api_version': '2025-09-03'}
            sync.run(config, VolatileClient('first-random-id'))
            first = {str(p.relative_to(output)): p.read_bytes() for p in output.rglob('*') if p.is_file()}
            sync.run(config, VolatileClient('second-random-id'))
            second = {str(p.relative_to(output)): p.read_bytes() for p in output.rglob('*') if p.is_file()}
            self.assertEqual(first, second)
            all_json = b'\n'.join(v for k, v in second.items() if k.endswith('.json'))
            self.assertNotIn(b'request_id', all_json)
            self.assertNotIn(b'private@example.com', all_json)
            manifest = json.loads(second['manifest.json'])
            self.assertNotIn('manifest.json', manifest['files'])
            import hashlib
            for name, metadata in manifest['files'].items():
                self.assertEqual(metadata['sha256'], hashlib.sha256(second[name]).hexdigest())

    def test_missing_asset_aborts_snapshot(self):
        class MissingAsset(FixtureClient):
            def asset(self, url):
                raise RuntimeError('missing asset')
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'current'
            output.mkdir()
            (output / 'old.json').write_text('{}')
            with self.assertRaises(RuntimeError):
                sync.run({'output': str(output), 'root_page': 'root', 'api_version': '2025-09-03'}, MissingAsset())
            self.assertEqual((output / 'old.json').read_text(), '{}')

    def test_replacement_failure_restores_previous(self):
        from unittest.mock import patch
        rename = Path.rename
        def fail_stage(path, target):
            if path.name.startswith('.notion-stage-'):
                raise OSError('replacement failed')
            return rename(path, target)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'current'
            output.mkdir()
            (output / 'old.json').write_text('{}')
            with patch.object(Path, 'rename', fail_stage), self.assertRaises(OSError):
                sync.run({'output': str(output), 'root_page': 'root', 'api_version': '2025-09-03'}, FixtureClient())
            self.assertEqual((output / 'old.json').read_text(), '{}')
            self.assertEqual(list(Path(temp).iterdir()), [output])

    def test_rejects_private_and_non_https_asset_urls(self):
        for url in ['http://example.com/x', 'https://127.0.0.1/x', 'https://user:password@example.com/x']:
            with self.assertRaises(ValueError):
                sync.public_https(url)


if __name__ == '__main__':
    unittest.main()
