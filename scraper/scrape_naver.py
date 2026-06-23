"""
scrape_naver.py
네이버 "방영중한국드라마" / "방영중한국예능" 검색 위젯을 수집해
주차별(월~일) JSON 스냅샷으로 저장한다.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

# 실행 경로 문제 방지 (현재 파일 디렉토리와 작업 디렉토리 우선 탐색)
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from naver_parser import parse_cards_from_html, dedupe_programs

DRAMA_URL = (
    "https://search.naver.com/search.naver?where=nexearch&sm=tab_etc&qvt=0"
    "&query=%EB%B0%A9%EC%98%81%EC%A4%91%ED%95%9C%EA%B5%AD%EB%93%9C%EB%9D%BC%EB%A7%88"
)
VARIETY_URL = (
    "https://search.naver.com/search.naver?sm=tab_hty.top&where=nexearch&ssc=tab.nx.all"
    "&query=%EB%B0%A9%EC%98%81%EC%A4%91%ED%95%9C%EA%B5%AD%EC%98%88%EB%8A%A5"
)

# ⚠️ [수정] 시청률 커트라인 기준을 1.0%로 조정!
MIN_RATING_DRAMA = 5.0
MIN_RATING_VARIETY = 1.0
KST = timezone(timedelta(hours=9))

DEBUG = False


def dprint(*args):
    if DEBUG:
        print("  [debug]", *args)


def monday_of(date_obj):
    return date_obj - timedelta(days=date_obj.weekday())


# ---------- 페이지 전환 감지 헬퍼 ----------

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


# ---------- 드라마 ----------

def fetch_drama(page):
    page.goto(DRAMA_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_selector("li.info_box", timeout=15000)
    html = page.content()
    try:
        raw_card_count = len(page.query_selector_all("li.info_box"))
    except Exception:
        raw_card_count = "?"
    print(f"  [drama] 전체카드(보이는+숨김 포함)={raw_card_count}개")
    programs = parse_cards_from_html(html, "drama", min_rating=MIN_RATING_DRAMA, base_url=DRAMA_URL, debug=DEBUG)
    return dedupe_programs(programs)


# ---------- 예능 ----------

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
    except Exception:
        pass

    try:
        next_btn.click(timeout=5000)
    except Exception as e:
        try:
            next_btn.click(force=True, timeout=5000)
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


def fetch_variety(page, max_pages: int = 30):
    page.goto(VARIETY_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_selector("li.info_box", timeout=15000)

    all_programs = []
    page_num = 1
    max_retries_per_page = 3

    while page_num <= max_pages:
        page.wait_for_timeout(500)
        
        paging_text = read_paging_text(page)
        cur, tot = parse_current_total(paging_text)
        html = page.content()

        try:
            raw_card_count = len(page.query_selector_all("li.info_box:visible"))
        except Exception:
            raw_card_count = "?"

        programs = parse_cards_from_html(html, "variety", min_rating=MIN_RATING_VARIETY, base_url=VARIETY_URL, debug=DEBUG)
        all_programs.extend(programs)

        print(
            f"  [variety] page {page_num} (네이버 표시: 현재{cur}/전체{tot}) "
            f"전체카드={raw_card_count}개 중 >= {MIN_RATING_VARIETY}%: {len(programs)}건"
        )

        if cur is not None and tot is not None and cur >= tot:
            print(f"  [variety] 네이버 페이지카운터 기준 마지막 페이지 도달 ({cur}/{tot})")
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
            print(f"  [variety] page {page_num}에서 {max_retries_per_page}회 재시도 후에도 "
                  f"다음 페이지로 못 넘어감 -> 수집 종료")
            break

        page_num += 1

    return dedupe_programs(all_programs)


# ---------- 저장/병합 ----------

def merge_into_week_file(out_path: str, new_programs: list, week_start: str, week_end: str):
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            existing = json.load(f)
        existing_programs = existing.get("programs", [])
    else:
        existing_programs = []

    by_id = {p["id"]: p for p in existing_programs}
    for p in new_programs:
        by_id[p["id"]] = p

    merged = {
        "weekStart": week_start,
        "weekEnd": week_end,
        "collectedAt": datetime.now(KST).isoformat(),
        "programs": list(by_id.values()),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


def main():
    global DEBUG
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="../data/dramavariety")
    parser.add_argument("--max-pages", type=int, default=30, help="예능 위젯 최대 순회 페이지 수")
    parser.add_argument("--debug", action="store_true", help="진행 상황 출력")
    parser.add_argument("--headful", action="store_true", help="브라우저 활성화 실행")
    args = parser.parse_args()
    DEBUG = args.debug

    os.makedirs(args.out_dir, exist_ok=True)

    today = datetime.now(KST).date()
    week_start_date = monday_of(today)
    week_end_date = week_start_date + timedelta(days=6)
    week_start = week_start_date.isoformat()
    week_end = week_end_date.isoformat()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not args.headful,
            args=["--disable-dev-shm-usage"],
        )
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="ko-KR",
        )
        page.set_default_timeout(20000)

        print("collecting drama...")
        drama_programs = fetch_drama(page)
        print(f"  drama total: {len(drama_programs)} programs >= {MIN_RATING_DRAMA}%")

        print("collecting variety...")
        variety_programs = fetch_variety(page, max_pages=args.max_pages)
        print(f"  variety total: {len(variety_programs)} programs >= {MIN_RATING_VARIETY}%")

        browser.close()

    all_programs = drama_programs + variety_programs
    out_path = os.path.join(args.out_dir, f"{week_start}.json")
    merged = merge_into_week_file(out_path, all_programs, week_start, week_end)

    print(f"saved {len(merged['programs'])} programs -> {out_path}")


if __name__ == "__main__":
    main()
