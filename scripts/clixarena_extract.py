"""Extract embedded subtitle streams; no speech recognition or timing repair."""
import html
import json
import re
import shutil
from pathlib import Path
from clixarena_subtitles import SetupError, settings, tool
from clixarena_media_download import download

TEXT = {'subrip','ass','ssa','webvtt','mov_text','text','microdvd','sami','realtext','subviewer','subviewer1','mpl2','jacosub','pjs','vplayer'}
NATIVE = {'subrip':('srt','srt'),'ass':('ass','ass'),'ssa':('ass','ass'),'webvtt':('vtt','webvtt'),'mov_text':('mp4','mp4'),'hdmv_pgs_subtitle':('sup','sup')}

def select_tracks(value, count):
    value=str(value).strip()
    if value=='all': return list(range(count))
    if not re.fullmatch(r'\d{1,3}(\s*,\s*\d{1,3})*',value):
        raise SetupError('Use all or comma-separated subtitle track numbers starting at 0.')
    selected=list(dict.fromkeys(int(x) for x in value.split(',')))
    if len(selected)>128 or any(x>=count for x in selected):
        raise SetupError('A requested subtitle track does not exist, or too many tracks were selected.')
    return selected

def probe_tracks(source):
    data=json.loads(tool(['ffprobe','-v','error','-protocol_whitelist','file,pipe','-show_streams','-of','json',str(source)],120))
    return [x for x in data.get('streams',[]) if x.get('codec_type')=='subtitle']

def track_list(source):
    return [{'number':i,'language':str(s.get('tags',{}).get('language','und'))[:40],
             'title':str(s.get('tags',{}).get('title',''))[:200],
             'codec':s.get('codec_name','unknown'),
             'default':bool(s.get('disposition',{}).get('default')),
             'forced':bool(s.get('disposition',{}).get('forced'))}
            for i,s in enumerate(probe_tracks(source))]

def extract_tracks(source, output, name, selection='all'):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    streams=probe_tracks(source)
    if not streams: raise SetupError('This source contains no embedded subtitle tracks. Burned-in text cannot be extracted.')
    tracks=[]
    for ordinal in select_tracks(selection,len(streams)):
        stream=streams[ordinal]; codec=stream.get('codec_name','unknown'); tags=stream.get('tags',{})
        lang=tags.get('language','und'); lang=lang if re.fullmatch(r'[A-Za-z0-9_-]{1,20}',lang) else 'und'
        stem=f'{name}.sub{ordinal:02d}.{lang}'
        ext,fmt=NATIVE.get(codec,('mks','matroska'))
        record={'subtitle_track':ordinal,'source_stream':stream['index'],'codec':codec,'language':lang,'title':tags.get('title',''),'disposition':stream.get('disposition',{}),'kind':'text' if codec in TEXT else 'image or other','files':[],'status':'failed'}
        def export(extension, muxer, encoder):
            dest=output/f'{stem}.{extension}'
            try:
                tool(['ffmpeg','-nostdin','-v','error','-y','-copyts','-protocol_whitelist','file,pipe','-i',str(source),'-map',f"0:{stream['index']}",'-c:s',encoder,'-avoid_negative_ts','disabled','-f',muxer,str(dest)],1800)
                if not dest.exists() or not dest.stat().st_size: raise SetupError('Empty subtitle output.')
            except SetupError:
                dest.unlink(missing_ok=True); return False
            record['files'].append(dest.name); return True
        native=export(ext,fmt,'copy')
        if codec in TEXT and ext!='srt':
            if not export('srt','srt','srt'): record['srt_note']='SRT conversion unavailable; use the original track.'
            else: record['srt_note']='SRT may omit styling; original format is retained when supported.'
        record['status']='extracted' if record['files'] else 'failed'
        record['original_preserved']=native
        if not native: record['note']='Original-format export was unsupported for this track.'
        tracks.append(record)
        print(f"Subtitle track {ordinal}: {record['status']}",flush=True)
    report={'method':'extract','timing':'Source timestamps retained; no alignment or offset correction.','tracks':tracks}
    (output/'extraction-info.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    rows=[]
    for t in tracks:
        links=' '.join(f'<a href="{html.escape(f,quote=True)}">{html.escape(f)}</a>' for f in t['files'])
        rows.append(f"<li>Track {t['subtitle_track']} · {html.escape(t['language'])} · {html.escape(t['codec'])} · {t['status']}: {links}</li>")
    (output/'extraction-report.html').write_text('<!doctype html><meta charset="utf-8"><title>Embedded subtitles</title><h1>Embedded subtitle extraction</h1><p>Source timestamps retained. No AI, translation or timing repair. SRT conversion may drop styling. Image tracks require a compatible player; no OCR was performed.</p><ul>'+''.join(rows)+'</ul>',encoding='utf-8')
    if any(t['status']=='failed' for t in tracks):
        raise SetupError('Some requested subtitle tracks could not be extracted. See the extraction report in the output artifact.')
    return report

def main(inputs):
    cfg=settings({**inputs,'model':'medium','language':'auto','sample_only':False,'start_seconds':0,'audio_track':'auto'})
    work=Path('.subtitle-work'); work.mkdir(exist_ok=True)
    try:
        source=work/'source.media'
        download(cfg['url'],source,12*1024**3,connections=cfg['download_connections'],source=inputs)
        extract_tracks(source,Path('generated-subtitles'),cfg['name'],inputs.get('subtitle_tracks','all'))
    finally: shutil.rmtree(work,ignore_errors=True)
