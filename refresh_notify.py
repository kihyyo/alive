"""Bounded background notification after channel state is updated.

Do not log callback URLs, tokens or response bodies. The receiver may read our
M3U again, so never perform the callback on the source reload/request thread.
"""
from threading import Lock, Thread
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from .setup import P

_lock = Lock()
_running = False
_pending = None


def _callback_request(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("invalid callback URL")
    headers = {}
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in ("x-token", "x-plex-token"):
            headers["X-Token"] = value
        else:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")), headers


def notify_after_refresh():
    global _running, _pending
    url = (P.ModelSetting.get("source_refresh_notify_url") or "").strip()
    if not url:
        return
    with _lock:
        _pending = url
        if _running:
            return
        _running = True
    Thread(target=_send, name="alive-refresh-notify", daemon=True).start()


def _send():
    global _running, _pending
    # One worker and one coalesced pending URL; no unbounded thread/task queue.
    try:
        while True:
            with _lock:
                url, _pending = _pending, None
                if not url:
                    _running = False
                    return
            try:
                target, headers = _callback_request(url)
                with requests.post(target, headers=headers, timeout=(3, 10),
                                   allow_redirects=False, stream=True) as response:
                    if 200 <= response.status_code < 300:
                        P.logger.info("소스 새로고침 후 알림 완료")
                    else:
                        P.logger.warning("소스 새로고침 후 알림 실패 HTTP %s", response.status_code)
            except Exception as exc:
                P.logger.warning("소스 새로고침 후 알림 실패 (%s)", type(exc).__name__)
    finally:
        with _lock:
            # The normal return released ownership inside the lock above.
            # Do not clear a new worker's state after that return.
            if url:
                _running = False
