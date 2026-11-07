from __future__ import annotations

import asyncio
import html
import json
import random
import re
from datetime import datetime, timedelta
from urllib.parse import quote
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Plain

PLUGIN_NAME = "astrbot_plugin_playlist_daily"


def _data_dir() -> Path:
    try:
        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:
        return Path(__file__).resolve().parents[2] / "plugin_data" / PLUGIN_NAME


DATA_DIR = _data_dir()
STORE = DATA_DIR / "songs.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class PlaylistDaily(Star):
    """导入歌单，每天推荐一首。"""

    def __init__(self, context: Context, config: Any | None = None):
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else getattr(context, "get_config", lambda: {})()
        self._task: asyncio.Task | None = None
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self._register_page_web_apis()
        except Exception as e:
            logger.warning(f"[推歌] 页面接口注册失败: {e}")
        try:
            self._task = asyncio.create_task(self._daily_loop())
        except Exception as e:
            logger.warning(f"[推歌] 定时任务启动失败: {e}")


    # ---------------- WebUI 页面 API ----------------

    def _register_page_web_apis(self) -> None:
        context = getattr(self, "_context", None) or getattr(self, "context", None)
        register = getattr(context, "register_web_api", None)
        logger.info(f"[推歌] 页面注册检查: ctx={type(context).__name__} callable={callable(register)}")
        if not callable(register):
            return
        routes = (
            ("page/status", self.page_status, ["GET"], "推歌状态"),
            ("page/import", self.page_import, ["POST"], "导入歌单"),
            ("page/push", self.page_push, ["POST"], "立即推送"),
        )
        for endpoint, callback, methods, desc in routes:
            path = f"/{PLUGIN_NAME}/{endpoint}"
            try:
                register(path, callback, methods, desc)
            except TypeError:
                register(path, callback, methods)
            except Exception as e:
                logger.warning(f"[推歌] 注册页面接口失败 {path}: {e}")

    async def _page_body(self) -> dict:
        try:
            from quart import request as _rq
            body = await _rq.get_json(silent=True)
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}

    async def page_status(self):
        try:
            data = self._load()
            sent = data.get("sent", [])
            return {
                "ok": True,
                "data": {
                    "tracks": len(data.get("tracks", [])),
                    "source": data.get("source", ""),
                    "imported_at": data.get("imported_at", ""),
                    "push_time": str(self.config.get("push_time", "09:00")),
                    "sent_count": len(sent),
                    "recent": sent[-7:],
                },
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def page_import(self):
        try:
            body = await self._page_body()
            url = str(body.get("url") or "").strip()
            if not url:
                return {"ok": False, "error": "缺少 url"}
            tracks = await self._fetch_apple_music(url)
            if not tracks:
                return {"ok": False, "error": "未解析到曲目"}
            data = self._load()
            have = {(t.get("title", ""), t.get("artist", "")) for t in data["tracks"]}
            added = [t for t in tracks if (t["title"], t.get("artist", "")) not in have]
            data["tracks"].extend(added)
            data["source"] = url
            data["imported_at"] = datetime.now().isoformat(timespec="seconds")
            self._save(data)
            return {"ok": True, "data": {"parsed": len(tracks), "added": len(added), "total": len(data["tracks"])}}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def page_push(self):
        try:
            text = await self._push_one()
            return {"ok": True, "data": {"text": text}}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- 存储 ----------------

    def _load(self) -> dict:
        if not STORE.exists():
            return {"source": "", "imported_at": "", "tracks": [], "sent": []}
        try:
            data = json.loads(STORE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("tracks", [])
                data.setdefault("sent", [])
                return data
        except Exception:
            pass
        return {"source": "", "imported_at": "", "tracks": [], "sent": []}

    def _save(self, data: dict) -> None:
        STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- Apple Music 解析 ----------------

    async def _fetch_apple_music(self, url: str) -> list[dict]:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"抓取失败 HTTP {resp.status}")
                page = await resp.text()

        tracks: list[dict] = []
        for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
            try:
                node = json.loads(html.unescape(m.group(1)))
            except Exception:
                continue
            if isinstance(node, dict) and node.get("@type") == "MusicPlaylist":
                for t in node.get("track") or []:
                    if not isinstance(t, dict) or not t.get("name"):
                        continue
                    artists = t.get("byArtist") or []
                    if isinstance(artists, dict):
                        artists = [artists]
                    artist = ", ".join(a.get("name", "") for a in artists if isinstance(a, dict))
                    tracks.append({"title": str(t["name"]).strip(), "artist": artist})

        m = re.search(r'id="serialized-server-data"[^>]*>(.*?)</script>', page, re.S)
        if m:
            try:
                blob = json.loads(m.group(1))
            except Exception:
                blob = None
            if blob is not None:
                pairs: list[tuple[str, str]] = []

                def walk(o: Any) -> None:
                    if isinstance(o, dict):
                        n, a = o.get("title") or o.get("name"), o.get("artistName")
                        if isinstance(n, str) and isinstance(a, str):
                            pairs.append((n.strip(), a.strip()))
                        for v in o.values():
                            walk(v)
                    elif isinstance(o, list):
                        for v in o:
                            walk(v)

                walk(blob)
                amap = {n: a for n, a in pairs}
                for t in tracks:
                    if not t.get("artist"):
                        t["artist"] = amap.get(t["title"], "")
                # [补丁 2026-11-07] 用户歌单（pl.u-*）没有 JSON-LD，回落到内嵌数据
                if not tracks and pairs:
                    seen_s: set[tuple[str, str]] = set()
                    for n, a in pairs:
                        if (n, a) in seen_s:
                            continue
                        seen_s.add((n, a))
                        tracks.append({"title": n, "artist": a})

        seen: set[tuple[str, str]] = set()
        out: list[dict] = []
        for t in tracks:
            key = (t["title"], t.get("artist", ""))
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out

    def _parse_text_playlist(self, text: str) -> list[dict]:
        out: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            if "-" in line:
                title, artist = line.split("-", 1)
                out.append({"title": title.strip(), "artist": artist.strip()})
            else:
                out.append({"title": line, "artist": ""})
        return out

    # ---------------- 选歌 ----------------

    def _pick(self, data: dict) -> dict | None:
        tracks = [t for t in data.get("tracks", []) if t.get("title")]
        if not tracks:
            return None
        days = int(self.config.get("history_days", 30) or 30)
        cutoff = datetime.now() - timedelta(days=days)
        recent: set[tuple[str, str]] = set()
        for item in data.get("sent", []):
            try:
                ts = datetime.fromisoformat(item.get("at", ""))
            except Exception:
                continue
            if ts >= cutoff:
                recent.add((item.get("title", ""), item.get("artist", "")))
        pool = [t for t in tracks if (t["title"], t.get("artist", "")) not in recent]
        if not pool:
            pool = tracks
        return random.choice(pool)

    async def _blurb(self, track: dict) -> str:
        if not bool(self.config.get("with_blurb", True)):
            return ""
        try:
            provider = None
            pid = str(self.config.get("provider_id", "") or "").strip()
            if pid:
                provider = self.context.get_provider_by_id(pid)
            if provider is None:
                provider = self.context.get_using_provider()
            if provider is None:
                return ""
            prompt = (
                "用一句话（不超过 30 字）向朋友推荐这首歌，像私人歌单分享，"
                "不要用「推荐」「必听」这类词，别夸张。\n"
                f"歌名：{track.get('title')}\n歌手：{track.get('artist') or '未知'}"
            )
            resp = await provider.text_chat(prompt=prompt, system_prompt="你是鱼鱼，说话轻松简短。")
            text = getattr(resp, "completion_text", "") or ""
            return text.strip().replace("\n", " ")[:60]
        except Exception as e:
            logger.debug(f"[推歌] 推荐语生成失败: {e}")
            return ""

    async def _push_one(self, umo: str | None = None) -> str:
        data = self._load()
        try:
            await self._sync_source(data)
        except Exception:
            pass
        track = await self._recommend_external(data) or self._pick(data)
        if not track:
            return "歌单是空的，先用「歌单导入」加歌。"
        line = await self._blurb(track)
        text = f"🎵 今天的歌：{track['title']}"
        if track.get("artist"):
            text += f" - {track['artist']}"
        if line:
            text += f"\n{line}"
        target = umo or str(self.config.get("push_target", "") or "").strip()
        if target:
            try:
                await self.context.send_message(target, MessageChain([Plain(text)]))
            except Exception as e:
                logger.warning(f"[推歌] 推送失败: {e}")
        data.setdefault("sent", []).append(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "title": track["title"],
                "artist": track.get("artist", ""),
            }
        )
        if len(data["sent"]) > 400:
            data["sent"] = data["sent"][-400:]
        self._save(data)
        return text


    async def _sync_source(self, data: dict) -> int:
        """重新抓取歌单页，把新增曲目并入。"""
        source = str(data.get("source") or "")
        if not source.startswith("http"):
            return 0
        try:
            fresh = await self._fetch_apple_music(source)
        except Exception:
            return 0
        if not fresh:
            return 0
        have = {(str(t.get("title") or "").strip(), str(t.get("artist") or "").strip()) for t in data.get("tracks", [])}
        added = [t for t in fresh if (t["title"], t.get("artist", "")) not in have]
        if added:
            data["tracks"].extend(added)
            data["synced_at"] = datetime.now().isoformat(timespec="seconds")
            self._save(data)
        return len(added)

    # ---------------- 外部推荐 ----------------

    def _top_artists(self, data: dict, limit: int = 6) -> list[str]:
        counts: dict[str, int] = {}
        for t in data.get("tracks", []):
            a = str(t.get("artist") or "").strip()
            if not a:
                continue
            for part in re.split(r"[,&/、]", a):
                name = part.strip()
                if name:
                    counts[name] = counts.get(name, 0) + 1
        return [k for k, _ in sorted(counts.items(), key=lambda x: -x[1])[:limit]]

    async def _search_artist_songs(self, artist: str) -> list[dict]:
        url = "https://music.apple.com/cn/search?term=" + quote(artist)
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": UA}) as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status != 200:
                        return []
                    page = await resp.text()
        except Exception:
            return []
        m = re.search(r'id="serialized-server-data"[^>]*>(.*?)</script>', page, re.S)
        if not m:
            return []
        try:
            blob = json.loads(m.group(1))
        except Exception:
            return []
        found: list[dict] = []

        def walk(o: Any) -> None:
            if isinstance(o, dict):
                title = o.get("title")
                subs = o.get("subtitleLinks")
                if isinstance(title, str) and isinstance(subs, list) and subs:
                    first = subs[0]
                    if isinstance(first, dict):
                        name = first.get("title")
                        if isinstance(name, str) and name.strip():
                            found.append({"title": title.strip(), "artist": name.strip()})
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(blob)
        return found

    async def _recommend_external(self, data: dict) -> dict | None:
        """按歌单口味去外部曲库找歌单里没有的歌。"""
        artists = self._top_artists(data)
        if not artists:
            return None
        have = {(str(t.get("title") or "").strip(), str(t.get("artist") or "").strip()) for t in data.get("tracks", [])}
        have_titles = {t[0] for t in have}
        days = int(self.config.get("history_days", 30) or 30)
        cutoff = datetime.now() - timedelta(days=days)
        recent: set[tuple[str, str]] = set()
        for item in data.get("sent", []):
            try:
                ts = datetime.fromisoformat(item.get("at", ""))
            except Exception:
                continue
            if ts >= cutoff:
                recent.add((item.get("title", ""), item.get("artist", "")))
        random.shuffle(artists)
        pool: list[dict] = []
        for artist in artists[:3]:
            songs = await self._search_artist_songs(artist)
            for song in songs:
                key = (song["title"], song["artist"])
                if song["title"] in have_titles or key in recent:
                    continue
                pool.append(song)
        if not pool:
            return None
        seen: set[tuple[str, str]] = set()
        uniq: list[dict] = []
        for song in pool:
            key = (song["title"], song["artist"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(song)
        return random.choice(uniq)

    # ---------------- 定时 ----------------

    def _next_run(self) -> datetime:
        raw = str(self.config.get("push_time", "09:00") or "09:00")
        try:
            hh, mm = [int(x) for x in raw.split(":")[:2]]
        except Exception:
            hh, mm = 9, 0
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    async def _daily_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                nxt = self._next_run()
                wait = max(30.0, (nxt - datetime.now()).total_seconds())
                logger.info(f"[推歌] 下一次推送 {nxt:%Y-%m-%d %H:%M}")
                await asyncio.sleep(wait)
                if bool(self.config.get("enabled", True)):
                    await self._push_one()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[推歌] 循环异常: {e}")
                await asyncio.sleep(60)

    # ---------------- 指令 ----------------

    @filter.command("歌单导入")
    async def cmd_import(self, event: AstrMessageEvent, url: str = ""):
        """导入歌单：/歌单导入 <Apple Music 链接>，或直接粘贴「歌名 - 歌手」多行文本"""
        raw = (url or "").strip()
        if not raw:
            raw = event.message_str.replace("歌单导入", "", 1).strip()
        if not raw:
            yield event.plain_result("用法：/歌单导入 <Apple Music 歌单链接>，或粘贴多行「歌名 - 歌手」")
            return
        if raw.startswith("http"):
            try:
                tracks = await self._fetch_apple_music(raw)
            except Exception as e:
                yield event.plain_result(f"抓取失败：{e}")
                return
            source = raw
        else:
            tracks = self._parse_text_playlist(raw)
            source = "text"
        if not tracks:
            yield event.plain_result("没解析到歌曲，检查链接或格式。")
            return
        data = self._load()
        exists = {(t.get("title", ""), t.get("artist", "")) for t in data["tracks"]}
        added = [t for t in tracks if (t["title"], t.get("artist", "")) not in exists]
        data["tracks"].extend(added)
        data["source"] = source
        data["imported_at"] = datetime.now().isoformat(timespec="seconds")
        self._save(data)
        yield event.plain_result(
            f"导入完成：解析 {len(tracks)} 首，新增 {len(added)} 首，当前共 {len(data['tracks'])} 首。"
        )

    @filter.command("歌单")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看歌单状态"""
        data = self._load()
        n = len(data.get("tracks", []))
        sent = len(data.get("sent", []))
        raw = str(self.config.get("push_time", "09:00") or "09:00")
        yield event.plain_result(
            f"歌单 {n} 首，已推送 {sent} 次，每天 {raw} 推送。"
            + ("" if n else " 用「歌单导入」先加歌。")
        )

    @filter.command("推歌")
    async def cmd_push(self, event: AstrMessageEvent):
        """立刻推一首"""
        text = await self._push_one(umo=event.unified_msg_origin)
        yield event.plain_result(text)

    @filter.command("歌单清空")
    async def cmd_clear(self, event: AstrMessageEvent):
        """清空歌单"""
        self._save({"source": "", "imported_at": "", "tracks": [], "sent": []})
        yield event.plain_result("歌单已清空。")

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()
