import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import clixarena_subtitles as sub
import clixarena_media_download as media

class SubtitleTests(unittest.TestCase):
    def options(self,**kwargs):
        return sub.settings({'media_url':'https://media.example/source.mkv','output_name':'Movie 2026',**kwargs})

    def test_original_language_defaults_and_no_sample(self):
        conf=self.options()
        self.assertEqual(conf['language'],'auto');self.assertEqual(conf['model'],'medium')
        self.assertFalse(conf['sample']);self.assertEqual(conf['name'],'Movie 2026')
        self.assertTrue(self.options(sample_only='true')['sample'])

    def test_filename_and_settings_cannot_inject_paths_or_commands(self):
        for name in ['../outside','../outside.srt','a\\b','bad\nname','CON','NUL.srt','a|b']:
            with self.subTest(name=name), self.assertRaises(sub.SetupError):self.options(output_name=name)
        for start in ['nan','inf','-1','$(whoami)']:
            with self.assertRaises(sub.SetupError):self.options(start_seconds=start)
        with self.assertRaises(sub.SetupError):self.options(model='../../model')

    def test_srt_suffix_and_unicode_name(self):
        self.assertEqual(self.options(output_name='සිංහල.SRT')['name'],'සිංහල')

    def test_secret_url_when_dispatch_field_is_blank(self):
        with patch.dict(os.environ,{'SUBTITLE_MEDIA_URL':'https://secret.example/audio'}):
            self.assertEqual(sub.settings({'media_url':''})['url'],'https://secret.example/audio')

    def test_multi_track_selection_never_silently_picks_first(self):
        streams=[{'tags':{'language':'hin'}},{'tags':{'language':'tam'}}]
        with self.assertRaises(sub.SetupError):sub.select_audio(streams,'auto')
        self.assertEqual(sub.select_audio(streams,'tamil')[0],1)
        self.assertEqual(sub.select_audio(streams,'ta')[0],1)
        self.assertEqual(sub.select_audio(streams,'0')[0],0)
        with self.assertRaises(sub.SetupError):sub.select_audio(streams,'eng')
        with self.assertRaises(sub.SetupError):sub.select_audio(streams,'2')
        with self.assertRaises(sub.SetupError):sub.select_audio([streams[1],streams[1]],'tam')
        self.assertEqual(sub.select_audio([streams[0]],'auto')[0],0)

    def test_formatting_milliseconds_and_long_times(self):
        self.assertEqual(sub.timestamp(3599.9996),'01:00:00,000')
        self.assertEqual(sub.timestamp(1.234,True),'00:00:01.234')

    def test_unicode_cues_offsets_and_nonoverlapping_timing(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root=Path(folder)
            segments=[NS(start=0,end=2,text='ignored',words=[NS(start=0.2,end=1,word='හෙලෝ'),NS(start=1,end=1.8,word='ලෝකය.')]),
                      NS(start=1.7,end=3,text='தமிழ் உரையாடல்',words=None)]
            count=sub.write_subtitles(segments,root/'x.srt',root/'x.vtt',root/'x.txt',3,60)
            srt=(root/'x.srt').read_text(encoding='utf-8')
            self.assertEqual(count,2);self.assertIn('00:01:00,200 --> 00:01:01,800',srt)
            self.assertIn('00:01:01,800 --> 00:01:03,000',srt)
            self.assertIn('හෙලෝ ලෝකය.',srt);self.assertIn('தமிழ் உரையாடல்',srt)
            self.assertTrue((root/'x.vtt').read_text().startswith('WEBVTT'))

    def test_word_timestamps_split_long_cues_and_preserve_words(self):
        words=[NS(start=i*.5,end=(i+1)*.5,word='Dialogue') for i in range(24)]
        cues=sub.segment_cues(NS(words=words),12)
        self.assertGreater(len(cues),1)
        self.assertEqual(sum(len(t.split()) for a,b,t in cues),24)
        self.assertTrue(all(b-a<=6 for a,b,t in cues))
        self.assertTrue(all(len(sub.textwrap.wrap(t,width=42,break_long_words=False))<=2 for a,b,t in cues))

    def test_empty_speech_is_a_useful_failure(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root=Path(folder)
            with self.assertRaises(sub.SetupError):sub.write_subtitles([],root/'x.srt',root/'x.vtt',root/'x.txt',3)

    def test_probe_tool_errors_do_not_echo_remote_metadata_or_stderr(self):
        with patch.object(sub.subprocess,'run',return_value=NS(returncode=1,stderr='secret-url-token',stdout='')):
            with self.assertRaises(sub.SetupError) as error:sub.audio_streams(Path('file'))
            self.assertNotIn('secret-url-token',str(error.exception))

    def test_private_download_addresses_are_blocked(self):
        with patch.object(media.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError):media.validate_url('https://private.example/file')
        for url in ['http://example.com/file','https://u:p@example.com/file','https://example.com:22/file','https://example.com/file#frag']:
            with self.assertRaises(ValueError):media.validate_url(url)

    def test_drive_and_dropbox_share_links(self):
        url,identity=media.resolve_video_source('https://drive.google.com/file/d/abcdefg/view')
        self.assertEqual(identity,'abcdefg');self.assertIn('export=download',url)
        url,identity=media.resolve_video_source('https://www.dropbox.com/s/test/file.mp3?dl=0')
        self.assertIn('dl=1',url);self.assertIsNone(identity)

    def test_short_download_is_rejected(self):
        class Response(io.BytesIO):
            headers={'Content-Type':'video/mp4','Content-Length':'20'}
        with tempfile.TemporaryDirectory(dir='.') as folder:
            with patch.object(media,'validate_url'),patch.object(media,'open_video_response',return_value=Response(b'only-five')),patch.object(media.shutil,'disk_usage',return_value=NS(free=10*1024**3)):
                with self.assertRaises(media.RetryableDownloadError):media._download_once('https://host/source',Path(folder)/'out',100)

    def test_actual_inference_contract_keeps_original_language(self):
        calls=[]
        class Model:
            def __init__(self,*args,**kwargs):calls.append(('model',args,kwargs))
        class Batch:
            def __init__(self,model):pass
            def transcribe(self,*args,**kwargs):calls.append(('transcribe',args,kwargs));return [],NS(language='ta')
        with patch.dict(sys.modules,{'faster_whisper':NS(WhisperModel=Model,BatchedInferencePipeline=Batch)}):
            sub.recognize(Path('speech.wav'),'medium','ta',Path('.cache'))
        self.assertEqual(calls[1][2]['task'],'transcribe')
        self.assertEqual(calls[1][2]['language'],'ta')
        self.assertTrue(calls[1][2]['word_timestamps'])
        self.assertEqual(calls[0][2]['compute_type'],'int8')

    def test_manual_job_writes_artifacts_and_cleans_source_without_url_logs(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root=Path(folder).resolve();event=root/'event.json';summary=root/'summary.md'
            event.write_text(json.dumps({'inputs':{'media_url':'https://example.com/file?token=private-canary','output_name':'My Movie','audio_track':'tam','download_connections':'6'}}))
            def fetch(url,target,limit,**kwargs):
                self.assertEqual(kwargs['connections'],6)
                target.write_bytes(b'fake-media')
            def audio(source,target,index,start,sample):target.write_bytes(b'fake-audio');return 5
            info=NS(language='ta',language_probability=.99)
            def speech(*args):return iter([NS(start=0,end=2,text='தமிழ் உரையாடல்',words=None)]),info
            previous=Path.cwd();logs=io.StringIO()
            try:
                os.chdir(root)
                with patch.dict(os.environ,{'GITHUB_EVENT_PATH':str(event),'GITHUB_STEP_SUMMARY':str(summary),'GITHUB_ACTIONS':'false'}),patch.object(sub,'download',fetch),patch.object(sub,'audio_streams',return_value=[{'tags':{'language':'tam'},'channels':2}]),patch.object(sub,'extract_audio',audio),patch.object(sub,'recognize',speech),patch.object(sub.shutil,'disk_usage',return_value=NS(free=20*1024**3)),patch('sys.stdout',logs):
                    self.assertEqual(sub.main(),0)
                out=root/'generated-subtitles'
                self.assertIn('தமிழ் உரையாடல்',(out/'My Movie.srt').read_text(encoding='utf-8'))
                metadata=json.loads((out/'transcription-info.json').read_text())
                self.assertEqual(metadata['task'],'transcribe');self.assertEqual(metadata['audio_track'],0)
                self.assertNotIn('private-canary',logs.getvalue());self.assertNotIn('private-canary',str(metadata))
                self.assertFalse((root/'.subtitle-work').exists())
                self.assertEqual(sorted(p.suffix for p in out.iterdir()),['.json','.srt','.txt','.vtt'])
            finally:os.chdir(previous)

if __name__=='__main__':unittest.main()

class AudioSelectionLogTests(unittest.TestCase):
 def test_missing_language_logs_numbered_tracks_without_exposing_metadata(self):
  import io
  output=io.StringIO()
  with patch('sys.stdout',output):
   with self.assertRaises(sub.SetupError):sub.select_audio([{'channels':2,'tags':{'title':'https://private.example/media'}},{'channels':6,'tags':{'language':'jpn'}}],'eng')
  log=output.getvalue();self.assertIn('Audio track 0: language=und, channels=2',log);self.assertIn('Audio track 1: language=jpn, channels=6',log);self.assertNotIn('private.example',log)
 def test_untagged_audio_accepts_explicit_number_without_language_guess(self):
  streams=[{'channels':2}]
  self.assertEqual(sub.select_audio(streams,'0')[0],0)
  self.assertEqual(sub.select_audio(streams,'auto')[0],0)
  with self.assertRaises(sub.SetupError):sub.select_audio(streams,'eng')
