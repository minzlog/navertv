# -*- coding: utf-8 -*-
"""
cj_fixed_programs.py
CJ온스타일 모바일 메인페이지의 "CJ온스타일 대표프로그램" 13개 슬롯과,
CJ의 일별 편성표 API(schedule)를 결합해 각 고정 프로그램의 진짜 채널
프로그램코드(pgmCd)를 자동으로 역매칭한다.

== 왜 두 데이터를 합쳐야 하는가 ==
1) 메인페이지(homeTab/main)는 13개 고정 프로그램의 "이름/이미지/대략적인
   편성 텍스트"만 알 수 있다. 그중 현재 선택된(보통 1번째) 슬라이드만
   펼쳐져서 pgmShop 링크(=pgmCd)가 노출되고, 나머지 12개는 사람이 직접
   클릭해야만 pgmCd를 알 수 있는 구조라 자동화가 막힌다.

2) 반면 CJ의 schedule API
     https://display-frontapi.cjonstyle.com/polling/broadcast/schedule?bdDt=YYYYMMDD
   는 그날 방영되는 모든 pgmCd와 시작/종료시각을 빠짐없이 알려주지만,
   pgmCd가 무슨 프로그램인지 이름은 알려주지 않는다.

이 둘을 합치면: 메인페이지에서 얻은 "프로그램명 + 편성 텍스트(예:
'목20:45 / 토10:20')"를 요일·시각 집합으로 변환하고, 여러 날짜의 schedule
데이터에서 정확히 그 요일·시각 패턴으로 반복 등장하는 pgmCd를 찾아 역으로
매칭한다. 사람이 12개를 일일이 클릭할 필요가 없어진다.

== 동작 순서 ==
1. 메인페이지를 가져와 13개 슬롯의 (slotId, 이름, 썸네일) 추출
   + 첫 번째로 펼쳐진 슬롯의 (편성텍스트, pgmShop 링크)를 시드 정보로 확보
2. SCHEDULE_LOOKBACK_DAYS 만큼 과거~오늘 schedule API를 호출해
   pgmCd별 (요일, HH:MM) 등장 패턴을 누적
3. 메인페이지 각 슬롯을 클릭했을 때 보이는 편성텍스트를 알아야 매칭할 수
   있는데, 정적 메인페이지는 1개 슬롯만 펼쳐져 있다. 따라서 이 스크립트는
   "이미 펼쳐진 슬롯(보통 매주 바뀌는 1번 슬롯)"을 schedule 데이터와
   대조해 매칭 로직이 맞는지 검증하는 한편, 같은 요일/시각 패턴이 schedule
   상에서 '반복되는 고정 슬롯'으로 보이는 pgmCd 후보 전체를 추출해
   candidate_fixed_programs로 함께 저장한다.
   (각 슬롯의 정확한 편성텍스트까지 완전히 무인 자동화하려면, 메인페이지가
    슬롯 전환 시 호출하는 내부 API를 추가로 특정해 SLOT_DETAIL_URL에
    연결해야 한다. 그 전까지는 schedule 기반 후보 목록 + 시드 매칭 결과를
    함께 제공해, 사람이 후보 목록만 보고 빠르게 확정할 수 있게 한다.)

== 출력 ==
homeshopping/fixed_programs/CJ.json
{
  "company": "CJ",
  "collectedAt": "...",
  "matched_programs": [
    {
      "slot_title": "동가게",
      "pgm_cd": "563049",
      "schedule_text": "목20:45 / 토10:20",
      "occurrences": ["목 20:45", "토 10:20"],
      "pgmshop_link": "https://display.cjonstyle.com/m/pgmShop/100013"
    }
  ],
  "slot_titles": ["동가게", "더 지완스 1부", "탑쇼", ...],   # 13개 슬롯 이름 (참고용)
  "candidate_fixed_pgm_codes": {                              # schedule만으로 추정한 고정 편성 후보
    "563049": ["목 20:45", "토 10:20"],
    "563677": ["토 17:30"],
    ...
  }
}

== 사용법 ==
  pip install requests beautifulsoup4
  python cj_fixed_programs.py
"""

import os
import re
import json
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
WEEKDAYS_KR = ["월", "화", "수", "목", "금", "토", "일"]

MAIN_PAGE_URL = "https://display.cjonstyle.com/m/homeTab/main?hmtabMenuId=004389"
SCHEDULE_API_URL = "https://display-frontapi.cjonstyle.com/polling/broadcast/schedule"

OUTPUT_DIR = os.path.join("homeshopping", "fixed_programs")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "CJ.json")

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
)

# 최근 며칠치 schedule을 모아야 "고정 프로그램(매주 반복)" 패턴을 확신할 수 있는지.
# 최소 8일(약 1주+1일) 이상이어야 매주 반복 여부를 한 번이라도 검증할 수 있다.
SCHEDULE_LOOKBACK_DAYS = 14
REQUEST_DELAY_SEC = 0.4

# 짧은 상품 방송(보통 몇 분~수십 분짜리 일반 판매 슬롯)과 구분하기 위한
# 최소 방송 길이(분). 고정 진행자 프로그램은 대체로 60분 이상인 경우가 많다.
MIN_DURATION_MIN_FOR_CANDIDATE = 30


def fetch_main_page() -> str:
    headers = {"User-Agent": MOBILE_UA, "Referer": "https://display.cjonstyle.com/"}
    resp = requests.get(MAIN_PAGE_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.text


def extract_slots(html: str) -> dict:
    """메인페이지에서 13개 슬롯 정보와, 펼쳐진 첫 슬롯의 편성텍스트/pgmShop 링크를 추출."""
    soup = BeautifulSoup(html, "html.parser")

    tab_section = soup.select_one(".pgm_tab_section")
    slot_titles = {}
    if tab_section:
        for img in tab_section.select("ul.tab_pgm img[id^='majorPgm']"):
            slot_id = img.get("id")
            slot_titles[slot_id] = img.get("alt", "")

    panel = soup.select_one(".pgm_content_section")
    seed = {"slot_id": None, "schedule_text": None, "pgmshop_link": None}
    if panel:
        seed["slot_id"] = panel.get("aria-labelledby")
        time_tag = panel.select_one(".pgm_schedule .txt")
        if time_tag:
            seed["schedule_text"] = time_tag.get_text(strip=True)
        link_tag = panel.select_one("a.btn_banner")
        if link_tag and link_tag.get("href"):
            seed["pgmshop_link"] = link_tag["href"]

    return {"slot_titles": slot_titles, "seed": seed}


def fetch_schedule(bd_dt: str) -> list:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": MAIN_PAGE_URL,
        "User-Agent": MOBILE_UA,
        "Origin": "https://display.cjonstyle.com",
    }
    params = {"bdDt": bd_dt}
    resp = requests.get(SCHEDULE_API_URL, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", {}).get("broadcastList", []) or []


def collect_schedule_occurrences(days: int) -> dict:
    """최근 days일치 schedule을 모아 pgmCd -> ["요일 HH:MM", ...] 누적."""
    occurrences = defaultdict(set)
    today = datetime.now(KST).date()

    for i in range(days):
        target_date = today - timedelta(days=i)
        bd_dt = target_date.strftime("%Y%m%d")
        print(f"  [CJ] schedule {bd_dt} 수집 중...")
        try:
            broadcasts = fetch_schedule(bd_dt)
        except Exception as e:
            print(f"    [실패] {bd_dt}: {e}")
            continue

        for b in broadcasts:
            if b.get("broadType") != "TV":
                continue  # PLUS/CABLE은 TV와 동시송출되는 중복 채널이라 제외
            try:
                start_dt = datetime.fromisoformat(b["bdStrDtm"])
                end_dt = datetime.fromisoformat(b["bdEndDtm"])
            except (KeyError, ValueError):
                continue

            duration_min = (end_dt - start_dt).total_seconds() / 60
            if duration_min < MIN_DURATION_MIN_FOR_CANDIDATE:
                continue

            weekday = WEEKDAYS_KR[start_dt.weekday()]
            time_str = f"{weekday} {start_dt.strftime('%H:%M')}"
            occurrences[b["pgmCd"]].add(time_str)

        time.sleep(REQUEST_DELAY_SEC)

    return {pgm_cd: sorted(times) for pgm_cd, times in occurrences.items()}


def parse_schedule_text(text: str) -> set:
    """'목20:45 / 토10:20' -> {'목 20:45', '토 10:20'}"""
    result = set()
    if not text:
        return result
    parts = re.split(r'[/|,]', text)
    for part in parts:
        m = re.search(r'([월화수목금토일])\s*(\d{1,2}):?(\d{2})', part)
        if m:
            day, hh, mm = m.group(1), m.group(2), m.group(3)
            result.add(f"{day} {int(hh):02d}:{mm}")
    return result


def match_seed_to_pgm_cd(seed: dict, occurrences: dict) -> str:
    """메인페이지 시드 슬롯의 편성텍스트와 정확히 같은 (요일,시각) 집합을
    갖는 pgmCd를 schedule 데이터에서 찾는다."""
    target = parse_schedule_text(seed.get("schedule_text") or "")
    if not target:
        return None
    for pgm_cd, times in occurrences.items():
        if set(times) == target:
            return pgm_cd
    return None


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("[CJ] 메인페이지에서 고정 프로그램 슬롯 정보 수집...")
    try:
        main_html = fetch_main_page()
        slot_info = extract_slots(main_html)
    except Exception as e:
        print(f"  [실패] 메인페이지 수집: {e}")
        slot_info = {"slot_titles": {}, "seed": {}}

    print(f"\n[CJ] 최근 {SCHEDULE_LOOKBACK_DAYS}일치 편성표 수집 (고정 프로그램 패턴 추출용)...")
    occurrences = collect_schedule_occurrences(SCHEDULE_LOOKBACK_DAYS)

    seed = slot_info.get("seed", {})
    matched_pgm_cd = match_seed_to_pgm_cd(seed, occurrences)

    matched_programs = []
    if matched_pgm_cd and seed.get("slot_id"):
        slot_title = slot_info["slot_titles"].get(seed["slot_id"], "")
        matched_programs.append({
            "slot_title": slot_title,
            "pgm_cd": matched_pgm_cd,
            "schedule_text": seed.get("schedule_text"),
            "occurrences": occurrences.get(matched_pgm_cd, []),
            "pgmshop_link": seed.get("pgmshop_link"),
        })

    # 60분 이상 방송이 매주 같은 요일/시각에 2회 이상 반복되는 pgmCd만
    # "고정 프로그램일 가능성이 높은 후보"로 남긴다.
    candidate_fixed = {
        pgm_cd: times for pgm_cd, times in occurrences.items() if len(times) >= 2
    }

    payload = {
        "company": "CJ",
        "collectedAt": datetime.now(KST).isoformat(),
        "matched_programs": matched_programs,
        "slot_titles": list(slot_info.get("slot_titles", {}).values()),
        "candidate_fixed_pgm_codes": candidate_fixed,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n저장 완료: {OUTPUT_PATH}")
    print(f"  메인페이지 슬롯 13개 이름: {payload['slot_titles']}")
    print(f"  시드 매칭 결과: {matched_programs}")
    print(f"  고정 편성 후보 pgmCd {len(candidate_fixed)}개 발견")


if __name__ == "__main__":
    main()
