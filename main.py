import asyncio
import csv
import json
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.api.typing import AstrMessageEvent, MessageEventResult
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

LOG = logging.getLogger("steam_free_game")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s)
    return lines


@dataclass(frozen=True)
class SteamFreebie:
    appid: int
    title: str
    store_url: str
    image_url: str
    original_price: str
    final_price: str
    discount_percent: int
    source: str = "steam_store_search"

    @property
    def deal_key(self) -> str:
        return f"steam_app:{self.appid}"


class SteamSearchResultsHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: List[Dict[str, Any]] = []
        self._cur: Optional[Dict[str, Any]] = None
        self._capture: Optional[str] = None

    @staticmethod
    def _has_class(attrs: Dict[str, str], cls: str) -> bool:
        classes = attrs.get("class", "")
        return cls in classes.split()

    def _flush(self) -> None:
        if not self._cur:
            return
        if self._cur.get("appid"):
            self.items.append(self._cur)
        self._cur = None
        self._capture = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        a = {k: (v or "") for k, v in attrs}

        if tag == "a" and self._has_class(a, "search_result_row"):
            self._flush()
            self._cur = {
                "appid": a.get("data-ds-appid", ""),
                "href": a.get("href", ""),
                "title": "",
                "image_url": "",
                "discount_percent": None,
                "price_final": None,
                "original_price": "",
                "final_price": "",
            }
            return

        if not self._cur:
            return

        if tag == "span" and self._has_class(a, "title"):
            self._capture = "title"
            return

        if tag == "img":
            src = a.get("src") or a.get("data-src") or ""
            if src and not self._cur.get("image_url"):
                self._cur["image_url"] = src
            return

        if tag == "div":
            if self._has_class(a, "discount_block") and self._has_class(a, "search_discount_block"):
                dp = a.get("data-discount", "")
                pf = a.get("data-price-final", "")
                if dp.isdigit():
                    self._cur["discount_percent"] = int(dp)
                if pf.isdigit():
                    self._cur["price_final"] = int(pf)
                return

            if self._has_class(a, "discount_pct"):
                self._capture = "discount_pct"
                return
            if self._has_class(a, "discount_original_price"):
                self._capture = "original_price"
                return
            if self._has_class(a, "discount_final_price"):
                self._capture = "final_price"
                return

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._flush()
            return
        if tag in ("span", "div"):
            self._capture = None

    def handle_data(self, data: str) -> None:
        if not self._cur or not self._capture:
            return
        s = (data or "").strip()
        if not s:
            return
        key = self._capture
        if key == "discount_pct":
            self._cur[key] = (self._cur.get(key) or "") + s
            return
        prev = self._cur.get(key, "")
        self._cur[key] = (prev + s).strip()


class WorkflowCSVStore:
    FIELDNAMES = [
        "deal_key",
        "appid",
        "title",
        "original_price",
        "final_price",
        "discount_percent",
        "store_url",
        "image_url",
        "source",
        "first_seen_at",
        "last_seen_at",
        "notified_at",
        "notified_targets",
        "last_error",
        "status",
    ]

    def __init__(self, path: Path) -> None:
        self.path = path

    def ensure_exists(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
            writer.writeheader()
        tmp.replace(self.path)

    def load(self) -> Dict[str, Dict[str, str]]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows: Dict[str, Dict[str, str]] = {}
            for row in reader:
                deal_key = (row.get("deal_key") or "").strip()
                if not deal_key:
                    continue
                rows[deal_key] = {k: (row.get(k) or "") for k in self.FIELDNAMES}
            return rows

    def save(self, rows: Dict[str, Dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
            writer.writeheader()
            for deal_key in sorted(rows.keys()):
                row = rows[deal_key]
                writer.writerow({k: row.get(k, "") for k in self.FIELDNAMES})
        tmp.replace(self.path)

    def upsert_seen(
        self, rows: Dict[str, Dict[str, str]], deal: SteamFreebie, now_iso: str
    ) -> None:
        r = rows.get(deal.deal_key)
        if not r:
            r = {k: "" for k in self.FIELDNAMES}
            r["deal_key"] = deal.deal_key
            r["first_seen_at"] = now_iso
        r["appid"] = str(deal.appid)
        r["title"] = deal.title
        r["original_price"] = deal.original_price
        r["final_price"] = deal.final_price
        r["discount_percent"] = str(deal.discount_percent)
        r["store_url"] = deal.store_url
        r["image_url"] = deal.image_url
        r["source"] = deal.source
        r["last_seen_at"] = now_iso
        if not r.get("status"):
            r["status"] = "seen"
        rows[deal.deal_key] = r

    def mark_notified(
        self, rows: Dict[str, Dict[str, str]], deal_key: str, now_iso: str, targets: Sequence[str]
    ) -> None:
        r = rows.get(deal_key)
        if not r:
            r = {k: "" for k in self.FIELDNAMES}
            r["deal_key"] = deal_key
            r["first_seen_at"] = now_iso
        r["notified_at"] = now_iso
        r["notified_targets"] = "|".join(targets)
        r["last_error"] = ""
        r["status"] = "notified"
        rows[deal_key] = r

    def mark_error(self, rows: Dict[str, Dict[str, str]], deal_key: str, err: str) -> None:
        r = rows.get(deal_key)
        if not r:
            r = {k: "" for k in self.FIELDNAMES}
            r["deal_key"] = deal_key
        r["last_error"] = err[:500]
        if r.get("status") != "notified":
            r["status"] = "error"
        rows[deal_key] = r


class SteamFreeGamePlugin(Star):
    def __init__(self, context: Context, config: dict) -> None:
        super().__init__(context)
        self.config = config

        self._stop_event: Optional[asyncio.Event] = None
        self._check_lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._last_check_at: Optional[str] = None

    def _plugin_name(self) -> str:
        return getattr(self, "name", None) or Path(__file__).resolve().parent.name

    def _plugin_data_dir(self) -> Path:
        base = get_astrbot_data_path()
        return base / "plugin_data" / self._plugin_name()

    def _workflow_path(self) -> Path:
        mode = (self.config.get("workflow_path_mode") or "plugin_dir").strip()
        if mode == "plugin_data":
            return self._plugin_data_dir() / "workflow.csv"
        return Path(__file__).resolve().parent / "workflow.csv"

    def _subscriptions_path(self) -> Path:
        return self._plugin_data_dir() / "subscriptions.json"

    def _load_subscriptions(self) -> List[str]:
        path = self._subscriptions_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        out: List[str] = []
        for x in data:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out

    def _save_subscriptions(self, targets: Sequence[str]) -> None:
        path = self._subscriptions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(targets), ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)

    def _effective_targets(self) -> List[str]:
        targets: List[str] = []
        targets.extend(_parse_lines(self.config.get("targets_text", "")))
        if bool(self.config.get("enable_subscribe_commands", True)):
            targets.extend(self._load_subscriptions())

        dedup: List[str] = []
        seen = set()
        for t in targets:
            if t not in seen:
                seen.add(t)
                dedup.append(t)
        return dedup

    async def initialize(self) -> None:
        self._stop_event = asyncio.Event()
        self._ensure_http_session()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def terminate(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            except Exception:
                LOG.exception("loop task terminated with error")
        if self._session:
            await self._session.close()
            self._session = None

    def _ensure_http_session(self) -> None:
        if self._session and not self._session.closed:
            return

        timeout_s = int(self.config.get("request_timeout_seconds", 20))
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AstrBot steam_free_game plugin)",
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        connector: Optional[aiohttp.TCPConnector] = None
        if bool(self.config.get("force_ipv4", True)):
            connector = aiohttp.TCPConnector(family=socket.AF_INET)

        self._session = aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector)

    async def _run_loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                if bool(self.config.get("enabled", True)):
                    await self.check_and_notify()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("auto check failed")

            interval = int(self.config.get("check_interval_seconds", 1800))
            interval = max(30, interval)
            if self._stop_event is None:
                await asyncio.sleep(interval)
                continue
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                continue

    async def _http_get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_http_session()
        assert self._session is not None

        retries = int(self.config.get("request_retries", 2))
        last_err: Optional[Exception] = None
        for _ in range(max(0, retries) + 1):
            try:
                async with self._session.get(url, params=params) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except Exception as e:
                last_err = e
                await asyncio.sleep(1)
        raise RuntimeError(f"GET {url} failed: {last_err}")

    async def _fetch_freebies_from_steam_search(self) -> List[SteamFreebie]:
        url = "https://store.steampowered.com/search/results/"
        cc = (self.config.get("steam_cc") or "US").strip()
        lang = (self.config.get("steam_language") or "schinese").strip()
        page_size = int(self.config.get("page_size", 50))
        page_size = max(10, min(100, page_size))
        max_pages = int(self.config.get("max_pages", 3))
        max_pages = max(1, min(10, max_pages))

        out: List[SteamFreebie] = []
        seen_keys = set()

        for page in range(max_pages):
            start = page * page_size
            params = {
                "query": "",
                "start": start,
                "count": page_size,
                "dynamic_data": "",
                "sort_by": "_ASC",
                "snr": "1_7_7_2300_150_1",
                "specials": 1,
                "maxprice": "free",
                "infinite": 1,
                "cc": cc,
                "l": lang,
            }

            data = await self._http_get_json(url, params=params)
            html = str(data.get("results_html") or "")
            if not html.strip():
                break

            parser = SteamSearchResultsHTMLParser()
            parser.feed(html)
            for item in parser.items:
                try:
                    deal = self._item_to_deal(item)
                except Exception:
                    continue
                if deal.deal_key in seen_keys:
                    continue
                seen_keys.add(deal.deal_key)
                out.append(deal)

            total_count = int(data.get("total_count") or 0)
            if total_count and start + page_size >= total_count:
                break

        return out

    @staticmethod
    def _clean_store_url(appid: int) -> str:
        return f"https://store.steampowered.com/app/{appid}/"

    def _item_to_deal(self, item: Dict[str, Any]) -> SteamFreebie:
        appid_raw = (item.get("appid") or "").strip()
        if not appid_raw:
            raise ValueError("missing appid")
        appid_str = appid_raw.split(",")[0].strip()
        if not appid_str.isdigit():
            raise ValueError("invalid appid")
        appid = int(appid_str)

        title = (item.get("title") or "").strip()
        if not title:
            raise ValueError("missing title")

        discount_percent = item.get("discount_percent")
        if discount_percent is None:
            dp_txt = str(item.get("discount_pct") or "").strip()
            dp_txt = dp_txt.replace("%", "").replace("-", "").strip()
            discount_percent = int(dp_txt) if dp_txt.isdigit() else 0

        price_final = item.get("price_final")
        final_price = (item.get("final_price") or "").strip()
        original_price = (item.get("original_price") or "").strip()

        if int(discount_percent) != 100:
            raise ValueError("not 100%")
        if price_final is not None and int(price_final) != 0:
            raise ValueError("final not 0")
        if not original_price:
            raise ValueError("missing original price")

        href = (item.get("href") or "").strip()
        store_url = self._clean_store_url(appid) if appid else href
        image_url = (item.get("image_url") or "").strip()

        return SteamFreebie(
            appid=appid,
            title=title,
            store_url=store_url,
            image_url=image_url,
            original_price=original_price,
            final_price=final_price or "Free",
            discount_percent=int(discount_percent),
        )

    async def check_and_notify(self) -> None:
        async with self._check_lock:
            now_iso = _utc_now_iso()
            self._last_check_at = now_iso

            deals = await self._fetch_freebies_from_steam_search()
            if not deals:
                return

            workflow = WorkflowCSVStore(self._workflow_path())
            workflow.ensure_exists()
            rows = workflow.load()

            targets = self._effective_targets()

            for deal in deals:
                workflow.upsert_seen(rows, deal, now_iso)
                notified_at = (rows.get(deal.deal_key, {}).get("notified_at") or "").strip()
                if notified_at:
                    continue
                if not targets:
                    continue

                ok_targets: List[str] = []
                for t in targets:
                    try:
                        await self._send_deal(t, deal)
                        ok_targets.append(t)
                    except Exception as e:
                        workflow.mark_error(rows, deal.deal_key, f"send to {t} failed: {e}")
                if ok_targets:
                    workflow.mark_notified(rows, deal.deal_key, now_iso, ok_targets)

            workflow.save(rows)

    async def _send_deal(self, unified_msg_origin: str, deal: SteamFreebie) -> None:
        lines = [
            f"【Steam 限时免费】{deal.title}",
            f"原价：{deal.original_price}",
            f"现价：{deal.final_price}",
            f"购买链接：{deal.store_url}",
        ]
        chain: List[Any] = [Comp.Plain("\n".join(lines))]
        if bool(self.config.get("include_image", True)) and deal.image_url:
            chain.append(Comp.Image.fromURL(deal.image_url))
        await self.context.send_message(unified_msg_origin, chain)

    @filter.command("steamfree_check")
    async def cmd_check(self, event: AstrMessageEvent) -> MessageEventResult:
        await self.check_and_notify()
        return event.plain_result("已触发检查。")

    @filter.command("steamfree_status")
    async def cmd_status(self, event: AstrMessageEvent) -> MessageEventResult:
        targets = self._effective_targets()
        workflow_path = str(self._workflow_path())
        last_check = self._last_check_at or "N/A"
        enabled = bool(self.config.get("enabled", True))
        interval = int(self.config.get("check_interval_seconds", 1800))
        return event.plain_result(
            "\n".join(
                [
                    f"enabled={enabled}",
                    f"check_interval_seconds={interval}",
                    f"targets={len(targets)}",
                    f"workflow={workflow_path}",
                    f"last_check_at={last_check}",
                ]
            )
        )

    @filter.command("steamfree_sub")
    async def cmd_subscribe(self, event: AstrMessageEvent) -> MessageEventResult:
        if not bool(self.config.get("enable_subscribe_commands", True)):
            return event.plain_result("订阅功能未启用（enable_subscribe_commands=false）。")

        target = (event.unified_msg_origin or "").strip()
        if not target:
            return event.plain_result("无法获取当前会话 unified_msg_origin。")

        subs = self._load_subscriptions()
        if target in subs:
            return event.plain_result("当前会话已在白名单中。")
        subs.append(target)
        self._save_subscriptions(subs)
        return event.plain_result(f"已加入推送白名单：{target}")

    @filter.command("steamfree_unsub")
    async def cmd_unsubscribe(self, event: AstrMessageEvent) -> MessageEventResult:
        if not bool(self.config.get("enable_subscribe_commands", True)):
            return event.plain_result("订阅功能未启用（enable_subscribe_commands=false）。")

        target = (event.unified_msg_origin or "").strip()
        if not target:
            return event.plain_result("无法获取当前会话 unified_msg_origin。")

        subs = self._load_subscriptions()
        if target not in subs:
            return event.plain_result("当前会话不在白名单中。")
        subs = [x for x in subs if x != target]
        self._save_subscriptions(subs)
        return event.plain_result(f"已移出推送白名单：{target}")

