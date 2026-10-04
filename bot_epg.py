"""Add received BOT event times only when the guide has no real programme.

No provider requests and no writes to the source XML. Event rows are read on
each EPG request, so start/update/end notifications need no scheduled rebuild.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

KST = timezone(timedelta(hours=9))


def _time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).strip())
        except ValueError:
            try:
                result = datetime.strptime(str(value).strip(), "%Y%m%d%H%M%S %z")
            except ValueError:
                return None
    return result.replace(tzinfo=KST) if result.tzinfo is None else result.astimezone(KST)


def _placeholder(programme):
    return (programme.findtext("desc") or "").strip() == "방송 정보 없음" or (
        programme.get("data-alive-bot") == "1"
    )


def merge_bot_epg(xml_data, items, now=None):
    """Return augmented XML bytes, or None when the original needs no change."""
    now = _time(now or datetime.now(KST))
    events = []
    for item in items:
        title = str(item.get("title") or "").strip()
        code = str(item.get("code") or "").strip()
        start = _time(item.get("start_time")) or _time(item.get("start_time_str"))
        stop = _time(item.get("end_time")) or _time(item.get("end_time_str"))
        if title and code and start and stop and stop > start and stop > now:
            events.append((item, title, code, start, stop))
    if not events:
        return None
    root = ET.fromstring(xml_data) if xml_data else ET.Element("tv")
    if root.tag != "tv":
        raise ValueError("Not an XMLTV document")
    channels = root.findall("channel")
    by_name = {}
    for channel in channels:
        for name in [channel.get("id", "")] + [n.text or "" for n in channel.findall("display-name")]:
            by_name.setdefault(name.casefold(), []).append(channel)
    programmes = {}
    for programme in root.findall("programme"):
        programmes.setdefault(programme.get("channel"), []).append(programme)
    changed = False
    seen = set()
    for item, title, code, start, stop in events:
        key = (code, start, stop)
        if key in seen:
            continue
        seen.add(key)
        matches = []
        for name in (title, f"{code}.bot"):
            for channel in by_name.get(name.casefold(), []):
                if channel not in matches:
                    matches.append(channel)
        real = False
        for channel in matches:
            for programme in programmes.get(channel.get("id"), []):
                pstart, pstop = _time(programme.get("start")), _time(programme.get("stop"))
                if (not _placeholder(programme) and pstart and pstop
                        and pstart < stop and pstop > max(start, now)):
                    real = True
                    break
        if real:
            continue
        channel = next((c for c in matches if c.get("id") == title), None)
        if channel is None:
            channel = matches[0] if matches else None
        if channel is None:
            # ALive m3u/m3uall use the channel name as tvg-id.
            channel = ET.Element("channel", {"id": title})
            ET.SubElement(channel, "display-name").text = title
            ET.SubElement(channel, "display-name").text = f"{code}.bot"
            poster = item.get("poster") or ""
            if isinstance(poster, str) and poster.startswith(("https://", "http://")):
                ET.SubElement(channel, "icon", {"src": poster})
            # XMLTV requires channels before programmes.
            first_programme = next((i for i, e in enumerate(root) if e.tag == "programme"), len(root))
            root.insert(first_programme, channel)
            by_name.setdefault(title.casefold(), []).append(channel)
            by_name.setdefault(f"{code}.bot".casefold(), []).append(channel)
        cid = channel.get("id")
        # Replace only generic no-information placeholders for this BOT channel.
        for programme in list(programmes.get(cid, [])):
            if _placeholder(programme):
                root.remove(programme)
                programmes[cid].remove(programme)
        programme = ET.SubElement(root, "programme", {
            "data-alive-bot": "1",
            "channel": cid,
            "start": start.strftime("%Y%m%d%H%M%S %z"),
            "stop": stop.strftime("%Y%m%d%H%M%S %z"),
        })
        ET.SubElement(programme, "title", {"lang": "ko"}).text = title
        ET.SubElement(programme, "desc", {"lang": "ko"}).text = (
            f"봇 수신 방송 시간: {start:%Y-%m-%d %H:%M} ~ {stop:%Y-%m-%d %H:%M} (KST)"
        )
        programmes.setdefault(cid, []).append(programme)
        changed = True
    if not changed:
        return None
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def epg_response(xmltv_path):
    """Serve MYEPG with a live BOT-only fallback; leave stored XML untouched."""
    from flask import Response
    from .setup import P
    from .source_bot import ModelBot

    try:
        if P.ModelSetting.get_bool("use_bot"):
            items = ModelBot.get_list(by_dict=True)
            path = Path(xmltv_path)
            # Do not turn a partially written/rebuilding guide into a BOT-only guide.
            data = path.read_bytes()
            if data:
                merged = merge_bot_epg(data, items)
                if merged is not None:
                    response = Response(merged, mimetype="application/xml")
                    response.headers["Cache-Control"] = "no-store"
                    return response
    except Exception as exc:
        # Never include received message data, stream URLs or keys in logs.
        P.logger.warning("BOT EPG 보충 실패 (%s); 원본 EPG 사용", type(exc).__name__)
    return None


def install_bot_epg_hook(app):
    """Keep integration in ALive; MYEPG may restore its own files on startup."""
    if app.extensions.get("alive_bot_epg_hook"):
        return
    from flask import request
    from importlib import import_module

    @app.after_request
    def add_bot_epg(response):
        if (request.path.rstrip("/") != "/myepg/api/epgall"
                or request.method not in ("GET", "HEAD", "POST")
                or response.status_code not in (200, 304)):
            return response
        try:
            module = import_module("myepg.mod_main")
            path = Path(module.__file__).parent / "file" / "xmltv.xml"
            supplemented = epg_response(str(path))
            if supplemented is not None:
                # A source-file ETag/304 cannot represent fresh BOT row changes.
                response.close()
                return supplemented
        except Exception as exc:
            from .setup import P
            P.logger.warning("BOT EPG 연결 실패 (%s); 원본 응답 사용", type(exc).__name__)
        return response

    app.extensions["alive_bot_epg_hook"] = True
