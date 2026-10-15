"""Manual, original-language speech transcription; no video encoding/upload."""
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
import time
import wave

from clixarena_media_download import DownloadError, download, mask

LANGUAGES = {'auto','ta','hi','ml','te','kn','en','si','ja','ko','zh','fr','de','es','it','ru','ar','pt','tr','th','id','vi'}
ALIASES = {'tamil':'tam','ta':'tam','hindi':'hin','hi':'hin','english':'eng','en':'eng',
           'malayalam':'mal','ml':'mal','telugu':'tel','te':'tel','kannada':'kan','kn':'kan',
           'sinhala':'sin','si':'sin','japanese':'jpn','ja':'jpn','korean':'kor','ko':'kor',
           'chinese':'zho','chi':'zho','zh':'zho','french':'fra','fre':'fra','fr':'fra',
           'german':'deu','ger':'deu','de':'deu','spanish':'spa','es':'spa','italian':'ita','it':'ita'}

class SetupError(Exception):
    """Fixed messages only: do not include remote URLs/responses/tool stderr."""

def settings(inputs):
    url = str(inputs.get('media_url','')).strip() or os.getenv('SUBTITLE_MEDIA_URL','').strip()
    mask(url)
    if not url:
        raise SetupError('Enter a video/audio download URL.')
    name = str(inputs.get('output_name','subtitles.srt')).strip()
    if name.lower().endswith('.srt'):
        name = name[:-4]
    if not name or len(name)>140 or any(ord(c)<32 for c in name) or re.search(r'[<>:"/\\|?*]',name) or name in ('.','..') or name.endswith(('.', ' ')):
        raise SetupError('Use a filename without folder paths, control characters or invalid filename symbols.')
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',name,re.I):
        raise SetupError('Choose another filename; this name is reserved on Windows.')
    model = str(inputs.get('model','medium'))
    language = str(inputs.get('language','auto')).strip().lower()
    if model not in ('small','medium','large-v3','turbo') or language not in LANGUAGES:
        raise SetupError('Choose a supported model and spoken-language hint.')
    sample = inputs.get('sample_only',False)
    if sample not in (True,False,'true','false'):
        raise SetupError('Sample mode must be true or false.')
    try:
        start = float(inputs.get('start_seconds','0'))
        if not math.isfinite(start) or not 0 <= start <= 86400:
            raise ValueError()
    except (ValueError,TypeError):
        raise SetupError('Start time must be a finite number of seconds between 0 and 86400.') from None
    audio = str(inputs.get('audio_track','auto')).strip().lower()
    if not re.fullmatch(r'auto|[a-z]{2,20}|[0-9]{1,3}',audio):
        raise SetupError('Audio track must be auto, a language code/name, or a zero-based audio track number.')
    return {'url':url,'name':name,'model':model,'language':language,'sample':sample in (True,'true'),'start':start,'audio':audio}

def tool(args, timeout):
    try:
        proc = subprocess.run(args,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
    except FileNotFoundError:
        raise SetupError('FFmpeg/ffprobe is missing. Install the audio tools first.') from None
    except subprocess.TimeoutExpired:
        raise SetupError('Audio processing timed out. Check the source or try a short sample.') from None
    if proc.returncode:
        raise SetupError('Media processing failed. The source may be incomplete, damaged or unsupported. Raw tool output was suppressed.')
    return proc.stdout

def audio_streams(source):
    try:
        data = json.loads(tool(['ffprobe','-protocol_whitelist','file,pipe','-v','error','-show_streams','-show_format','-of','json',str(source)],120))
    except (ValueError,TypeError):
        raise SetupError('Media probe returned invalid data.') from None
    streams = [s for s in data.get('streams',[]) if s.get('codec_type')=='audio']
    if not streams:
        raise SetupError('The downloaded media has no audio track.')
    return streams

def select_audio(streams, preference):
    if preference.isdigit():
        index = int(preference)
        if index>=len(streams):
            raise SetupError('That audio track number does not exist. Check the audio track list in the run log.')
    elif preference=='auto':
        if len(streams)>1:
            raise SetupError('Multiple audio tracks found. Choose an audio language or track number from the list above and rerun; no track was silently selected.')
        index=0
    else:
        target=ALIASES.get(preference,preference)
        matches=[i for i,s in enumerate(streams) if ALIASES.get(str(s.get('tags',{}).get('language','und')).lower(),str(s.get('tags',{}).get('language','und')).lower())==target]
        if not matches:
            raise SetupError('The requested audio language is missing or untagged. Select an audio track number from the log instead.')
        if len(matches)>1:
            raise SetupError('More than one audio track matches that language. Select the exact audio track number from the log.')
        index=matches[0]
    return index,streams[index]

def extract_audio(source,target,index,start,sample):
    args=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y']
    if start:
        args+=['-ss',str(start)]
    args+=['-protocol_whitelist','file,pipe','-i',str(source),'-map',f'0:a:{index}','-vn','-sn','-dn','-map_metadata','-1']
    if sample:
        args+=['-t','180']
    args+=['-ac','1','-ar','16000','-af','aresample=async=1:first_pts=0','-c:a','pcm_s16le',str(target)]
    tool(args,1800)
    try:
        with wave.open(str(target),'rb') as wav:
            duration=wav.getnframes()/wav.getframerate()
    except (wave.Error,EOFError,OSError):
        raise SetupError('Could not read extracted audio.') from None
    if duration<0.1:
        raise SetupError('No audio remains at that start time. Check start_seconds.')
    return duration

def clean_text(value):
    # Remove controls/SRT separator injection while preserving Unicode combining marks.
    return ' '.join(''.join(c if ord(c)>=32 else ' ' for c in str(value)).split())

def timestamp(seconds, vtt=False):
    ms=max(0,round(seconds*1000));h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
    return f'{h:02}:{m:02}:{s:02}{"." if vtt else ","}{ms:03}'

def segment_cues(segment,duration,max_chars=84,max_seconds=6):
    words=getattr(segment,'words',None)
    if not words:
        text=clean_text(segment.text)
        start=max(0,float(segment.start));end=min(duration,float(segment.end))
        return [(start,end,text)] if text and end>start else []
    result=[];group=[];start=0;end=0
    for word in words:
        text=clean_text(word.word)
        a=max(0,float(word.start));b=min(duration,float(word.end))
        if not text or not math.isfinite(a) or not math.isfinite(b) or b<=a:
            continue
        candidate=' '.join(group+[text])
        if group and (len(candidate)>max_chars or b-start>max_seconds or len(textwrap.wrap(candidate,width=42,break_long_words=False,break_on_hyphens=False))>2):
            result.append((start,end,' '.join(group)));group=[]
        if not group:start=a
        group.append(text);end=b
        if len(' '.join(group))>=24 and re.search(r'[.!?。！？]$',text):
            result.append((start,end,' '.join(group)));group=[]
    if group:result.append((start,end,' '.join(group)))
    return result

def write_subtitles(segments,srt,vtt,transcript,duration,offset=0,progress=None):
    count=0;previous=0
    with srt.open('w',encoding='utf-8',newline='\n') as sr, vtt.open('w',encoding='utf-8',newline='\n') as vt, transcript.open('w',encoding='utf-8',newline='\n') as tx:
        vt.write('WEBVTT\n\n')
        for segment in segments:
            for start,end,text in segment_cues(segment,duration):
                start=max(start,previous);end=min(end,duration)
                a=round((start+offset)*1000)/1000;b=round((end+offset)*1000)/1000
                if b<=a:continue
                previous=end;count+=1
                lines='\n'.join(textwrap.wrap(text,width=42,break_long_words=False,break_on_hyphens=False))
                sr.write(f'{count}\n{timestamp(a)} --> {timestamp(b)}\n{lines}\n\n')
                vt.write(f'{timestamp(a,True)} --> {timestamp(b,True)}\n{lines}\n\n')
                tx.write(text+'\n')
            if progress:progress(min(duration,max(0,float(segment.end))))
    if not count:
        raise SetupError('No speech was transcribed. Check audio selection, start time and language; try a sample with audible dialogue.')
    return count

def recognize(audio,model_name,language,cache):
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    # Inference logs are unnecessary; transcript/source data are never printed.
    for logger in ('faster_whisper','huggingface_hub','httpx','httpcore'):
        logging.getLogger(logger).setLevel(logging.ERROR)
    model=WhisperModel(model_name,device='cpu',compute_type='int8',cpu_threads=min(8,os.cpu_count() or 2),num_workers=1,download_root=str(cache))
    engine=BatchedInferencePipeline(model)
    return engine.transcribe(str(audio),task='transcribe',language=None if language=='auto' else language,
        multilingual=language=='auto',batch_size=2,beam_size=5,word_timestamps=True,vad_filter=True,
        vad_parameters={'min_silence_duration_ms':500},condition_on_previous_text=False)

def main():
    root=Path.cwd().resolve();work=root/'.subtitle-work';out=root/'generated-subtitles';stage='input validation'
    try:
        if work.is_symlink() or out.is_symlink():raise SetupError('Output/work directories must not be symbolic links.')
        work.mkdir(exist_ok=True);out.mkdir(exist_ok=True)
        event_path=os.getenv('GITHUB_EVENT_PATH')
        if not event_path:raise SetupError('Run this script through the manual GitHub Actions workflow.')
        event=json.loads(Path(event_path).read_text(encoding='utf-8'))
        options=settings(event.get('inputs',{}))
        if work.is_symlink() or out.is_symlink():raise SetupError('Output/work directories must not be symbolic links.')
        if shutil.disk_usage(root).free<4*1024**3:raise SetupError('Runner disk space is below the 4 GB reserve.')
        source=work/'source.media';audio=work/'speech.wav'
        stage='media download';print('Downloading video/audio',flush=True)
        download(options['url'],source,12*1024**3)
        stage='audio selection';streams=audio_streams(source)
        for i,stream in enumerate(streams):
            lang=str(stream.get('tags',{}).get('language','und'))
            if not re.fullmatch(r'[a-zA-Z]{2,3}',lang):lang='und'
            print(f'Audio track {i}: language={lang}, channels={int(stream.get("channels",0))}',flush=True)
        index,selected=select_audio(streams,options['audio'])
        print(f'Selected audio track {index}',flush=True)
        stage='audio extraction';duration=extract_audio(source,audio,index,options['start'],options['sample'])
        source.unlink();print(f'Audio prepared: {duration/60:.1f} minutes. Model: {options["model"]}; task: original-language transcription.',flush=True)
        stage='speech model loading/transcription';cache=Path(os.getenv('WHISPER_CACHE',str(root/'.whisper-cache')))
        segments,info=recognize(audio,options['model'],options['language'],cache)
        detected=str(info.language)
        if not re.fullmatch(r'[a-z]{2,3}',detected):detected='und'
        print(f'Detected speech language: {detected}',flush=True)
        beginning=time.monotonic();last=[0.0]
        def progress(position):
            now=time.monotonic()
            if now-last[0]>=30:
                print(f'Transcribing {min(100,position/duration*100):.1f}% | elapsed {(now-beginning)/60:.1f} min',flush=True);last[0]=now
        name=options['name']
        count=write_subtitles(segments,out/(name+'.srt'),out/(name+'.vtt'),out/(name+'.txt'),duration,options['start'],progress)
        metadata={'language':detected,'language_probability':round(float(info.language_probability),3),
            'model':options['model'],'task':'transcribe','audio_track':index,'audio_duration_seconds':round(duration,3),
            'timestamp_offset_seconds':options['start'],'sample_only':options['sample'],'subtitle_cues':count,'review_required':True}
        (out/'transcription-info.json').write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
        print(f'Complete: {count} subtitle cues. Download the run artifact for SRT/VTT/TXT.',flush=True)
        if os.getenv('GITHUB_STEP_SUMMARY'):
            Path(os.environ['GITHUB_STEP_SUMMARY']).write_text(f'## Subtitles ready\n\nOriginal language: `{detected}` · model: `{options["model"]}` · audio track: `{index}` · cues: {count}\n\nDownload **clixarena-subtitles-{os.getenv("GITHUB_RUN_ID","")}** from this run’s Artifacts section. Generated files include SRT, VTT, TXT and timing metadata. Review recognition and timing before use.\n',encoding='utf-8')
        return 0
    except (SetupError,DownloadError) as error:
        print('::error::'+str(error),flush=True)
        return 1
    except Exception:
        print(f'::error::Failed during {stage}. Check model availability, media format, memory and disk space. Raw errors suppressed to protect source URLs.',flush=True)
        return 1
    finally:
        if work.parent==root and not work.is_symlink():shutil.rmtree(work,ignore_errors=True)

if __name__=='__main__':sys.exit(main())
