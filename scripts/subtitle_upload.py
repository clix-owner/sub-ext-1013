"""Bounded subtitle input transfer using request-scoped GitHub Actions artifacts."""
import base64
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from clixarena_subtitles import SetupError
from subtitle_selection import github, NoRedirect, UUID

MAX_PACKED = 512 * 1024
MAX_PARTS = 15
PART_SIZE = 48000

def save(inputs):
    kind='torrent' if inputs.get('mode')=='torrent-upload' else 'subtitle'
    max_parts=30 if kind=='torrent' else MAX_PARTS
    request_id = inputs.get('request_id', '')
    if not UUID.fullmatch(request_id): raise SetupError('Invalid subtitle upload identifier.')
    try:
        part = json.loads(inputs.get('subtitle_tracks', ''))
        index, total, data = part['index'], part['total'], part['data']
        if type(index) is not int or type(total) is not int or not 0 <= index < total <= max_parts: raise ValueError()
        if not isinstance(data, str) or not 0 < len(data) <= PART_SIZE or not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', data): raise ValueError()
        if index < total - 1 and len(data) != PART_SIZE: raise ValueError()
    except (ValueError, TypeError, KeyError): raise SetupError('Invalid subtitle input part.') from None
    Path(kind+'-input.json').write_text(json.dumps({'request_id': request_id, **part}), encoding='utf-8')
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as out: out.write('index=' + str(index) + '\n')

def read_part(artifact_id,kind='subtitle'):
    try: github('/actions/artifacts/' + str(int(artifact_id)) + '/zip')
    except urllib.error.HTTPError as error:
        if error.code != 302: raise
        location = error.headers.get('Location', '')
    else: raise SetupError('Subtitle input artifact did not return a download.')
    parsed = urllib.parse.urlsplit(location)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port or not any((parsed.hostname or '').endswith(s) for s in ('.blob.core.windows.net', '.githubusercontent.com', '.github.com')):
        raise SetupError('Unexpected subtitle input artifact host.')
    with urllib.request.build_opener(NoRedirect).open(location, timeout=30) as response: raw = response.read(65001)
    if len(raw) > 65000: raise SetupError('Subtitle input artifact is too large.')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo(kind+'-input.json')
        if info.file_size > 60000: raise SetupError('Subtitle input part is too large.')
        return json.loads(archive.read(info))

def receive(manifest, timeout=900, kind='subtitle'):
    if kind not in ('subtitle','torrent'):raise SetupError('Invalid artifact input kind.')
    max_parts=30 if kind=='torrent' else MAX_PARTS
    max_packed=1024*1024 if kind=='torrent' else MAX_PACKED
    try:
        request_id, total, digest = manifest['request_id'], manifest['total'], manifest['sha256']
        if not UUID.fullmatch(request_id) or type(total) is not int or not 1 <= total <= max_parts or not re.fullmatch('[0-9a-f]{64}', digest): raise ValueError()
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        if event.get('inputs', {}).get('request_id') != request_id: raise ValueError()
    except (KeyError, ValueError, TypeError): raise SetupError('Invalid subtitle upload manifest.') from None
    parts = {}; deadline = time.monotonic() + timeout; errors = 0
    while time.monotonic() < deadline:
        try:
            for index in range(total):
                if index in parts: continue
                name = kind+'-input-' + request_id + '-' + str(index)
                with github('/actions/artifacts?name=' + name + '&per_page=100') as response: listing = json.load(response)
                candidates = [a for a in listing.get('artifacts', []) if a.get('name') == name and not a.get('expired')]
                if not candidates: continue
                artifact = max(candidates, key=lambda a: a['id'])
                part = read_part(artifact['id'],kind) if kind=='torrent' else read_part(artifact['id'])
                if part.get('request_id') != request_id or part.get('index') != index or part.get('total') != total: raise SetupError('Subtitle input part belongs to another request.')
                data = part.get('data', '')
                if not isinstance(data,str) or not 0 < len(data) <= PART_SIZE or not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}',data): raise SetupError('Invalid subtitle input part.')
                parts[index] = data
            if len(parts) == total:
                packed = ''.join(parts[i] for i in range(total))
                try: raw = base64.b64decode(packed, validate=True)
                except ValueError: raise SetupError('Invalid subtitle upload encoding.') from None
                if len(raw) > max_packed or hashlib.sha256(raw).hexdigest() != digest: raise SetupError('Subtitle upload integrity check failed.')
                print('All subtitle input parts received and verified.', flush=True)
                return packed
            errors = 0
        except SetupError: raise
        except Exception:
            errors += 1
            if errors >= 6: raise SetupError('Could not retrieve subtitle input parts from GitHub.') from None
        print('Waiting for subtitle input parts: ' + str(len(parts)) + '/' + str(total), flush=True)
        time.sleep(10)
    raise SetupError('Subtitle upload did not finish within 15 minutes. Check the Subtitle input upload runs in GitHub Actions.')

if __name__ == '__main__':
    try:
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        save(event.get('inputs', {}))
    except SetupError as error: print(str(error)); raise SystemExit(1)
    except Exception: print('Could not save subtitle input part; source details suppressed.'); raise SystemExit(1)
