"""Retiming by independently recognized words. Output dialogue is always uploaded text."""
import argparse
import base64
from collections import Counter
import difflib
import gzip
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import unicodedata
import wave
from clixarena_subtitles import SetupError,settings,audio_streams,select_audio,extract_audio,timestamp
from clixarena_media_download import download,mask

LIMIT=2*1024*1024
TIME=r'(?:\d{1,3}:)?\d{2}:\d{2}[,.]\d{3}'
TIMING=re.compile(rf'^({TIME})\s+-->\s+({TIME})(?:[ \t]+[^\r\n]*)?$')

def seconds(value):
    parts=value.replace(',','.').split(':')
    if len(parts)==2:parts.insert(0,'0')
    h,m,s=parts;value=int(h)*3600+int(m)*60+float(s)
    if int(m)>=60 or float(s)>=60:raise SetupError('Invalid subtitle timestamp.')
    return value

def parse_subtitles(text):
    text=text.lstrip('\ufeff').replace('\r\n','\n').replace('\r','\n')
    if '\0' in text:raise SetupError('Subtitle contains invalid controls.')
    cues=[];previous=-1
    for block in re.split(r'\n[ \t]*\n',text.strip()):
        lines=block.split('\n')
        if lines[0].startswith(('WEBVTT','NOTE','STYLE','REGION')):continue
        pos=0;match=TIMING.fullmatch(lines[0].strip())
        if not match and len(lines)>1:pos=1;match=TIMING.fullmatch(lines[1].strip())
        if not match:raise SetupError('Upload a valid UTF-8 SRT or VTT file; malformed cues are never discarded.')
        start,end=map(seconds,match.groups());body='\n'.join(lines[pos+1:])
        if not body.strip() or end<=start or start<previous or end-start>120:raise SetupError('Subtitles must contain ordered, nonempty cues shorter than 120 seconds.')
        cues.append({'source_id':lines[0] if pos else str(len(cues)+1),'start':start,'end':end,'text':body})
        previous=start
        if len(cues)>20000:raise SetupError('Subtitle exceeds 20,000 cues.')
    if not cues:raise SetupError('Subtitle contains no cues.')
    return cues

def spoken_text(text):
    text=html.unescape(re.sub(r'<[^>]*>','',text))
    text=re.sub(r'\{\\[^}]*\}','',text)
    lines=[]
    for line in text.splitlines():
        if '♪' in line or '♫' in line:continue
        line=re.sub(r'\[[^]]*\]|\([^)]*\)',' ',line)
        line=re.sub(r'^\s*(?:-\s*)?[A-Z][A-Z .\d_’\x27-]{0,35}:\s*','',line)
        lines.append(line)
    return ' '.join(lines)

def tokens(text):
    text=unicodedata.normalize('NFKC',text).casefold().replace('’',"'")
    text=re.sub(r"\bwon't\b",'will not',text)
    text=re.sub(r"\bcan't\b",'can not',text)
    text=re.sub(r"n\x27t\b",' not',text)
    for suffix,replacement in [("'re",' are'),("'ve",' have'),("'ll",' will'),("'m",' am')]:text=text.replace(suffix,replacement)
    text=re.sub(r'([\u3400-\u9fff\u3040-\u30ff])',r' \1 ',text)
    return re.findall(r'[^\W_]+',text.replace("'",''),re.UNICODE)

def anchor_transform(anchors):
    if len(anchors)<10:return None
    points=[((c['original_start']+c['original_end'])/2,(c['start']+c['end'])/2) for c in anchors]
    if points[-1][0]-points[0][0]<120:return None
    # A robust slope estimate resists individual recognition boundary mistakes.
    subset=points[::max(1,len(points)//100)]
    slopes=[(b[1]-a[1])/(b[0]-a[0]) for i,a in enumerate(subset) for b in subset[i+1:] if b[0]-a[0]>=60]
    if not slopes:return None
    scale=statistics.median(slopes);offset=statistics.median(y-scale*x for x,y in points)
    residuals=[abs(y-scale*x-offset) for x,y in points]
    inliers=[(x,y) for (x,y),error in zip(points,residuals) if error<=1.25]
    if len(inliers)<10 or len(inliers)/len(points)<.7 or statistics.median(residuals)>.6 or not .85<=scale<=1.15 or abs(offset)>120:return None
    mean_x=statistics.mean(x for x,y in inliers);mean_y=statistics.mean(y for x,y in inliers)
    denom=sum((x-mean_x)**2 for x,y in inliers)
    if denom:
        scale=sum((x-mean_x)*(y-mean_y) for x,y in inliers)/denom;offset=mean_y-scale*mean_x
    return {'scale':scale,'offset':offset,'anchors':len(inliers),'median_residual_seconds':statistics.median(abs(y-scale*x-offset) for x,y in inliers)}

def match_cues(cues,words,duration):
    source=[];ranges=[]
    for cue in cues:
        start=len(source);source.extend(tokens(spoken_text(cue['text'])));ranges.append((start,len(source)))
    target=[];audio=[]
    for word in words:
        try:a=float(word['start']);b=float(word['end']);prob=float(word.get('probability',1))
        except (KeyError,ValueError,TypeError):continue
        if not all(math.isfinite(x) for x in (a,b,prob)) or a<0 or b<=a or b>duration+.2 or prob<.12:continue
        for token in tokens(word.get('word','')):target.append(token);audio.append({'start':a,'end':b,'probability':prob})
    if not source or not target:raise SetupError('No recognizable dialogue words were available for matching.')
    counts_a=Counter(source);counts_b=Counter(target)
    mapping={};blocks=difflib.SequenceMatcher(None,source,target,autojunk=False).get_matching_blocks()
    for block in blocks:
        if not block.size:continue
        if block.size==1:
            token=source[block.a]
            if counts_a[token]!=1 or counts_b[token]!=1 or audio[block.b]['probability']<.8:continue
            # Isolated rare words alone are insufficient without a nearby phrase anchor.
            if not any(other.size>=3 and abs(other.a-block.a)<30 and abs(other.b-block.b)<45 for other in blocks):continue
        if block.size==2 and all(counts_a[t]>3 or counts_b[t]>3 for t in source[block.a:block.a+2]):continue
        for i in range(block.size):mapping[block.a+i]=block.b+i
    result=[]
    for i,cue in enumerate(cues):
        a,b=ranges[i];total=b-a;indices=[j for j in range(a,b) if j in mapping]
        item={**cue,'original_start':cue['start'],'original_end':cue['end'],'status':'unchanged_unmatched','matched_words':len(indices),'source_words':total,'coverage':round(len(indices)/total,3) if total else 0,'flags':[]}
        if not total:item['status']='unchanged_non_dialogue'
        elif indices:
            selected=[audio[mapping[j]] for j in indices]
            confidence=sum(w['probability'] for w in selected)/len(selected)
            start=selected[0]['start'];end=selected[-1]['end']
            gaps=[selected[j]['start']-selected[j-1]['end'] for j in range(1,len(selected))]
            complete_edges=a in mapping and b-1 in mapping
            enough=len(indices)/total>=.6 and (len(indices)>=2 or (total==1 and confidence>=.8))
            sane=end>start and end-start<=max(18,(cue['end']-cue['start'])*3) and max(gaps,default=0)<=max(3,(cue['end']-cue['start'])*.7)
            if complete_edges and enough and sane and confidence>=.5:
                item.update(start=round(max(0,start),3),end=round(min(duration,end),3),status='word_matched',word_probability=round(confidence,3))
            else:item['flags'].append('insufficient_boundary_or_phrase_evidence')
        result.append(item)
    transform=anchor_transform([c for c in result if c['status']=='word_matched'])
    baseline=[]
    for item in result:
        start,end=item['original_start'],item['original_end'];status=item['status']
        if transform:
            start=start*transform['scale']+transform['offset'];end=end*transform['scale']+transform['offset']
            if start>=0 and end<=duration:
                status='speech_anchor_transform'
                if item['status']=='word_matched' and abs((item['start']+item['end']-start-end)/2)>2:
                    item['status']='speech_anchor_transform';item['flags'].append('word_match_outlier_against_consistent_speech_anchors')
                if item['status']!='word_matched':item.update(start=round(start,3),end=round(end,3),status=status)
            else:start,end=item['original_start'],item['original_end']
        baseline.append({'start':round(start,3),'end':round(end,3),'status':status if status!='word_matched' else 'unchanged_conflict'})
    # Infer only a local offset supported on both sides; never call it a word match.
    anchors=[(i,c) for i,c in enumerate(result) if c['status']=='word_matched']
    for i,item in enumerate(result):
        if item['status'] in ('word_matched','speech_anchor_transform'):continue
        left=next(((j,c) for j,c in reversed(anchors) if j<i),None)
        right=next(((j,c) for j,c in anchors if j>i),None)
        if not left or not right:continue
        l,r=left[1],right[1]
        lc=(l['original_start']+l['original_end'])/2;rc=(r['original_start']+r['original_end'])/2
        center=(item['original_start']+item['original_end'])/2
        lo=((l['start']+l['end'])-(l['original_start']+l['original_end']))/2
        ro=((r['start']+r['end'])-(r['original_start']+r['original_end']))/2
        if rc<=lc or center-lc>45 or rc-center>45 or abs(lo-ro)>1:continue
        delta=lo+(ro-lo)*(center-lc)/(rc-lc)
        start=item['original_start']+delta;end=item['original_end']+delta
        if start>=0 and end<=duration:
            item.update(start=round(start,3),end=round(end,3),status='anchor_inferred')
            item['flags'].append('local_offset_from_neighboring_word_matches_not_independently_verified')
    # Preserve original overlaps; roll back new conflicts rather than trim dialogue.
    for _ in range(len(result)+1):
        changed=False
        for i in range(1,len(result)):
            previous,current=result[i-1],result[i]
            original_overlap=max(0,cues[i-1]['end']-cues[i]['start'])
            if current['start']<previous['start'] or previous['end']-current['start']>original_overlap+.08:
                for item,original in [(previous,baseline[i-1]),(current,baseline[i])]:
                    if item['status'] in ('word_matched','anchor_inferred'):
                        item.update(start=original['start'],end=original['end'],status=original['status'])
                        item['flags'].append('new_overlap_or_order_conflict_rejected');changed=True
        if not changed:break
    for item in result:
        if transform:item['anchor_transform']=transform
    return result

def sample_cues(cues,start,end):
    result=[]
    for cue in cues:
        transform=cue.get('anchor_transform')
        a,b=cue['start'],cue['end']
        if transform and cue['status']!='word_matched':
            a=cue['original_start']*transform['scale']+transform['offset']
            b=cue['original_end']*transform['scale']+transform['offset']
        if a>=start and b<=end:result.append(cue)
    if not result:raise SetupError('No complete subtitle cues could be placed within this audio sample.')
    return result

def export(cues,out,name,metadata):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    srt=''.join(f'{c["source_id"]}\n{timestamp(c["start"])} --> {timestamp(c["end"])}\n{c["text"]}\n\n' for c in cues)
    (out/(name+'.srt')).write_text(srt,encoding='utf-8',newline='\n')
    original=''.join(f'{c["source_id"]}\n{timestamp(c["original_start"])} --> {timestamp(c["original_end"])}\n{c["text"]}\n\n' for c in cues)
    (out/(name+'.original.srt')).write_text(original,encoding='utf-8',newline='\n')
    counts=dict(Counter(c['status'] for c in cues));matched=counts.get('word_matched',0);inferred=counts.get('anchor_inferred',0)+counts.get('speech_anchor_transform',0)
    report={**metadata,'method':'recognized_word_matching','cues':len(cues),'status_counts':counts,'dialogue_preserved':True,'text_sha256':hashlib.sha256('\n\n'.join(c['text'] for c in cues).encode()).hexdigest(),'note':'Word matches are model estimates, not manually verified ground truth. Direct word matches and inferred timing corrections are labeled separately. Lines without sufficient timing support keep their original timestamps.','anchor_transform':next((c['anchor_transform'] for c in cues if 'anchor_transform' in c),None),'timings':[{k:v for k,v in c.items() if k not in ('text','anchor_transform')} for c in cues]}
    (out/'word-match-info.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    rows=[]
    for i,c in enumerate(cues,1):
        rows.append(f'<tr><td>{i}</td><td>{html.escape(c["status"])}</td><td>{timestamp(c["original_start"])} → {timestamp(c["start"])}</td><td>{timestamp(c["original_end"])} → {timestamp(c["end"])}</td><td>{c["matched_words"]}/{c["source_words"]}</td><td style="white-space:pre-wrap">{html.escape(c["text"])}</td></tr>')
    embedded=json.dumps([{'start':c['start'],'end':c['end'],'original_start':c['original_start'],'original_end':c['original_end'],'text':c['text']} for c in cues],ensure_ascii=False).replace('<','\\u003c')
    player='<p>Choose the original local video/audio to review timing. The media stays in this browser; nothing is uploaded.</p><input id="media" type="file" accept="video/*,audio/*,.mkv"><p><label><input id="original" type="checkbox"> Compare original timestamps</label></p><video id="player" controls style="width:min(100%,900px);max-height:65vh;background:#111"></video><p id="mediaStatus">For MKV playback, your browser must support its video/audio codecs. If it cannot play this file, load the output SRT alongside the original video in your video player.</p>'
    script="""<script>
const cues=DATA;const player=document.getElementById('player');let mediaURL,trackURL;
function stamp(s){const n=Math.round(s*1000);return String(Math.floor(n/3600000)).padStart(2,'0')+':'+String(Math.floor(n/60000)%60).padStart(2,'0')+':'+String(Math.floor(n/1000)%60).padStart(2,'0')+'.'+String(n%1000).padStart(3,'0');}
function timing(){const old=document.getElementById('original').checked;return cues.map(c=>({start:old?c.original_start:c.start,end:old?c.original_end:c.end,text:c.text}));}
function captions(){if(trackURL)URL.revokeObjectURL(trackURL);const text='WEBVTT\\n\\n'+timing().map(c=>stamp(c.start)+' --> '+stamp(c.end)+'\\n'+c.text+'\\n\\n').join('');trackURL=URL.createObjectURL(new Blob([text],{type:'text/vtt'}));player.querySelector('track')?.remove();const track=document.createElement('track');track.kind='subtitles';track.label='Existing dialogue';track.srclang='en';track.default=true;track.src=trackURL;player.append(track);if(player.textTracks[0])player.textTracks[0].mode='showing';}
document.getElementById('media').onchange=e=>{const file=e.target.files[0];if(!file)return;if(mediaURL)URL.revokeObjectURL(mediaURL);mediaURL=URL.createObjectURL(file);player.src=mediaURL;captions();document.getElementById('mediaStatus').textContent='Local media loaded. Click a subtitle row to jump to that line.';};
document.getElementById('original').onchange=captions;
player.onerror=()=>{document.getElementById('mediaStatus').textContent='This browser could not play this media format. Open the video and output SRT in your usual video player.';};
document.querySelectorAll('table tr').forEach((row,i)=>{if(!i)return;row.style.cursor='pointer';row.onclick=()=>{if(!player.src)return;player.currentTime=Math.max(0,timing()[i-1].start-1);player.play().catch(()=>{});};});
</script>""".replace('DATA',embedded)
    (out/'word-match-review.html').write_text('<!doctype html><meta charset="utf-8"><title>Word matching review</title><style>body{font:16px system-ui;margin:2rem}table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:.5rem}tr:hover{background:#eee}</style><h1>Existing subtitle timing</h1>'+f'<p>{matched}/{len(cues)} cues matched to recognized audio words; {inferred} use a timing transform supported by recognized speech matches. Remaining cues keep original timing. Dialogue and line breaks preserved. This is an automated estimate; matches have not been manually verified.</p>'+player+'<table><tr><th>Cue</th><th>Status</th><th>Start: original → output</th><th>End: original → output</th><th>Words</th><th>Original text</th></tr>'+''.join(rows)+'</table>'+script,encoding='utf-8')
    print(f'Word matching complete: {matched}/{len(cues)} cues; dialogue preserved.',flush=True)
    return report

def recognize(audio,model,language,cache,allow_download=True):
    from faster_whisper import WhisperModel
    model=model+'.en' if language=='en' and model in ('base','small','medium') else model
    engine=WhisperModel(model,device='cpu',compute_type='int8',cpu_threads=min(8,os.cpu_count() or 2),num_workers=1,download_root=str(cache),local_files_only=not allow_download)
    segments,info=engine.transcribe(str(audio),language=None if language=='auto' else language,task='transcribe',beam_size=5,word_timestamps=True,vad_filter=False,vad_parameters={'min_silence_duration_ms':500,'speech_pad_ms':300},condition_on_previous_text=False,temperature=0)
    if language=='auto':print(f'Detected speech language: {info.language}',flush=True)
    words=[];progress=0
    for segment in segments:
        if segment.no_speech_prob>.8 and segment.avg_logprob<-1:continue
        for word in segment.words or []:words.append({'word':word.word,'start':word.start,'end':word.end,'probability':word.probability})
        if segment.end-progress>30:progress=segment.end;print(f'Audio word recognition: {progress:.0f} seconds processed.',flush=True)
    return words,info.duration

def subtitle_text(payload,work):
    try:options=json.loads(payload)
    except (ValueError,TypeError):raise SetupError('Invalid subtitle input.') from None
    if 'subtitle_upload' in options:
        if options.get('subtitle_data') or options.get('subtitle_url'):raise SetupError('Ambiguous subtitle input.')
        from subtitle_upload import receive
        options={'subtitle_data':receive(options['subtitle_upload']),'subtitle_url':''}
    data=str(options.get('subtitle_data',''));url=str(options.get('subtitle_url',''))
    mask(data);mask(url)
    if bool(data)==bool(url):raise SetupError('Choose one subtitle file or download URL.')
    if data:
        try:
            raw=base64.b64decode(data,validate=True)
            if len(raw)>512*1024:raise ValueError()
            import io
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:plain=f.read(LIMIT+1)
        except Exception:raise SetupError('Invalid compressed subtitle file.') from None
    else:
        target=work/'uploaded-subtitle.srt';download(url,target,LIMIT);plain=target.read_bytes()
    if not plain or len(plain)>LIMIT:raise SetupError('Subtitle must be under 2 MB.')
    try:return plain.decode('utf-8-sig')
    except UnicodeError:raise SetupError('Subtitle must be UTF-8.') from None

def main(inputs):
    cfg=settings(inputs)
    work=Path('.subtitle-work');work.mkdir(exist_ok=True)
    try:
        cues=parse_subtitles(subtitle_text(inputs.get('subtitle_tracks',''),work))
        source=work/'source.media';download(cfg['url'],source,12*1024**3)
        index,_=select_audio(audio_streams(source),cfg['audio'])
        audio=work/'speech.wav';duration=extract_audio(source,audio,index,cfg['start'],cfg['sample']);source.unlink()
        offset=cfg['start'];chosen=cues if not cfg['sample'] and offset==0 else [c for c in cues if c['start']>=offset and c['end']<=offset+duration]
        if not chosen:raise SetupError('No complete subtitle cues fall within this audio sample.')
        words,_=recognize(audio,cfg['model'],cfg['language'],Path(os.getenv('WHISPER_CACHE','.speech-models')))
        for word in words:word['start']+=offset;word['end']+=offset
        matched=match_cues(chosen,words,offset+duration)
        if cfg['sample'] or offset:matched=sample_cues(matched,offset,offset+duration)
        export(matched,Path('generated-subtitles'),cfg['name'],{'model':cfg['model'],'audio_track':index,'language':cfg['language'],'sample_only':cfg['sample'],'audio_start':offset,'audio_end':offset+duration,'original_cues':len(cues),'omitted_outside_sample':len(cues)-len(matched)})
    finally:shutil.rmtree(work,ignore_errors=True)
