import base64,contextlib,hashlib,io,json,os,shutil,sys,unittest,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import subtitle_source as source
import clixarena_torrent as torrent
import clixarena_media_download as media
import subtitle_selection as selection
from clixarena_subtitles import SetupError,settings


class SourceTests(unittest.TestCase):
 def setUp(self):
  self.previous=Path.cwd();self.root=Path(__file__).resolve().parent/('source-'+uuid.uuid4().hex);self.root.mkdir();os.chdir(self.root)
  self.inputs={'source_kind':'torrent','request_id':'aabbccdd-1122-4111-8111-112233445566','torrent_source':json.dumps({'data':base64.b64encode(b'de').decode()})}
  Path('event.json').write_text(json.dumps({'inputs':self.inputs}))
  self.env=patch.dict(os.environ,{'GITHUB_EVENT_PATH':str(self.root/'event.json'),'GITHUB_RUN_ID':'55','GITHUB_RUN_ATTEMPT':'1','GITHUB_OUTPUT':str(self.root/'output')});self.env.start()
 def tearDown(self):self.env.stop();os.chdir(self.previous);shutil.rmtree(self.root)
 def test_multiple_files_publish_choices_without_payload(self):
  def download(*args):torrent.select_file([{'index':0,'name':'one.mkv','bytes':100},{'index':2,'name':'two.mp4','bytes':200}],None);self.fail('Payload should not start')
  with patch.object(torrent,'download_torrent',side_effect=download):self.assertTrue(source.prepare(self.inputs))
  data=json.loads(Path('torrent-files.json').read_text());self.assertEqual(data['run_id'],'55');self.assertEqual([f['index'] for f in data['files']],[0,2]);self.assertFalse(Path('.subtitle-work/source.media').exists())
 def test_single_file_downloaded_once_and_reused_by_media_pipeline(self):
  def download(*args):
   self.assertEqual(torrent.select_file([{'index':2,'name':'one.mkv','bytes':3}],None),2)
   Path('.subtitle-work/source.media').write_bytes(b'abc')
  with patch.object(torrent,'download_torrent',side_effect=download) as call:
   self.assertFalse(source.prepare(self.inputs));self.assertTrue(source.ready(self.inputs))
   with patch.object(media,'validate_url',side_effect=AssertionError('Must not redownload')):
    media.download('placeholder',Path('.subtitle-work/source.media'),100,source=self.inputs)
   self.assertEqual(call.call_count,1)
  with self.assertRaises(SetupError):source.use_prepared(dict(self.inputs,request_id='other'),Path('.subtitle-work/source.media'),100)
 def test_wait_only_accepts_matching_run_and_attempt_then_downloads(self):
  Path('.subtitle-work').mkdir();Path('.subtitle-work/source.torrent').write_bytes(b'de')
  listing={'artifacts':[{'id':8,'name':'torrent-selection-'+self.inputs['request_id'],'expired':False}]}
  chosen={'request_id':self.inputs['request_id'],'run_id':'55','run_attempt':'1','selected':2}
  with patch.object(selection,'github',return_value=io.StringIO(json.dumps(listing))),patch.object(source,'read_selection',return_value=chosen),patch.object(source,'run_download',return_value=False) as run:
   source.wait(self.inputs);self.assertEqual(run.call_args.args[-1],2)
  wrong=dict(chosen,run_id='999')
  ticks=iter([0,0,2])
  with patch.object(selection,'github',return_value=io.StringIO(json.dumps(listing))),patch.object(source,'read_selection',return_value=wrong),patch.object(source,'run_download') as run,patch.object(source.time,'sleep'),patch.object(source.time,'monotonic',side_effect=lambda:next(ticks)):
   with self.assertRaises(SetupError):source.wait(self.inputs,timeout=1)
   run.assert_not_called()
 def test_selection_artifact_is_bound_and_has_no_source(self):
  inputs=dict(self.inputs,torrent_source=json.dumps({'selected':2,'run_id':'55','run_attempt':'1','secret':'hidden'}))
  source.save_selection(inputs);data=json.loads(Path('torrent-selection.json').read_text())
  self.assertEqual(set(data),{'request_id','selected','run_id','run_attempt'})
  for index in [-1,True,'2',10000]:
   with self.assertRaises(SetupError):source.save_selection(dict(inputs,torrent_source=json.dumps({'selected':index,'run_id':'55','run_attempt':'1'})))
 def test_source_descriptor_rejects_invalid_magnet_and_oversized_torrent(self):
  Path('.subtitle-work').mkdir()
  for value in ['magnet:?xt=urn:btmh:bad','https://example.com/file']:
   with self.assertRaises(SetupError):source.descriptor(dict(self.inputs,source_kind='magnet',torrent_source=json.dumps({'magnet':value})))
  with self.assertRaises(SetupError):source.descriptor(dict(self.inputs,torrent_source=json.dumps({'data':base64.b64encode(b'x'*(1024*1024+1)).decode()})))
  self.assertEqual(settings(self.inputs)['url'],'https://torrent.invalid/source')
 def test_selected_file_must_exist_in_the_same_torrent(self):
  Path('.subtitle-work').mkdir()
  def download(*args):torrent.select_file([{'index':1,'name':'one.mkv','bytes':3}],None)
  with patch.object(torrent,'download_torrent',side_effect=download),self.assertRaises(SetupError):source.run_download(self.inputs,{},selected=2)

class TorrentTransportTests(unittest.TestCase):
 def test_torrent_parts_use_separate_namespace_and_larger_bound(self):
  import subtitle_upload as transport
  from urllib.parse import parse_qs,urlsplit
  request='aabbccdd-1122-4111-8111-112233445566';raw=b'x'*600000;encoded=base64.b64encode(raw).decode();parts=[encoded[i:i+48000] for i in range(0,len(encoded),48000)]
  manifest={'request_id':request,'total':len(parts),'sha256':hashlib.sha256(raw).hexdigest()}
  previous=Path.cwd();root=Path(__file__).resolve().parent/('transport-'+uuid.uuid4().hex);root.mkdir()
  try:
   os.chdir(root);Path('event.json').write_text(json.dumps({'inputs':{'request_id':request}}));names=[]
   def github(path):
    name=parse_qs(urlsplit(path).query)['name'][0];names.append(name);index=int(name.rsplit('-',1)[1])
    return io.StringIO(json.dumps({'artifacts':[{'id':index+1,'name':name,'expired':False}]}))
   def part(artifact,kind):
    self.assertEqual(kind,'torrent');index=artifact-1
    return {'request_id':request,'index':index,'total':len(parts),'data':parts[index]}
   with patch.dict(os.environ,{'GITHUB_EVENT_PATH':str(root/'event.json')}),patch.object(transport,'github',side_effect=github),patch.object(transport,'read_part',side_effect=part):
    self.assertEqual(transport.receive(manifest,kind='torrent'),encoded)
   self.assertTrue(all(name.startswith('torrent-input-') for name in names))
  finally:os.chdir(previous);shutil.rmtree(root)
