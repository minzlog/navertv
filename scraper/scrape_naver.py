"""
scrape_naver.py
네이버 "방영중한국드라마" / "방영중한국예능" 검색 위젯 수집
중복 카드 자동 병합 및 요일 완전 복원 로직 탑재
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

DRAMA_URL = "https://search.naver.com/search.naver?where=nexearch&sm=top_hty&fbm=0&ie=utf8&query=%EB%B0%A9%EC%98%81%EC%A4%91%ED%95%9C%EA%B5%AD%EB%93%9C%EB%9D%BC%EB%A7%88"
VARIETY_URL = "https://search.naver.com/search.naver?where=nexearch&sm=top_hty&fbm=0&ie=utf8&query=%EB%B0%A9%EC%98%81%EC%A4%91%ED%95%9C%EA%B5%AD%EC%98%88%EB%8A%A5"

MIN_RATING_DRAMA = 5.0
MIN_RATING_VARIETY = 1.0
KST = timezone(timedelta(hours=9))

DAY_ORDER = ["월", "화", "수", "목", "금", "토", "일"]
DAY_INDEX = {d: i for i, d in enumerate(DAY_ORDER)}
DEBUG = False


def to_local_date_str(d):
    return f"{d.getFullYear()}-{str(d.getMonth() + 1).zfill(2)}-{str(d.getDate()).zfill(2)}"


def monday_of(date_obj):
    return date_obj - timedelta(days=date_obj.weekday())


# ==========================================
#              파서 및 병합 로직
# ==========================================

def expand_days(day_token: str):
    days = []
    clean_token = day_token.replace(" ", "").strip()
    for part in [p.strip() for p in clean_token.split(",")]:
        if not part:
            continue
        if "~" in part:
            try:
                start, end = [p.strip() for p in part.split("~")]
                si, ei = DAY_INDEX[start], DAY_INDEX[end]
                days.extend(DAY_ORDER[si:ei + 1])
            except KeyError:
                continue
        else:
            if part in DAY_INDEX:
                days.append(part)
    return days


def parse_schedule_text(schedule_text: str):
    groups = re.findall(r'\(([^)]+)\)\s*((?:오전|오후)\s*\d{1,2}:\d{2})', schedule_text)
    results = []
    for day_token, time_token in groups:
        expanded = expand_days(day_token)
        if expanded:
            results.append({"days": expanded, "time": time_token.strip()})
    return results


def parse_card(li, category: str, base_url: str = ""):
    title_tag = li.select_one('strong.title a')
    if not title_tag:
        return []
    title = title_tag.get_text(strip=True)
    link = title_tag.get('href', '')
    if base_url and link:
        link = urljoin(base_url, link)

    info_txt = li.select_one('div.main_info span.info_txt')
    if not info_txt:
        return []
    broadcaster_tag = info_txt.select_one('a.broadcaster')
    channel = broadcaster_tag.get_text(strip=True) if broadcaster_tag else ""
    full = info_txt.get_text(strip=True)
    schedule_text = full.replace(channel, "", 1).strip()
    slots = parse_schedule_text(schedule_text)
    if not slots:
        return []

    sub_info = li.select_one('div.sub_info span.info_txt')
    rating = None
    rating_date = None
    if sub_info:
        num_txt = sub_info.select_one('span.num_txt')
        if num_txt:
            try:
                rating = float(num_txt.get_text(strip=True).replace('%', ''))
            except ValueError:
                rating = None
        m = re.search(r'\(([\d.]+)\)', sub_info.get_text(strip=True))
        if m:
            rating_date = m.group(1).rstrip('.')

    if rating is None:
        return []

    programs = []
    for slot in slots:
        # 🔥 핵심 수정: 식별자(ID)에서 요일과 시간을 빼버리고 오직 카테고리+제목+채널로만 고정
        programs.append({
            "id": f"{category}_{title}_{channel}",
            "category": category,
            "channel": channel,
            "title": title,
            "days": slot["days"],
            "time": slot["time"],
            "rating": rating,
            "ratingDate": rating_date,
            "link": link,
        })
    return programs


def dedupe_programs(programs: list):
    """중복 카드가 들어오면 요일(days)을 영리하게 합쳐버립니다."""
    merged = {}
    for p in programs:
        key = p["id"]
        if key not in merged:
            merged[key] = p
        else:
            # 기존 카드와 새 카드의 요일을 합집합으로 묶음
            existing_days = set(merged[key]["days"])
            existing_days.update(p["days"])
            # 요일 순서(월~일)에 맞게 재정렬
            merged[key]["days"] = [d for d in DAY_ORDER if d in existing_days]
    return list(merged.values())


def parse_cards_from_html(html: str, category: str, min_rating: float = 5.0, base_url: str = ""):
    soup = BeautifulSoup(html, 'lxml')
    results = []
    for li in soup.select('li.info_box'):
        for p in parse_card(li, category, base_url=base_url):
            if p["rating"] >= min_rating:
                results.append(p)
    return results


# ==========================================
#            페이지 전환 감지 헬퍼
# ==========================================

VISIBLE_SIG_JS = """
    () => Array.from(document.querySelectorAll('li.info_box'))
        .filter(el => el.offsetParent !== null)
        .map(el => {
            const titleEl = el.querySelector('strong.title');
            return titleEl ? titleEl.innerText.trim() : '';
        })
        .filter(t => t.length > 0)
        .join('|')
"""

PAGING_TEXT_JS = """
    () => {
        const el = document.querySelector('.cm_paging_area._kgs_page')
            || document.querySelector('.cm_paging_area')
            || document.querySelector('[class*="paging"]');
        return el ? el.innerText.replace(/\\s+/g, ' ').trim() : null;
    }
"""

def visible_signature(page):
    return page.evaluate(VISIBLE_SIG_JS)

def read_paging_text(page):
    try:
        return page.evaluate(PAGING_TEXT_JS)
    except Exception:
        return None

def parse_current_total(paging_text):
    if not paging_text:
        return None, None
    m = re.search(r'현재\s*(\d+)\s*전체\s*(\d+)', paging_text)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))

def click_next_and_wait(page, before_paging_text, before_visible_sig, timeout_s=12):
    next_btn = page.query_selector("a.pg_next._next")
    if not next_btn:
        return False

    aria_disabled = next_btn.get_attribute("aria-disabled")
    classes = next_btn.get_attribute("class") or ""
    if aria_disabled == "true" or "on" not in classes.split():
        return False

    try:
        next_btn.scroll_into_view_if_needed(timeout=3000)
        page.wait_for_timeout(200)
        # JS 네이티브 클릭으로 강제 실행
        next_btn.evaluate("node => node.click()")
    except Exception:
        return False

    steps = int(timeout_s / 0.5)
    for _ in range(steps):
        page.wait_for_timeout(500)
        after_paging_text = read_paging_text(page)
        cur, tot = parse_current_total(after_paging_text)
        before_cur, before_tot = parse_current_total(before_paging_text)
        if cur is not None and before_cur is not None and cur != before_cur:
            return True
        after_visible_sig = visible_signature(page)
        if after_visible_sig and before_visible_sig and after_visible_sig != before_visible_sig:
            return True
    return False


def click_all_days_tab(page, category):
    """
    🔥 핵심 수정: 네이버가 겉핥기 클릭을 무시하지 못하도록 a 태그 심장부를 JS로 직접 때립니다.
    """
    try:
        target_selector = "div.cm_tap_area ul > li > a > span.menu._text"
        page.wait_for_selector(target_selector, timeout=10000)
        tabs = page.query_selector_all(target_selector)
        
        for tab in tabs:
            if "전체" in tab.inner_text().strip():
                tab.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(500)
                # Playwright의 click() 대신 DOM 객체(a)를 강제 호출하여 화면 갱신 100% 보장
                tab.evaluate("node => { const link = node.closest('a') || node; link.click(); }")
                print(f"  [{category}] 🚀 '전체' 요일 탭 뇌관 직접 타격 성공!")
                page.wait_for_timeout(2500) # 네이버 비동기 렌더링 완충 시간
                return True
    except Exception as e:
        print(f"  [{category}] '전체' 탭 타격 실패 (기본 화면으로 계속 진행): {e}")
    return False


# ---------- 데이터 수집 함수 ----------

def fetch_drama(page):
    page.goto(DRAMA_URL, wait_until="networkidle", timeout=30000)
    click_all_days_tab(page, "drama")
    html = page.content()
    programs = parse_cards_from_html(html, "drama", min_rating=MIN_RATING_DRAMA, base_url=DRAMA_URL)
    return dedupe_programs(programs)


def fetch_variety(page, max_pages: int = 30):
    page.goto(VARIETY_URL, wait_until="networkidle", timeout=30000)
    click_all_days_tab(page, "variety")

    all_programs = []
    page_num = 1
    max_retries_per_page = 3

    while page_num <= max_pages:
        page.wait_for_timeout(800)
        paging_text = read_paging_text(page)
        cur, tot = parse_current_total(paging_text)
        html = page.content()

        programs = parse_cards_from_html(html, "variety", min_rating=MIN_RATING_VARIETY, base_url=VARIETY_URL)
        all_programs.extend(programs)

        print(f"  [variety] page {page_num} (네이버 표시: 현재{cur}/전체{tot}) 수집 중...")

        if cur is not None and tot is not None and cur >= tot:
            break

        before_paging_text = paging_text
        before_visible_sig = visible_signature(page)

        advanced = False
        for attempt in range(1, max_retries_per_page + 1):
            advanced = click_next_and_wait(page, before_paging_text, before_visible_sig)
            if advanced:
                break
            page.wait_for_timeout(1000)

        if not advanced:
            break
        page_num += 1

    return dedupe_programs(all_programs)


# ---------- 저장 분배 처리기 ----------

def dispatch_and_merge_by_matching_week(out_dir: str, programs: list):
    current_year = datetime.now(KST).year
    buckets = {}
    
    for p in programs:
        r_date_str = p.get("ratingDate")
        if not r_date_str:
            target_monday = monday_of(datetime.now(KST).date())
        else:
            try:
                month, day = map(int, r_date_str.split('.'))
                actual_date = datetime(current_year, month, day).date()
                target_monday = monday_of(actual_date)
            except Exception:
                target_monday = monday_of(datetime.now(KST).date())
                
        file_name_key = target_monday.isoformat() 
        if file_name_key not in buckets:
            buckets[file_name_key] = []
        buckets[file_name_key].append(p)

    for file_date, p_list in buckets.items():
        file_path = os.path.join(out_dir, f"{file_date}.json")
        week_start_date = datetime.fromisoformat(file_date).date()
        week_end = (week_start_date + timedelta(days=6)).isoformat()
        
        if os.path.exists(file_path):
            try:
                with open(file_path, encoding="utf-8") as f:
                    existing_data = json.load(f)
                existing_programs = existing_data.get("programs", [])
            except Exception:
                existing_programs = []
        else:
            existing_programs = []
            
        # 파일에 저장하기 전, 기존 데이터와 새 데이터 간의 중복도 완벽 차단 및 병합
        by_id = {p["id"]: p for p in existing_programs}
        for p in p_list:
            if p["id"] in by_id:
                existing_days = set(by_id[p["id"]]["days"])
                existing_days.update(p["days"])
                p["days"] = [d for d in DAY_ORDER if d in existing_days]
            by_id[p["id"]] = p
            
        merged_payload = {
            "weekStart": file_date,
            "weekEnd": week_end,
            "collectedAt": datetime.now(KST).isoformat(),
            "programs": list(by_id.values()),
        }
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(merged_payload, f, ensure_ascii=False, indent=2)
            
        print(f"  [Merge Success] {file_date}.json 파일에 {len(merged_payload['programs'])}개 데이터 누적 완료!")


def main():
    global DEBUG
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="../data/dramavariety")
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()
    DEBUG = args.debug

    CURRENT_FILE_DIR = os.path.dirname(os.path.abspath(__file__))
    final_out_dir = os.path.isabs(args.out_dir) and args.out_dir or os.path.normpath(os.path.join(CURRENT_FILE_DIR, args.out_dir))
    os.makedirs(final_out_dir, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headful, args=["--disable-dev-shm-usage"])
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            locale="ko-KR"
        )
        page.set_default_timeout(25000)

        print("collecting drama...")
        drama_programs = fetch_drama(page)

        print("collecting variety...")
        variety_programs = fetch_variety(page, max_pages=args.max_pages)

        browser.close()

    all_raw_programs = drama_programs + variety_programs
    dispatch_and_merge_by_matching_week(final_out_dir, all_raw_programs)


if __name__ == "__main__":
    main()
