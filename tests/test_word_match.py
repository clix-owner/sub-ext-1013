import base64
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import subtitle_word_match as m
from clixarena_subtitles import SetupError

def words(text,start=1,step=.4,prob=.95):
 return [{'word':token,'start':start+i*step,'end':start+(i+1)*step-.05,'probability':prob} for i,token in enumerate(text.split())]

def cue(text,start,end):return {'source_id':'1','start':start,'end':end,'text':text}

class WordMatchTests(unittest.TestCase):
 def test_parse_preserves_tags_labels_line_breaks(self):
  original='1\n00:00:01,600 --> 00:00:04,840\nDRAGON: <i>In a land of myth</i>\n<i>and a time of magic,</i>\n\n'
  result=m.parse_subtitles(original)
  self.assertEqual(result[0]['text'],'DRAGON: <i>In a land of myth</i>\n<i>and a time of magic,</i>')
  self.assertNotIn('DRAGON',m.spoken_text(result[0]['text']));self.assertEqual(len(result),1)
 def test_bad_blocks_not_silently_removed(self):
  for text in ['1\ninvalid\nhello','1\n00:00:02,000 --> 00:00:01,000\nhello']:
   with self.assertRaises(SetupError):m.parse_subtitles(text)
 def test_exact_audio_words_retime_without_rewriting(self):
  original=cue('<i>Beautiful morning</i>\nfor a journey.',10,13)
  result=m.match_cues([original],words('Beautiful morning for a journey.',1),20)
  self.assertEqual(result[0]['status'],'word_matched');self.assertEqual(result[0]['start'],1)
  self.assertEqual(result[0]['text'],original['text']);self.assertEqual(original['start'],10)
 def test_audio_extras_never_inserted(self):
  result=m.match_cues([cue('Hello world today',10,13)],words('Invented opening Hello world today extra words'),20)
  self.assertEqual(len(result),1);self.assertEqual(result[0]['text'],'Hello world today')
 def test_silence_and_invalid_words_rejected(self):
  for bad in [[],[{'word':'hello','start':float('nan'),'end':5}],[{'word':'hello','start':1,'end':2,'probability':.01}]]:
   with self.assertRaises(SetupError):m.match_cues([cue('hello',1,2)],bad,20)
 def test_non_dialogue_preserved(self):
  cues=[cue('[MUSIC]',0,1),cue('Hello world today',3,5)]
  result=m.match_cues(cues,words('Hello world today',3),20)
  self.assertEqual(result[0]['status'],'unchanged_non_dialogue');self.assertEqual(result[0]['text'],'[MUSIC]')
 def test_song_markers_not_forced_into_dialogue(self):
  self.assertEqual(m.spoken_text('♪ Sing this song ♪'),'')
 def test_low_confidence_not_called_word_match(self):
  result=m.match_cues([cue('hello world today',10,13)],words('hello world today',1,prob=.2),20)
  self.assertNotEqual(result[0]['status'],'word_matched');self.assertEqual(result[0]['start'],10)
 def test_missing_boundary_keeps_original(self):
  result=m.match_cues([cue('hello world this morning',10,13)],words('world this morning',1),20)
  self.assertEqual(result[0]['start'],10)
 def test_local_anchor_inference_is_separate(self):
  cues=[cue('bright morning sunshine',10,11.15),cue('[DOOR CLOSES]',12,12.5),cue('beautiful evening moonlight',13,14.15)]
  audio=words('bright morning sunshine',1)+words('beautiful evening moonlight',4)
  result=m.match_cues(cues,audio,20)
  self.assertEqual(result[1]['status'],'anchor_inferred');self.assertAlmostEqual(result[1]['start'],3,places=2)
 def test_far_apart_phrase_does_not_stretch_a_cue(self):
  audio=words('hello world',1)+words('this morning',50)
  result=m.match_cues([cue('hello world this morning',1,4)],audio,60)
  self.assertNotEqual(result[0]['status'],'word_matched')
 def test_export_text_identity_and_report(self):
  with tempfile.TemporaryDirectory() as temp:
   cues=[cue('A <i>beautiful</i>\nnew morning.',10,13)]
   matched=m.match_cues(cues,words('A beautiful new morning.',1),20)
   report=m.export(matched,Path(temp),'Test',{'model':'fixture'})
   after=m.parse_subtitles((Path(temp)/'Test.srt').read_text(encoding='utf-8'))
   before=m.parse_subtitles((Path(temp)/'Test.original.srt').read_text(encoding='utf-8'))
   self.assertEqual([c['text'] for c in after],[c['text'] for c in before]);self.assertTrue(report['dialogue_preserved'])
   self.assertNotIn('<i>beautiful</i>',(Path(temp)/'word-match-review.html').read_text())
 def test_subtitle_inline_validation(self):
  text='1\n00:00:01,000 --> 00:00:02,000\nHello\n'
  payload=json.dumps({'subtitle_data':base64.b64encode(gzip.compress(text.encode())).decode(),'subtitle_url':''})
  with tempfile.TemporaryDirectory() as temp:self.assertEqual(m.subtitle_text(payload,Path(temp)),text)
  for data in [{},{'subtitle_data':'bad','subtitle_url':''},{'subtitle_data':'abc','subtitle_url':'https://example.com/a'}]:
   with self.assertRaises(SetupError):m.subtitle_text(json.dumps(data),Path('.'))
class TransformTests(unittest.TestCase):
 def test_recovers_speed_and_offset_from_speech_anchors(self):
  points=[]
  for i in range(20):
   original=10+i*20;new=original*1.0427+2
   points.append({'original_start':original,'original_end':original+2,'start':new,'end':new+2*1.0427})
  transform=m.anchor_transform(points)
  self.assertAlmostEqual(transform['scale'],1.0427,places=4);self.assertAlmostEqual(transform['offset'],2,places=3)
 def test_does_not_invent_transform_from_too_few_anchors(self):
  self.assertIsNone(m.anchor_transform([{'original_start':1,'original_end':2,'start':10,'end':11}]))
 def test_transform_retimes_non_dialogue_without_new_overlap_cascade(self):
  cues=[];audio=[]
  for i in range(15):
   start=10+i*20;cue_text=f'Unique phrase number {i} together'
   cues.append(cue(cue_text,start,start+1.95));audio.extend(words(cue_text,start*1.04+2))
  cues.insert(3,cue('[MUSIC]',55,58))
  result=m.match_cues(cues,audio,400)
  self.assertGreater(sum(c['status']=='word_matched' for c in result),10)
  self.assertEqual(result[3]['status'],'speech_anchor_transform');self.assertTrue(all(c['text']==source['text'] for c,source in zip(result,cues)))
class SampleTests(unittest.TestCase):
 def test_sample_does_not_keep_a_cue_known_to_belong_after_sample(self):
  c={'start':95,'end':98,'original_start':95,'original_end':98,'status':'unchanged_unmatched','anchor_transform':{'scale':1.1,'offset':0}}
  with self.assertRaises(SetupError):m.sample_cues([c],0,100)
 def test_webvtt_short_timestamp_supported(self):
  c=m.parse_subtitles('WEBVTT\n\n00:01.000 --> 00:02.000\nHello\n')
  self.assertEqual(c[0]['start'],1)
 def test_review_embedded_json_preserves_tags_safely(self):
  import re,json
  with tempfile.TemporaryDirectory() as temp:
   original=cue('<i>Hello</i> world </script>',1,3)
   c=m.match_cues([original],words('Hello world script',1),10)
   m.export(c,temp,'Test',{'model':'fixture'})
   doc=(Path(temp)/'word-match-review.html').read_text()
   data=re.search(r'const cues=(.*);const player',doc).group(1)
   self.assertEqual(json.loads(data)[0]['text'],original['text'])
   self.assertEqual(doc.count('</script>'),1)
if __name__=='__main__':unittest.main()
