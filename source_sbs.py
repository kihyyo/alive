from collections import OrderedDict
from time import monotonic, time

from .model import ChannelItem, ProgramItem
from .setup import P
from .source_base import SourceBase, URLCacher, parse_hls_master_playlist
from .signed_playlist_cache import SignedPlaylistCache, RefreshValue

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting


class SourceSBS(SourceBase):
    source_id = "sbs"
    ttl = 180  # 3분

    def __init__(self):
        # session for api
        proxy_url = ModelSetting.get("sbs_proxy_url") if ModelSetting.get_bool("sbs_use_proxy") else None
        self.apisess = self.new_session(proxy_url=proxy_url, add_headers={"Referer": "https://www.sbs.co.kr/"})
        # session for playlists
        plproxy = proxy_url if ModelSetting.get_bool("sbs_use_proxy_for_playlist") else None
        self.plsess = self.new_session(proxy_url=plproxy)
        # caching master playlist url
        self.get_url = URLCacher(self.ttl)(self.__get_url)
        self._master_cache = SignedPlaylistCache(
            self._load_master,
            on_error=lambda channel, exc: logger.warning(
                "SBS master refresh failed channel=%s error=%s", channel, type(exc).__name__),
        )

    def load_channels(self) -> None:
        ret = []
        url_list = ["https://static.apis.sbs.co.kr/play-api/1.0/onair/channels"]
        if ModelSetting.get_bool("sbs_include_vod_ch"):
            url_list += ["https://static.apis.sbs.co.kr/play-api/1.0/onair/virtual/channels"]
        for url in url_list:
            data = self.apisess.get(url).json()
            for item in data["list"]:
                try:
                    cname = item["channelname"]
                    is_tv = item.get("type", "TV") == "TV"
                    if item["channelid"] in ["S17", "S18"]:
                        cname += " (보는 라디오)"
                        is_tv = True
                    p = ProgramItem(
                        title=item["title"],
                        onair=item.get("onair_yn", "N") == "Y",
                        stime=item["starttime"],
                        etime=item["endtime"],
                        image=item["thumbimg"],
                    )
                    c = ChannelItem(self.source_id, item["channelid"], cname, None, is_tv, program=p)
                    ret.append(c)
                except Exception:
                    logger.exception("라이브 채널 분석 중 예외: %s", item)
        self.channels = OrderedDict((c.channel_id, c) for c in ret)

    def get_data(self, channel_id: str) -> dict:
        prefix = "virtual/"
        if channel_id.startswith("EVENT"):
            prefix = ""
        else:
            try:
                if int(channel_id[1:]) < 21:
                    prefix = ""
            except ValueError:
                pass
        url = f"https://apis.sbs.co.kr/play-api/1.0/onair/{prefix}channel/{channel_id}?v_type=2&platform=pcweb&protocol=hls&ssl=N&rscuse=&jwt-token=&sbsmain="
        started = monotonic()
        try:
            with self.apisess.get(url, timeout=(3.05, 5)) as response:
                response.raise_for_status()
                return response.json()
        finally:
            elapsed = monotonic() - started
            if elapsed >= 2:
                logger.warning("SBS signed URL API slow channel=%s elapsed_ms=%d",
                               channel_id, elapsed * 1000)

    def __get_url(self, channel_id: str) -> str:
        data = self.get_data(channel_id)
        return data["onair"]["source"]["mediasource"]["mediaurl"]  # master playlist url

    def _load_master(self, channel_id: str, previous: dict | None) -> RefreshValue:
        # Renew with at least 30 seconds left. A slow API call then runs while
        # clients continue fetching fresh media playlists with the valid URL.
        url = (previous or {}).get("_signed_source_url")
        expires = (previous or {}).get("_signed_url_expires", 0.0)
        if not url or expires - time() <= 30:
            url = self.__get_url(channel_id)
            expires = URLCacher(self.ttl).parse_expiry(url) or time() + self.ttl
        if expires - time() <= 5:
            raise ValueError("SBS returned a signed URL too close to expiry")
        started = monotonic()
        data, resolved_url = self.fetch_hls_playlist(url)
        master = parse_hls_master_playlist(data, resolved_url)
        elapsed = monotonic() - started
        if elapsed >= 2:
            logger.warning("SBS master fetch slow channel=%s elapsed_ms=%d",
                           channel_id, elapsed * 1000)
        master["_signed_source_url"] = url
        master["_signed_url_expires"] = expires
        # Keep a five-second safety margin; never return an expired URL even
        # when the background refresh fails. Topology is still checked at 15s.
        return RefreshValue(master, expires - time() - 5, refresh_after=15)

    def make_m3u8(self, channel_id: str, mode: str, quality: str) -> tuple[str, str | dict]:
        return self.streaming_type, self._master_cache.get(channel_id)
