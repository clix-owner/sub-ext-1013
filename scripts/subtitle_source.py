"""Request-scoped torrent selection; no source URLs or metadata committed to Git."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.parse


def error(message):
    from clixarena_subtitles import SetupError
    return SetupError(message)


def event_inputs():
    return json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8')).get('inputs',{})


def identity(inputs):
    return hashlib.sha256(json.dumps({k:inputs.get(k,'') for k in ('request_id','source_kind','torrent_source')},sort_keys=True).encode()).hexdigest()


def ready(inputs):
    source=Path('.subtitle-work/source.media')
    try:
        record=json.loads(Path('.subtitle-work/source-ready.json').read_text())
        return (not source.is_symlink() and source.is_file() and source.stat().st_size==record['bytes']
                and record['identity']==identity(inputs) and record['run_id']==os.getenv('GITHUB_RUN_ID',''))
    except (OSError,KeyError,ValueError):return False


def use_prepared(inputs,target,limit):
    if Path(target).resolve()!=Path('.subtitle-work/source.media').resolve() or not ready(inputs):
        raise error('Torrent source is not prepared for this request. Update the workflow and complete file selection.')
    if Path(target).stat().st_size>limit:raise error('Selected torrent source exceeds the media limit.')
    print('Using selected torrent video already downloaded on this runner',flush=True)


def descriptor(inputs):
    kind=inputs.get('source_kind','https')
    try:source=json.loads(inputs.get('torrent_source','{}'))
    except (ValueError,TypeError):raise error('Invalid torrent source descriptor.') from None
    if kind=='magnet':
        magnet=source.get('magnet','');query=urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
        if len(magnet)>8192 or not magnet.startswith('magnet:?') or not any(re.fullmatch(r'urn:btih:(?:[a-fA-F0-9]{40}|[A-Za-z2-7]{32})',v) for v in query.get('xt',[])):
            raise error('Use a public BitTorrent v1 magnet link.')
        return {'kind':'magnet','magnet':magnet}
    if kind!='torrent':raise error('Choose a supported source type.')
    if 'torrent_upload' in source:
        from subtitle_upload import receive
        encoded=receive(source['torrent_upload'],kind='torrent')
    else:encoded=source.get('data','')
    try:
        blob=base64.b64decode(encoded,validate=True)
        if not 0<len(blob)<=1024*1024:raise ValueError()
    except (ValueError,TypeError):raise error('Upload a valid torrent file up to 1 MiB.') from None
    path=Path('.subtitle-work/source.torrent')
    if path.is_symlink():raise error('Unsafe torrent input path.')
    path.write_bytes(blob)
    return {'kind':'file','path':path.as_posix()}


def state():
    return {'request_id':event_inputs().get('request_id',''),'run_id':os.getenv('GITHUB_RUN_ID',''),
            'run_attempt':os.getenv('GITHUB_RUN_ATTEMPT','1')}


def run_download(inputs,desc,selected=None):
    import clixarena_torrent as torrent
    class SelectionNeeded(Exception):pass
    original=torrent.select_file
    def choose(files,request):
        if selected is not None:
            if type(selected) is not int or not any(f['index']==selected for f in files):raise error('Selected video does not belong to this torrent.')
            return selected
        if len(files)==1:return files[0]['index']
        # Bound published metadata before any payload download begins.
        value=dict(state(),files=files)
        raw=json.dumps(value,ensure_ascii=False).encode('utf-8')
        if len(raw)>1500000:raise error('Torrent file list is too large for selection.')
        Path('torrent-files.json').write_bytes(raw)
        raise SelectionNeeded()
    request_id=inputs.get('request_id','')
    job=request_id.replace('-','')
    if not re.fullmatch('[a-f0-9]{32}',job):raise error('Submit torrent sources using the website so this run has a request ID.')
    previous=os.environ.get('PORTAL_JOB_ID')
    os.environ['PORTAL_JOB_ID']=job
    torrent.select_file=choose
    try:
        torrent.download_torrent(desc,Path('.subtitle-work/source.media'),12*1024**3,Path.cwd(),lambda *a,**kw:None)
    except SelectionNeeded:return True
    except torrent.TorrentError as exc:raise error(str(exc)) from None
    finally:
        torrent.select_file=original
        if previous is None:os.environ.pop('PORTAL_JOB_ID',None)
        else:os.environ['PORTAL_JOB_ID']=previous
    source=Path('.subtitle-work/source.media')
    Path('.subtitle-work/source-ready.json').write_text(json.dumps({'identity':identity(inputs),'run_id':os.getenv('GITHUB_RUN_ID',''),'bytes':source.stat().st_size}))
    return False


def prepare(inputs):
    kind=inputs.get('source_kind','https')
    if kind not in ('https','torrent','magnet'):raise error('Choose a supported source type.')
    waiting=False
    if kind!='https':
        work=Path('.subtitle-work')
        if work.is_symlink():raise error('Unsafe media work directory.')
        work.mkdir(exist_ok=True)
        if any((work/name).is_symlink() for name in ('source.torrent','source.media','source-ready.json','torrent-payload')):
            raise error('Unsafe torrent work path.')
        waiting=run_download(inputs,descriptor(inputs))
    if os.getenv('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as output:output.write('waiting='+str(waiting).lower()+'\n')
    if waiting:print('Multiple torrent videos found. Select the video in the website; this same runner waits up to 20 minutes.',flush=True)
    return waiting


def read_selection(artifact_id):
    # Same bounded, trusted GitHub artifact transport as subtitle track selection.
    from subtitle_selection import selection_file
    return selection_file(artifact_id,filename='torrent-selection.json')


def wait(inputs,timeout=1200):
    from subtitle_selection import github,UUID
    if not UUID.fullmatch(inputs.get('request_id','')):raise error('Invalid torrent request identifier.')
    deadline=time.monotonic()+timeout
    expected=state()
    errors=0
    while time.monotonic()<deadline:
        try:
            name='torrent-selection-'+inputs['request_id']
            with github('/actions/artifacts?name='+name+'&per_page=100') as response:listing=json.load(response)
            for artifact in sorted(listing.get('artifacts',[]),key=lambda a:a.get('id',0),reverse=True):
                if artifact.get('name')!=name or artifact.get('expired'):continue
                chosen=read_selection(artifact['id'])
                if any(str(chosen.get(k,''))!=str(v) for k,v in expected.items()):continue
                selected=chosen.get('selected')
                if type(selected) is not int:raise error('Invalid torrent video choice.')
                return run_download(inputs,{'kind':'file','path':'.subtitle-work/source.torrent'},selected)
            errors=0
        except Exception as exc:
            from clixarena_subtitles import SetupError
            if isinstance(exc,SetupError):raise
            errors+=1
            if errors>=6:raise error('Could not retrieve torrent video selection from GitHub.') from None
        time.sleep(15)
    raise error('Torrent video selection expired after 20 minutes; no video payload was downloaded.')


def save_selection(inputs):
    from subtitle_selection import UUID
    if not UUID.fullmatch(inputs.get('request_id','')):raise error('Invalid torrent selection request.')
    try:
        value=json.loads(inputs.get('torrent_source','{}'))
        if type(value['selected']) is not int or not 0<=value['selected']<10000:raise ValueError()
        if not str(value['run_id']).isdigit() or not str(value['run_attempt']).isdigit():raise ValueError()
    except (ValueError,TypeError,KeyError):raise error('Invalid torrent video selection.') from None
    value={k:value[k] for k in ('selected','run_id','run_attempt')}
    value['request_id']=inputs['request_id']
    Path('torrent-selection.json').write_text(json.dumps(value))


if __name__=='__main__':
    import sys
    from clixarena_subtitles import SetupError
    try:
        options=event_inputs()
        if sys.argv[1]=='prepare':prepare(options)
        elif sys.argv[1]=='wait':wait(options)
        elif sys.argv[1]=='select':save_selection(options)
        else:raise error('Unknown source phase.')
    except SetupError as exc:print(str(exc),file=sys.stderr);sys.exit(1)
    except Exception:print('Source preparation failed; private details suppressed.',file=sys.stderr);sys.exit(1)
