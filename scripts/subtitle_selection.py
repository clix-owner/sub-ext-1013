"""Keep source on the same runner while the user selects subtitle tracks."""
import io
import json
import os
import re
import time
import urllib.request
import zipfile
from pathlib import Path
from clixarena_extract import extract_tracks,track_list,select_tracks
from clixarena_subtitles import SetupError,settings
from clixarena_media_download import download

UUID=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}')

def event_inputs():
    return json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8')).get('inputs',{})

def prepare(inputs):
    cfg=settings({**inputs,'model':'medium','language':'auto','sample_only':False,'start_seconds':0,'audio_track':'auto'})
    work=Path('.subtitle-work');work.mkdir(exist_ok=True)
    source=work/'source.media'
    download(cfg['url'],source,12*1024**3,connections=cfg['download_connections'])
    tracks=track_list(source)
    if not tracks:raise SetupError('This source contains no embedded subtitle tracks. Burned-in text cannot be extracted.')
    requested=inputs.get('subtitle_tracks','ask')
    waiting=len(tracks)>1 and requested=='ask'
    if waiting:
        if not UUID.fullmatch(inputs.get('request_id','')):raise SetupError('Use the website to choose tracks, or specify subtitle track numbers in the manual workflow.')
        Path('subtitle-tracks.json').write_text(json.dumps({'tracks':tracks},ensure_ascii=False),encoding='utf-8')
        print('Waiting for subtitle track selection in the website.',flush=True)
    else:
        extract_tracks(source,Path('generated-subtitles'),cfg['name'],'0' if len(tracks)==1 and requested=='ask' else requested)
    if os.getenv('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as f:f.write('waiting='+str(waiting).lower()+'\n')
    return waiting

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None

def github(path):
    token=os.environ['GH_TOKEN'];repo=os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise SetupError('Invalid repository.')
    request=urllib.request.Request('https://api.github.com/repos/'+repo+path,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'clixarena-subtitle-selection'})
    return urllib.request.build_opener(NoRedirect).open(request,timeout=30)

def selection_file(artifact_id):
    from urllib.error import HTTPError
    from urllib.parse import urlsplit
    try:github('/actions/artifacts/'+str(int(artifact_id))+'/zip')
    except HTTPError as e:
        if e.code!=302:raise
        location=e.headers.get('Location','')
    else:raise SetupError('Selection artifact did not return a download.')
    u=urlsplit(location)
    if u.scheme!='https' or u.username or u.password or u.port or not any(u.hostname.endswith(x) for x in ('.blob.core.windows.net','.githubusercontent.com','.github.com')):
        raise SetupError('Unexpected selection artifact host.')
    # No authorization header is forwarded to signed artifact storage.
    with urllib.request.build_opener(NoRedirect).open(location,timeout=30) as response:
        raw=response.read(100001)
    if len(raw)>100000:raise SetupError('Selection artifact is too large.')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info=archive.getinfo('selection.json')
        if info.file_size>4096:raise SetupError('Selection file is too large.')
        return json.loads(archive.read(info))

def wait(inputs,timeout=1800):
    request_id=inputs.get('request_id','')
    if not UUID.fullmatch(request_id):raise SetupError('Invalid selection request identifier.')
    deadline=time.monotonic()+timeout;errors=0
    while time.monotonic()<deadline:
        try:
            with github('/actions/artifacts?name=subtitle-selection-'+request_id+'&per_page=100') as response:data=json.load(response)
            candidates=[a for a in data.get('artifacts',[]) if a.get('name')=='subtitle-selection-'+request_id and not a.get('expired')]
            # A selection workflow uses exactly this request UUID; never reuse another request's choice.
            if candidates:
                selected=selection_file(max(candidates,key=lambda x:x['id'])['id'])
                if selected.get('request_id')!=request_id:raise SetupError('Selection does not match this request.')
                source=Path('.subtitle-work/source.media');tracks=track_list(source)
                choice=selected.get('subtitle_tracks','')
                if choice in ('all','ask'):raise SetupError('Tick at least one subtitle track.')
                select_tracks(choice,len(tracks))
                cfg=settings({**inputs,'model':'medium','language':'auto','sample_only':False,'start_seconds':0,'audio_track':'auto'})
                return extract_tracks(source,Path('generated-subtitles'),cfg['name'],choice)
            errors=0
        except SetupError:raise
        except Exception:
            errors+=1
            if errors>=6:raise SetupError('Could not retrieve the subtitle selection from GitHub. Retry this request.') from None
        time.sleep(10)
    raise SetupError('No subtitle tracks were chosen within 30 minutes. Start a new request and tick the tracks to extract.')

def save_selection(inputs):
    request_id=inputs.get('request_id','')
    if not UUID.fullmatch(request_id):raise SetupError('Invalid selection request identifier.')
    choice=str(inputs.get('subtitle_tracks',''))
    if choice in ('all','ask'):raise SetupError('Choose subtitle track numbers.')
    select_tracks(choice,1000)
    Path('selection.json').write_text(json.dumps({'request_id':request_id,'subtitle_tracks':choice}),encoding='utf-8')

if __name__=='__main__':
    import sys
    from clixarena_media_download import DownloadError
    try:
        inputs=event_inputs()
        if sys.argv[1]=='prepare':prepare(inputs)
        elif sys.argv[1]=='wait':wait(inputs)
        elif sys.argv[1]=='select':save_selection(inputs)
        else:raise SetupError('Unknown selection phase.')
    except (SetupError,DownloadError) as e:print(str(e),file=sys.stderr);sys.exit(1)
    except Exception:print('Subtitle track preparation or selection failed. Source details were suppressed.',file=sys.stderr);sys.exit(1)
