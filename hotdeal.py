"""
핫딜 알리미 — 에펨코리아·뽐뿌 새 핫딜 중 '내 키워드'에 맞는 글만 텔레그램으로 보내줍니다.

15분마다 실행되며
  · 매번: 텔레그램 명령 처리 ("OO 알림 해줘" / "OO 알림 꺼줘" / "키워드 목록")
  · 3시간마다(새벽 3~6시 제외): 새 핫딜 확인 → 키워드에 맞는 딜만 메시지 1개로 묶어 전송
키워드는 keywords.json 에 저장되고, 변경되면 워크플로가 저장소에 자동 커밋합니다.
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "seen.json"
KEYWORDS_FILE = ROOT / "keywords.json"
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FORCE_SCAN = os.environ.get("FORCE_SCAN") == "1"
KST = timezone(timedelta(hours=9))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
SEEN_LIMIT = 4000


def log(*a):
    print(*a, flush=True)


# ───────────────────────── 저장 ─────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    state["seen"] = state.get("seen", [])[-SEEN_LIMIT:]
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def load_keywords():
    if KEYWORDS_FILE.exists():
        return json.loads(KEYWORDS_FILE.read_text(encoding="utf-8")).get("keywords", [])
    return []


def save_keywords(kws):
    KEYWORDS_FILE.write_text(json.dumps({"keywords": kws}, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")


# ───────────────────────── 수집 ─────────────────────────
def paginate(fetch_page, seen, max_pages):
    """이미 본 글이 나올 때까지(또는 최대 페이지까지) 여러 페이지를 넘겨 수집"""
    items = []
    for page in range(1, max_pages + 1):
        got = fetch_page(page)
        items += got
        if not seen or any(it["id"] in seen for it in got):
            break
        time.sleep(2)
    return items


def fetch_fmkorea_page(page):
    url = "https://www.fmkorea.com/hotdeal" if page == 1 else f"https://www.fmkorea.com/index.php?mid=hotdeal&page={page}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for li in soup.select(".fm_best_widget li"):
        a = li.select_one("h3.title a")
        if not a:
            continue
        m = re.search(r"/(\d+)", a.get("href", ""))
        if not m:
            continue
        ended = "hotdeal_var8Y" in (a.get("class") or [])
        cc = a.select_one(".comment_count")
        comments = cc.get_text(strip=True).strip("[]") if cc else "0"
        if cc:
            cc.extract()
        info = {}
        for span in li.select(".hotdeal_info span"):
            k, _, v = span.get_text(" ", strip=True).partition(":")
            info[k.strip()] = v.strip()
        cat = li.select_one(".category")
        votes = li.select_one(".count")
        items.append({
            "id": f"fm:{m.group(1)}",
            "source": "펨코",
            "title": re.sub(r"\s+", " ", a.get_text(" ", strip=True)),
            "url": f"https://www.fmkorea.com/{m.group(1)}",
            "category": cat.get_text(strip=True).rstrip(" /") if cat else "",
            "shop": info.get("쇼핑몰", ""),
            "price": info.get("가격", ""),
            "delivery": info.get("배송", ""),
            "votes": votes.get_text(strip=True) if votes else "0",
            "comments": comments,
            "ended": ended,
        })
    if not items:
        raise RuntimeError("목록을 찾지 못함 (차단 또는 구조 변경)")
    return items


def fetch_ppomppu_page(page):
    r = requests.get("https://www.ppomppu.co.kr/zboard/zboard.php",
                     params={"id": "ppomppu", "page": page}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.content.decode("euc-kr", errors="replace"), "html.parser")
    items = []
    for tr in soup.select("tr.baseList"):
        a = tr.select_one("a.baseList-title")
        if not a:
            continue
        m = re.search(r"no=(\d+)", a.get("href", ""))
        if not m:
            continue
        head = tr.select_one(".baseList-head")
        cat = tr.select_one(".baseList-small")
        cc = tr.select_one(".baseList-c")
        rec = tr.select_one(".baseList-rec")
        span = a.find("span")
        name = (span or a).get_text(" ", strip=True)
        shop = head.get_text(strip=True).strip("[]") if head else ""
        items.append({
            "id": f"pp:{m.group(1)}",
            "source": "뽐뿌",
            "title": (f"[{shop}] " if shop and not name.startswith("[") else "") + re.sub(r"\s+", " ", name),
            "url": f"https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no={m.group(1)}",
            "category": cat.get_text(strip=True).strip("[]") if cat else "",
            "shop": shop, "price": "", "delivery": "",
            "votes": (rec.get_text(strip=True).split("-")[0].strip() or "0") if rec else "0",
            "comments": cc.get_text(strip=True) if cc else "0",
            "ended": "end2" in (a.get("class") or []),
        })
    if not items:
        raise RuntimeError("목록을 찾지 못함 (차단 또는 구조 변경)")
    return items

# ───────────────────────── 매칭 ─────────────────────────
def norm(s):
    return re.sub(r"\s+", "", str(s)).lower()


def match_keyword(title, kws):
    """제목에 맞는 키워드 항목을 반환 (없으면 None)"""
    t = norm(title)
    if any(norm(x) in t for x in CONFIG.get("exclude_keywords", [])):
        return None
    for kw in kws:
        words = [kw["word"]] + kw.get("aliases", [])
        hit = next((w for w in words if norm(w) and norm(w) in t), None)
        if not hit:
            continue
        if any(norm(x) in t for x in kw.get("exclude", []) if norm(x)):
            continue
        need = kw.get("require_any", [])
        if need and not any(norm(x) in t for x in need):
            continue
        return kw, hit
    return None


# ───────────────────────── Gemini ─────────────────────────
def gemini_json(prompt):
    errors = []
    for model in CONFIG.get("ai", {}).get("models", ["gemini-3.5-flash"]):
        last = None
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}
        for _ in range(2):
            try:
                r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=60)
                if r.status_code in (429, 500, 503):
                    last = f"{model} {r.status_code}"
                    time.sleep(4)
                    continue
                r.raise_for_status()
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                txt = re.sub(r"^```(?:json)?|```$", "", txt).strip()
                return json.loads(txt)
            except Exception as e:
                last = f"{model}: {e}"
                break
        errors.append(last)
    raise RuntimeError("Gemini 실패 (" + " / ".join(map(str, errors)) + ")")


EXPAND_RULE = (
    "각 키워드마다 핫딜 게시글 제목에서 같은 상품을 가리키는 동의어·영문/한글 표기·대표 브랜드/모델명을 aliases로 3~8개 만든다. "
    "(예: 단백질 → 프로틴, 단백질쉐이크, 단백질바, WPI, 웨이프로틴 / 러닝화 → 런닝화, 페가수스, 노바블라스트, 젤카야노)\n"
    "너무 넓은 단어(예: '세트', '할인')는 넣지 않는다. 다른 상품과 헷갈릴 수 있으면 exclude에 제외어를 넣고, "
    "단어가 애매하면 require_any에 같이 있어야 할 단어를 넣는다 (예: 고스트 → require_any: [러닝화, 브룩스], exclude: [PS5, 게임]).\n"
)


def parse_command(text, kws):
    """텔레그램 메시지를 명령으로 해석"""
    current = ", ".join(k["word"] for k in kws) or "(없음)"
    if GEMINI_KEY:
        try:
            prompt = (
                "너는 핫딜 알림 봇의 명령 해석기야. 사용자의 메시지를 읽고 JSON으로만 답해.\n"
                f"현재 등록된 키워드: {current}\n\n"
                "action 종류: add(알림 추가), remove(알림 끄기), list(목록 보기), help(사용법), none(명령 아님)\n"
                "remove일 때 words에는 현재 등록된 키워드 중 해당하는 것을 정확히 적는다.\n"
                "add일 때:\n" + EXPAND_RULE +
                '형식: {"action":"add","items":[{"word":"단백질","aliases":["프로틴"],"exclude":[],"require_any":[]}]}\n'
                '      {"action":"remove","words":["단백질"]}  {"action":"list"}\n\n'
                f"사용자 메시지: {text}"
            )
            return gemini_json(prompt)
        except Exception as e:
            log("⚠️ 명령 AI 해석 실패, 규칙 기반으로 대체:", e)
    # ── AI가 없을 때 규칙 기반 해석
    t = text.strip()
    if re.search(r"목록|리스트|뭐\s*있|확인", t):
        return {"action": "list"}
    if re.search(r"사용법|도움|help", t, re.I) or t.startswith("/start"):
        return {"action": "help"}
    m = re.match(r"(.+?)\s*(?:관련\s*)?(?:알림|알람|키워드)?\s*(?:을|를)?\s*(꺼|끄|빼|삭제|그만|중지|제거)", t)
    if m:
        return {"action": "remove", "words": [w.strip() for w in re.split(r"[,/·]", m.group(1)) if w.strip()]}
    m = re.match(r"(.+?)\s*(?:관련\s*)?(?:알림|알람|키워드)?\s*(?:을|를)?\s*(해|추가|등록|켜|받|알려)", t)
    if m:
        return {"action": "add", "items": [{"word": w.strip()} for w in re.split(r"[,/·]", m.group(1)) if w.strip()]}
    return {"action": "none"}


def fmt_kw(k):
    extra = ", ".join(k.get("aliases", [])[:6])
    return f"• <b>{html.escape(k['word'])}</b>" + (f"  <i>({html.escape(extra)})</i>" if extra else "")


HELP = ("🛒 <b>핫딜 알리미 사용법</b>\n"
        "• <code>단백질 알림 해줘</code> → 키워드 추가 (프로틴 등 연관어 자동 포함)\n"
        "• <code>단백질 알림 꺼줘</code> → 키워드 삭제\n"
        "• <code>키워드 목록</code> → 현재 목록 보기\n"
        "딜 확인은 3시간마다, 명령은 15분 안에 반영됩니다.")


def handle_commands(state, kws):
    """쌓인 텔레그램 메시지를 처리. 키워드가 바뀌면 True"""
    changed = False
    updates = tg("getUpdates", offset=state.get("tg_offset", 0), timeout=0)
    for upd in updates:
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if not chat or not text:
            continue
        if not state.get("chat_id"):
            state["chat_id"] = str(chat["id"])
        if str(chat["id"]) != str(state["chat_id"]):
            continue   # 다른 사람이 보낸 메시지는 무시
        cmd = parse_command(text, kws)
        act = cmd.get("action")
        log(f"명령: {text!r} → {act}")
        if act == "add":
            added = []
            for it in cmd.get("items", []):
                word = str(it.get("word", "")).strip()
                if not word:
                    continue
                entry = {"word": word,
                         "aliases": [a for a in it.get("aliases", []) if norm(a) != norm(word)],
                         "exclude": it.get("exclude", []),
                         "require_any": it.get("require_any", []),
                         "added": datetime.now(KST).strftime("%Y-%m-%d")}
                kws[:] = [k for k in kws if norm(k["word"]) != norm(word)] + [entry]
                added.append(entry)
            if added:
                changed = True
                reply = "✅ 알림 키워드 추가\n" + "\n".join(fmt_kw(k) for k in added)
                hits = quick_search(added)
                if hits:
                    reply += "\n\n지금 올라와 있는 관련 딜\n" + "\n".join(hits)
            else:
                reply = "추가할 키워드를 못 찾았어요. 예: <code>단백질 알림 해줘</code>"
        elif act == "remove":
            targets = {norm(w) for w in cmd.get("words", [])}
            gone = [k for k in kws if norm(k["word"]) in targets]
            if gone:
                kws[:] = [k for k in kws if k not in gone]
                changed = True
                reply = "🔕 알림 끔: " + ", ".join(html.escape(k["word"]) for k in gone)
            else:
                reply = "등록된 키워드에서 찾지 못했어요. <code>키워드 목록</code>으로 확인해보세요."
        elif act == "list":
            reply = (f"📋 <b>알림 키워드 {len(kws)}개</b>\n" + "\n".join(fmt_kw(k) for k in kws)) if kws else "등록된 키워드가 없어요."
        elif act == "help":
            reply = HELP
        else:
            reply = "무슨 뜻인지 잘 모르겠어요.\n\n" + HELP
        send(state["chat_id"], reply)
    return changed


def quick_search(new_kws):
    """방금 추가한 키워드로 최신 1페이지를 바로 검색"""
    lines = []
    for fn in (fetch_fmkorea_page, fetch_ppomppu_page):
        try:
            for it in fn(1):
                if not it["ended"] and match_keyword(it["title"], new_kws):
                    lines.append(f'• <a href="{html.escape(it["url"])}">{html.escape(it["title"])}</a>')
        except Exception as e:
            log("⚠️ 즉시 검색 실패:", e)
    return lines[:8]


# ───────────────────────── 텔레그램 ─────────────────────────
def tg(method, **params):
    if DRY_RUN and method != "getUpdates":
        log(f"[DRY] {method}: {params.get('text', '')}")
        return {}
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} 실패: {data.get('description')}")
    return data["result"]


def send(chat_id, text):
    try:
        tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
           link_preview_options={"is_disabled": True})
    except Exception as e:
        log("⚠️ 전송 실패:", e)


def fmt(it, hit, n):
    e = html.escape
    meta = " · ".join(x for x in [it["price"], it["delivery"]] if x)
    stats = ""
    if it["votes"] not in ("", "0") or it["comments"] not in ("", "0"):
        stats = f"  👍{it['votes']} 💬{it['comments']}"
    title = f'<a href="{e(it["url"])}">{e(it["title"])}</a>'
    line2 = f"   🔑 {e(hit)}" + (f" · {e(meta)}" if meta else "") + stats + f" · {it['source']}"
    return f"{n}. {title}\n{line2}"


def build_digest(picks):
    head = f"🛒 <b>핫딜 알림</b> — 관심 딜 {len(picks)}건\n"
    msgs, cur = [], head
    for n, (it, hit) in enumerate(picks, 1):
        block = "\n" + fmt(it, hit, n) + "\n"
        if len(cur) + len(block) > 3900:
            msgs.append(cur)
            cur = ""
        cur += block
    msgs.append(cur)
    return msgs


# ───────────────────────── 딜 확인 ─────────────────────────
def scan_slot(now):
    """3시간 단위 슬롯 번호. 새벽 3~6시(슬롯 1)는 건너뜀"""
    slot = now.hour // 3
    return None if slot == 1 else now.strftime("%Y%m%d-") + str(slot)


def scan_deals(state, kws):
    seen = set(state.get("seen", []))
    fetched = []
    for name, fn, pages in (("fmkorea", fetch_fmkorea_page, 5), ("ppomppu", fetch_ppomppu_page, 8)):
        if not CONFIG.get("sources", {}).get(name, True):
            continue
        try:
            got = paginate(fn, seen, pages)
            log(f"{name}: {len(got)}건 수집")
            fetched += got
            state.setdefault("fail", {})[name] = 0
        except Exception as e:
            n = state.setdefault("fail", {}).get(name, 0) + 1
            state["fail"][name] = n
            log(f"⚠️ {name} 수집 실패 ({n}회 연속): {e}")
            if n == 3 and state.get("chat_id"):
                label = {"fmkorea": "에펨코리아", "ppomppu": "뽐뿌"}[name]
                send(state["chat_id"], f"⚠️ {label} 접속이 연속 3번 실패했어요. 사이트가 서버 접속을 막았을 수 있습니다.")

    new_items = [it for it in fetched if it["id"] not in seen]
    log(f"새 글 {len(new_items)}건")
    first_run = not state.get("seen")
    state["seen"] = state.get("seen", []) + [it["id"] for it in new_items]
    if first_run:
        log("첫 실행: 기존 글은 기록만 함")
        return

    picks, dup = [], set()
    for it in new_items:
        if it["ended"]:
            continue
        m = match_keyword(it["title"], kws)
        if m and norm(it["title"]) not in dup:
            dup.add(norm(it["title"]))
            picks.append((it, m[0]["word"]))
    log(f"알림 대상 {len(picks)}건")
    if picks and state.get("chat_id"):   # 관심 딜이 없으면 조용히
        for text in build_digest(picks):
            send(state["chat_id"], text)


# ───────────────────────── 메인 ─────────────────────────
def main():
    if not BOT_TOKEN and not DRY_RUN:
        sys.exit("TELEGRAM_BOT_TOKEN 이 없습니다 (GitHub Secret 확인)")
    state = load_state()
    kws = load_keywords()
    if CONFIG.get("chat_id"):
        state["chat_id"] = str(CONFIG["chat_id"])

    is_new = not state.get("chat_id")
    changed = False
    if not DRY_RUN:
        try:
            changed = handle_commands(state, kws)
        except Exception as e:
            log("⚠️ 명령 처리 실패:", e)
    if changed:
        save_keywords(kws)
        log("keywords.json 변경됨")
    if is_new and state.get("chat_id"):
        send(state["chat_id"], "✅ <b>핫딜 알리미 연결 완료</b>\n등록된 키워드에 맞는 딜만 3시간마다 묶어서 보내드릴게요.")

    slot = scan_slot(datetime.now(KST))
    if FORCE_SCAN or (slot and slot != state.get("last_slot")):
        scan_deals(state, kws)
        if slot:
            state["last_slot"] = slot
    else:
        log("이번 실행은 명령만 처리 (딜 확인은 3시간 단위)")
    save_state(state)


if __name__ == "__main__":
    main()
