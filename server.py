#!/usr/bin/env python3
"""Local LINE Rangers community monitor. Run: python3 server.py"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import requests

SKILL = Path.home() / ".codex/skills/user/insane-search"
if SKILL.exists():
    sys.path.insert(0, str(SKILL))
    from engine import fetch  # noqa: E402
else:
    fetch = None  # GitHub Actions uses public HTTP and Chromium directly.

HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / "latest-cache.json"
KST = timezone(timedelta(hours=9))
TTL_SECONDS = 15 * 60
WINDOW_DAYS = 180
BAHAMUT = "https://m.gamer.com.tw/forum/B.php?bsn=25967"
PANTIP = "https://pantip.com/search?q=LINE%20Rangers&timebias=true"
SPGAME = "https://app-spgame.net/user_review/detail/?gid=1354&order=0"
NAVER = "https://cafe.naver.com/linerangers1004"
FACEBOOK_TH = "https://www.facebook.com/groups/1216791171685249/"
FACEBOOK_TW = "https://www.facebook.com/groups/385613083508950/"
X_JP = "https://x.com/search?q=LINE%E3%83%AC%E3%83%B3%E3%82%B8%E3%83%A3%E3%83%BC&f=live"
SOURCE_URLS = {"th": PANTIP, "tw": BAHAMUT, "jp": SPGAME, "kr": NAVER}
SOURCE_DEFS = {
    "pantip-search": ("th", PANTIP), "facebook-th": ("th", FACEBOOK_TH),
    "bahamut": ("tw", BAHAMUT), "facebook-tw": ("tw", FACEBOOK_TW),
    "spgame": ("jp", SPGAME), "x-jp": ("jp", X_JP),
    "naver-kr": ("kr", NAVER),
}

# Each category has conservative topic triggers and human-readable signals.
# Matching a topic alone never determines sentiment.
TOPIC_RULES = {
    "collab": [
        ("콜라보·이벤트", r"콜라보|이벤트|복각|合作|聯名|活動|イベント|コラボ|event|collab|กิจกรรม|อีเวนต์|คอลแลบ"),
    ],
    "gacha": [
        ("가챠·상품", r"가챠|뽑기|확률|천장|패키지|현질|과금|루비|ガチャ|課金|確率|天井|ルビー|抽|轉蛋|機率|課金|禮包|儲值|付費|ราคา|เติม|สุ่ม|กาชา|แพค|แพ็ก|รูบี|ruby|gacha|gem|purchas"),
    ],
    "meta": [
        ("PvP·메타", r"pvp|PVP|밸런스|장비|캐릭|덱|육성|競技|對戰|公會戰|角色|裝備|隊伍|練角|装備|キャラ|バランス|スキル|อุปกรณ์|สกิล|สมดุล"),
    ],
    "return": [
        ("신규·복귀", r"초보|뉴비|신규|복귀|新手|回鍋|入門|初心者|復帰|育成|新手|มือใหม่|กลับมาเล่น|復活|starter|beginner|return"),
    ],
    "quality": [
        ("품질·CS", r"환불|정지|접속|로그인|팅기|튕기|오류|버그|문의|업데이트.{0,20}(안|못|튕)|閃退|無法(?:玩|登入|連線|進入|使用)|不能登入|連線|異常|BUG|バグ|ログイン|起動|ロード|不具合|落ちる|接続|サポート|入れない|開かない|開けない|固まる|広告|เข้าไม่ได้|เข้า\s*ID|เข้า.{0,20}ไม่ได้|ล็อคอิน|ล็อกอิน|โดนหัก|ไม่ได้ของ|โหลดนาน|อัพเดต.{0,20}(นาน|ไม่ได้)|โกง|ชำระ|ติดต่อ|error|crash|bug|login|support|refund"),
    ],
    "social": [
        ("길드·소셜", r"길드|친구|모집|公會|好友|招募|朋友|ギルド|友達|フレンド|กิลด์|หากิล|เพื่อน|guild|friend|clan"),
    ],
}
NEGATIVE = {
    "th": [(r"โกง|หลอก|ฟ้อง|แบนเกม|เสียความรู้สึก", 3), (r"ไม่ได้|เข้าไม่ได้|หาย|เลิกเล่น|แย่|แพง|บั๊ก|บัค|ผิดหวัง|ไม่ตอบ|ล่ม|ตังค์โดนหัก", 2)],
    "tw": [(r"閃退|無法|不能|故障|異常|騙|詐|退坑|不想玩|垃圾|坑錢|一隻都沒中", 3), (r"失望|太貴|糟|很爛|不值|連線問題|卡住|壞|難玩|沒用|太瞎", 2)],
    "jp": [(r"バグ|起動しない|ログインできない|入れない|詐欺|最悪|ゴミ|課金圧", 3), (r"つまらない|高すぎ|不具合|重い|長すぎ|不満|苦痛|ストレス|やめとけ|遊びづらい|確率低|心配", 2)],
    "kr": [(r"환불|정지당|접속.{0,15}안|로그인.{0,15}안|계속\s*팅기|계속\s*튕기|사기|망겜", 3), (r"불편|오류|버그|너무 비싸|안 됩니다|안되나요|문제", 2)],
}
POSITIVE = {
    "th": [(r"สนุก|ชอบ|ดีมาก|คุ้ม|ขอบคุณ|ยอดเยี่ยม", 2)],
    "tw": [(r"好玩|很棒|不錯|值得|佛心", 2)],
    "jp": [(r"楽しい|面白い|最高|良い|好き|嬉しい|ありがとう|遊びやすい|取れちゃいました", 2)],
    "kr": [(r"재밌|재미있|좋아요|만족|최고|고맙|감사합니다", 2)],
}
ISSUES = {
    "collab": [("콜라보 캐릭터·보상", r"コラボ|合作|聯名|คอลแลบ|配布|報酬|イベント|活動")],
    "gacha": [("확률·뽑기", r"確率|機率|ガチャ|抽|สุ่ม"), ("과금·가격", r"課金|儲值|付費|เติม|ราคา|แพค|パック")],
    "meta": [("캐릭터·장비", r"裝備|角色|隊伍|レンジャー|装備|キャラ|ตัวละคร|ทีม"), ("PvP·밸런스", r"pvp|PVP|バランス|對戰|競技")],
    "return": [("신규 육성", r"新手|入門|初心者|育成|มือใหม่"), ("복귀 적응", r"回鍋|復帰|กลับมาเล่น")],
    "quality": [("접속·로그인", r"ログイン|起動|連線|登入|เข้าไม่ได้|ล็อคอิน|ล็อกอิน|login"), ("오류·튕김", r"バグ|閃退|異常|不具合|crash|bug"), ("결제·지원", r"サポート|ชำระ|โดนหัก|ไม่ได้ของ|ติดต่อ|support")],
    "social": [("길드 모집", r"公會|招募|ギルド|กิลด์|guild"), ("친구·협동", r"好友|朋友|友達|フレンド|เพื่อน|friend")],
}
_lock = threading.Lock()
try:
    _cache: dict = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
except (OSError, ValueError):
    _cache = {"updated_at": None, "sources": {}, "posts": []}
_refreshing = False


def now() -> datetime:
    return datetime.now(KST)


def clean(value: str, limit: int = 700) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def page(url: str) -> BeautifulSoup:
    if fetch:
        result = fetch(url, timeout=20)
        if not result.ok and result.stop_reason == "rate_limited":
            time.sleep(8)
            result = fetch(url, timeout=20, device_class="mobile")
        if not result.ok and result.stop_reason == "rate_limited":
            time.sleep(15)
            result = fetch(url, timeout=20, device_class="desktop")
        if not result.ok:
            raise RuntimeError(f"수집 실패: {result.summary}")
        return BeautifulSoup(result.content, "html.parser")
    response = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0 (compatible; RangersSignalDesk/1.0)"})
    response.raise_for_status()
    if len(response.content) < 500:
        raise RuntimeError("본문이 비어 있습니다")
    return BeautifulSoup(response.content, "html.parser")


def parse_thai_date(value: str) -> str | None:
    months = {"ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4, "พ.ค.": 5, "มิ.ย.": 6, "ก.ค.": 7, "ส.ค.": 8, "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12}
    m = re.search(r"(\d{1,2})\s+([^\s]+)\s+(\d{2,4})", value)
    if not m or m.group(2) not in months:
        return None
    year = int(m.group(3))
    year = 2000 + year - 43 if year < 100 else year - 543 if year > 2400 else year
    try:
        return datetime(year, months[m.group(2)], int(m.group(1)), tzinfo=KST).isoformat()
    except ValueError:
        return None


def collect_th() -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            tab = browser.new_page(locale="th-TH", timezone_id="Asia/Bangkok")
            tab.goto(PANTIP, wait_until="domcontentloaded", timeout=35000)
            tab.locator("li.pt-list-item").first.wait_for(timeout=12000)
            soup = BeautifulSoup(tab.content(), "html.parser")
        finally:
            browser.close()
    posts = []
    for item in soup.select("li.pt-list-item")[:30]:
        link = item.select_one("h2 a[href*='/topic/']")
        if not link:
            continue
        title = clean(link.get_text(" ", strip=True), 180)
        body = clean(item.select_one(".pt-list-item__sr__content").get_text(" ", strip=True) if item.select_one(".pt-list-item__sr__content") else "")
        if not re.search(r"line\s*rangers?|ไลน์\s*เรนเจอร์", title + " " + body, re.I):
            continue
        date = item.select_one(".pt-sm-toggle-date-hide")
        posts.append({"id": "th:" + link["href"].rstrip("/").split("/")[-1], "country": "th", "source": "Pantip", "url": link["href"], "title": title, "text": body, "published_at": parse_thai_date(date.get_text(" ", strip=True)) if date else None, "rating": None})
    return posts


def collect_tw() -> list[dict]:
    # The desktop board intermittently returns 429; its mobile board renders
    # the same public threads through a local browser session.
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            tab = browser.new_page(locale="zh-TW", timezone_id="Asia/Taipei")
            tab.goto(BAHAMUT, wait_until="domcontentloaded", timeout=30000)
            tab.locator("a[href*='C.php'] h4").first.wait_for(timeout=10000)
            soup = BeautifulSoup(tab.content(), "html.parser")
        finally:
            browser.close()
    candidates = []
    for link in soup.select("a[href*='C.php']"):
        if "bsn=25967" not in link.get("href", ""):
            continue
        title = link.select_one("h4")
        if not link or not title:
            continue
        date = link.select_one(".blist2 span")
        dm = re.search(r"(\d{1,2})-(\d{1,2})", date.get_text(" ", strip=True)) if date else None
        pub = None
        if dm:
            yr = now().year - (int(dm.group(1)) > now().month)
            pub = datetime(yr, int(dm.group(1)), int(dm.group(2)), tzinfo=KST).isoformat()
        url = "https://m.gamer.com.tw/forum/" + link["href"]
        ident = re.search(r"snA=(\d+)", url)
        if not ident:
            continue
        kind = link.select_one(".blist1")
        label = clean(kind.get_text(" ", strip=True), 12) if kind else ""
        label = re.sub(r"\d+$", "", label).strip()
        titled = f"【{label}】{title.get_text(' ', strip=True)}" if label else title.get_text(" ", strip=True)
        candidates.append({"id": "tw:" + ident.group(1), "country": "tw", "source": "巴哈姆特", "url": url, "title": clean(titled, 180), "text": "", "published_at": pub, "rating": None})
    cutoff = now() - timedelta(days=WINDOW_DAYS)
    unique = {x["id"]: x for x in candidates if x["published_at"] and datetime.fromisoformat(x["published_at"]) >= cutoff}
    candidates = list(unique.values())[:18]
    def detail(post: dict) -> dict:
        try:
            content = page(post["url"]).select_one("article.cbox_txt")
            if content:
                post["text"] = clean(content.get_text(" ", strip=True), 1500)
        except Exception:
            pass  # The board title remains available as evidence.
        return post
    with ThreadPoolExecutor(max_workers=2) as pool:
        detailed = list(pool.map(detail, candidates[:12]))
    return detailed + candidates[12:]


def collect_jp() -> list[dict]:
    soup = page(SPGAME)
    posts = []
    for i, item in enumerate(soup.select(".rev_list_item")[:20]):
        title = item.select_one(".rev_item_title")
        body = item.select_one(".rev_item_content")
        date = item.select_one(".rev_item_time")
        rid = item.select_one(".rev_item_id")
        if not title or not date:
            continue
        dm = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", date.get_text(" ", strip=True))
        if not dm:
            continue
        rating = len(item.select(".rev_item_point i.fas.fa-star"))
        ident = re.search(r"\d+", rid.get_text(" ", strip=True)) if rid else None
        posts.append({"id": "jp:" + (ident.group() if ident else str(i)), "country": "jp", "source": "エスピーゲーム", "url": SPGAME + "#rev_order_frame", "title": clean(title.get_text(" ", strip=True), 180), "text": clean(body.get_text(" ", strip=True), 1500) if body else "", "published_at": datetime.strptime(dm.group(), "%Y-%m-%d %H:%M").replace(tzinfo=KST).isoformat(), "rating": rating or None})
    return posts


def parse_naver_date(value: str) -> str | None:
    value = clean(value)
    dated = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", value)
    if dated:
        try:
            return datetime(*map(int, dated.groups()), tzinfo=KST).isoformat()
        except ValueError:
            return None
    relative = re.search(r"(\d+)\s*(분|시간|일|주|개월|달)\s*전", value)
    if not relative:
        return now().isoformat() if value in {"방금", "어제", "오늘"} else None
    amount, unit = int(relative.group(1)), relative.group(2)
    days = amount * 30 if unit in {"개월", "달"} else amount * 7 if unit == "주" else amount if unit == "일" else 0
    return (now() - timedelta(days=days, hours=amount if unit == "시간" else 0, minutes=amount if unit == "분" else 0)).isoformat()


def collect_kr() -> list[dict]:
    # Naver's public cafe search exposes titles and snippets. These are not
    # full article bodies, and private cafe pages may still require login.
    posts = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            tab = browser.new_page(locale="ko-KR", timezone_id="Asia/Seoul")
            for query in ["라인레인저스", "라레", "라인 레인저스"]:
                url = "https://search.naver.com/search.naver?ssc=tab.cafe.all&query=" + quote(query)
                tab.goto(url, wait_until="domcontentloaded", timeout=30000)
                soup = BeautifulSoup(tab.content(), "html.parser")
                for item in soup.select("li.bx._bx"):
                    link = item.select_one(".title_area a[href*='cafe.naver.com/linerangers1004/']")
                    if not link:
                        continue
                    match = re.search(r"/linerangers1004/(\d+)", link.get("href", ""))
                    if not match:
                        continue
                    title = clean(link.get_text(" ", strip=True), 180)
                    snippet = item.select_one(".dsc_area .dsc_link")
                    text = clean(snippet.get_text(" ", strip=True), 900) if snippet else ""
                    date = item.select_one(".user_info .sub")
                    published = parse_naver_date(date.get_text(" ", strip=True)) if date else None
                    if not published:
                        continue
                    posts[match.group(1)] = {"id": "kr:" + match.group(1), "country": "kr", "source": "네이버 카페", "source_id": "naver-kr", "url": f"{NAVER}/{match.group(1)}", "title": title, "text": text, "published_at": published, "rating": None, "snippet_only": True, "date_approximate": bool(date and "전" in date.get_text(" ", strip=True))}
        finally:
            browser.close()
    return list(posts.values())


def yahoo_results(query: str) -> list[dict]:
    """Public search results used only to discover individual social posts."""
    response = requests.get("https://search.yahoo.co.jp/search", params={"p": query}, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    if not soup.title or "Yahoo" not in soup.title.get_text():
        raise RuntimeError("검색 결과를 확인할 수 없습니다")
    results = []
    for item in soup.select("ol li"):
        link = item.select_one("a[href^='https://']")
        if not link:
            continue
        date = item.select_one(".util-Text--sub")
        detail = link.find_next_sibling("div")
        results.append({"url": link.get("href", ""), "title": clean(link.get_text(" ", strip=True), 180), "snippet": clean(detail.get_text(" ", strip=True), 700) if detail else "", "date_label": clean(date.get_text(" ", strip=True)) if date else ""})
    return results


def parse_yahoo_date(label: str) -> str | None:
    dated = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", label)
    if dated:
        try:
            return datetime(*map(int, dated.groups()), tzinfo=KST).isoformat()
        except ValueError:
            return None
    relative = re.search(r"(\d+)\s*(日|時間|分)前", label)
    if not relative:
        return None
    amount, unit = int(relative.group(1)), relative.group(2)
    delta = timedelta(days=amount) if unit == "日" else timedelta(hours=amount) if unit == "時間" else timedelta(minutes=amount)
    return (now() - delta).isoformat()


def collect_facebook(country: str, group_id: str) -> list[dict]:
    terms = (["LINE Rangers", "2026 กิลด์", "2026 มือใหม่", "2026 อัพเดต", "2026 กาชา"] if country == "th"
             else ["銀河特攻隊", "2026 合作", "2026 角色", "2026 閃退", "2026 抽"])
    candidates = {}
    for term in terms:
        for item in yahoo_results(f"site:facebook.com/groups/{group_id}/posts/ {term}"):
            match = re.search(rf"facebook\.com/groups/{group_id}/posts/(\d+)", item["url"])
            published = parse_yahoo_date(item["date_label"])
            if match and published and datetime.fromisoformat(published) >= now() - timedelta(days=WINDOW_DAYS):
                candidates[match.group(1)] = {**item, "published_at": published, "id_number": match.group(1)}
    def detail(item: dict) -> dict | None:
        url = f"https://www.facebook.com/groups/{group_id}/posts/{item['id_number']}/"
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            desc = soup.find("meta", property="og:description")
            body = clean(desc.get("content", ""), 1200) if desc else ""
        except Exception:
            body = ""
        title = clean(re.sub(r"\s*- Facebook$", "", item["title"]), 180)
        content = title + " " + body
        # Account trading is common in these groups and is not game feedback.
        if re.search(r"ขาย|รับซื้อ|ซื้อID|ซื้อไอดี|ราคาร้อน|GG GACHA|^\d+\s*โดน|ไอดีเกม|บัญชีขาย|售|帳號|账号|賣帳|透過中介|交易平台|不連接|ไม่เชื่อม|ยังไม่เชื่อม", content, re.I):
            return None
        if len(title) < 4 or not body and len(title) < 12:
            return None
        return {"id": f"{country}:fb:{item['id_number']}", "country": country, "source": "Facebook 그룹", "source_id": f"facebook-{country}", "url": url, "title": title, "text": body or item["snippet"], "published_at": item["published_at"], "rating": None, "snippet_only": True, "date_approximate": True}
    with ThreadPoolExecutor(max_workers=4) as pool:
        posts = [post for post in pool.map(detail, list(candidates.values())[:24]) if post]
    return posts


def collect_fb_th() -> list[dict]:
    return collect_facebook("th", "1216791171685249")


def collect_fb_tw() -> list[dict]:
    return collect_facebook("tw", "385613083508950")


def collect_x_jp() -> list[dict]:
    queries = [
        "site:x.com/ LINEレンジャー バグ 2026 -LINE_Rangers_JP",
        "site:x.com/ LINEレンジャー PVP 2026 -LINE_Rangers_JP",
        "site:x.com/ LINEレンジャー ギルド 2026 -LINE_Rangers_JP",
        "site:x.com/ LINEレンジャー ガチャ 2026 -LINE_Rangers_JP",
        "site:x.com/BED2_2_Ranger/status/ LINEレンジャー",
        "site:x.com/Dnkz15/status/ LINEレンジャー",
        "site:x.com/taro_channel777/status/ LINEレンジャー",
    ]
    candidates = {}
    for query in queries:
        for item in yahoo_results(query):
            match = re.search(r"x\.com/([^/]+)/status/(\d+)", item["url"])
            if match and match.group(1).lower() not in {"line_rangers_jp", "linegame_japan", "line_omaren_jp", "gamewith_review"}:
                candidates[match.group(2)] = match.group(1)
    def detail(pair: tuple[str, str]) -> dict | None:
        ident, handle = pair
        if handle.lower() in {"line_rangers_jp", "linegame_japan", "line_omaren_jp", "gamewith_review"}:
            return None
        try:
            response = requests.get(f"https://cdn.syndication.twimg.com/tweet-result?id={ident}&token=a", timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            data = response.json()
            body = clean(data.get("text", ""), 1400)
            if not re.search(r"LINEレンジャー|ラインレンジャー", body, re.I) or re.search(r"LINE\s*おまかせ\s*レンジャー", body):
                return None
            author = data.get("user", {}).get("screen_name", handle)
            if author.lower() in {"line_rangers_jp", "linegame_japan", "line_omaren_jp", "gamewith_review"}:
                return None
            published = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00")).astimezone(KST).isoformat()
            if datetime.fromisoformat(published) < now() - timedelta(days=WINDOW_DAYS):
                return None
            title = clean(re.sub(r"https://t\.co/\S+", "", body).split("\n")[0], 150)
            return {"id": "jp:x:" + ident, "country": "jp", "source": "X", "source_id": "x-jp", "url": f"https://x.com/{author}/status/{ident}", "title": title or "LINE Rangers 관련 글", "text": body, "published_at": published, "rating": None}
        except (requests.RequestException, ValueError, KeyError):
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [post for post in pool.map(detail, list(candidates.items())[:50]) if post]


def classify(post: dict) -> dict:
    country = post["country"]
    post.setdefault("source_id", {"th": "pantip-search", "tw": "bahamut", "jp": "spgame", "kr": "naver-kr"}[country])
    content = post["title"] + " " + post["text"]
    matched = [key for key, rules in TOPIC_RULES.items() if any(re.search(pattern, content, re.I) for _, pattern in rules)]
    # A title or body with no issue terms is kept as source evidence, not inserted into a category.
    if post["rating"]:
        sentiment = "positive" if post["rating"] >= 4 else "negative" if post["rating"] <= 2 else "neutral"
        basis = f"{post['rating']}점 리뷰"
    elif country == "tw" and re.search(r"【情報】|招生|招募|求個好友|公會.*收人", post["title"]):
        sentiment = "neutral"
        basis = "정보·모집글은 평가에서 제외"
    else:
        negative = sum(weight for pattern, weight in NEGATIVE[country] if re.search(pattern, content, re.I))
        positive = sum(weight for pattern, weight in POSITIVE[country] if re.search(pattern, content, re.I))
        if country == "tw" and "【問題】" in post["title"]:
            positive = 0  # A help request is not praise for the game.
        sentiment = "negative" if negative > positive and negative >= 2 else "positive" if positive > negative and positive >= 2 else "mixed" if negative and positive else "neutral"
        basis = ("공개 미리보기 표현 사전" if post.get("snippet_only") else "표현 사전") if sentiment != "neutral" else "명확한 평가 표현 없음"
    post["categories"] = matched
    post["sentiment"] = sentiment
    post["basis"] = basis
    # A store star rates the whole game; the wording around an issue takes
    # precedence when assigning a category-specific dot.
    post["category_sentiments"] = {key: scoped_sentiment(post, key, sentiment) for key in matched}
    post["mentions"] = {key: mention_summary(post, key) for key in matched}
    return post


def scoped_sentiment(post: dict, category: str, overall: str) -> str:
    if post["country"] != "jp" or not post["rating"]:
        return overall
    content = post["title"] + " " + post["text"]
    negative_by_category = {
        "quality": r"バグ|ログインできない|入れない|開かない|開けない|起動しない|固ま|落ちる|ロード長い|遊びづらい|押せない|受け取れません",
        "gacha": r"ガチャ.{0,100}(廃止|無駄|不可能)|天井引き下げ|確率の引き上げ|課金要求",
        "meta": r"パワーバランスがおかしい|敵が強すぎ|PVP.{0,80}低ランク",
        "return": r"初心者脱出が不可能|初心者.{0,50}不可能",
        "collab": r"コラボ.{0,50}(不満|最悪|つまらない)",
        "social": r"ギルド.{0,50}(不満|最悪)",
    }
    positive_by_category = {
        "collab": r"コラボ.{0,30}(楽しい|最高|好き)",
        "meta": r"PVP.{0,30}(楽しい|面白い)",
        "gacha": r"ガチャ.{0,30}(満足|嬉しい)",
    }
    neg = bool(re.search(negative_by_category.get(category, r"(?!)"), content, re.I))
    pos = bool(re.search(positive_by_category.get(category, r"(?!)"), content, re.I))
    if neg and pos:
        return "mixed"
    if neg:
        return "negative"
    if pos:
        return "positive"
    if any(re.search(pattern, post["title"], re.I) for _, pattern in TOPIC_RULES[category]):
        return overall
    return "neutral"


def mention_summary(post: dict, category: str) -> str:
    """Short Korean paraphrase of a matched mention; never presents it as a quote."""
    content = post["title"] + " " + post["text"]
    rules = {
        "quality": [
            (r"流れ星が降ってこないバグ", "유성 효과가 나타나지 않는 버그를 제보"),
            (r"更新後.{0,40}閃退|完全玩不了.{0,40}閃退", "업데이트 후 플레이 중 여러 번 튕겨 게임을 하기 어렵다고 호소"),
            (r"โหลดนานหลังอัพเดต|โหลดนาน", "업데이트 후 로딩이 길어졌다고 질문"),
            (r"환불", "결제 환불 처리와 게임 계정 조치에 관해 도움을 요청"),
            (r"업데이트.{0,35}(팅기|튕기)|계속\s*(팅기|튕기)", "업데이트 후 게임이 계속 종료된다고 제보"),
            (r"라인 계정 문의", "LINE 계정 연동·이용 문제를 문의"),
            (r"접속|로그인", "접속·로그인 문제와 해결 방법을 언급"),
            (r"โดนหัก|ไม่ได้ของ", "결제 금액이 빠졌지만 상품을 받지 못했고 고객지원 답변도 없다고 호소"),
            (r"เข้า\s*ID|เข้า.{0,20}ไม่ได้", "기존 계정으로 로그인하지 못해 해결 방법을 질문"),
            (r"閃退修好了.{0,30}無法連線", "튕김 해결 뒤 접속 불가가 생겼다고 지적"),
            (r"閃退|登入畫面", "앱이 튕기거나 로그인 화면에서 멈춘다고 언급"),
            (r"官方目前對閃退|官方目前進度", "튕김 장애에 대한 운영진 답변을 공유"),
            (r"広告.{0,50}押せない|報酬を受け取れません", "광고 닫기 버튼이 눌리지 않아 보상을 받지 못한다고 지적"),
            (r"広告.{0,50}固ま|広告閲覧後", "광고 시청 뒤 앱이 멈추거나 종료된다고 지적"),
            (r"ログイン|入れない|起動しない|開かない|開けない", "로그인·실행이 되지 않는다고 호소"),
            (r"ロード長い|遊びづらい|合成に時間", "합성·반복 작업과 긴 로딩으로 플레이가 불편하다고 지적"),
        ],
        "gacha": [
            (r"ガチャチケ1発.{0,35}取れちゃいました", "가챠 티켓 한 장으로 원하는 캐릭터를 얻었다고 기뻐함"),
            (r"確率低かった|★9.{0,50}確率", "★9 장비 출시 후 뽑기 확률을 걱정"),
            (r"五十幾抽.{0,25}一隻都沒中", "50여 회 뽑았지만 원하는 캐릭터를 얻지 못해 확률에 불만"),
            (r"มือใหม่.{0,40}กาชา|มือใหม่.{0,40}สุ่ม", "초보 유저가 가챠에 재화를 쓸지 조언을 요청"),
            (r"패키지|현질", "유료 패키지 구매·환불 경험을 언급"),
            (r"가챠|뽑기|확률|천장", "뽑기 결과나 확률·비용을 언급"),
            (r"ガチャから星7|天井引き下げ|ピックアップ確率", "★7 가챠 결과와 천장·픽업 확률 개선을 요구"),
            (r"ルビー配布", "루비 지급을 조금 더 늘려 달라고 요청"),
            (r"新手.{0,25}抽活動裝備", "신규 유저가 콜라보 장비를 뽑을지 조언을 요청"),
            (r"抽到兩隻|兩隻蘿蔔", "중복 캐릭터를 합칠지 따로 키울지 질문"),
            (r"โดนหัก|ไม่ได้ของ", "유료 패키지를 샀지만 게임 내 상품을 못 받았다고 주장"),
            (r"出現機率提升|機率提升", "콜라보 캐릭터 등장 확률 상승 정보를 공유"),
        ],
        "collab": [
            (r"次のコラボは.{0,25}怪獣8号", "다음 콜라보가 ‘괴수 8호’일지 추측"),
            (r"合作角都沒有復刻", "콜라보 캐릭터 복각이 없는 이유를 질문"),
            (r"콜라보|복각|이벤트", "콜라보·이벤트 정보나 참여 경험을 언급"),
            (r"コラボキャラが恒常より|コラボ以外", "콜라보 캐릭터와 기존 캐릭터의 성능 격차, 콜라보 외 콘텐츠의 매력을 평가"),
            (r"CODE GEASS|反叛的魯路修|反叛者", "코드 기어스 콜라보와 신규 캐릭터 정보를 공유"),
            (r"活動裝備|合作.*裝備", "콜라보 장비를 뽑을지 질문"),
        ],
        "meta": [
            (r"パワーバランスがおかしい|PVP.{0,80}低ランク", "PvP 성능 격차와 강한 적 때문에 낮은 랭크에 머문다고 지적"),
            (r"PVP", "PvP에서 쓸 캐릭터 성능과 선택을 논의"),
            (r"장비|캐릭|덱|육성|밸런스", "캐릭터·장비·덱 구성이나 육성 방향을 논의"),
            (r"隊伍怎麼組|練角|優先練誰", "어떤 캐릭터를 키우고 팀을 꾸릴지 조언을 요청"),
            (r"裝備|角色|キャラ|อุปกรณ์", "캐릭터·장비 선택이나 성능을 논의"),
        ],
        "return": [
            (r"초보|뉴비|신규|복귀", "신규·복귀 유저의 게임 적응과 육성을 언급"),
            (r"初心者脱出が不可能", "가챠와 육성 부담 때문에 초보 단계를 벗어나기 어렵다고 지적"),
            (r"新手|入門|初心者", "신규 유저의 장비·캐릭터 육성 방법을 질문"),
            (r"回鍋|復帰|กลับมาเล่น", "복귀 후 육성 방향을 질문"),
        ],
        "social": [
            (r"หากิล|กิลด์.*รับ|รับคนเข้า", "길드 가입처를 찾거나 신규 길드원을 모집"),
            (r"길드|친구|모집", "길드·친구 모집이나 교류를 언급"),
            (r"招生|招募|收人|ギルド|กิลด์", "길드 가입자를 모집하고 참여 조건을 안내"),
            (r"求個好友|好友|友達|เพื่อน", "게임 친구를 찾거나 교류를 요청"),
        ],
    }
    for pattern, summary in rules.get(category, []):
        if re.search(pattern, content, re.I):
            return summary
    fallback = {
        "collab": "콜라보·이벤트에 관한 정보나 의견을 언급",
        "gacha": "가챠·상품에 관한 질문이나 의견을 언급",
        "meta": "캐릭터·장비·PvP에 관한 질문이나 의견을 언급",
        "return": "신규·복귀 유저의 육성에 관한 질문이나 의견을 언급",
        "quality": "접속·오류·고객지원에 관한 경험을 언급",
        "social": "길드·친구 등 소셜 활동을 언급",
    }
    return fallback[category]


def build_summary(posts: list[dict], sources: dict) -> dict:
    cutoff = now() - timedelta(days=WINDOW_DAYS)
    recent = [classify(p) for p in posts if p["published_at"] and datetime.fromisoformat(p["published_at"]) >= cutoff]
    recent.sort(key=lambda p: p["published_at"], reverse=True)
    categories = {}
    for country in ["th", "tw", "jp", "kr"]:
        categories[country] = {}
        for category in TOPIC_RULES:
            matching = [p for p in recent if p["country"] == country and category in p["categories"]]
            counts = Counter(p["category_sentiments"].get(category, "neutral") for p in matching)
            judged = counts["positive"] + counts["negative"]
            if not matching or judged < 2:
                status = "insufficient"
            elif counts["positive"] > counts["negative"]:
                status = "positive"
            elif counts["negative"] > counts["positive"]:
                status = "negative"
            else:
                status = "mixed"
            signals = Counter(label for p in matching for label, pattern in ISSUES[category] if re.search(pattern, p["title"] + " " + p["text"], re.I))
            top_signals = [label for label, _ in signals.most_common(2)]
            if not matching:
                summary = "최근 180일 표본이 없습니다."
            elif status == "insufficient":
                issue = " · ".join(top_signals) if top_signals else "주제 관련 질문·후기"
                summary = f"관련 글 {len(matching)}건에서 {issue} 언급. 명확한 긍·부정 {judged}건으로 방향 판단을 보류합니다."
            else:
                direction = "부정이 우세" if status == "negative" else "긍정이 우세" if status == "positive" else "긍정·부정이 비슷함"
                summary = f"{direction}. " + ("주요 언급: " + "·".join(top_signals) + ". " if top_signals else "") + f"긍정 {counts['positive']} · 부정 {counts['negative']}건."
            categories[country][category] = {"status": status, "summary": summary, "total": len(matching), "counts": {k: counts[k] for k in ["positive", "negative", "mixed", "neutral"]}, "evidence": [{"title": p["title"], "mention": mention_summary(p, category), "url": p["url"], "published_at": p["published_at"], "sentiment": p["category_sentiments"].get(category, "neutral"), "basis": p["basis"] if p["category_sentiments"].get(category) == p["sentiment"] else "전체 별점과 이 주제 평가를 구분", "source": p["source"], "source_id": p["source_id"], "snippet_only": p.get("snippet_only", False), "date_approximate": p.get("date_approximate", False)} for p in matching]}
    return {"updated_at": now().isoformat(), "window_days": WINDOW_DAYS, "sources": sources, "categories": categories, "post_count": len(recent), "posts": recent[:180], "method": "일본 리뷰는 별점, 그 외 글은 명시적 평가 표현 사전으로 판정. Facebook은 공개 검색·게시글 미리보기, X는 공개 게시글 본문을 사용. 검색 미리보기의 날짜는 추정치. 긍·부정 합계 2건 이상일 때만 방향을 제시."}


def refresh() -> dict:
    global _cache, _refreshing
    with _lock:
        if _refreshing:
            return _cache
        _refreshing = True
        previous = _cache
    posts_by_source = {sid: [p for p in previous.get("posts", []) if p.get("source_id") == sid] for sid in SOURCE_DEFS}
    source_details = {}
    collectors = {"pantip-search": collect_th, "facebook-th": collect_fb_th, "bahamut": collect_tw, "facebook-tw": collect_fb_tw, "spgame": collect_jp, "x-jp": collect_x_jp, "naver-kr": collect_kr}
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(fn): sid for sid, fn in collectors.items()}
            for future in as_completed(futures):
                sid = futures[future]
                country, url = SOURCE_DEFS[sid]
                try:
                    fresh = future.result()
                    posts_by_source[sid] = fresh
                    source_details[sid] = {"status": "ok", "count": len(fresh), "url": url, "fetched_at": now().isoformat()}
                except Exception as exc:
                    old = previous.get("source_details", {}).get(sid, previous.get("sources", {}).get(country, {}))
                    source_details[sid] = {"status": "stale" if posts_by_source[sid] else "error", "count": len(posts_by_source[sid]), "url": url, "fetched_at": old.get("fetched_at"), "error": clean(str(exc), 160)}
        sources = {}
        for country, main_url in SOURCE_URLS.items():
            parts = [source_details[sid] for sid, (home, _) in SOURCE_DEFS.items() if home == country]
            states = {part["status"] for part in parts}
            status = "ok" if states == {"ok"} else "partial" if "ok" in states else "stale" if "stale" in states else "error"
            sources[country] = {"status": status, "count": sum(part["count"] for part in parts), "url": main_url, "fetched_at": max((part.get("fetched_at") or "" for part in parts), default="") or None}
        result = build_summary([p for group in posts_by_source.values() for p in group], sources)
        result["source_details"] = source_details
        with _lock:
            _cache = result
        try:
            CACHE_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # Running without cache write permission still serves live data.
        return result
    finally:
        with _lock:
            _refreshing = False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/summary":
            params = parse_qs(parsed.query)
            with _lock:
                current = _cache
                updating = _refreshing
            retry_seconds = 60 if any(x.get("status") in {"error", "partial"} for x in current.get("sources", {}).values()) else TTL_SECONDS
            expired = not current.get("updated_at") or (now() - datetime.fromisoformat(current["updated_at"])).total_seconds() >= retry_seconds
            result = refresh() if (params.get("refresh") == ["1"] or expired) and not updating else current
            payload = json.dumps({**result, "refreshing": updating}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--refresh-only", action="store_true", help="Collect once, update latest-cache.json, and exit")
    args = parser.parse_args()
    if args.refresh_only:
        result = refresh()
        print(json.dumps({"updated_at": result["updated_at"], "post_count": result["post_count"], "sources": result["sources"]}, ensure_ascii=False))
        if not any(source["status"] == "ok" for source in result["source_details"].values()):
            raise SystemExit("모든 출처 수집 실패")
        return
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"LINE Rangers 모니터: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
