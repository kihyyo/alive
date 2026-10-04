import re
import traceback
from collections import OrderedDict
from datetime import datetime
from urllib.parse import quote, unquote

import requests
from flask import Response, request
from plugin import F, ModelBase, db  # type: ignore # pylint: disable=import-error
from tool import ToolUtil  # type: ignore # pylint: disable=import-error

from .model import ChannelItem, ProgramItem
from .setup import P
from .source_base import SourceBase
from .bot_epg import _time

logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting


class SourceBot(SourceBase):
    source_id = "bot"

    def load_channels(self) -> None:
        ModelBot.time_check()
        items = ModelBot.get_list(by_dict=True)
        logger.debug("SourceBot.load_channels %d items found", len(items))
        ret = []
        for item in items:
            c = ChannelItem(self.source_id, item["code"], item["title"], item["poster"], True, item["is_drm"])
            c.program = ProgramItem(title=f"{item['start_time_str']} ~ {item['end_time_str']}")
            start = _time(item.get("start_time")) or _time(item.get("start_time_str"))
            stop = _time(item.get("end_time")) or _time(item.get("end_time_str"))
            if start and stop and stop > start:
                # Preserve the event's actual date (including overnight events),
                # rather than ProgramItem's provider-specific today adjustment.
                c.epg_fallback = (start.strftime("%Y%m%d%H%M%S %z"),
                                  stop.strftime("%Y%m%d%H%M%S %z"))
            ret.append(c)
        self.channels = OrderedDict((c.channel_id, c) for c in ret)

    def get_url(self, channel_id: str) -> str:
        return self.channels[channel_id].url

    def make_drm(self, data: dict, mode: str) -> tuple[str, dict]:
        if mode == "web_play":
            return "drm+web", {
                "src": data["uri"],
                "type": "application/x-mpegurl",
                "keySystems": {
                    "com.widevine.alpha": {
                        "url": "/alive/proxy/license",
                        "licenseHeaders": {
                            "Real-Url": data["drm_license_uri"],
                            "Real-Origin": data["drm_key_request_properties"]["origin"],
                            "Real-Referer": data["drm_key_request_properties"]["referer"],
                        },
                        "persistentState": "required",
                    }
                },
            }
        return "drm", data

    def make_m3u8(self, channel_id: str, mode: str, quality: str) -> tuple[str, str | dict]:
        db_item = ModelBot.get_by_code(channel_id)
        if db_item is not None:
            if db_item.ott == "TVING":
                uri = f"/alive/bot/proxy?url={quote(db_item.stream_url)}"
                url = ToolUtil.make_apikey_url(uri)
                return "redirect", url
            if db_item.ott == "CPP":
                if not db_item.is_drm:
                    return "redirect", db_item.stream_url
                info = db_item.info or {}
                key = info.get("key", "")
                # An encrypted envelope is opaque to ALive; only Shyni holds
                # the wrapping key and verifies the GCM authentication tag.
                if not isinstance(key, str) or not (
                    re.fullmatch(
                        r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}):[0-9a-fA-F]{32}", key.strip()
                    ) or re.fullmatch(r"shyni-key:v1:[A-Za-z0-9_-]{124}", key.strip())
                ):
                    raise ValueError("CPP DRM key must be KID:KEY or a supported encrypted envelope")
                return "drm", {
                    "src": db_item.stream_url,
                    "type": "application/dash+xml",
                    "key": key.strip(),
                }
        return None, ""

    def process_discord_data(self, msg):
        refresh = ModelBot.process(msg["msg"]["data"])
        if refresh:
            self.load_channels()
            from .refresh_notify import notify_after_refresh
            notify_after_refresh()


@F.app.route("/alive/bot/proxy")
@F.check_api
def tving_proxy():
    try:
        if not (url := request.args.get("url")):
            return Response(status=400, response="'url' parameter is missing")
        url = unquote(url)
        res = requests.get(url, timeout=5)
        if not res.ok:
            return res
        if "application/vnd.apple.mpegurl" not in res.headers.get("Content-Type", ""):
            return Response(status=500, response=f"invalid response from source: {url}")

        # streaming type - direct
        prefix = url.split("chunklist_")[0]
        suffix = url.split("?")[-1]
        ret = re.sub(r"media_.*?\.ts", lambda m: f"{prefix}{m.group(0)}?{suffix}", res.text)
        ret = ret.replace("http://", "https://")
        response = Response(ret, content_type="application/vnd.apple.mpegurl")
        response.headers["Content-Disposition"] = 'attachment; filename="chunklist.m3u8"'
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    except Exception as e:
        return Response(status=500, response=f"Error: {str(e)}")


class ModelBot(ModelBase):
    P = P
    __tablename__ = f"{P.package_name}_bot_item"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_time = db.Column(db.DateTime)
    code = db.Column(db.String)
    start_time_str = db.Column(db.String)
    end_time_str = db.Column(db.String)
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)
    poster = db.Column(db.String)
    title = db.Column(db.String)
    is_drm = db.Column(db.Boolean, default=False)
    info = db.Column(db.JSON)
    stream_url = db.Column(db.String)
    ott = db.Column(db.String)

    def __init__(self):
        self.created_time = datetime.now()

    @classmethod
    def process(cls, data) -> bool:
        try:
            # logger.debug(d(data))
            db_item = cls.get_by_code(data["id"])
            if data["s"] == "start":
                if db_item is None:
                    db_item = cls()
                    db_item.code = data["id"]
                    db_item.title = data["t"]
                    db_item.poster = data["p"]
                    db_item.start_time_str = data["t1"]
                    db_item.end_time_str = data["t2"]

                    db_item.start_time = datetime.strptime(data["t1"], "%Y-%m-%d %H:%M")
                    db_item.end_time = datetime.strptime(data["t2"], "%Y-%m-%d %H:%M")
                    db_item.is_drm = data["i"]["is_drm"]
                    db_item.info = data["i"]
                    db_item.stream_url = data["i"]["src"]
                    db_item.ott = data["id"].split("_", 1)[0]
                    db_item.save()
                    logger.debug("새로운 방송: %s", data["t"])
                    return True
                # A start event can rotate the signed URL/key of an existing event.
                # Keep the latest info instead of silently retaining expired data.
                changed = (db_item.info != data["i"] or db_item.title != data["t"]
                           or db_item.poster != data["p"] or db_item.start_time_str != data["t1"]
                           or db_item.end_time_str != data["t2"])
                if changed:
                    db_item.title = data["t"]
                    db_item.poster = data["p"]
                    db_item.start_time_str = data["t1"]
                    db_item.end_time_str = data["t2"]
                    db_item.start_time = datetime.strptime(data["t1"], "%Y-%m-%d %H:%M")
                    db_item.end_time = datetime.strptime(data["t2"], "%Y-%m-%d %H:%M")
                    db_item.is_drm = data["i"]["is_drm"]
                    db_item.info = data["i"]
                    db_item.stream_url = data["i"]["src"]
                    db_item.save()
                    logger.debug("방송 정보 갱신: %s", data["t"])
                    return True
                logger.debug("이미 등록된 방송: %s", data["t"])
            elif data["s"] == "end":
                if db_item is not None:
                    logger.debug("종료된 방송: %s", db_item.title)
                    db_item.delete_by_id(db_item.id)
                    return True

        except Exception as e:
            # SQLAlchemy errors may contain bound JSON, including DRM keys.
            logger.error("BOT 데이터 처리 실패 (%s)", type(e).__name__)
        return False

    @classmethod
    def get_by_code(cls, code):
        try:
            with F.app.app_context():
                return F.db.session.query(cls).filter_by(code=code).first()
        except Exception as e:
            logger.error(f"Exception:{str(e)}")
            logger.error(traceback.format_exc())

    @classmethod
    def time_check(cls):
        try:
            with F.app.app_context():
                now = datetime.now()  # + timedelta(days=1)
                items = F.db.session.query(cls).filter(cls.end_time <= now).all()
                logger.debug("ModelBot.time_check found %d items to delete", len(items))
                F.db.session.query(cls).filter(cls.end_time <= now).delete()
                F.db.session.commit()
        except Exception as e:
            logger.error(f"Exception:{str(e)}")
            logger.error(traceback.format_exc())
