import contextlib
import io
import os
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import clixarena_media_download as media
import clixarena_extract as extract
import subtitle_selection as selection
import subtitle_word_match as match
from clixarena_subtitles import settings,SetupError


class ConnectionTests(unittest.TestCase):
    data={'media_url':'https://example.com/source','output_name':'Test','download_connections':'6'}

    def test_settings_default_validates_and_parses_manual_inputs(self):
        self.assertEqual(settings({'media_url':'https://example.com/source'})['download_connections'],4)
        for count in (1,6,16,'1','6','16'):
            self.assertEqual(settings(dict(self.data,download_connections=count))['download_connections'],int(count))
        for count in (True,0,17,'','1.5',1.5,None,'1; command'):
            with self.assertRaises(SetupError):settings(dict(self.data,download_connections=count))

    def test_downloader_forwards_count_and_falls_back(self):
        with patch.object(media,'validate_url'),patch.object(media,'parallel_download',return_value=True) as parallel,patch.object(media,'_download_once') as single,contextlib.redirect_stdout(io.StringIO()):
            media.download(self.data['media_url'],Path('unused'),12*1024**3,connections=6)
            self.assertEqual(parallel.call_args.kwargs['connections'],6)
            single.assert_not_called()
        with patch.object(media,'validate_url'),patch.object(media,'parallel_download',return_value=False),patch.object(media,'_download_once') as single,contextlib.redirect_stdout(io.StringIO()):
            media.download(self.data['media_url'],Path('unused'),12*1024**3,connections=6)
            single.assert_called_once()

    def test_drive_keeps_confirmation_downloader(self):
        with patch.object(media,'validate_url'),patch.object(media,'parallel_download') as parallel,patch.object(media,'_download_once') as single,contextlib.redirect_stdout(io.StringIO()):
            media.download('https://drive.google.com/file/d/example123/view',Path('unused'),12*1024**3,connections=6)
            parallel.assert_not_called()
            single.assert_called_once()

    def test_extract_selection_and_word_matching_forward_same_job_count(self):
        root=Path(__file__).resolve().parent/('connections-'+uuid.uuid4().hex)
        root.mkdir();previous=Path.cwd()
        class Stop(Exception):pass
        try:
            os.chdir(root)
            for module,call in [(extract,lambda:extract.main(self.data)),(selection,lambda:selection.prepare(self.data)),(match,lambda:match.main(self.data))]:
                with patch.object(module,'download',side_effect=Stop) as download,contextlib.ExitStack() as stack:
                    if module is match:
                        stack.enter_context(patch.object(match,'subtitle_text',return_value='1\n00:00:01,000 --> 00:00:02,000\nHello\n'))
                    with self.assertRaises(Stop):call()
                    self.assertEqual(download.call_args.kwargs['connections'],6)
        finally:
            os.chdir(previous)
            shutil.rmtree(root)
