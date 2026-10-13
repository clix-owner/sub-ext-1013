"""HTTPS media downloader shared with the video pipeline; never logs source URLs."""
import http.cookiejar
import http.client
from html.parser import HTMLParser
import ipaddress
import os
import re
import shutil
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

def mask(value):
    if not value:
        return
    escaped = value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    if os.getenv('GITHUB_ACTIONS') == 'true':
        print('::add-mask::' + escaped, flush=True)


def validate_url(url):
    if any(ord(c) < 32 for c in url) or len(url) > 8192:
        raise ValueError('Invalid URL characters or length')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Use a direct HTTPS URL without embedded credentials or fragments')
    if parsed.port not in (None, 443, 8443):
        raise ValueError('Only HTTPS ports 443 and 8443 are allowed')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('URL must resolve to a public Internet host')
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        mask(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect())


class DownloadError(Exception):
    """Only fixed, credential-free messages may be used here."""



class RetryableDownloadError(DownloadError):
    """Retry only downloads, never upload or workflow dispatch."""


class DriveConfirmation(HTMLParser):
    def __init__(self):
        super().__init__(); self.forms = []; self.current = None
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.current = {'action': attrs.get('action', ''), 'method': attrs.get('method', 'get').lower(), 'fields': {}}
        elif tag == 'input' and self.current is not None and attrs.get('name'):
            self.current['fields'][attrs['name']] = attrs.get('value', '')
    def handle_endtag(self, tag):
        if tag == 'form' and self.current is not None:
            self.forms.append(self.current); self.current = None


def resolve_video_source(url):
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    if parsed.hostname in ('drive.google.com', 'www.drive.google.com'):
        match = re.fullmatch(r'/file/d/([A-Za-z0-9_-]+)(?:/[^/]*)?/?', parsed.path)
        file_id = match.group(1) if match else query.get('id', [''])[0] if parsed.path in ('/open', '/uc') else ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{5,200}', file_id):
            raise DownloadError('Use a Google Drive file share link, not a folder or document link.')
        params = {'export': 'download', 'id': file_id}
        resource_key = query.get('resourcekey', [''])[0]
        if resource_key:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', resource_key):
                raise DownloadError('Invalid Google Drive resource key.')
            params['resourcekey'] = resource_key
        return 'https://drive.google.com/uc?' + urllib.parse.urlencode(params), file_id
    if parsed.hostname in ('www.dropbox.com', 'dropbox.com') and parsed.path.startswith(('/s/', '/scl/fi/')):
        params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        params = [(k, v) for k, v in params if k not in ('dl', 'raw')] + [('dl', '1')]
        return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(params))), None
    return url, None


def open_video_response(opener, request, drive_id):
    for step in range(3):
        response = opener.open(request, timeout=60)
        if not drive_id or 'text/html' not in response.headers.get('Content-Type', '').lower():
            return response
        try:
            final_url = response.geturl()
            body = response.read(2 * 1024**2 + 1)
        finally:
            response.close()
        if len(body) > 2 * 1024**2:
            raise DownloadError('Google Drive returned an unexpectedly large confirmation page.')
        text = body.decode('utf-8', errors='replace')
        parser = DriveConfirmation(); parser.feed(text)
        confirmed = None
        for form in parser.forms:
            action = urllib.parse.urljoin(final_url, form['action'])
            parsed = urllib.parse.urlsplit(action)
            if (form['method'] == 'get' and parsed.scheme == 'https' and parsed.hostname in ('drive.google.com', 'drive.usercontent.google.com')
                    and parsed.path in ('/uc', '/download') and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment):
                params = dict(urllib.parse.parse_qsl(parsed.query)); params.update(form['fields'])
                if params.get('id') != drive_id or params.get('export', 'download') != 'download' or not params.get('confirm'):
                    continue
                confirmed = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(params)))
                break
        if confirmed is None:
            lower = text.lower()
            if any(x in lower for x in ('too many users', 'download quota', 'quota exceeded')):
                raise DownloadError('Google Drive download quota is exceeded. Wait or use another source.')
            raise DownloadError('Google Drive did not provide a downloadable file. Enable Anyone with the link and allow downloads; login, CAPTCHA and restricted files are unsupported.')
        validate_url(confirmed); mask(confirmed)
        request = urllib.request.Request(confirmed, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'})
    raise DownloadError('Google Drive download confirmation did not complete. Check sharing and download permissions.')


def download(url, target, limit):
    validate_url(url); mask(url)
    source, drive_id = resolve_video_source(url)
    if drive_id:
        print('Google Drive file link detected; resolving download', flush=True)
    for attempt in range(3):
        cookies = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect(), urllib.request.HTTPCookieProcessor(cookies)) if drive_id else OPENER
        try:
            _download_once(source, target, limit, opener, drive_id)
            return
        except RetryableDownloadError:
            target.unlink(missing_ok=True)
            if attempt == 2:
                raise
            print('Video download interrupted; retrying from the beginning (attempt ' + str(attempt + 2) + '/3)', flush=True)
            time.sleep(2 * (attempt + 1))
        except Exception:
            target.unlink(missing_ok=True)
            raise


def _download_once(url, target, limit, opener=OPENER, drive_id=None):
    validate_url(url)
    mask(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': '*/*', 'Accept-Encoding': 'identity'})
    try:
        with open_video_response(opener, request, drive_id) as response, target.open('wb') as output:
            content_type = response.headers.get('Content-Type', '').lower()
            if 'text/html' in content_type or 'application/json' in content_type:
                raise DownloadError('The server returned a webpage or API response instead of video. Obtain a fresh direct download link.')
            total = 0
            declared_size = response.headers.get('Content-Length', '')
            if declared_size.isdigit() and int(declared_size) > limit:
                raise DownloadError('Video exceeds the 12 GB download limit.')
            while block := response.read(1024 * 1024):
                if total == 0 and block.lstrip()[:32].lower().startswith((b'<!doctype html', b'<html')):
                    raise DownloadError('The server returned an HTML page instead of video. Obtain a fresh direct download link.')
                total += len(block)
                if total > limit:
                    raise DownloadError('Video exceeds the 12 GB download limit.')
                if shutil.disk_usage(target.parent).free < 2 * 1024**3:
                    raise DownloadError('Runner disk space is below the 2 GB reserve.')
                output.write(block)
            if declared_size.isdigit() and total != int(declared_size):
                raise RetryableDownloadError('Download is incomplete after three attempts: received size differs from the server Content-Length. The host may be truncating the file or reporting an incorrect size.')
        if not target.stat().st_size:
            raise DownloadError('The server returned an empty file.')
        print('Download complete; validating downloaded file', flush=True)
    except urllib.error.HTTPError as error:
        hints = {401: 'Link requires authentication.', 403: 'Link expired, needs cookies, or the host blocked this runner.',
                 404: 'File was not found or the link expired.', 429: 'Host rate limit reached.'}
        kind = RetryableDownloadError if error.code in (408, 429, 500, 502, 503, 504) else DownloadError
        raise kind('Download HTTP ' + str(int(error.code)) + '. ' + hints.get(error.code, 'Video host rejected the download request.')) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLError):
            message = 'Download TLS certificate validation failed.'
        elif isinstance(error.reason, socket.gaierror):
            message = 'Download hostname could not be resolved.'
        elif isinstance(error.reason, (TimeoutError, socket.timeout)):
            message = 'Download connection timed out; the host or port may be unreachable from GitHub.'
        else:
            message = 'Download connection failed; the host or port may be unavailable or blocked from GitHub.'
        raise (DownloadError if isinstance(error.reason, (ssl.SSLError, socket.gaierror)) else RetryableDownloadError)(message) from None
    except (TimeoutError, socket.timeout):
        raise RetryableDownloadError('Download timed out while receiving video data.') from None
    except (http.client.IncompleteRead, ConnectionError):
        raise RetryableDownloadError('Video connection ended before the download completed.') from None

