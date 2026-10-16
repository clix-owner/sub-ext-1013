# Fix Timestamps submit — තහවුරු කළ හේතුව සහ නිවැරදි කිරීම

2026-10-16 ඔබ පිරවූ live browser form එකෙන්ම ගැටලුව නැවත ඇති කළා. Method 3, Movie, English තෝරා subtitle file එක සමඟ submit කළ විට:

Compressed subtitle exceeds the request limit. Use a subtitle download link.

ඒ පණිවිඩය frontend subtitlePayload() එකේ gzip file size > 45000 bytes යන guard එකෙන් ආවා. /api/start කැඳවීමට පෙරම එය නතර වෙන නිසා GitHub run එකක් සෑදෙන්නේ නැහැ. Error message එක Your requests කොටසටත් පහළින් තිබුණා. මෙය නිරීක්ෂණයෙන් තහවුරු කළ root cause එකයි. කලින් run title පිළිබඳ අනුමානය මේ submit එකේ හේතුව නොවේ. HTTP 404 log එක වෙනත් extraction run එකකට අදාළයි.

## ස්ථාපනය — දෙකම අවශ්‍යයි

1. මෙම ZIP එකේ github-actions/ තුළ ඇති ගොනු ඔබේ clix-owner/sub-ext-1013 repository එකේ root එකට යොදන්න. .github/workflows/clixarena-subtitles.yml සහ scripts/subtitle_upload.py ඇතුළු වෙනස් වූ scripts commit කර main branch එකට යොදන්න. අවශ්‍ය නම් web editor තුළ GitHub source changes review කර commit කරන්න. පවතින workflow/settings/secrets ඉවත් නොකරන්න.
2. Cloudflare > Workers & Pages > sub-ext-api > Edit code තුළ මෙම ZIP එකේ sub-ext-api-worker-large-subtitles.mjs සම්පූර්ණ code එක යොදා Deploy කරන්න. මෙය frontend HTML/JS/CSS ඇතුළත් තනි Worker එකක්; පරණ static assets නැවත සේවය නොකරන නිසා app.js වෙනම upload කිරීම අවශ්‍ය නැහැ. පවතින environment variables/secrets තබාගන්න. SESSION_SECRET වෙනස් නොකරන්න. Source project එකෙන් deploy කිරීමට කැමති නම් sub-ext-api/ තුළ npm ci, npm run deploy භාවිත කරන්න; එයත් frontend සහ API දෙකම deploy කළ යුතුයි. ක්‍රම දෙකෙන් එකක් පමණක් භාවිත කරන්න.
3. Site එක reload කර subtitle file එක නැවත තෝරා submit කරන්න. ඔබේ පරණ selected file එක reload එකෙන් ඉවත් වේ. අවශ්‍ය නම් නැවත sign in කරන්න.

## වෙනස් වූ හැසිරීම

කුඩා subtitle uploads සෘජුවම workflow inputs ලෙස යනවා. Base64 gzip payload එක අක්ෂර 56000ට වැඩි නම් Worker එක එය උපරිම අක්ෂර 48000 කොටස් කර upload-mode workflow runs වෙත යවනවා. එක් එක් dispatch එක GitHub payload budget එකට යටින් තිබෙනවා. ඉන්පසු මුල් match job එක නිර්මාණය කර එහි run ID එකෙන් status track කරනවා.

Upload runs subtitle-input-UUID-index නම් GitHub artifacts සාදනවා. ඒවා දින 1කින් expire වෙනවා. Match job එක කොටස් සියල්ල ලැබෙන තෙක් උපරිම විනාඩි 15ක් බලා සිට, request UUID, chunk count සහ මුළු compressed file එකේ SHA-256 hash තහවුරු කර UTF-8 subtitles නැවත ගන්නවා. නැති/වැරදි කොටස් මත processing නොකරනවා. වෙනම R2, bucket හෝ upload service එකක් නැහැ. Uploaded subtitles GitHub workflow inputs සහ තාවකාලික artifacts තුළ තිබෙනවා. Media එක chunk upload කරන්නේ නැහැ.

දැන් limits: subtitle plain file 2 MiB; gzip compressed 512 KiB; උපරිම input-part runs 15ක් සහ processing run එකක්. සාමාන්‍ය file එකකට runs කිහිපයක් පමණක් ඇති වේ; කුඩා files සඳහා අමතර runs නැහැ. GitHub runner queue තද නම් transfer එකට කාලය ගත විය හැකියි. Compressed 512 KiBටත් වැඩි නම් subtitle URL ක්‍රමය භාවිත කළ යුතුයි. Bad/expired media URL එකක් HTTP 404 වීම වෙනම source-host දෝෂයකි; මෙම වෙනස ඒ link එක යථා තත්ත්වයට පත් කරන්නේ නැහැ.

Error/status පණිවිඩ submit button එකට ඉහළින් දැන් පෙනෙනවා. Signed request identifiers browser refresh එකෙන් අතුරුදහන් වීම නිවැරදි කළා. Skipped workflow jobs තිබුණත් processing job එකේ status පෙන්වනවා.

## Verification

- API/frontend: 43 tests passed, 0 failed.
- Workflow/Python: 53 tests passed, 0 failed (UTF-8 mode, local FFmpeg).
- 45 KBට වැඩි gzip subtitle fixture එක browser code එකෙන් API වෙත යෑම පරීක්ෂා කළා.
- Large payload එක කොටස් කර byte budget එක තුළ dispatch කිරීම සහ නැවත එකතු කළ subtitle එක byte-for-byte ගැළපීම පරීක්ෂා කළා.
- Failed staging, missing parts, wrong request UUID, wrong hash, invalid part count, timeout සහ artifact download token isolation පරීක්ෂා කළා.
- Workflow YAML parsed: inputs 10, upload job isolation සහ GH_TOKEN read permissions wiring තහවුරු කළා.
- Self-contained Worker syntax සහ updated HTML/JS/CSS සේවය කිරීම පරීක්ෂා කළා.

Live අසාර්ථක submit එක මෙහි නැවත ඇති කළා. නිවැරදි කළ code තවම ඔබේ GitHub/Cloudflare වෙත deploy කර නැහැ; GitHub artifact transport සහ සම්පූර්ණ media processing එක live end-to-end පරීක්ෂා කර නැහැ. Recognition/timestamp algorithm එක වෙනස් කර නැහැ.
