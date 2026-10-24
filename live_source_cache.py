"""Keep live URL/master renewal off active media-playlist requests."""
from time import monotonic, time

from .signed_playlist_cache import RefreshValue, SignedPlaylistCache
from .source_base import URLCacher, parse_hls_master_playlist
from .setup import P


class LiveSourceCache:
    def __init__(self, source, resolve_url):
        self.source = source
        self.resolve_url = resolve_url
        self.cache = SignedPlaylistCache(
            self._load,
            on_error=lambda key, exc: P.logger.warning(
                "Live source refresh failed provider=%s channel=%s error=%s",
                source.source_id, key[0], type(exc).__name__),
        )

    def get(self, channel_id, *, master):
        return self.cache.get((channel_id, master))

    def _load(self, key, previous):
        channel_id, master = key
        url = (previous or {}).get('_source_url')
        expires = (previous or {}).get('_source_expires', 0.0)
        if not master or not url or expires - time() <= 30:
            started = monotonic()
            url = self.resolve_url(channel_id)
            elapsed = monotonic() - started
            if elapsed >= 2:
                P.logger.warning("Live URL API slow provider=%s channel=%s elapsed_ms=%d",
                                 self.source.source_id, channel_id, elapsed * 1000)
            try:
                expires = URLCacher(self.source.ttl).parse_expiry(url)
            except (ValueError, KeyError, TypeError):
                expires = None
            # Some providers use an opaque signature with no readable expiry.
            # Retain that provider's existing cache lifetime as a local deadline;
            # this is not a claim about the upstream token's actual lifetime.
            expires = expires or time() + self.source.ttl
        if expires - time() <= 5:
            raise ValueError('Live source URL is too close to expiry')
        value = {}
        if master:
            started = monotonic()
            data, resolved = self.source.fetch_hls_playlist(url)
            value = parse_hls_master_playlist(data, resolved)
            elapsed = monotonic() - started
            if elapsed >= 2:
                P.logger.warning("Live master slow provider=%s channel=%s elapsed_ms=%d",
                                 self.source.source_id, channel_id, elapsed * 1000)
        value['_source_url'] = url
        value['_source_expires'] = expires
        remaining = expires - time() - 5
        # URL-only paths refresh at half their remaining lease. Master paths
        # keep checking topology every 15s, independently of URL renewal.
        return RefreshValue(value, remaining, 15 if master else remaining / 2)
