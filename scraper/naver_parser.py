"""
naver_parser.py
방영드라마 / 방영예능 네이버 검색 위젯 HTML 파서 (공용 모듈)
"""
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup

DAY_ORDER = ["월", "화", "수", "목", "금", "토", "일"]
DAY_INDEX = {d: i for i, d in enumerate(DAY_ORDER)}


def expand_days(day_token: str):
    """'월~목' -> ['월','화','수','목'] / '월, 화' -> ['월','화'] / '월~수, 금' -> ['월','화','수','금']"""
    days = []
    # 공백 및 불필요한 문장 부호 제거
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
                # 정의되지 않은 요일 텍스트 예외 처리
                continue
        else:
            if part in DAY_INDEX:
                days.append(part)
    return days


def parse_schedule_text(schedule_text: str):
    """'(월, 화) 오후 08:50' 등을 [{'days':[...], 'time': '...'}] 리스트로 변환"""
    groups = re.findall(r'\(([^)]+)\)\s*((?:오전|오후)\s*\d{1,2}:\d{2})', schedule_text)
    results = []
    for day_token, time_token in groups:
        expanded = expand_days(day_token)
        if expanded:
            results.append({"days": expanded, "time": time_token.strip()})
    return results


def parse_card(li, category: str, base_url: str = ""):
    """li.info_box 하나 -> 방영 슬롯별로 펼쳐진 program dict 리스트"""
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
        programs.append({
            "id": f"{category}_{title}_{'-'.join(slot['days'])}_{slot['time']}",
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


def parse_cards_from_html(html: str, category: str, min_rating: float = 5.0, base_url: str = "", debug: bool = False):
    """HTML 문자열 전체에서 li.info_box 를 모두 찾아 파싱 + 시청률 필터링 (debug 인자 추가)"""
    soup = BeautifulSoup(html, 'lxml')
    results = []
    for li in soup.select('li.info_box'):
        for p in parse_card(li, category, base_url=base_url):
            if p["rating"] >= min_rating:
                results.append(p)
    return results


def dedupe_programs(programs: list):
    """동일 id 중복 제거"""
    seen = set()
    out = []
    for p in programs:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        out.append(p)
    return out
