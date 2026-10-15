import copy
import base64
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import subtitle_pipeline as p

SRT = '1\n00:01:00,000 --> 00:01:03,000\n<i>Hello,</i> how are you?\n- I am fine.\n\n2\n00:01:04,000 --> 00:01:07,000\nThank you!\n'


class PipelineTests(unittest.TestCase):
    def test_direct_gzip_input_preserves_bytes_and_rejects_bad_or_oversized_data(self):
        encoded=base64.b64encode(gzip.compress(SRT.encode('utf-8'))).decode('ascii')
        self.assertEqual(p.decode_subtitle(encoded), SRT)
        with self.assertRaises(p.base.SetupError):
            p.decode_subtitle('broken')
        huge=base64.b64encode(gzip.compress(b'x'*(2*1024*1024+1))).decode('ascii')
        with self.assertRaises(p.base.SetupError):
            p.decode_subtitle(huge)
        conf=p.config({'media_url':'https://example.com/a','model':'large-v3','language':'en',
            'subtitle_data':encoded,'processing_options':'{"mode":"align"}'})
        self.assertEqual(conf['subtitle_data'],encoded)

    def test_parse_preserves_dialogue_markup_and_line_breaks(self):
        cues = p.parse_subtitles('\ufeff' + SRT.replace('\n', '\r\n'))
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]['display_text'], '<i>Hello,</i> how are you?\n- I am fine.')
        self.assertNotIn('<i>', cues[0]['text'])
        self.assertEqual(cues[1]['start'], 64)
        vtt = 'WEBVTT\n\nintro\n01:00.000 --> 01:03.000 align:start\n<v Speaker>Hello &amp; welcome</v>\n'
        self.assertEqual(p.parse_subtitles(vtt)[0]['text'], 'Hello & welcome')

    def test_malformed_cues_are_not_silently_dropped(self):
        for text in [SRT + '\n3\nbad timestamp\nMissing line', SRT.replace('00:01:03,000', '00:01:00,000'),
                     SRT.replace('00:01:04,000', '00:00:04,000'), SRT.replace('00:01:03,000', '00:61:03,000'), '\0'+SRT]:
            with self.subTest(text=text), self.assertRaises(p.base.SetupError):
                p.parse_subtitles(text)

    def test_sample_clipping_and_shift_apply_offset_exactly_once(self):
        cues = p.clip_cues(p.parse_subtitles(SRT), offset=62, duration=5, shift=1)
        self.assertEqual([(c['start'], c['end']) for c in cues], [(0, 2), (3, 5)])
        self.assertIn('clipped_at_sample_boundary', cues[0]['flags'])
        self.assertEqual(p.absolute_cue(cues[0], 62)['end'], 64)
        with self.assertRaises(p.base.SetupError):
            p.clip_cues(p.parse_subtitles(SRT), 100, 3)

    def test_full_repair_preserves_cues_outside_audio_instead_of_deleting_them(self):
        cues = p.clip_cues(p.parse_subtitles(SRT), 0, 61, preserve_all=True)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[1]['end'], 67)
        self.assertIn('outside_audio_original_timing_kept', cues[1]['flags'])
        with self.assertRaises(p.base.SetupError):
            p.clip_cues(p.parse_subtitles(SRT), 0, 90, shift=-100, preserve_all=True)

    def test_repair_configuration_needs_no_provider_key_and_cannot_generate_dialogue(self):
        raw = {'media_url': 'https://example.com/a', 'model': 'large-v3', 'language': 'en',
            'subtitle_url': 'https://example.com/file.srt', 'processing_options': '{"mode":"align"}'}
        with patch.dict(os.environ, {}, clear=True):
            conf = p.config(raw)
            self.assertTrue(conf['alignment']); self.assertFalse(conf['recover'])
            for extra in ['{"mode":"align","recover":true}', '{"mode":"align","alignment":"true"}', '[]', '{"mode":"align","subtitle_shift":"nan"}']:
                with self.assertRaises(p.base.SetupError):
                    p.config({**raw, 'processing_options': extra})
            with self.assertRaises(p.base.SetupError):
                p.config({**raw, 'language': 'auto'})
            with self.assertRaises(p.base.SetupError):
                p.config({**raw, 'subtitle_url': ''})
            with self.assertRaises(p.base.SetupError):
                p.config({**raw, 'processing_options': '{"mode":"transcribe"}'})

    def test_repair_stage_does_not_import_or_run_speech_recognition(self):
        with patch.object(p, 'read_state', return_value={'uploaded': True, 'config': {'mode': 'align'}, 'notes': []}), patch.object(p, 'save') as save:
            p.recognize()
            self.assertEqual(len(save.call_args[0][0]['notes']), 1)

    def test_alignment_never_fabricates_missing_word_timing(self):
        cue = p.parse_subtitles(SRT)[0]
        rejected = p.apply_alignment(cue, [{'words': [{'word': 'Hello'}]}], 90)
        self.assertEqual(rejected['start'], cue['start'])
        self.assertIn('alignment_unreliable_original_timing_kept', rejected['flags'])
        measured = [{'word': 'Hello', 'start': 60.2, 'end': 61, 'score': .9}]
        accepted = p.apply_alignment({**cue, 'text': 'Hello'}, [{'words': measured}], 90)
        self.assertEqual(accepted['start'], 60.2)
        self.assertEqual(accepted['display_text'], cue['display_text'])
        self.assertEqual(accepted['alignment_coverage'], 1)
        low = p.apply_alignment(cue, [{'words': [{**measured[0], 'score': .1}]}], 90)
        self.assertEqual(low['start'], 60)

    def test_recognition_candidates_do_not_modify_reference(self):
        reference = p.parse_subtitles(SRT)
        before = copy.deepcopy(reference)
        alternate = [{**reference[0], 'start': 80, 'end': 82, 'text': 'Do not leave me!'}]
        self.assertEqual(len(p.differences(reference, alternate)), 1)
        self.assertEqual(reference, before)

    def test_unicode_wrapping_preserves_joiners_and_marks(self):
        text = 'ශ්‍රී ලංකාව ඔබට ආයුබෝවන් ' * 8
        self.assertEqual(' '.join(p.wrap_lines(text)), ' '.join(text.split()))
        self.assertLess(p.display_length(text), len(text))

    def test_export_roundtrip_keeps_exact_dialogue_and_offsets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); out = root/'out'; out.mkdir()
            cues = p.clip_cues(p.parse_subtitles(SRT), 60, 10)
            state = {'config': {'mode': 'align', 'name': 'Movie.v2', 'model': 'large-v3', 'sample': True, 'start': 60, 'subtitle_shift': 0, 'alignment_window': 3},
                'cues': cues, 'original_cues': copy.deepcopy(cues), 'language': 'en', 'duration': 10, 'audio_track': 1,
                'uploaded': True, 'alignment_applied': True, 'notes': [], 'candidates': []}
            state['cues'][0]['start'] = .2
            with patch.object(p, 'STATE', root/'state.json'), patch.object(p, 'OUT', out):
                p.save(state); p.export()
                fixed = p.parse_subtitles((out/'Movie.v2.srt').read_text(encoding='utf-8'))
                original = p.parse_subtitles(SRT)
                self.assertEqual([c['display_text'] for c in fixed], [c['display_text'] for c in original])
                self.assertEqual(fixed[0]['start'], 60.2)
                report = json.loads((out/'review-report.json').read_text(encoding='utf-8'))
                self.assertTrue(report['dialogue_preserved'])
                self.assertIsNone(report['recognition_model'])
                state['cues'][0]['text'] = 'changed dialogue'
                p.save(state)
                with self.assertRaises(p.base.SetupError):
                    p.export()


if __name__ == '__main__':
    unittest.main()
