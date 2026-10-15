# CLIXARENA embedded subtitle track picker

## Install both updated parts
1. Copy the Actions ZIP contents into the existing GitHub Actions repository, including .github/workflows and scripts. Commit to its default branch. Alignment scripts are no longer used.
2. Replace the Cloudflare UI project files with the UI ZIP contents. Keep the existing GITHUB_OWNER, GITHUB_REPO, GITHUB_REF and GITHUB_WORKFLOW settings, and GITHUB_TOKEN (Actions read/write), UI_PASSWORD and SESSION_SECRET secrets. Keep TMDB_TOKEN if used. Deploy Worker and public assets together. The workflow filename remains clixarena-subtitles.yml.
3. Refresh the website. Choose Extract embedded subtitles, enter the source link and filename, then Find embedded subtitles.

## User flow
The runner downloads the media once and reads its embedded subtitle tracks. A single track is extracted automatically without waiting for a selection. Multiple tracks appear under Your requests with checkboxes, language, title, codec and default/forced labels. Tick at least one and press Extract selected subtitles. Only those tracks are exported. The download ZIP appears in the same request when finished. An empty selection is rejected. A source with no embedded tracks fails clearly.

Selection can take a short time to arrive through GitHub Actions. The runner keeps the source locally while waiting, so extraction does not download the video again. The user has 30 minutes to choose. Returning to the page resumes tracking. Once the selection is submitted, the page remembers that it was sent. Keep the tab open or return within that selection period. Waiting consumes runner time and requires another available GitHub runner for the short selection workflow. If that workflow fails, its GitHub run contains the failure details; start a new extraction request if necessary.

No R2, separate upload service, extra model or private media artifact is used. Small track-list/selection JSON artifacts are retained for one day to transfer the choice between the UI and the runner. The video stays on the original runner and is removed when it finishes. Final subtitles are retained seven days. The media link is sent as a workflow input. Use a private repository for private links.

## Output
Source subtitle timestamps are retained. SubRip copies to SRT. ASS/SSA and WebVTT retain a native-format copy and an SRT conversion where supported. MP4 timed text retains a subtitle-only MP4 copy plus SRT. PGS exports as SUP; other compatible codecs export to subtitle-only MKS. SRT conversions may drop styling; the original-format copy is retained when supported. Image subtitles remain images, not text; no OCR is done. Burned-in subtitles cannot be extracted. HTML/JSON reports identify exported tracks and failures. Unsupported track exports fail explicitly, with any successful outputs still available in the GitHub run artifact.

Create new subtitles remains a separate speech-recognition method. Only that method installs/downloads speech models. No translation or alignment is performed by embedded extraction.

## Manual workflow
Use mode extract with subtitle_tracks=all or explicit zero-based subtitle indices, e.g. 0,2. The ask option is for requests started from the website (which supplies a request UUID). The select workflow mode is an internal transport for checkbox choices and is not a new subtitle method.

## Validation
28 UI/Worker tests and 25 Python tests passed (53 total). Local FFmpeg tests verified multiple-track discovery without extraction, selection of only one actual subtitle track, no repeated source download during selection, single-track immediate extraction, exact SRT text/line breaks/timestamps, ASS/native and SRT export, MP4 timed text, and no-track failure. Selection request identity, invalid/empty ticks, timeout, signed artifact host checks and token isolation were tested. Worker bundled offline successfully. Bitmap mappings have not been tested with a real bitmap fixture. No user video or remote media was downloaded during implementation. Live GitHub/Cloudflare deployment and the live cross-run selection transfer have not been tested or deployed from this chat.
