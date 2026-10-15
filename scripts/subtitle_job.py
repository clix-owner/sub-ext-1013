import json
import os
import sys
from pathlib import Path
from clixarena_subtitles import SetupError
from clixarena_media_download import DownloadError

def main():
    inputs=json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8')).get('inputs',{})
    mode=inputs.get('mode','extract')
    if mode=='extract':
        from clixarena_extract import main as extract
        extract(inputs)
    elif mode=='match':
        from subtitle_word_match import main as match
        match(inputs)
    elif mode=='transcribe':
        from clixarena_subtitles import main as transcribe
        if transcribe(): raise SetupError("Speech transcription failed; see the preceding step message.")
    else: raise SetupError('Choose extract or transcribe. Alignment has been removed.')

if __name__=='__main__':
    try: main()
    except (SetupError,DownloadError) as error:
        print(str(error),file=sys.stderr);sys.exit(1)
    except Exception:
        print('Subtitle processing failed unexpectedly. No private source details were logged.',file=sys.stderr);sys.exit(1)
