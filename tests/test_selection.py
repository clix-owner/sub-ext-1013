import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import subtitle_selection as selection
from clixarena_subtitles import SetupError

class SelectionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.previous=Path.cwd();os.chdir(self.temp.name)
  self.inputs={'media_url':'https://example.com/video','output_name':'Test','request_id':'aabbccdd-1122-4111-8111-112233445566','subtitle_tracks':'ask'}
 def tearDown(self):os.chdir(self.previous);self.temp.cleanup()
 def test_single_extracts_immediately(self):
  with patch.object(selection,'download') as download,patch.object(selection,'track_list',return_value=[{'number':0}]),patch.object(selection,'extract_tracks') as extract:
   self.assertFalse(selection.prepare(self.inputs));extract.assert_called_once();self.assertEqual(extract.call_args.args[-1],'0');download.assert_called_once();self.assertFalse(Path('subtitle-tracks.json').exists())
 def test_multiple_waits_without_extracting(self):
  with patch.object(selection,'download'),patch.object(selection,'track_list',return_value=[{'number':0},{'number':1}]),patch.object(selection,'extract_tracks') as extract:
   self.assertTrue(selection.prepare(self.inputs));extract.assert_not_called();self.assertEqual(len(json.loads(Path('subtitle-tracks.json').read_text())['tracks']),2)
 def test_no_tracks_errors(self):
  with patch.object(selection,'download'),patch.object(selection,'track_list',return_value=[]):
   with self.assertRaisesRegex(SetupError,'no embedded'):selection.prepare(self.inputs)
 def test_wait_extracts_only_selected_without_redownload(self):
  response={'artifacts':[{'id':42,'name':'subtitle-selection-'+self.inputs['request_id'],'expired':False}]}
  with patch.object(selection,'github',return_value=io.StringIO(json.dumps(response))),patch.object(selection,'selection_file',return_value={'request_id':self.inputs['request_id'],'subtitle_tracks':'1'}),patch.object(selection,'track_list',return_value=[{},{}]),patch.object(selection,'extract_tracks') as extract,patch.object(selection,'download') as download:
   selection.wait(self.inputs);self.assertEqual(extract.call_args.args[-1],'1');download.assert_not_called()
 def test_rejects_other_request_selection(self):
  response={'artifacts':[{'id':42,'name':'subtitle-selection-'+self.inputs['request_id'],'expired':False}]}
  with patch.object(selection,'github',return_value=io.StringIO(json.dumps(response))),patch.object(selection,'selection_file',return_value={'request_id':'different','subtitle_tracks':'1'}):
   with self.assertRaisesRegex(SetupError,'does not match'):selection.wait(self.inputs)
 def test_timeout(self):
  with self.assertRaisesRegex(SetupError,'30 minutes'):selection.wait(self.inputs,timeout=0)
 def test_selection_artifact_contains_only_request_and_tracks(self):
  inputs={**self.inputs,'subtitle_tracks':'1'};selection.save_selection(inputs)
  self.assertEqual(json.loads(Path('selection.json').read_text()),{'request_id':inputs['request_id'],'subtitle_tracks':'1'})
  for value in ['all','ask','','0;bad','-1']:
   with self.assertRaises(SetupError):selection.save_selection({**inputs,'subtitle_tracks':value})
 def test_real_media_single_auto_and_multiple_selection(self):
  import shutil
  from clixarena_subtitles import tool
  first=Path('first.srt');second=Path('second.srt')
  first.write_text('1\n00:00:01,000 --> 00:00:02,000\nFirst track\n\n',encoding='utf-8')
  second.write_text('1\n00:00:03,000 --> 00:00:04,000\nSecond track\n\n',encoding='utf-8')
  tool(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=duration=5','-i',str(first),'-i',str(second),'-map','0:a','-map','1:s','-map','2:s','-c:a','pcm_s16le','-c:s','copy','-metadata:s:s:0','language=eng','-metadata:s:s:1','language=sin','multi.mkv'],30)
  with patch.object(selection,'download',side_effect=lambda url,dest,limit,**kwargs:shutil.copyfile('multi.mkv',dest)):
   self.assertTrue(selection.prepare(self.inputs))
  self.assertFalse(Path('generated-subtitles').exists())
  listing=json.loads(Path('subtitle-tracks.json').read_text());self.assertEqual([t['language'] for t in listing['tracks']],['eng','sin'])
  response={'artifacts':[{'id':42,'name':'subtitle-selection-'+self.inputs['request_id'],'expired':False}]}
  with patch.object(selection,'github',return_value=io.StringIO(json.dumps(response))),patch.object(selection,'selection_file',return_value={'request_id':self.inputs['request_id'],'subtitle_tracks':'1'}):
   selection.wait(self.inputs)
  outputs=list(Path('generated-subtitles').glob('*.srt'));self.assertEqual(len(outputs),1)
  self.assertIn('Second track',outputs[0].read_text());self.assertNotIn('First track',outputs[0].read_text())
  shutil.rmtree('generated-subtitles');Path('subtitle-tracks.json').unlink()
  tool(['ffmpeg','-v','error','-y','-i','multi.mkv','-map','0:a','-map','0:s:0','-c','copy','single.mkv'],30)
  with patch.object(selection,'download',side_effect=lambda url,dest,limit,**kwargs:shutil.copyfile('single.mkv',dest)):
   self.assertFalse(selection.prepare(self.inputs))
  self.assertFalse(Path('subtitle-tracks.json').exists())
  self.assertEqual(len(list(Path('generated-subtitles').glob('*.srt'))),1)
if __name__=='__main__':unittest.main()
