"""
핫딜 알리미 — 에펨코리아·뽐뿌 핫딜 중 '내 키워드'에 맞는 글과 인기 딜을 텔레그램으로 보내줍니다.

5분마다 실행되며
  · 매번: 텔레그램 명령·버튼(👍/👎) 처리
  · 30분마다(새벽 3~6시 제외): 새 글 수집
      - ⚡ 급한 키워드 / 기준 단가보다 훨씬 싼 딜 → 즉시 알림
      - 🔥 키워드와 상관없이 추천·댓글이 빠르게 붙은 인기 딜 → 이유 한 줄과 함께 즉시 알림
      - 나머지 키워드 딜 → 모아뒀다가 3시간마다 메시지 1개로 묶어 전송
키워드는 keywords.json 에 저장됩니다.
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
POP = CONFIG.get("popular", {})


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
    cutoff = time.time() - 48 * 3600
    state["alerted"] = {k: v for k, v in state.get("alerted", {}).items() if v.get("t", 0) > cutoff}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def load_keywords():
    """keywords.json 전체 (keywords 목록 + pop_exclude 등)"""
    if KEYWORDS_FILE.exists():
        data = json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))
    else:
        data = {}
    data.setdefault("keywords", [])
    data.setdefault("pop_exclude", [])
    return data


def save_keywords(data):
    KEYWORDS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ───────────────────────── 수집 ─────────────────────────
def paginate(fetch_page, seen, max_pages, delay, min_pages=1):
    """이미 본 글이 나올 때까지(최소 min_pages, 최대 max_pages) 페이지를 넘겨 수집.
    중간 페이지에서 실패하면 그때까지 모은 글과 오류를 함께 돌려줌"""
    items = []
    for page in range(1, max_pages + 1):
        try:
            got = fetch_page(page)
        except Exception as e:
            if not items:
                raise
            return items, e
        items += got
        if page >= min_pages and (not seen or any(it["id"] in seen for it in got)):
            break
        time.sleep(delay)
    return items, None


def retry_after(e):
    """430/429 응답의 Retry-After(초). 없으면 30분"""
    resp = getattr(e, "response", None)
    try:
        return max(int(resp.headers.get("Retry-After", "")), 300)
    except (AttributeError, ValueError):
        return 1800


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


def num(s):
    try:
        return int(re.sub(r"[^\d]", "", str(s)) or 0)
    except ValueError:
        return 0


def match_keyword(title, kws):
    """제목에 맞는 키워드 항목과 걸린 단어를 반환 (없으면 None)"""
    t = norm(title)
    if any(norm(x) in t for x in CONFIG.get("exclude_keywords", [])):
        return None
    for kw in kws:
        words = [kw["word"]] + kw.get("aliases", [])
        hit = next((w for w in words if norm(w) and norm(w) in t), None)
        if not hit:
            continue
        if kw_excludes(title, kw):
            continue
        need = kw.get("require_any", [])
        if need and not any(norm(x) in t for x in need):
            continue
        return kw, hit
    return None


def kw_excludes(title, kw):
    """키워드의 제외어(exclude)나 제외 패턴(exclude_regex)에 걸리면 True"""
    t = norm(title)
    if any(norm(x) in t for x in kw.get("exclude", []) if norm(x)):
        return True
    return any(re.search(p, title) for p in kw.get("exclude_regex", []))


def blocked_by_keyword(title, kws):
    """키워드 단어는 들어 있지만 그 키워드의 제외 규칙에 걸리는 글 (인기 딜에서도 빼기 위함)"""
    t = norm(title)
    for kw in kws:
        if any(re.search(p, title) for p in kw.get("exclude_regex", [])):
            return True      # 패턴(지방 출발 항공권 등)은 키워드 단어가 없어도 적용
        if any(norm(w) and norm(w) in t for w in [kw["word"]] + kw.get("aliases", [])) and kw_excludes(title, kw):
            return True
    return False


# ───────────────────────── 단위가격 ─────────────────────────
COUNT_UNITS = "개입|개|구|캔|팩|병|입|봉|포|컵|매|롤|장|알"
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g|ml|l)(?![a-z])", re.I)
COUNT_RE = re.compile(rf"(\d+)\s*({COUNT_UNITS})(?![가-힣])")


def parse_price(it):
    """판매가(원). 펨코는 가격 칸, 뽐뿌는 제목 괄호 안 '(14,500원/무료)'"""
    src = it.get("price") or ""
    m = (re.search(r"([\d,]{3,})\s*원", src) or re.search(r"\(([^)]*?)([\d,]{3,})\s*원", it["title"])
         or re.search(r"\(([^)]*?)(\d{1,3}(?:,\d{3})+)\s*/", it["title"]))       # '(22,900/무료)'
    if not m:
        return None
    p = num(m.group(m.lastindex))
    return p if p >= 100 else None


def parse_quantity(title):
    """제목에서 총 수량 추출 → {'count': 개수, 'ml': 총 용량, 'g': 총 무게}. 옵션이 여러 개(1kg/2kg)면 None"""
    t = re.sub(r"\([^)]*원[^)]*\)", " ", title)          # 가격 괄호 제거
    if re.search(r"\d\s*(kg|g|ml|l|개|구|팩|캔)\s*/\s*\d", t, re.I):
        return None                                        # 옵션별 수량이 달라 계산 불가
    total = {"count": 0, "ml": 0.0, "g": 0.0}
    # '190ml 30캔', '250ml*24개', '1kg x 2개'처럼 [크기][개수] 쌍을 먼저 처리
    for m in re.finditer(rf"(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\s*[x×*,]?\s*(\d+)\s*({COUNT_UNITS})(?![가-힣])", t, re.I):
        size, unit, cnt = float(m.group(1)), m.group(2).lower(), int(m.group(3))
        key, mult = {"kg": ("g", 1000), "g": ("g", 1), "l": ("ml", 1000), "ml": ("ml", 1)}[unit]
        total[key] += size * mult * cnt
        total["count"] += cnt
    t2 = re.sub(rf"(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\s*[x×*,]?\s*(\d+)\s*({COUNT_UNITS})(?![가-힣])", " ", t, flags=re.I)
    counts = [int(m.group(1)) for m in COUNT_RE.finditer(t2)]
    sizes = [(float(m.group(1)), m.group(2).lower()) for m in SIZE_RE.finditer(t2)]
    if counts:
        total["count"] += sum(counts)
    if sizes and not total["ml"] and not total["g"]:
        size, unit = sizes[0]
        key, mult = {"kg": ("g", 1000), "g": ("g", 1), "l": ("ml", 1000), "ml": ("ml", 1)}[unit]
        total[key] = size * mult * (sum(counts) if counts else 1)
    return total if any(total.values()) else None


def unit_prices(it):
    """{'개': 원, '100ml': 원, '100g': 원, 'kg': 원, 'L': 원} 중 계산 가능한 것"""
    price, q = parse_price(it), parse_quantity(it["title"])
    if not price or not q:
        return {}
    out = {}
    if q["count"]:
        out["개"] = price / q["count"]
    if q["ml"]:
        out["100ml"] = price / q["ml"] * 100
        out["L"] = price / q["ml"] * 1000
    if q["g"]:
        out["100g"] = price / q["g"] * 100
        out["kg"] = price / q["g"] * 1000
    return out


COUNT_ALIASES = {"구", "개", "캔", "팩", "병", "입", "봉", "포", "컵", "개입", "알"}


def unit_key(u):
    u = str(u).strip()
    return "개" if u in COUNT_ALIASES else {"ml": "100ml", "100ml": "100ml", "g": "100g", "100g": "100g",
                                            "kg": "kg", "l": "L", "L": "L"}.get(u, u)


def price_check(it, kw):
    """(단가 표시 문자열, 통과여부, 역대급여부)"""
    ups = unit_prices(it)
    base = kw.get("max_unit_price") if kw else None
    if base:
        key = unit_key(base["unit"])
        if key in ups:
            v = ups[key]
            label = f"{base['unit']}당 {v:,.0f}원 (기준 {base['max']:,}원)"
            ratio = POP.get("bargain_ratio", 0.85)
            return label, v <= base["max"], v <= base["max"] * ratio
        return "", True, False       # 단가를 못 구하면 놓치지 않도록 통과
    for key in ("개", "100ml", "100g"):
        if key in ups:
            return f"{key}당 {ups[key]:,.0f}원", True, False
    return "", True, False


# ───────────────────────── 가격 이력 ─────────────────────────
def hist_key(kw, ups):
    """키워드별로 비교할 단가 단위 (수동 기준이 있으면 그 단위, 없으면 용량·무게 우선)"""
    base = kw.get("max_unit_price")
    if base:
        k = unit_key(base["unit"])
        return k if k in ups else None
    return next((k for k in ("100ml", "100g", "개") if k in ups), None)


def record_price(state, kw, it):
    """이 키워드로 본 딜의 단가를 기록 (60일, 키워드당 최대 300건)"""
    ups = unit_prices(it)
    k = hist_key(kw, ups)
    if not k:
        return
    hist = state.setdefault("prices", {}).setdefault(kw["word"], [])
    if any(x["id"] == it["id"] for x in hist):
        return
    hist.append({"id": it["id"], "u": k, "v": round(ups[k], 1), "t": time.time()})
    cutoff = time.time() - 60 * 86400
    state["prices"][kw["word"]] = [x for x in hist if x["t"] > cutoff][-300:]


def history_note(state, kw, it):
    """최근 30일 같은 키워드 단가와 비교 → (한 줄 설명, 평소보다 훨씬 싼지)"""
    ups = unit_prices(it)
    k = hist_key(kw, ups)
    if not k:
        return "", False
    v = ups[k]
    cutoff = time.time() - 30 * 86400
    past = sorted(x["v"] for x in state.get("prices", {}).get(kw["word"], [])
                  if x["u"] == k and x["id"] != it["id"] and x["t"] > cutoff)
    if len(past) < 5:                      # 비교할 이력이 쌓일 때까지는 표시 안 함
        return "", False
    med = past[len(past) // 2]
    diff = (v - med) / med * 100
    if v <= past[0]:
        note = "🏆 30일 최저 단가" + (f" (평소보다 {-diff:.0f}%↓)" if diff <= -1 else "")
    elif diff <= -10:
        note = f"📉 평소보다 {-diff:.0f}% 저렴"
    elif diff >= 10:
        note = f"📈 평소보다 {diff:.0f}% 비쌈"
    else:
        note = "평소 수준 가격"
    return note, v <= med * POP.get("history_bargain_ratio", 0.8)


# ───────────────────────── 중복 딜 묶기 ─────────────────────────
def sig(title):
    """쇼핑몰 [..]·가격 (..)을 뺀 제목의 2글자 조각 집합 (띄어쓰기가 달라도 비교되도록)"""
    t = re.sub(r"\[[^\]]*\]|\([^)]*원[^)]*\)", "", title.lower())
    t = "".join(re.findall(r"[가-힣a-z0-9]", t))
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similar(a, b):
    """짧은 쪽 제목의 75% 이상이 겹치면 같은 상품"""
    if len(a) < 4 or len(b) < 4:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.75


def group_similar(entries):
    """[(item, extra)] → [[(item, extra), ...], ...] 같은 상품끼리 묶음"""
    groups = []
    for e in entries:
        s = sig(e[0]["title"])
        for g in groups:
            if similar(s, g[0]):
                g[1].append(e)
                break
        else:
            groups.append((s, [e]))
    return [g[1] for g in groups]


def recently_alerted(state, title, kind):
    s = sig(title)
    return any(v.get("kind") == kind and similar(s, set(v.get("sig", []))) for v in state.get("alerted", {}).values())


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
                "action 종류: add(알림 추가), remove(알림 끄기), list(목록 보기), help(사용법), "
                "price(단가 기준 설정/해제), urgent(즉시 알림 켜기/끄기), none(명령 아님)\n"
                "remove/price/urgent일 때 word(s)에는 현재 등록된 키워드 중 해당하는 것을 정확히 적는다.\n"
                "price: '계란 1구 300원 이하만' → unit은 구/개/캔/팩/병/100ml/100g/kg/L 중 하나, max는 원 단위 정수. "
                "'기준 없애줘'면 max는 null.\n"
                "urgent: '러닝화는 바로 알려줘' → on:true, '묶어서 보내줘' → on:false.\n"
                "unexclude(제외어 되돌리기): '제로콜라 제외어 210ml 빼줘' → word와 exclude. "
                "인기 딜 제외를 되돌리면 word는 null.\n"
                "add일 때:\n" + EXPAND_RULE +
                '형식: {"action":"add","items":[{"word":"단백질","aliases":["프로틴"],"exclude":[],"require_any":[]}]}\n'
                '      {"action":"remove","words":["단백질"]}  {"action":"list"}\n'
                '      {"action":"price","word":"계란","unit":"구","max":300}  {"action":"urgent","word":"러닝화","on":true}\n'
                '      {"action":"unexclude","word":"제로콜라","exclude":"210ml"}\n\n'
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
    m = re.match(r"(.+?)\s*제외어\s*(.+?)\s*(?:을|를)?\s*(빼|삭제|취소|없애)", t)
    if m:
        return {"action": "unexclude", "word": m.group(1).strip(), "exclude": m.group(2).strip()}
    m = re.match(r"(.+?)\s*1?\s*(구|개|캔|팩|병|100ml|100g|kg|L)\s*당?\s*([\d,]+)\s*원\s*이하", t)
    if m:
        return {"action": "price", "word": m.group(1).strip(), "unit": m.group(2), "max": num(m.group(3))}
    m = re.match(r"(.+?)\s*(?:은|는)?\s*(즉시|바로)", t)
    if m:
        return {"action": "urgent", "word": m.group(1).strip(), "on": True}
    m = re.match(r"(.+?)\s*(?:은|는)?\s*(묶어서|모아서)", t)
    if m:
        return {"action": "urgent", "word": m.group(1).strip(), "on": False}
    m = re.match(r"(.+?)\s*(?:관련\s*)?(?:알림|알람|키워드)?\s*(?:을|를)?\s*(꺼|끄|빼|삭제|그만|중지|제거)", t)
    if m:
        return {"action": "remove", "words": [w.strip() for w in re.split(r"[,/·]", m.group(1)) if w.strip()]}
    m = re.match(r"(.+?)\s*(?:관련\s*)?(?:알림|알람|키워드)?\s*(?:을|를)?\s*(해|추가|등록|켜|받|알려)", t)
    if m:
        return {"action": "add", "items": [{"word": w.strip()} for w in re.split(r"[,/·]", m.group(1)) if w.strip()]}
    return {"action": "none"}


def suggest_exclude(title, kw):
    """👎 받은 딜에서 이 키워드의 제외어로 쓸 단어를 고름 (제목 안에 있는 단어만)"""
    if GEMINI_KEY:
        try:
            ctx = f"키워드 '{kw['word']}'(연관어: {', '.join(kw.get('aliases', []))})" if kw else "인기 딜(키워드 없음)"
            r = gemini_json(
                "핫딜 알림 봇이 사용자에게 원치 않는 딜을 보냈어. 이런 딜이 다시 오지 않게 할 제외어 1개를 골라 JSON으로 답해.\n"
                f"알림 기준: {ctx}\n딜 제목: {title}\n"
                "규칙: 제외어는 반드시 제목에 그대로 들어 있는 짧은 단어(2~6자)이고, 사용자가 원하는 상품에는 잘 안 들어가는 단어여야 한다. "
                "숫자·용량·수량(210ml, 30캔 등)·쇼핑몰 이름·키워드 자체나 그 연관어는 절대 고르지 않는다. "
                "상품 종류·브랜드·용도처럼 '이런 건 싫다'를 나타내는 단어를 고른다. "
                "인기 딜이면 상품 종류를 대표하는 단어(예: 항공권, 비데)를 고른다. 적당한 게 없으면 null.\n"
                '형식: {"exclude":"비데","reason":"과일이 아니라 비데 제품"}')
            w = str(r.get("exclude") or "").strip()
            own = [kw["word"]] + kw.get("aliases", []) if kw else []
            if (w and norm(w) in norm(title) and not re.search(r"\d", w)
                    and not any(norm(w) in norm(o) or norm(o) in norm(w) for o in own)):
                return w
        except Exception as e:
            log("⚠️ 제외어 추천 실패:", e)
    return None


def popular_reasons(items):
    """인기 딜마다 인기 이유 한 줄"""
    fallback = {it["id"]: f"올라온 지 {it['_age']}시간 만에 추천 {it['votes']}·댓글 {it['comments']}" for it in items}
    if not GEMINI_KEY or not items:
        return fallback
    lines = "\n".join(
        f"- id={it['id']} | {it['title']} | 가격 {it.get('price') or '-'} | 단가 {it.get('_unit') or '-'} | "
        f"{it['_age']}시간 만에 추천 {it['votes']}·댓글 {it['comments']}" for it in items)
    try:
        r = gemini_json(
            "아래는 한국 핫딜 커뮤니티에서 빠르게 인기를 얻은 딜이야. 각 딜이 왜 인기인지 한국어 한 줄(35자 이내)로 설명해.\n"
            "가격·단가·구성(1+1, 증정)·브랜드·희소성 중 제목에서 알 수 있는 근거를 쓰고, 모르는 사실(역대가 등)은 단정하지 마. "
            "근거가 부족하면 반응 속도를 언급해.\n"
            f"{lines}\n\n"
            '형식: {"reasons":{"fm:123":"한우 1등급이 kg당 5만원대, 평소보다 저렴"}}')
        got = r.get("reasons", {})
        return {k: str(got.get(k) or v)[:60] for k, v in fallback.items()}
    except Exception as e:
        log("⚠️ 인기 이유 생성 실패:", e)
        return fallback


def fmt_kw(k):
    extra = ", ".join(k.get("aliases", [])[:6])
    flags = ""
    if k.get("urgent"):
        flags += " ⚡"
    if k.get("max_unit_price"):
        b = k["max_unit_price"]
        flags += f" 💰{b['unit']}당 {b['max']:,}원↓"
    return f"• <b>{html.escape(k['word'])}</b>{flags}" + (f"  <i>({html.escape(extra)})</i>" if extra else "")


HELP = ("🛒 <b>핫딜 알리미 사용법</b>\n"
        "• <code>단백질 알림 해줘</code> → 키워드 추가 (프로틴 등 연관어 자동 포함)\n"
        "• <code>단백질 알림 꺼줘</code> → 키워드 삭제\n"
        "• <code>러닝화는 바로 알려줘</code> → ⚡ 즉시 알림 (<code>묶어서 보내줘</code>로 해제)\n"
        "• <code>계란 1구 300원 이하만</code> → 💰 단가 기준 (<code>계란 기준 없애줘</code>로 해제)\n"
        "• <code>키워드 목록</code> → 현재 목록 보기\n"
        "• 알림의 👎 → 비슷한 딜이 다시 안 오게 제외어 자동 추가\n"
        "키워드 딜은 3시간마다 묶어서, ⚡·🔥 인기 딜은 바로 보내드려요.")


def find_kw(kws, word):
    return next((k for k in kws if norm(k["word"]) == norm(word)), None)


def handle_commands(state, data):
    """쌓인 텔레그램 메시지와 버튼 입력을 처리. 키워드가 바뀌면 True"""
    kws = data["keywords"]
    changed = False
    updates = tg("getUpdates", offset=state.get("tg_offset", 0), timeout=0)
    for upd in updates:
        state["tg_offset"] = upd["update_id"] + 1
        if upd.get("callback_query"):
            changed |= handle_button(state, data, upd["callback_query"])
            continue
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
                old = find_kw(kws, word) or {}
                entry = {"word": word,
                         "aliases": [a for a in it.get("aliases", []) if norm(a) != norm(word)],
                         "exclude": it.get("exclude", []),
                         "require_any": it.get("require_any", []),
                         "added": datetime.now(KST).strftime("%Y-%m-%d")}
                for keep in ("urgent", "max_unit_price", "exclude_regex"):
                    if keep in old:
                        entry[keep] = old[keep]
                kws[:] = [k for k in kws if norm(k["word"]) != norm(word)] + [entry]
                added.append(entry)
            if added:
                changed = True
                reply = "✅ 알림 키워드 추가\n" + "\n".join(fmt_kw(k) for k in added)
                hits = quick_search(added, state)
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
        elif act == "price":
            kw = find_kw(kws, cmd.get("word", ""))
            if not kw:
                reply = "먼저 키워드를 등록해 주세요. 예: <code>계란 알림 해줘</code>"
            elif cmd.get("max"):
                kw["max_unit_price"] = {"unit": str(cmd.get("unit") or "개"), "max": num(cmd["max"])}
                changed = True
                reply = f"💰 {html.escape(kw['word'])}: {kw['max_unit_price']['unit']}당 {kw['max_unit_price']['max']:,}원 이하만 알려드릴게요.\n(기준보다 15% 이상 싸면 바로 알림)"
            else:
                kw.pop("max_unit_price", None)
                changed = True
                reply = f"💰 {html.escape(kw['word'])} 단가 기준을 없앴어요."
        elif act == "urgent":
            kw = find_kw(kws, cmd.get("word", ""))
            if not kw:
                reply = "먼저 키워드를 등록해 주세요."
            else:
                kw["urgent"] = bool(cmd.get("on", True))
                changed = True
                reply = (f"⚡ {html.escape(kw['word'])} 딜은 올라오면 바로 알려드릴게요." if kw["urgent"]
                         else f"📦 {html.escape(kw['word'])} 딜은 3시간마다 묶어서 보내드릴게요.")
        elif act == "unexclude":
            w = str(cmd.get("exclude") or "").strip()
            kw = find_kw(kws, cmd.get("word") or "")
            pool = kw.get("exclude", []) if kw else data["pop_exclude"]
            hit = next((x for x in pool if norm(x) == norm(w)), None)
            if hit:
                pool.remove(hit)
                changed = True
                reply = f"↩️ {html.escape(kw['word'] if kw else '인기 딜')} 제외어에서 <b>{html.escape(hit)}</b>를 뺐어요."
            else:
                reply = "그 제외어를 찾지 못했어요. <code>키워드 목록</code>으로 확인해보세요."
        elif act == "list":
            reply = (f"📋 <b>알림 키워드 {len(kws)}개</b>\n" + "\n".join(fmt_kw(k) for k in kws)) if kws else "등록된 키워드가 없어요."
            if data.get("pop_exclude"):
                reply += "\n\n🔥 인기 딜 제외: " + html.escape(", ".join(data["pop_exclude"]))
        elif act == "help":
            reply = HELP
        else:
            reply = "무슨 뜻인지 잘 모르겠어요.\n\n" + HELP
        send(state["chat_id"], reply)
    return changed


def handle_button(state, data, cq):
    """👍/👎 버튼. 👎면 제외어를 자동으로 추가"""
    chat = (cq.get("message") or {}).get("chat") or {}
    if str(chat.get("id")) != str(state.get("chat_id")):
        return False
    act, _, item_id = str(cq.get("data", "")).partition("|")
    rec = state.get("alerted", {}).get(item_id)
    changed = False
    if not rec:
        toast = "오래된 알림이라 처리할 수 없어요"
    elif act == "up":
        state.setdefault("likes", []).append({"title": rec["title"], "word": rec.get("word"), "t": time.time()})
        state["likes"] = state["likes"][-200:]
        toast = "👍 기록했어요"
    else:
        kw = find_kw(data["keywords"], rec.get("word") or "")
        w = suggest_exclude(rec["title"], kw)
        if w and kw:
            if w not in kw.setdefault("exclude", []):
                kw["exclude"].append(w)
                changed = True
            toast = f"'{w}' 들어간 딜은 {kw['word']}에서 뺄게요"
            send(state["chat_id"], f"🔕 <b>{html.escape(kw['word'])}</b> 제외어에 <b>{html.escape(w)}</b> 추가\n"
                                   f"되돌리려면: <code>{html.escape(kw['word'])} 제외어 {html.escape(w)} 빼줘</code>")
        elif w:
            if w not in data["pop_exclude"]:
                data["pop_exclude"].append(w)
                changed = True
            toast = f"'{w}' 인기 딜은 앞으로 안 보낼게요"
            send(state["chat_id"], f"🔕 인기 딜 제외에 <b>{html.escape(w)}</b> 추가\n"
                                   f"되돌리려면: <code>인기 딜 제외어 {html.escape(w)} 빼줘</code>")
        else:
            toast = "제외어를 못 찾았어요. 필요하면 키워드를 꺼주세요"
        log(f"👎 {rec['title']} → {w}")
    try:
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=toast)
    except Exception as e:
        log("⚠️ 버튼 응답 실패:", e)
    return changed


def quick_search(new_kws, state):
    """방금 추가한 키워드로 최신 1페이지를 바로 검색 (접속 제한 중인 사이트는 건너뜀)"""
    lines = []
    for name, (fn, _, _) in SOURCES.items():
        if is_blocked(state, name):
            continue
        try:
            for it in fn(1):
                if not it["ended"] and match_keyword(it["title"], new_kws):
                    lines.append(f'• <a href="{html.escape(it["url"])}">{html.escape(it["title"])}</a>')
        except Exception as e:
            state.setdefault("blocked_until", {})[name] = time.time() + retry_after(e)
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


def send(chat_id, text, buttons=None):
    """전송 후 Telegram 메시지 객체를 돌려줌 (실패하면 None)"""
    try:
        extra = {"reply_markup": {"inline_keyboard": buttons}} if buttons else {}
        return tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                  link_preview_options={"is_disabled": True}, **extra)
    except Exception as e:
        log("⚠️ 전송 실패:", e)
        return None


def fmt_group(group, n, note=""):
    """같은 상품 묶음 1개 → (제목 줄, 나머지 줄) 알림 항목 텍스트"""
    e = html.escape
    it, info = group[0]
    meta = " · ".join(x for x in [it["price"], it["delivery"], info.get("unit", ""), info.get("hist", "")] if x)
    stats = ""
    if it["votes"] not in ("", "0") or it["comments"] not in ("", "0"):
        stats = f"  👍{it['votes']} 💬{it['comments']}"
    links = " · ".join(f'<a href="{e(x["url"])}">{x["source"]}</a>' for x, _ in group)
    head = f'{n}. <a href="{e(it["url"])}">{e(it["title"])}</a>'
    tag = f"🔑 {e(info['word'])}" if info.get("word") else ""
    line2 = "   " + " · ".join(x for x in [tag, e(meta)] if x) + stats + f" · {links}"
    if note:
        line2 += f"\n   💬 {e(note)}"
    return head, line2


def render(msg):
    """저장해 둔 알림 메시지를 다시 그림 (종료된 딜은 취소선 + ⛔)"""
    out = msg["head"]
    for b in msg["blocks"]:
        out += (f"\n<s>{b['line1']}</s> ⛔종료\n{b['rest']}\n" if b.get("ended")
                else f"\n{b['line1']}\n{b['rest']}\n")
    return out


def send_alert(state, title, entries, kind, notes=None):
    """entries: [(item, info)] → 같은 상품끼리 묶어 👍/👎 버튼과 함께 전송. info={'word','unit','hist'}"""
    if not entries or not state.get("chat_id"):
        return
    groups = group_similar(entries)
    msgs, cur, btns, row = [], {"head": f"{title} {len(groups)}건\n", "blocks": []}, [], []
    for n, g in enumerate(groups, 1):
        it = g[0][0]
        line1, rest = fmt_group(g, n, (notes or {}).get(it["id"], ""))
        block = {"ids": [x["id"] for x, _ in g], "line1": line1, "rest": rest}
        if len(render(cur)) + len(line1) + len(rest) > 3700 or len(btns) >= 20:
            if row:
                btns.append(row)
            msgs.append((cur, btns))
            cur, btns, row = {"head": "", "blocks": []}, [], []
        cur["blocks"].append(block)
        row += [{"text": f"{n} 👍", "callback_data": f"up|{it['id']}"},
                {"text": f"{n} 👎", "callback_data": f"dn|{it['id']}"}]
        if len(row) == 4:
            btns.append(row)
            row = []
        for x, info in g:
            state.setdefault("alerted", {})[x["id"]] = {"title": x["title"], "word": info.get("word"),
                                                        "kind": kind, "sig": sorted(sig(x["title"])),
                                                        "t": time.time()}
    if row:
        btns.append(row)
    msgs.append((cur, btns))
    for m, b in msgs:
        res = send(state["chat_id"], render(m), b)
        if res and res.get("message_id"):     # 종료 표시를 위해 24시간 보관
            state.setdefault("msgs", []).append({**m, "mid": res["message_id"], "buttons": b, "t": time.time()})
    state["msgs"] = [m for m in state.get("msgs", []) if time.time() - m["t"] < 24 * 3600]


def mark_ended(state, ended_ids):
    """보낸 알림 속 딜이 종료되면 그 메시지를 수정해 취소선 표시"""
    for m in state.get("msgs", []):
        hit = [b for b in m["blocks"] if not b.get("ended") and set(b["ids"]) & ended_ids]
        if not hit:
            continue
        for b in hit:
            b["ended"] = True
        try:
            extra = {"reply_markup": {"inline_keyboard": m["buttons"]}} if m.get("buttons") else {}
            tg("editMessageText", chat_id=state["chat_id"], message_id=m["mid"], text=render(m),
               parse_mode="HTML", link_preview_options={"is_disabled": True}, **extra)
            log(f"종료 표시: {len(hit)}건 (메시지 {m['mid']})")
        except Exception as e:
            log("⚠️ 종료 표시 실패:", e)


# ───────────────────────── 딜 확인 ─────────────────────────
def scan_slot(now):
    """3시간 단위 슬롯 번호. 새벽 3~6시(슬롯 1)는 건너뜀"""
    slot = now.hour // 3
    return None if slot == 1 else now.strftime("%Y%m%d-") + str(slot)


# 사이트별 (수집 함수, 최대 페이지, 페이지 사이 대기초). 펨코는 보안 시스템이 잦은 요청을 막으므로 천천히
SOURCES = {"fmkorea": (fetch_fmkorea_page, 3, 10), "ppomppu": (fetch_ppomppu_page, 8, 2)}
LABEL = {"fmkorea": "에펨코리아", "ppomppu": "뽐뿌"}


def is_blocked(state, name):
    return time.time() < state.get("blocked_until", {}).get(name, 0)


def is_popular(it):
    rule = POP.get(it["id"][:2] == "fm" and "fmkorea" or "ppomppu", {})
    return num(it["votes"]) >= rule.get("votes", 10 ** 9) or num(it["comments"]) >= rule.get("comments", 10 ** 9)


def collect(state, data, names):
    """names 사이트의 새 글 수집 → 즉시 알림/인기 알림 보내고 나머지 키워드 딜은 queue에 모음"""
    kws = data["keywords"]
    seen = set(state.get("seen", []))
    fetched = []
    pending = state.setdefault("pending", {})
    for name in names:
        fn, pages, delay = SOURCES[name]
        if is_blocked(state, name):
            pending[name] = True
            log(f"{name}: 접속 제한 중이라 나중에 다시 시도")
            continue
        min_pages = POP.get(name, {}).get("pages", 1) if POP.get("enabled", True) else 1
        try:
            got, err = paginate(fn, seen, pages, delay, min_pages)
        except Exception as e:
            got, err = [], e
        if got:
            log(f"{name}: {len(got)}건 수집")
            fetched += got
        if err is None:
            pending.pop(name, None)
            state.setdefault("fail", {})[name] = 0
            continue
        # 실패: 차단 시간을 기록하고 풀리면 다시 시도 (Retry-After 준수, 재시도로 몰아붙이지 않음)
        wait = retry_after(err)
        state.setdefault("blocked_until", {})[name] = time.time() + wait
        pending[name] = True
        n = state.setdefault("fail", {}).get(name, 0) + 1
        state["fail"][name] = n
        log(f"⚠️ {name} 수집 실패 ({n}회 연속, {wait // 60}분 뒤 재시도): {err}")
        today = datetime.now(KST).strftime("%Y-%m-%d")
        warned = state.setdefault("warned", {})
        if n >= 6 and warned.get(name) != today and state.get("chat_id"):   # 하루 1번만 알림
            warned[name] = today
            send(state["chat_id"], f"⚠️ {LABEL[name]} 보안 시스템이 자동 접속을 계속 막고 있어요. "
                                   f"막힌 동안의 {LABEL[name]} 딜은 알림이 늦거나 빠질 수 있습니다.")

    now = time.time()
    first_run = not state.get("seen")
    watch = state.setdefault("watch", {})
    init_watch = not watch
    new_items = [it for it in fetched if it["id"] not in seen]
    state["seen"] = state.get("seen", []) + [it["id"] for it in new_items]
    log(f"새 글 {len(new_items)}건")

    # 인기 추적용: 새 글은 처음 본 시각을 기록, 이미 추적 중인 글은 추천·댓글 갱신
    for it in fetched:
        if it["id"] in watch:
            watch[it["id"]].update({k: it[k] for k in ("votes", "comments", "ended")})
        elif it["id"] not in seen or init_watch:
            watch[it["id"]] = {**it, "first": now, "done": init_watch and is_popular(it)}
    max_age = POP.get("max_age_hours", 8) * 3600
    state["watch"] = watch = {k: v for k, v in watch.items() if now - v["first"] < max_age}
    mark_ended(state, {it["id"] for it in fetched if it["ended"]})

    if first_run:
        log("첫 실행: 기존 글은 기록만 함")
        return

    urgent, queued = [], 0
    for it in new_items:
        if it["ended"]:
            continue
        m = match_keyword(it["title"], kws)
        if not m:
            continue
        kw = m[0]
        unit, ok, bargain = price_check(it, kw)
        if not ok:
            log(f"단가 기준 초과로 제외: {it['title']} ({unit})")
            continue
        if recently_alerted(state, it["title"], "kw"):
            continue
        hist, cheap = history_note(state, kw, it)
        info = {"word": kw["word"], "unit": unit, "hist": hist}
        if kw.get("urgent") or bargain or cheap:
            urgent.append((it, info))
        else:
            state.setdefault("queue", []).append({"item": it, "info": info})
            queued += 1
    for it in fetched:                     # 가격 이력: 본 딜의 단가를 모두 기록 (종료 딜도 시세 참고용)
        m = match_keyword(it["title"], kws)
        if m:
            record_price(state, m[0], it)
    log(f"즉시 알림 {len(urgent)}건, 묶음 대기 {queued}건")
    send_alert(state, "⚡ <b>바로 확인할 딜</b> —", urgent, "kw")

    if POP.get("enabled", True):
        hot = []
        for w in watch.values():
            if w.get("done") or w["ended"] or not is_popular(w):
                continue
            w["done"] = True
            t = norm(w["title"])
            if any(norm(x) in t for x in CONFIG.get("exclude_keywords", []) + data.get("pop_exclude", [])):
                continue
            if blocked_by_keyword(w["title"], kws):      # 예: 항공권 키워드의 지방 출발 제외 규칙
                continue
            if recently_alerted(state, w["title"], "pop"):
                continue
            kw = (match_keyword(w["title"], kws) or [None])[0]
            unit, _, _ = price_check(w, kw)
            hist = history_note(state, kw, w)[0] if kw else ""
            hot.append({**w, "_age": max(1, round((now - w["first"]) / 3600)), "_unit": unit,
                        "_word": kw["word"] if kw else None, "_hist": hist})
        log(f"인기 딜 {len(hot)}건")
        resumed = state.pop("resumed_hours", None)
        if hot:
            reasons = popular_reasons(hot)
            entries = [(h, {"word": h["_word"], "unit": h["_unit"], "hist": h["_hist"]}) for h in hot]
            title = (f"💤 <b>{resumed}시간 쉬는 동안 인기였던 딜</b> —" if resumed
                     else "🔥 <b>지금 인기 딜</b> —")
            send_alert(state, title, entries, "pop", reasons)


def send_digest(state):
    """3시간 동안 모은 키워드 딜을 묶어서 전송 (그사이 종료된 딜은 뺌)"""
    queue, state["queue"] = state.get("queue", []), []
    watch = state.get("watch", {})
    entries = [(q["item"], q["info"]) for q in queue
               if not watch.get(q["item"]["id"], {}).get("ended")
               and not recently_alerted(state, q["item"]["title"], "kw")]   # ⚡로 이미 보낸 같은 상품은 뺌
    log(f"묶음 알림 {len(entries)}건")
    send_alert(state, "🛒 <b>핫딜 알림</b> — 관심 딜", entries, "kw")


# ───────────────────────── 메인 ─────────────────────────
def main():
    if not BOT_TOKEN and not DRY_RUN:
        sys.exit("TELEGRAM_BOT_TOKEN 이 없습니다 (.env 확인)")
    state = load_state()
    data = load_keywords()
    if CONFIG.get("chat_id"):
        state["chat_id"] = str(CONFIG["chat_id"])

    is_new = not state.get("chat_id")
    changed = False
    if not DRY_RUN:
        try:
            changed = handle_commands(state, data)
        except Exception as e:
            log("⚠️ 명령 처리 실패:", e)
    if changed:
        save_keywords(data)
        log("keywords.json 변경됨")
    if is_new and state.get("chat_id"):
        send(state["chat_id"], "✅ <b>핫딜 알리미 연결 완료</b>\n등록된 키워드에 맞는 딜은 3시간마다 묶어서, 급한 딜과 인기 딜은 바로 보내드릴게요.")

    gap = time.time() - state.get("last_run", time.time())
    if gap > 2 * 3600:                     # PC가 꺼져 있었음 → 다음 수집의 인기 딜을 '쉬는 동안' 요약으로 표시
        state["resumed_hours"] = round(gap / 3600)
        log(f"{gap / 3600:.1f}시간 만에 실행됨")

    now = datetime.now(KST)
    enabled = [n for n in SOURCES if CONFIG.get("sources", {}).get(n, True)]
    night = now.hour // 3 == 1
    due = time.time() - state.get("last_collect", 0) >= CONFIG.get("collect_minutes", 30) * 60
    retry = [n for n in enabled if state.get("pending", {}).get(n) and not is_blocked(state, n)]
    if FORCE_SCAN or (due and not night):
        collect(state, data, enabled)
        state["last_collect"] = time.time()
    elif retry and not night:
        log(f"차단이 풀린 사이트 다시 확인: {', '.join(retry)}")
        collect(state, data, retry)
    else:
        log("이번 실행은 명령만 처리")

    slot = scan_slot(now)
    if slot and slot != state.get("last_slot"):
        send_digest(state)
        state["last_slot"] = slot
    state["last_run"] = time.time()
    state["errors"] = 0
    save_state(state)


def report_error(err):
    """실행이 계속 실패하면(30분 이상) 하루 1번 텔레그램으로 알림"""
    try:
        state = load_state()
        state["errors"] = n = state.get("errors", 0) + 1
        state["last_run"] = time.time()
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if n >= 6 and state.get("err_warned") != today and state.get("chat_id") and BOT_TOKEN:
            state["err_warned"] = today
            send(state["chat_id"], f"⚠️ <b>핫딜 알리미 오류</b>\n{n}번 연속 실행에 실패했어요. "
                                   f"PC의 logs 폴더를 확인해 주세요.\n<code>{html.escape(str(err))[:300]}</code>")
        save_state(state)
    except Exception as e:
        log("⚠️ 오류 기록 실패:", e)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as err:
        import traceback
        traceback.print_exc()
        report_error(err)
        sys.exit(1)
