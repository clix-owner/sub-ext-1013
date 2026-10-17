"""Bounded parallel HTTPS range download; never log URLs or exceptions."""
from concurrent.futures import ThreadPoolExecutor
import os
import re
import shutil
import threading
import urllib.request
import urllib.parse
import math

MIN_SIZE = 16 * 1024**2
RESERVE = 2 * 1024**3
CHUNK_SIZE = 8 * 1024**2


class ParallelCheckError(Exception):
    """Only fixed local diagnostic messages, never remote data."""


def parallel_download(url, target, limit, opener_factory, validate, log=print, connections=4):
    """Reduce failed parallel transfers one connection at a time before fallback."""
    if type(connections) is not int or not 1 <= connections <= 16:
        raise ValueError('Choose 1 to 16 download connections')
    if connections == 1 or os.getenv('PARALLEL_DOWNLOAD','true').lower() in ('0','false','off'):
        return _parallel_download_once(url,target,limit,opener_factory,validate,log,connections)
    for count in range(connections,1,-1):
        if count < connections:
            log(f'Retrying parallel download with {count} connections',flush=True)
        result=_parallel_download_once(url,target,limit,opener_factory,validate,log,count)
        if result is True:return True
        # Eligibility failures (no ranges/validator, small file, disk reserve)
        # cannot be fixed by lowering concurrent transfers. Do not repeat them.
        if result is False:return False
    log('Parallel download unavailable; using a single connection',flush=True)
    return False


def _parallel_download_once(url, target, limit, opener_factory, validate, log=print, connections=4):
    """Return False for single-stream fallback.

    Each worker has its own redirect-validating opener. If-Range plus exact
    Content-Range, ETag and byte-count checks prevent silently mixing versions.
    No upload retries or certificate verification changes are made here.
    """
    if type(connections) is not int or not 1 <= connections <= 16:
        raise ValueError('Choose 1 to 16 download connections')
    if connections == 1:
        log('Single connection selected; parallel download disabled', flush=True)
        return False
    if os.getenv('PARALLEL_DOWNLOAD', 'true').lower() in ('0', 'false', 'off'):
        log('Parallel download disabled by backend configuration', flush=True)
        return False
    started = False
    stop = threading.Event()
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept': '*/*', 'Accept-Encoding': 'identity'}
    try:
        validate(url)
        # This host fabricates validators per response. Restrict compatibility
        # handling to the tested host; other hosts retain strong ETag checks.
        compatibility = urllib.parse.urlsplit(url).hostname == 'trendimovies.com'
        log('Checking server byte-range support', flush=True)
        for attempt in range(3):
            request = urllib.request.Request(url, headers=dict(headers, Range='bytes=0-0'))
            with opener_factory().open(request, timeout=30) as response:
                if response.status != 206:
                    if response.status == 200 and attempt < 2:
                        log('Range probe returned full response; retrying probe', flush=True)
                        continue
                    raise ParallelCheckError('server did not return a partial response after range probing')
                match = re.fullmatch(r'bytes 0-0/(\d+)', response.headers.get('Content-Range', ''))
                etag = response.headers.get('ETag', '')
                if not match:raise ParallelCheckError('server returned an invalid Content-Range')
                if not compatibility and not re.fullmatch(r'"[^"\r\n]+"', etag):
                    raise ParallelCheckError('server did not provide a strong file validator')
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise ParallelCheckError('server compressed the byte-range response')
                total = int(match[1])
                if total < MIN_SIZE:raise ParallelCheckError('file is smaller than the 16 MiB parallel threshold')
                if total > limit:raise ParallelCheckError('declared file size exceeds the download limit')
                if len(response.read(2)) != 1:raise ParallelCheckError('server returned an incorrect probe byte count')
                break
        if shutil.disk_usage(target.parent).free < total + RESERVE:
            raise ParallelCheckError('insufficient disk space for parallel download and reserve')
        def sample(first):
            last = min(total - 1, first + 65535)
            req = urllib.request.Request(url, headers=dict(headers, Range=f'bytes={first}-{last}'))
            with opener_factory().open(req, timeout=60) as response:
                if (response.status != 206 or response.headers.get('Content-Range') != f'bytes {first}-{last}/{total}'
                        or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
                    raise ValueError('Sample range mismatch')
                body = response.read(last - first + 2)
                if len(body) != last - first + 1:
                    raise ValueError('Sample length mismatch')
                return body
        samples = {}
        if compatibility:
            for first in sorted({0, total - min(total, 65536)} | {total*i//connections for i in range(1,connections)}):
                samples[first] = sample(first)
            log('Parallel download: range compatibility checks enabled', flush=True)
        log(f'Parallel video download: {connections} connections', flush=True)
        started = True
        with target.open('wb') as output:
            output.truncate(total)

        def open_range(first, last):
            # Some proxies sporadically ignore a valid conditional Range.
            # Retry only this range; unconditioned responses must still carry
            # the same strong ETag before any bytes are accepted.
            for attempt in range(3):
                if stop.is_set():
                    raise ValueError('Sibling connection stopped')
                conditional = {} if compatibility or attempt else {'If-Range': etag}
                req = urllib.request.Request(url, headers=dict(headers, Range=f'bytes={first}-{last}', **conditional))
                response = opener_factory().open(req, timeout=60)
                if response.status == 206:
                    return response
                response.close()
                if stop.is_set():
                    raise ValueError('Sibling connection stopped')
            raise ValueError('Server repeatedly ignored byte ranges')

        def segment(bounds):
            try:
                first, last = bounds
                with open_range(first, last) as response, target.open('r+b') as output:
                    if (response.status != 206 or response.headers.get('Content-Range') != f'bytes {first}-{last}/{total}'
                            or (not compatibility and response.headers.get('ETag') != etag)
                            or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
                        raise ValueError('Range identity mismatch')
                    remaining = last - first + 1
                    length = response.headers.get('Content-Length', '')
                    if length and length != str(remaining):
                        raise ValueError('Range length mismatch')
                    output.seek(first)
                    while remaining:
                        if stop.is_set():
                            raise ValueError('Sibling connection stopped')
                        block = response.read(min(1024**2, remaining))
                        if not block:
                            raise ValueError('Truncated range')
                        if shutil.disk_usage(target.parent).free < RESERVE:
                            raise ValueError('Disk reserve reached')
                        output.write(block)
                        remaining -= len(block)
                    if response.read(1):
                        raise ValueError('Oversized range')
            except Exception:
                stop.set()
                raise

        with ThreadPoolExecutor(max_workers=connections) as pool:
            chunk = min(CHUNK_SIZE, math.ceil(total / connections))
            ranges = [(first, min(total - 1, first + chunk - 1)) for first in range(0, total, chunk)]
            futures = [pool.submit(segment, bounds) for bounds in ranges]
            for future in futures:
                future.result()
        if target.stat().st_size != total:
            raise ValueError('Final size mismatch')
        if compatibility:
            with target.open('rb') as output:
                for first, expected in samples.items():
                    output.seek(first)
                    if output.read(len(expected)) != expected or sample(first) != expected:
                        raise ValueError('Source content changed')
        log('Download complete; validating downloaded file', flush=True)
        return True
    except Exception as error:
        if started:
            target.unlink(missing_ok=True)
            log('Parallel attempt failed; partial download discarded', flush=True)
        if isinstance(error, ParallelCheckError):
            log('Parallel fallback reason: '+str(error), flush=True)
        else:
            log('Parallel fallback reason: range transfer or verification failed', flush=True)
        return None if started else False
