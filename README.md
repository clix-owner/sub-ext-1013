# CLIXARENA: embedded track picker + existing-dialogue word matching

## Install
Update BOTH archives together. Copy the Actions archive into the existing GitHub repository including .github/workflows and scripts, and commit to its default branch. Replace the Cloudflare UI project with the UI archive and deploy its Worker/public assets together. Keep existing GITHUB_OWNER/GITHUB_REPO/GITHUB_REF/GITHUB_WORKFLOW settings and GITHUB_TOKEN (Actions read/write), UI_PASSWORD, SESSION_SECRET secrets. Keep TMDB_TOKEN if used. No R2 or additional storage service is required. This package has not been deployed to the live site from this chat.

## Three separate methods
Method 1. Extract embedded subtitles: reads tracks, extracts immediately if there is one, or shows a checkbox list under Your requests if there are several. Only checked tracks are extracted. The source remains on the same runner while waiting, so there is no second media download. Selection expires after 30 minutes. The small selection workflow needs an available GitHub runner. Track-list/selection JSON artifacts expire after one day; private video is not uploaded as an artifact.
Method 3. Fix timestamps - match spoken words: upload existing UTF-8 SRT/VTT in the same language as the selected audio (or provide its direct HTTPS download link). Select the audio track/language and speech model. Recognition is used internally to locate the uploaded words; ASR dialogue is never substituted into the output. Source cue identifiers, text, tags and line breaks are preserved. Only timestamps change. No translation, line insertion, merging or dialogue rewriting occurs.
Method 2. Create new subtitles: retains the separate speech-transcription method.

## Word matching
The implementation uses faster-whisper word timestamps and monotonic normalized token matching. Formatting, speaker labels and stage directions are ignored only in matching input. Matching contractions are normalized internally. Clear phrase/boundary matches refine individual cues. When enough well-distributed speech matches agree, a robust time scale/offset corrects progressive timing drift for other cues. Such cues are labeled speech_anchor_transform, not word_matched. Short local corrections supported on both sides are labeled anchor_inferred. Insufficient evidence keeps original timing; new overlaps are rolled back. A global transform is not suitable for every edit/cut pattern, and this implementation does not guarantee per-line or song accuracy.

The recognition pass for this method runs without voice-activity filtering to avoid dropping quiet dialogue; extraneous recognized text is excluded by matching to the uploaded subtitle. This can still produce recognition/matching errors, especially with music or overlapping speech. Languages other than English were not validated on real media in this release. Do not interpret match counts as accuracy percentages.

Base, Small, Medium, Large-v3 and Turbo are available. English word matching uses Base.en/Small.en/Medium.en for those respective sizes. Base.en was used for the supplied local test; larger models have not been tested against this file. Extraction does not install or download any speech model. Word matching and new-subtitle modes install the speech runtime and fetch the requested model as needed on the GitHub runner. Local recognition needs the runtime/model too.

Subtitle uploads are gzip-compressed and passed with the workflow request; 2 MB uncompressed / 45 KB compressed limits protect GitHub's input budget. A direct HTTPS subtitle URL is available for larger compressed files (still 2 MB downloaded). Video URLs and subtitle payloads are workflow inputs; use a private repository for private material. Internally, the existing subtitle_tracks input carries a JSON subtitle source in match mode to retain ten workflow inputs. Other modes continue using it for track indices; the user does not need to manage that encoding.

## Output
Word matching produces an SRT with original dialogue, a copy with original timing, a JSON timing manifest and an HTML comparison/player. The HTML can load a local video in the browser without uploading it; MKV codec support depends on the browser. Otherwise open the SRT with the source video in a normal video player. Samples omit cues outside the processed range and report the omission count; full runs preserve all input cues. The UI's optional sample is three minutes. The separately supplied Merlin output is a custom first-half test.

## Actual local Merlin test
Source: user-provided VidVault (1).mkv (2810.811 seconds), English audio track 0, and the supplied 407-cue English SRT. No video/subtitle was uploaded or re-downloaded. Base.en model assets totaled 147,770,612 bytes on disk; installing the speech runtime required additional package downloads. Two first-half recognition passes were compared using the same cached model, with and without voice-activity filtering. The final pass processed 1405.4055 seconds (23m25.4055s) and used no voice-activity filtering. It took approximately 277 seconds of recognition on this machine.

206 complete cues belong within the requested half: 78 use direct word matches and 128 use the robust speech-anchor timing transform. All 206 dialogue payloads, tags and line breaks match their original source cues exactly. 201 cues from the remaining video range are outside this requested output. The inferred scale is approximately 1.049376 and offset -0.715 seconds, supported by 87 speech anchors with median fit residual about 0.249 seconds. These are consistency measurements, not human-verified accuracy figures, and are not hard-coded into the UI or implementation.

Three separately seeked local clips with longer dialogue context were recognized again using the cached model: start-time differences versus the full-half pass were -0.02s, 0.00s and -0.06s. Earlier very short/context-poor clips could not confirm two short cues; those checks are also retained in the local validation report. No complete human listening review has been performed.

## Validation
44 Python and 33 UI/Worker tests passed (77 total). Includes original extraction/track-selection behavior, timing scale recovery, matching evidence/low-confidence guards, text identity, markup-safe review, subtitle upload compression/size limits, ten-input dispatch, invalid selection prevention and authentication/token isolation. Real local FFmpeg extraction fixtures and the supplied first-half video test were run. Worker bundled offline, workflow YAML parsed with ten inputs, and review JavaScript passed syntax checking. Cloudflare/GitHub live deployment and cross-run selection transport were not exercised here.

## Method numbering and model order update
The UI dropdown is ordered Method 1 (embedded extraction), Method 2 (new subtitles), Method 3 (existing-dialogue word matching). Method 3 initially selects Base, using Base.en for English as in the Merlin test. The model list runs Base → Small → Medium → Large-v3 Turbo → Large-v3. English Method 3 uses the .en versions of Base, Small and Medium. Model choices are remembered separately for Methods 2 and 3 while the page remains open. Large-v3 is the largest model supported by this package; a larger model is not a guarantee of perfect timing. Other sizes were not downloaded or tested on the supplied video during this update.


## Selectable parallel downloads — 2026-10-17

Choose Download connections (1–16, default 4) below the source URL. 1 keeps single-stream downloads. This is per job and applies to embedded extraction, original-language speech transcription and timestamp/word matching. The workflow also exposes this as download_connections for manual runs. Deploy the GitHub workflow/scripts before the updated UI so dispatch recognizes the input. No new secrets or services are required.

Each range is at most 8 MiB, with up to the selected number of concurrent requests. If a proxy returns a full HTTP 200 response instead of a range, the response is closed without consuming the whole file and only that range is retried (up to three requests). Later attempts omit If-Range but still require the same strong ETag, exact Content-Range and received byte count. Files below 16 MiB, unsupported ranges or inconsistent responses use the existing single-stream downloader. Failed parallel data is discarded before single-stream fallback. Google Drive confirmation and small external subtitle downloads remain single-stream.

The exact trendimovies.com hostname retains the preceding compatibility method: exact range/length checks and source samples before/after the download. Samples do not prove full-file identity. Other hosts require strong ETag identity. URL/public-address/redirect checks, normal TLS verification, disk reserve, 12 GiB media limit and existing FFprobe validation remain enabled. Neither tokens nor source URLs are printed by the new downloader. More connections do not guarantee more speed.

Download selection does not change transcription accuracy, models, subtitle text/timings or upload/track-selection job behavior. Actions logs show the selected count and whether parallel or single-stream download is used. PARALLEL_DOWNLOAD=off remains an optional backend escape hatch if supplied in the environment.


## Parallel probe v3

The downloader now logs version 2026.10.17-parallel-probe-v3 and the actual selected connections before probing. Initial full HTTP 200 probe responses are closed and retried up to three times, rather than immediately falling back. Explicit credential-free fallback reasons show why parallel mode could not proceed (missing validator, invalid range, size threshold, disk reserve or transfer verification). Persistent failures still use the single-stream downloader. The existing 8 MiB chunks, per-range retries, ETag/byte-count checks, Trendimovies compatibility and 1–16 per-job choice remain. No URL, token or raw response is exposed by the new diagnostics.

If the previous connection-selection update is already installed, replace scripts/clixarena_parallel.py and scripts/clixarena_media_download.py in GitHub. No Cloudflare redeployment is needed for this v3-only update. This complete bundle also contains the UI and workflow connection selector for installations that have not yet applied it.
