#!/usr/bin/env python3
"""Scoped, fail-closed Notion API mirror. Python standard library only."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_ASSET = 19 * 1024 * 1024


def public_https(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Asset must use public HTTPS on port 443')
    for result in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(result[4][0]).is_global:
            raise ValueError('Asset resolves to a nonpublic address')
    return url


def hosted_https(url):
    public_https(url)
    host = urllib.parse.urlsplit(url).hostname.lower()
    if not (host.endswith('.amazonaws.com') or host.endswith('.notion.so') or host.endswith('.notion-static.com') or host.endswith('.notionusercontent.com')):
        raise ValueError('Unrecognized Notion hosted-asset domain')
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        hosted_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, token, version='2025-09-03'):
        self.token, self.version, self.last = token, version, 0

    def api(self, path, body=None):
        for attempt in range(5):
            time.sleep(max(0, 0.35 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            req = urllib.request.Request('https://api.notion.com/v1/' + path,
                data=None if body is None else json.dumps(body).encode(),
                headers={'Authorization': 'Bearer ' + self.token, 'Notion-Version': self.version,
                         'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code != 429 and error.code < 500:
                    raise RuntimeError('Notion API request failed: HTTP %s' % error.code) from None
                if attempt == 4:
                    raise RuntimeError('Notion API retries exhausted') from None
                try:
                    delay = float(error.headers.get('Retry-After', 2 ** attempt))
                except ValueError:
                    delay = 2 ** attempt
                time.sleep(min(30, max(1, delay)))
            except (urllib.error.URLError, TimeoutError):
                raise RuntimeError('Notion API connection failed') from None

    def asset(self, url):
        hosted_https(url)
        try:
            with urllib.request.build_opener(SafeRedirect()).open(url, timeout=30) as response:
                hosted_https(response.geturl())
                if int(response.headers.get('Content-Length', '0')) > MAX_ASSET:
                    raise ValueError('Hosted media exceeds 19 MiB; configure separate media storage')
                data = response.read(MAX_ASSET + 1)
                if len(data) > MAX_ASSET:
                    raise ValueError('Hosted media exceeds 19 MiB; configure separate media storage')
                return data
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError('Hosted asset download failed') from None


class Mirror:
    def __init__(self, client, directory):
        self.client, self.directory = client, Path(directory)
        self.seen_pages, self.seen_blocks, self.seen_dbs = set(), set(), set()
        self.references, self.unsupported, self.assets = [], [], {}

    def sanitize(self, value):
        if isinstance(value, list):
            return [self.sanitize(v) for v in value]
        if not isinstance(value, dict):
            return value
        value = dict(value)
        if isinstance(value.get('person'), dict):
            value['person'] = {k: v for k, v in value['person'].items() if k != 'email'}
        if value.get('type') == 'file' and isinstance(value.get('file'), dict) and value['file'].get('url'):
            data = self.client.asset(value['file']['url'])
            digest = hashlib.sha256(data).hexdigest()
            relative = 'assets/' + digest
            target = self.directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.assets[relative] = {'sha256': digest, 'bytes': len(data)}
            value['file'] = {'url': relative}
        # Request IDs change on every API call. Person emails are private contact
        # metadata rather than project content and must not enter a public mirror.
        return {k: self.sanitize(v) for k, v in value.items()
                if k != 'request_id' and not (k == 'email' and value.get('object') == 'user')}

    def save(self, relative, data):
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.sanitize(data), ensure_ascii=False, indent=2, sort_keys=True) + '\n')

    def paginate(self, path, folder, post=False):
        cursor, results, index = None, [], 0
        while True:
            params = {'page_size': 100}
            if cursor:
                params['start_cursor'] = cursor
            response = self.client.api(path if post else path + '?' + urllib.parse.urlencode(params), params if post else None)
            self.save('%s/%04d.json' % (folder, index), response)
            results.extend(response.get('results', []))
            if not response.get('has_more'):
                return results
            next_cursor = response.get('next_cursor')
            if not next_cursor or next_cursor == cursor:
                raise RuntimeError('Invalid pagination cursor')
            cursor, index = next_cursor, index + 1

    def page(self, page_id):
        if page_id in self.seen_pages:
            return
        self.seen_pages.add(page_id)
        page = self.client.api('pages/' + page_id)
        self.save('pages/' + page_id + '.json', page)
        for prop in page.get('properties', {}).values():
            if prop.get('id') and prop.get('type') in ('relation', 'title', 'rich_text', 'people', 'rollup'):
                encoded = urllib.parse.quote(urllib.parse.unquote(prop['id']), safe='')
                self.paginate('pages/%s/properties/%s' % (page_id, encoded), 'properties/%s/%s' % (page_id, encoded))
        self.blocks(page_id)

    def blocks(self, block_id):
        if block_id in self.seen_blocks:
            return
        self.seen_blocks.add(block_id)
        for block in self.paginate('blocks/' + block_id + '/children', 'blocks/' + block_id):
            kind, identity = block.get('type'), block['id']
            if kind == 'child_page':
                self.page(identity)
            elif kind == 'child_database':
                self.database(identity)
            elif kind == 'link_to_page':
                self.references.append({'block': identity, 'target': block.get(kind)})
            elif kind == 'unsupported':
                self.unsupported.append(identity)
            if block.get('has_children') and kind not in ('child_page', 'child_database'):
                self.blocks(identity)

    def database(self, db_id):
        if db_id in self.seen_dbs:
            return
        self.seen_dbs.add(db_id)
        database = self.client.api('databases/' + db_id)
        self.save('databases/' + db_id + '.json', database)
        for source in database.get('data_sources', []):
            source_id = source['id']
            self.save('data_sources/' + source_id + '.json', self.client.api('data_sources/' + source_id))
            for page in self.paginate('data_sources/' + source_id + '/query', 'queries/' + source_id, post=True):
                self.page(page['id'])


def run(config, client):
    destination = Path(config['output']).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.notion-stage-', dir=destination.parent))
    backup = destination.with_name('.' + destination.name + '-previous')
    try:
        mirror = Mirror(client, stage)
        mirror.page(config['root_page'])
        for db in config.get('databases', []):
            mirror.database(db)
        files = {str(path.relative_to(stage)): {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                                'bytes': path.stat().st_size}
                 for path in sorted(stage.rglob('*')) if path.is_file()}
        mirror.save('manifest.json', {'schema': 1, 'api_version': config['api_version'],
            'root_page': config['root_page'], 'explicit_databases': config.get('databases', []),
            'pages': sorted(mirror.seen_pages), 'databases': sorted(mirror.seen_dbs),
            'assets': mirror.assets, 'files': files, 'references_not_followed': mirror.references,
            'unsupported_blocks': mirror.unsupported, 'partial': True,
            'limitations': ['API-visible scoped content only', 'No workspace export, comments, or version history',
                            'Referenced pages are not traversed', 'Unsupported blocks remain raw API objects',
                            'External assets remain links; hosted media limited to 19 MiB']})
        if backup.exists():
            raise RuntimeError('Previous recovery directory exists; inspect it before continuing')
        if destination.exists():
            destination.rename(backup)
        try:
            stage.rename(destination)
        except Exception:
            if backup.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='automation/notion-sync.config.json')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    token = os.environ.get('NOTION_TOKEN')
    if not token:
        parser.exit(1, 'NOTION_TOKEN is required; sync has not run.\n')
    try:
        run(config, Client(token, config['api_version']))
    except Exception as error:
        # Never expose exception messages: upstream errors may include signed URLs.
        parser.exit(1, 'Notion sync failed (%s); inspect snapshot and recovery directory.\n' % type(error).__name__)


if __name__ == '__main__':
    main()
