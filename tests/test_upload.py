import base64, gzip, hashlib, io, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import subtitle_upload as upload
import subtitle_word_match as matching
from clixarena_subtitles import SetupError

ID='aabbccdd-1122-4111-8111-112233445566'
class UploadTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.previous=Path.cwd();os.chdir(self.temp.name)
  self.text='1\n00:00:01,000 --> 00:00:02,000\n'+base64.b64encode(os.urandom(80000)).decode()+'\n'
  raw=gzip.compress(self.text.encode());self.assertGreater(len(raw),45000)
  self.packed=base64.b64encode(raw).decode();self.parts=[self.packed[i:i+48000] for i in range(0,len(self.packed),48000)]
  self.manifest={'request_id':ID,'total':len(self.parts),'sha256':hashlib.sha256(raw).hexdigest()}
  Path('event.json').write_text(json.dumps({'inputs':{'request_id':ID}}),encoding='utf-8')
  self.env=patch.dict(os.environ,{'GITHUB_EVENT_PATH':str(Path('event.json').resolve()),'GITHUB_OUTPUT':str(Path('output').resolve())});self.env.start()
 def tearDown(self):self.env.stop();os.chdir(self.previous);self.temp.cleanup()
 def listing(self,path):
  index=int(path.split('&')[0].rsplit('-',1)[1]);name='subtitle-input-'+ID+'-'+str(index)
  return io.StringIO(json.dumps({'artifacts':[{'id':index+1,'name':name,'expired':False}]}))
 def part(self,artifact_id):return {'request_id':ID,'index':artifact_id-1,'total':len(self.parts),'data':self.parts[artifact_id-1]}
 def test_save_parts_and_reconstruct_large_utf8_file(self):
  for index,data in enumerate(self.parts):
   upload.save({'request_id':ID,'subtitle_tracks':json.dumps({'index':index,'total':len(self.parts),'data':data})})
   self.assertEqual(json.loads(Path('subtitle-input.json').read_text())['data'],data)
  self.assertEqual(Path('output').read_text(),''.join('index='+str(i)+'\n' for i in range(len(self.parts))))
  with patch.object(upload,'github',side_effect=self.listing),patch.object(upload,'read_part',side_effect=self.part):
   self.assertEqual(upload.receive(self.manifest),self.packed)
   self.assertEqual(matching.subtitle_text(json.dumps({'subtitle_upload':self.manifest}),Path('.')),self.text)
 def test_hash_mismatch_rejected(self):
  manifest={**self.manifest,'sha256':'0'*64}
  with patch.object(upload,'github',side_effect=self.listing),patch.object(upload,'read_part',side_effect=self.part):
   with self.assertRaisesRegex(SetupError,'integrity'):upload.receive(manifest)
 def test_other_requests_part_rejected(self):
  with patch.object(upload,'github',side_effect=self.listing),patch.object(upload,'read_part',return_value={**self.part(1),'request_id':'other'}):
   with self.assertRaisesRegex(SetupError,'another request'):upload.receive(self.manifest)
 def test_missing_parts_timeout(self):
  with self.assertRaisesRegex(SetupError,'15 minutes'):upload.receive(self.manifest,timeout=0)
 def test_manifest_must_match_processing_event(self):
  Path('event.json').write_text(json.dumps({'inputs':{'request_id':'other'}}))
  with self.assertRaisesRegex(SetupError,'manifest'):upload.receive(self.manifest)
 def test_invalid_parts_and_manifests_rejected(self):
  for part in [{'index':True,'total':1,'data':'AAAA'},{'index':0,'total':16,'data':'AAAA'},{'index':0,'total':1,'data':'A'*48001},{'index':0,'total':2,'data':'AAAA'}]:
   with self.assertRaises(SetupError):upload.save({'request_id':ID,'subtitle_tracks':json.dumps(part)})
  for value in [{},None,{'request_id':ID,'total':16,'sha256':'0'*64}]:
   with self.assertRaises(SetupError):upload.receive(value)
 def test_network_failure_is_bounded(self):
  with patch.object(upload,'github',side_effect=OSError('private internal details')),patch.object(upload.time,'sleep'):
   with self.assertRaisesRegex(SetupError,'Could not retrieve'):upload.receive(self.manifest)

if __name__=='__main__':unittest.main()

class ArtifactDownloadTests(unittest.TestCase):
 def test_signed_artifact_download_never_forwards_token(self):
  import urllib.error,zipfile
  from email.message import Message
  payload={'request_id':ID,'index':0,'total':1,'data':'AAAA'}
  raw=io.BytesIO()
  with zipfile.ZipFile(raw,'w',compression=zipfile.ZIP_STORED) as archive:archive.writestr('subtitle-input.json',json.dumps(payload))
  headers=Message();headers['Location']='https://test.blob.core.windows.net/input?sig=private'
  redirect=urllib.error.HTTPError('https://api.github.com/artifact',302,'redirect',headers,None)
  class Response(io.BytesIO):pass
  opener=unittest.mock.Mock();opener.open.return_value=Response(raw.getvalue())
  with patch.object(upload,'github',side_effect=redirect),patch.object(upload.urllib.request,'build_opener',return_value=opener):
   self.assertEqual(upload.read_part(1),payload)
  self.assertEqual(opener.open.call_args.args,('https://test.blob.core.windows.net/input?sig=private',))
  self.assertEqual(opener.open.call_args.kwargs,{'timeout':30})
 def test_untrusted_artifact_host_is_rejected(self):
  import urllib.error
  from email.message import Message
  headers=Message();headers['Location']='https://evil.example/input'
  with patch.object(upload,'github',side_effect=urllib.error.HTTPError('https://api.github.com/artifact',302,'redirect',headers,None)):
   with self.assertRaisesRegex(SetupError,'Unexpected'):upload.read_part(1)
