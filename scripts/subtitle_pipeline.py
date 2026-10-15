"""Staged subtitle pipeline. Timings always originate from source-language audio.

No transcript, API response, media URL or credential is printed to Actions logs.
Heavy model imports are lazy so parsing/export/tests do not require model downloads.
"""
import argparse
import base64
import gzip
import contextlib
import difflib
import gc
import html
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import sys
import unicodedata
import zlib

import clixarena_subtitles as base
from clixarena_media_download import download, mask, DownloadError

WORK = Path('.subtitle-work')
OUT = Path('generated-subtitles')
STATE = WORK / 'pipeline.json'
MODELS = {'small', 'medium', 'large-v3', 'turbo'}
TIME = r'(?:\d{1,3}:)?\d{2}:\d{2}[,.]\d{3}'
TIMING = re.compile(rf'^({TIME})\s+-->\s+({TIME})(?:[ \t]+[^\r\n]*)?$')


def config(raw):
    conf = base.settings(raw)
    try:
        extra = json.loads(raw.get('processing_options') or '{}')
    except (ValueError, TypeError):
        raise base.SetupError('Invalid processing options.') from None
    if not isinstance(extra, dict):
        raise base.SetupError('Invalid processing options.')
    conf['mode'] = extra.get('mode', 'transcribe')
    conf['alignment'] = extra.get('alignment', conf['mode'] != 'transcribe')
    conf['recover'] = extra.get('recover', False)
    if conf['mode'] not in ('transcribe', 'align') or any(type(conf[k]) is not bool for k in ('alignment', 'recover')):
        raise base.SetupError('Invalid workflow or quality options.')
    try:
        conf['subtitle_shift'] = float(extra.get('subtitle_shift', 0))
        if not math.isfinite(conf['subtitle_shift']) or abs(conf['subtitle_shift']) > 86400:
            raise ValueError()
    except (ValueError, TypeError):
        raise base.SetupError('Invalid subtitle timing shift.') from None
    conf['subtitle_data'] = str(raw.get('subtitle_data', ''))
    if len(conf['subtitle_data']) > 60000:
        raise base.SetupError('Compressed subtitle exceeds the direct request limit.')
    conf['subtitle_url'] = str(raw.get('subtitle_url', '')).strip()
    mask(conf['subtitle_url'])
    mask(conf['subtitle_data'])
    if conf['subtitle_data'] and conf['subtitle_url']:
        raise base.SetupError('Choose a subtitle file or URL, not both.')
    if conf['mode'] == 'align' and (not (conf['subtitle_data'] or conf['subtitle_url']) or not conf['alignment']):
        raise base.SetupError('Uploaded-subtitle mode requires a subtitle file and alignment.')
    if (conf['subtitle_data'] or conf['subtitle_url']) and conf['language'] == 'auto':
        raise base.SetupError('Choose the language of the uploaded subtitles and selected audio.')
    if conf['mode'] == 'align' and conf['recover']:
        raise base.SetupError('Timestamp repair cannot add or recognize new dialogue. Use Create new subtitles separately.')
    if conf['mode'] == 'transcribe' and (conf['subtitle_data'] or conf['subtitle_url']):
        raise base.SetupError('Use timestamp repair for uploaded subtitles.')
    conf['alignment_model'] = extra.get('alignment_model', 'default')
    if conf['alignment_model'] not in ('default', 'english-large', 'configured'):
        raise base.SetupError('Invalid alignment model.')
    if conf['alignment_model'] == 'english-large' and conf['language'] != 'en':
        raise base.SetupError('English alignment model requires English audio and subtitles.')
    if conf['alignment_model'] == 'configured' and not os.getenv('ALIGN_MODEL', '').strip():
        raise base.SetupError('Set repository variable ALIGN_MODEL to a tested model for the selected audio language.')
    try:
        conf['alignment_window'] = float(extra.get('alignment_window', 3))
        if not math.isfinite(conf['alignment_window']) or not .5 <= conf['alignment_window'] <= 30:
            raise ValueError()
    except (TypeError, ValueError):
        raise base.SetupError('Timing search window must be between 0.5 and 30 seconds.') from None
    return conf


def decode_subtitle(encoded):
    try:
        packed = base64.b64decode(encoded, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as source:
            plain = source.read(2*1024*1024+1)
        if len(plain) > 2*1024*1024:
            raise base.SetupError('Subtitle exceeds the 2 MB decompressed limit.')
        return plain.decode('utf-8-sig')
    except (ValueError, OSError, EOFError, UnicodeError, zlib.error):
        raise base.SetupError('Invalid compressed UTF-8 subtitle file.') from None


def seconds(value):
    parts = value.replace(',', '.').split(':')
    if len(parts) == 2:
        parts.insert(0, '0')
    hours, minutes, secs = map(float, parts)
    if minutes >= 60 or secs >= 60:
        raise base.SetupError('Subtitle timestamp has invalid minutes or seconds.')
    return hours * 3600 + minutes * 60 + secs


def parse_subtitles(text):
    """Strict block parsing: malformed cues are rejected, never silently dropped."""
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    if '\0' in text:
        raise base.SetupError('Subtitle file contains invalid control characters.')
    result = []
    previous = -1
    for block in re.split(r'\n[ \t]*\n', text.strip()):
        lines = block.splitlines()
        if not lines:
            continue
        if lines[0] in ('WEBVTT', 'NOTE', 'STYLE', 'REGION') or lines[0].startswith(('WEBVTT ', 'NOTE ')):
            continue
        match = TIMING.fullmatch(lines[0].strip())
        position = 0
        if not match and len(lines) > 1:
            match = TIMING.fullmatch(lines[1].strip())
            position = 1
        if not match:
            raise base.SetupError('Malformed subtitle block. Upload a valid UTF-8 SRT or VTT file.')
        start, end = map(seconds, match.groups())
        body = html.unescape(re.sub(r'<[^>]*>', '', ' '.join(lines[position + 1:])))
        body = base.clean_text(re.sub(r'\{\\[^}]*\}', '', body))
        if not body or end <= start or start < previous or end - start > 120:
            raise base.SetupError('Subtitles need ordered timestamps, nonempty text and cues shorter than 120 seconds.')
        previous = start
        result.append({'id': f'c{len(result)+1:06}', 'source_id': lines[0] if position == 1 else '', 'start': start, 'end': end, 'text': body, 'display_text': '\n'.join(lines[position + 1:]), 'flags': [], 'words': []})
        if len(result) > 20000:
            raise base.SetupError('Subtitle file exceeds 20,000 cues.')
    if not result:
        raise base.SetupError('Subtitle file contains no timed dialogue.')
    return result


def clip_cues(cues, offset, duration, shift=0, preserve_all=False):
    result = []
    for cue in cues:
        start, end = cue['start'] + shift - offset, cue['end'] + shift - offset
        if preserve_all:
            if start < 0:
                raise base.SetupError('Subtitle timing shift moves a cue before zero. Adjust the shift; no dialogue was removed.')
            item = {**cue, 'flags': list(cue.get('flags', [])), 'start': start, 'end': end}
            if start >= duration or end > duration:
                item['flags'].append('outside_audio_original_timing_kept')
            result.append(item)
            continue
        if end <= 0 or start >= duration:
            continue
        item = {**cue, 'flags': list(cue.get('flags', [])), 'start': max(0, start), 'end': min(duration, end)}
        if start < 0 or end > duration:
            item['flags'].append('clipped_at_sample_boundary')
        result.append(item)
    if not result:
        raise base.SetupError('No uploaded subtitles overlap the selected audio range. Check start time and subtitle shift.')
    return result


def save(state):
    # Download URLs and secrets must not enter state or final artifacts.
    STATE.write_text(json.dumps(state, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def read_state():
    return json.loads(STATE.read_text(encoding='utf-8'))


def prepare():
    if WORK.is_symlink() or OUT.is_symlink():
        raise base.SetupError('Work/output directories must not be symbolic links.')
    WORK.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    conf = config(event.get('inputs', {}))
    source = WORK / 'source.media'
    download(conf['url'], source, 12 * 1024**3)
    streams = base.audio_streams(source)
    for index, stream in enumerate(streams):
        language = str(stream.get('tags', {}).get('language', 'und'))
        if not re.fullmatch('[a-zA-Z]{2,3}', language):
            language = 'und'
        print(f'Audio track {index}: language={language}', flush=True)
    index, _ = base.select_audio(streams, conf['audio'])
    duration = base.extract_audio(source, WORK / 'speech.wav', index, conf['start'], conf['sample'])
    source.unlink()
    cues = []
    if conf['subtitle_data'] or conf['subtitle_url']:
        if conf['subtitle_data']:
            subtitle_text = decode_subtitle(conf['subtitle_data'])
        else:
            uploaded = WORK / 'uploaded.subtitles'
            download(conf['subtitle_url'], uploaded, 2 * 1024**2)
            try:
                subtitle_text = uploaded.read_text(encoding='utf-8-sig')
            except UnicodeError:
                raise base.SetupError('Save the subtitle file as UTF-8 before uploading.') from None
            uploaded.unlink()
        cues = clip_cues(parse_subtitles(subtitle_text), conf['start'], duration, conf['subtitle_shift'], preserve_all=not conf['sample'] and conf['start'] == 0)
    conf.pop('url')
    conf.pop('subtitle_url')
    conf.pop('subtitle_data')
    save({'config': conf, 'duration': duration, 'audio_track': index, 'cues': cues, 'uploaded': bool(cues),
          'language': conf['language'], 'original_cues': cues, 'candidates': [], 'notes': [], 'alignment_applied': False})
    print(f'Audio prepared: track {index}, {duration/60:.1f} minutes.', flush=True)


@contextlib.contextmanager
def quiet_models():
    # Some aligner versions print failed transcript text. Suppress model output.
    for name in ('faster_whisper', 'whisperx', 'huggingface_hub', 'transformers', 'httpx', 'speechbrain'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def recognize_cues(model, duration, language, vad):
    with quiet_models():
        segments, info = model.transcribe(str(WORK / 'speech.wav'), language=None if language == 'auto' else language,
            task='transcribe', beam_size=5, temperature=0, word_timestamps=True,
            condition_on_previous_text=False, vad_filter=vad,
            vad_parameters={'min_silence_duration_ms': 300, 'speech_pad_ms': 400})
        result = []
        for segment in segments:
            for start, end, text in base.segment_cues(segment, duration):
                flags = []
                if segment.avg_logprob < -0.8 or segment.no_speech_prob > 0.5:
                    flags.append('low_recognition_confidence')
                words = [{'word': w.word.strip(), 'start': w.start, 'end': w.end, 'score': w.probability}
                         for w in (segment.words or []) if w.start >= start - .01 and w.end <= end + .01]
                result.append({'id': f'c{len(result)+1:06}', 'start': start, 'end': end, 'text': text, 'words': words, 'flags': flags})
    return result, info.language, float(info.language_probability)


def normalized(text):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold()))


def differences(reference, alternate):
    """Conservative candidates only; never splice unverified ASR into supplied text."""
    result = []
    for cue in alternate:
        nearby = [r for r in reference if r['end'] >= cue['start'] - 1 and r['start'] <= cue['end'] + 1]
        proposed = normalized(cue['text'])
        existing = normalized(' '.join(r['text'] for r in nearby))
        if not proposed or proposed in existing:
            continue
        words = proposed.split()
        coverage = sum(w in existing.split() for w in words) / max(1, len(words))
        ratio = difflib.SequenceMatcher(None, proposed, existing).ratio()
        if coverage >= .85 or ratio >= .8:
            continue
        result.append({**cue, 'id': f'r{len(result)+1:06}', 'flags': ['possible_missing_or_different_dialogue', *cue.get('flags', [])],
                       'reference_ids': [r['id'] for r in nearby]})
    return result


def recognize():
    state = read_state()
    conf = state['config']
    if conf['mode'] == 'align':
        state['notes'].append('Timestamp repair: uploaded dialogue is preserved; no speech recognition or new dialogue generation was run.')
        save(state)
        return
    from faster_whisper import WhisperModel
    with quiet_models():
        model = WhisperModel(conf['model'], device='cpu', compute_type='int8',
            cpu_threads=min(8, os.cpu_count() or 2), num_workers=1,
            download_root=os.getenv('WHISPER_CACHE', '.whisper-cache'))
    # Main pass filters nonspeech; alternate pass disables VAD to surface quiet lines.
    first, language, probability = recognize_cues(model, state['duration'], conf['language'], not state['uploaded'])
    state['language'] = language
    state['language_probability'] = probability
    if not state['uploaded']:
        state['cues'] = first
        if conf['recover']:
            print('Checking speech with VAD disabled; candidates require review.', flush=True)
            second, _, _ = recognize_cues(model, state['duration'], language, False)
            state['candidates'] = differences(first, second)
    else:
        state['candidates'] = differences(state['cues'], first)
    del model
    gc.collect()
    if not state['cues']:
        raise base.SetupError('No speech was recognized. Check audio track, language and sample range.')
    save(state)
    print(f'Recognition complete: {len(state["cues"])} cues, {len(state["candidates"])} review candidates.', flush=True)


def apply_alignment(cue, aligned, duration):
    """Only accept measured words; missing timestamps remain explicit review items."""
    item = {**cue, 'flags': list(cue.get('flags', []))}
    words = [w for s in aligned for w in s.get('words', [])]
    measured = [w for w in words if all(isinstance(w.get(k), (int, float)) and math.isfinite(w[k]) for k in ('start', 'end', 'score'))
                and 0 <= w['start'] < w['end'] <= duration]
    coverage = len(measured) / max(1, len(words), len(cue['text'].split()))
    score = sum(w['score'] for w in measured) / max(1, len(measured))
    item['alignment_score'] = round(score, 3)
    item['alignment_coverage'] = round(coverage, 3)
    item['words'] = words
    # Missing / failed phonetic alignment must not masquerade as refined timing.
    if not measured or coverage < .7 or score < .35:
        item['flags'].append('alignment_unreliable_original_timing_kept')
        return item
    item['start'] = min(w['start'] for w in measured)
    item['end'] = max(w['end'] for w in measured)
    if coverage < 1 or score < .6:
        item['flags'].append('alignment_needs_review')
    if abs(item['start'] - cue['start']) > 1.5 or abs(item['end'] - cue['end']) > 1.5:
        item['flags'].append('large_timing_change')
    return item


def align():
    state = read_state()
    if not state['config']['alignment']:
        state['notes'].append('Forced alignment was disabled. Timings are from uploaded subtitles or ASR.')
        save(state)
        return
    with quiet_models():
        from whisperx.alignment import load_align_model, align as align_audio, DEFAULT_ALIGN_MODELS_TORCH, DEFAULT_ALIGN_MODELS_HF
        from whisperx.audio import load_audio
    language = state['language']
    choice = state['config']['alignment_model']
    custom_model = 'WAV2VEC2_ASR_LARGE_LV60K_960H' if choice == 'english-large' else os.getenv('ALIGN_MODEL', '').strip() if choice == 'configured' else None
    if language not in {*DEFAULT_ALIGN_MODELS_TORCH, *DEFAULT_ALIGN_MODELS_HF} and not custom_model:
        raise base.SetupError('No default forced-alignment model for this audio language. Set a tested ALIGN_MODEL repository variable, or disable alignment in original/audio mode.')
    with quiet_models():
        model, metadata = load_align_model(language_code=language, device='cpu', model_name=custom_model)
        audio = load_audio(str(WORK / 'speech.wav'))
    state['alignment_model'] = custom_model or DEFAULT_ALIGN_MODELS_TORCH.get(language) or DEFAULT_ALIGN_MODELS_HF.get(language)
    result = []
    for index, cue in enumerate(state['cues']):
        if cue['start'] >= state['duration'] or cue['end'] > state['duration']:
            result.append(cue)
            continue
        # SRT timings are anchors, with a local search window. Not a global resync.
        window = {**cue, 'start': max(0, cue['start'] - state['config']['alignment_window']), 'end': min(state['duration'], cue['end'] + state['config']['alignment_window'])}
        with quiet_models():
            aligned = align_audio([window], model, metadata, audio, 'cpu', interpolate_method='ignore', return_char_alignments=False)
        result.append(apply_alignment(cue, aligned['segments'], state['duration']))
        if index % 100 == 0:
            print(f'Aligned {index+1}/{len(state["cues"])} cues.', flush=True)
    state['cues'] = preserve_cue_order(result, state['cues'])
    state['alignment_applied'] = True
    del model, audio
    gc.collect()
    save(state)


def preserve_cue_order(aligned, original):
    """Ambiguous/repeated dialogue must not invert the supplied cue order."""
    for _ in range(len(aligned)):
        changed = False
        for index in range(1, len(aligned)):
            if aligned[index]['start'] < aligned[index-1]['start']:
                for position in (index-1, index):
                    cue = aligned[position]
                    old = original[position]
                    if (cue['start'], cue['end']) != (old['start'], old['end']):
                        aligned[position] = {**cue, 'start': old['start'], 'end': old['end'],
                            'flags': [*cue['flags'], 'ambiguous_cue_order_original_timing_kept']}
                        changed = True
        if not changed:
            break
    return aligned


def display_length(text):
    # Combining marks / Sinhala joiners must not count as separate visible letters.
    return sum(not unicodedata.combining(c) and unicodedata.category(c) not in ('Mn', 'Mc', 'Me', 'Cf') for c in text)


def wrap_lines(text, width=42):
    lines = []
    current = ''
    for word in text.split():
        candidate = (current + ' ' + word).strip()
        if current and display_length(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def quality_checks(cues):
    result = []
    previous_end = 0
    for cue in cues:
        flags = list(cue.get('flags', []))
        length = cue['end'] - cue['start']
        if not math.isfinite(length) or length <= 0 or not math.isfinite(cue['start']) or cue['start'] < 0:
            raise base.SetupError('Invalid output timing. No subtitle was published.')
        if cue['start'] < previous_end - .02:
            flags.append('overlapping_cues')
        previous_end = max(previous_end, cue['end'])
        if length < .7:
            flags.append('short_display_time')
        if length > 7:
            flags.append('long_display_time')
        cps = display_length(cue['text']) / length
        if cps > 20:
            flags.append('fast_reading_speed')
        lines = wrap_lines(cue['text'])
        if len(lines) > 2 or any(display_length(line) > 42 for line in lines):
            flags.append('line_length_review')
        result.append({**cue, 'flags': sorted(set(flags)), 'reading_speed': round(cps, 1)})
    return result


def write_track(cues, path, offset):
    with path.with_suffix('.srt').open('w', encoding='utf-8', newline='\n') as srt, \
            path.with_suffix('.vtt').open('w', encoding='utf-8', newline='\n') as vtt, \
            path.with_suffix('.txt').open('w', encoding='utf-8', newline='\n') as txt:
        vtt.write('WEBVTT\n\n')
        for number, cue in enumerate(cues, 1):
            start, end = cue['start'] + offset, cue['end'] + offset
            if round(end * 1000) <= round(start * 1000):
                raise base.SetupError('A cue is shorter than timestamp precision. Review source subtitles.')
            body = cue.get('display_text', '\n'.join(wrap_lines(cue['text'])))
            source_id = cue.get('source_id', '')
            srt_id = source_id if source_id.isdigit() else str(number)
            srt.write(f'{srt_id}\n{base.timestamp(start)} --> {base.timestamp(end)}\n{body}\n\n')
            vtt.write(f'{source_id or cue["id"]}\n{base.timestamp(start, True)} --> {base.timestamp(end, True)}\n{body}\n\n')
            txt.write(cue['text'] + '\n')


def absolute_cue(cue, offset):
    result = {**cue, 'start': round(cue['start'] + offset, 3), 'end': round(cue['end'] + offset, 3)}
    result['words'] = [{**w, **{k: round(w[k]+offset, 3) for k in ('start', 'end') if isinstance(w.get(k), (int, float))}} for w in cue.get('words', [])]
    return result


def export():
    state = read_state()
    conf = state['config']
    source = quality_checks(state['cues'])
    offset = conf['start']
    name = conf['name']
    if state['uploaded']:
        original = state['original_cues']
        if len(source) != len(original) or any(c['id'] != o['id'] or c['text'] != o['text'] or c.get('display_text') != o.get('display_text') for c, o in zip(source, original)):
            raise base.SetupError('Dialogue preservation check failed. No repaired subtitle was published.')
    write_track(source, OUT / (name + '.track'), offset)
    candidates = quality_checks(state['candidates'])
    if candidates and conf['mode'] == 'transcribe':
        write_track(candidates, OUT / (name + '.review-candidates.track'), offset)
    absolute = [absolute_cue(c, offset) for c in source]
    report = {'method': conf['mode'], 'review_required': True, 'accuracy_guaranteed': False,
        'dialogue_preserved': state['uploaded'], 'source_language': state['language'],
        'recognition_model': conf['model'] if conf['mode'] == 'transcribe' else None,
        'alignment_applied': state['alignment_applied'], 'alignment_model': state.get('alignment_model'),
        'audio_track': state['audio_track'], 'sample_only': conf['sample'], 'timestamp_offset_seconds': offset,
        'subtitle_shift_seconds': conf['subtitle_shift'], 'alignment_window_seconds': conf['alignment_window'],
        'audio_duration_seconds': state['duration'], 'source_cues': len(source),
        'flagged_source_cues': sum(bool(c['flags']) for c in source),
        'possible_missing_dialogue': [absolute_cue(c, offset) for c in candidates], 'notes': state['notes'], 'cues': absolute}
    (OUT / 'review-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    body = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Subtitle timing review</title>',
        '<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;background:#121515;color:#eee}table{border-collapse:collapse;width:100%}td,th{padding:12px;border:1px solid #48504d;text-align:left}small{color:#e6c66b}pre{white-space:pre-wrap}</style>',
        f'<h1>Subtitle timing review</h1><p>Audio track {state["audio_track"]} · {len(source)} cues. Method: {conf["mode"]}.</p>',
        '<p>Uploaded dialogue and line breaks are preserved in timestamp repair. Low-confidence matches retain original timestamps. Flags require review; they are not an accuracy certificate.</p>',
        '<table><tr><th>Time / ID</th><th>Dialogue</th><th>Review</th></tr>']
    for cue in absolute:
        body.append(f'<tr><td>{base.timestamp(cue["start"])}<br>{html.escape(cue["id"])}</td><td><pre>{html.escape(cue.get("display_text", cue["text"]))}</pre></td><td><small>{html.escape(", ".join(cue["flags"]))}</small></td></tr>')
    if candidates:
        body.append('</table><h2>Recognition review candidates (new-subtitle method only)</h2>')
        for cue in candidates:
            body.append(f'<p>{base.timestamp(cue["start"]+offset)} — {html.escape(cue["text"])}</p>')
    else:
        body.append('</table>')
    body.append('</html>')
    (OUT / 'review.html').write_text('\n'.join(body), encoding='utf-8')
    if os.getenv('GITHUB_STEP_SUMMARY'):
        Path(os.environ['GITHUB_STEP_SUMMARY']).write_text(f'## Subtitle timestamps ready for review\n\nAudio track {state["audio_track"]} · {len(source)} cues · method: {conf["mode"]}.\n\nDownload the subtitle artifact and open review.html. Review flagged timings before publishing.\n', encoding='utf-8')
    print(f'Exported {len(source)} cues. Review report included.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['prepare', 'recognize', 'align', 'export'])
    args = parser.parse_args()
    try:
        globals()[args.stage]()
        return 0
    except (base.SetupError, DownloadError) as error:
        print('::error::' + str(error), flush=True)
    except Exception:
        print(f'::error::Subtitle stage {args.stage} failed. Check model availability, media, memory and disk space. Raw provider/model output was suppressed.', flush=True)
    return 1


if __name__ == '__main__':
    sys.exit(main())
