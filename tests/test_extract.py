import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from clixarena_extract import extract_tracks,select_tracks
from clixarena_subtitles import SetupError,tool

class ExtractionTests(unittest.TestCase):
    def test_selection(self):
        self.assertEqual(select_tracks('1, 0,1',2),[1,0])
        for value in ['-1','2','0;echo','all,1','']:
            with self.assertRaises(SetupError):select_tracks(value,2)
    def test_real_mkv_and_mp4(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            text='1\n00:00:01,600 --> 00:00:04,840\nHello there.\nSecond line.\n\n'
            sub=root/'in.srt';sub.write_text(text,encoding='utf-8')
            ass=root/'in.ass'
            tool(['ffmpeg','-v','error','-y','-i',str(sub),str(ass)],30)
            mkv=root/'source.mkv'
            tool(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=black:s=32x32:d=6','-i',str(sub),'-i',str(ass),'-map','0:v','-map','1:s','-map','2:s','-c:v','mpeg4','-c:s','copy','-metadata:s:s:0','language=eng','-metadata:s:s:1','language=sin',str(mkv)],30)
            out=root/'out';result=extract_tracks(mkv,out,'Test')
            self.assertEqual(len(result['tracks']),2)
            self.assertEqual((out/'Test.sub00.eng.srt').read_text(encoding='utf-8'),text)
            self.assertIn('Hello there.',(out/'Test.sub01.sin.ass').read_text(encoding='utf-8'))
            self.assertIn('00:00:01,600 --> 00:00:04,840',(out/'Test.sub01.sin.srt').read_text(encoding='utf-8'))
            self.assertTrue(all(t['original_preserved'] for t in result['tracks']))
            self.assertEqual(len(extract_tracks(mkv,root/'selected','Test','1')['tracks']),1)
            mp4=root/'source.mp4'
            tool(['ffmpeg','-v','error','-y','-i',str(mkv),'-map','0:v','-map','0:s:0','-c:v','copy','-c:s','mov_text',str(mp4)],30)
            result=extract_tracks(mp4,root/'mp4out','Test')
            self.assertTrue(result['tracks'][0]['original_preserved'])
            self.assertIn('00:00:01,600 --> 00:00:04,840',(root/'mp4out/Test.sub00.eng.srt').read_text(encoding='utf-8'))
            video=root/'video.mp4'
            tool(['ffmpeg','-v','error','-y','-i',str(mp4),'-map','0:v','-c','copy',str(video)],30)
            with self.assertRaisesRegex(SetupError,'no embedded'):extract_tracks(video,root/'none','Test')
if __name__=='__main__':unittest.main()
