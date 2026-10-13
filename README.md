# CLIXARENA — video/audio to original-language subtitles

This standalone manual GitHub Actions workflow downloads a video or audio file, extracts the selected audio and creates **UTF-8 SRT subtitles in the spoken language** using Faster Whisper. It transcribes speech; it does not translate it or copy an existing embedded subtitle track.

## Install

For a browser UI hosted on Cloudflare Workers, see **cloudflare-ui/README.md**. This bundle contains both the Actions workflow and the Cloudflare UI; the UI requires the included updated workflow with its request identifier.

Upload all ZIP contents to the repository root. Keep `.github/workflows/clixarena-subtitles.yml` in that exact directory. It can live alongside the existing video-setup files; the filenames are distinct and no Streamtape/Vercel/HF Space changes are required.

## Run

1. Open **Actions → CLIXARENA audio to subtitles → Run workflow**.
2. Enter the **media_url**: direct HTTPS video/audio, a downloadable Google Drive file share link or Dropbox file share link. Supported media includes MP4, MKV, MP3, WAV, FLAC and M4A, provided FFmpeg can decode its audio.
3. Set **output_name**, e.g. `My Movie 2026`. `.srt` is added automatically.
4. Set **audio_track** to `auto` for a single audio track. For multiple tracks use `tam`/`Tamil`, `hin`/`Hindi`, `eng`/`English`, `mal`/`Malayalam`, or the zero-based audio track number. All available audio track numbers/language tags are printed before selection. An ambiguous `auto` selection stops with a clear message instead of transcribing the wrong language.
5. Set the spoken **language** hint: `auto` detects it, `ta` Tamil, `hi` Hindi, `ml` Malayalam, `te` Telugu, `kn` Kannada, `en` English, `si` Sinhala, etc. Auto mode allows segment-level language detection. Output remains original-language transcription, not translation.
6. Choose **model**: `medium` is the default; `small` is faster with potentially more recognition errors; `large-v3` takes more time/memory. All run on CPU with INT8 inference, without a paid API key or GPU.
7. Optionally tick **sample_only** to process 3 minutes. It is **off by default**. **start_seconds** can start at a dialogue scene; generated timestamps retain that offset so they still match the original video. A full run with a nonzero start transcribes from that time to the end.
8. After completion, download **clixarena-subtitles-RUN_ID** from the run's **Artifacts** section. It contains `.srt`, `.vtt`, `.txt` and `transcription-info.json`. Artifacts expire after 7 days.

## Practical details

* This generates an ASR draft. Names, accents, mixed languages, background music and unclear speech can be misrecognized. Check a sample and review text/timing before use; no model guarantees perfect movie subtitles.
* Only audio is converted to mono 16 kHz WAV. Video is not re-encoded or uploaded. Fonts and existing subtitles are not needed.
* Word timestamps split long speech into subtitle cues; punctuation and Unicode combining marks are preserved. Silence filtering uses Silero VAD. Extremely long unbroken words may exceed the preferred line width.
* First run downloads dependencies and the selected model. Python packages/models are cached for later runs. Cache availability and download/CPU speed affect runtime; feature-film processing can be lengthy. The job has a 350-minute timeout. Each simultaneous job has its own source, audio, subtitle outputs and artifact ID.
* Source downloads are limited to 12 GB, require public HTTPS hosts, validate redirects, reject incomplete downloads and retry transient download failures up to three attempts. Login pages, CAPTCHA, Drive quota blocks, HLS playlists and protected streaming sites are not direct file downloads. Google Drive files must allow download access.
* Normal run logs omit source URLs, transcript text and raw FFmpeg/provider errors. GitHub manual **inputs remain accessible to repository users**. For a sensitive signed URL, set repository Actions secret **SUBTITLE_MEDIA_URL** and leave `media_url` blank; that keeps the URL out of dispatch inputs. This is optional; otherwise no secrets are required.
* Nothing commits generated subtitles to the repository. Temporary media is removed after processing; the artifact contains only subtitle/text/metadata files. Public repository artifact access follows GitHub's permissions; use a private repository if your transcript needs restricted access.

Failure messages identify missing audio, ambiguous track/language selection, invalid links, incomplete downloads, no detected speech or model/memory/disk problems. A failed run does not publish partial subtitles as a successful artifact.

Implementation references: [Faster Whisper](https://github.com/SYSTRAN/faster-whisper) and its [transcription API](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py).

## Local verification

```
python -m unittest discover -s tests -v
```

Tests do not download a model or start a workflow. Install `requirements-subtitles.txt` and FFmpeg to run real transcription through the provided workflow. No live GitHub Actions run was triggered while preparing this bundle.

Bundle validation: 15 unit tests passed; an actual two-track FFmpeg test selected Tamil and extracted mono 16 kHz audio; an 11-second speech recording was transcribed with the real tiny model and exported as SRT/VTT/TXT. Workflow YAML was checked for manual-only execution, read-only permissions and subtitle-only artifacts. The default medium model and a full feature-length run were not exercised locally. PyAV is pinned to 16.0.1 because PyAV 19 removed an argument required by Faster Whisper 1.2.1.
