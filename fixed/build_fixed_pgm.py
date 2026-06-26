# -*- coding: utf-8 -*-
"""
build_fixed_pgm.py
HD/GS/CJ/LT 4개 홈쇼핑사의 homeshopping/fixed_programs/{COMPANY}.json을
대시보드 '고정PGM' 탭이 바로 쓸 수 있는 단일 파일로 합친다.

== 왜 이 스크립트가 필요한가 ==
4개사의 원본 JSON은 회사마다 필드명과 schedule 표기 형식이 전부 다르다
(LT: "📍1호점 일 08:50 📍2호점 수 19:35", CJ: "목20:45 / 토10:20",
 HD: 이미 day/time으로 파싱되어 있음, GS: "매주 토요일 17시" 류).
프론트엔드(index.html)에서 요일 탭 + 시간대 그리드를 그리려면 모든
회사가 동일한 (day, time) 슬롯 리스트를 갖고 있어야 하므로, 이 스크립트가
자유 형식 텍스트에서 "요일 + 정확한 HH:MM"만 추출해 표준화한다.

요일과 정확한 시각이 함께 파싱되지 않는 항목(요일만 있고 "오전"/"오후"
처럼 시각이 모호한 경우, 또는 요일/시각이 전혀 없는 캐치프레이즈성
문구인 경우)은 그리드에 올리지 않고 unscheduled 목록으로 따로 뺀다
(데이터 자체는 버리지 않음 - 추후 수동 보정이나 디버깅에 쓸 수 있게).

LT처럼 한 프로그램이 요일마다 다른 시각에 방송하는 경우
("일 08:50", "수 19:35" 두 슬롯)는 같은 프로그램이 슬롯마다 한 번씩,
총 2개의 그리드 항목으로 등록된다 (요일 탭을 "일"로 보면 08:50 자리에,
"수"로 보면 19:35 자리에 동일 카드가 보이는 구조).

== 입력 ==
homeshopping/fixed_programs/HD.json
homeshopping/fixed_programs/GS.json
homeshopping/fixed_programs/CJ.json
homeshopping/fixed_programs/LT.json

== 출력 ==
homeshopping/fixed_programs/merged.json
{
  "collectedAt": "2026-06-26T21:00:00+09:00",
  "companies": ["HD", "GS", "CJ", "LT"],
  "slots": [
    {
      "company": "LT",
      "day": "일",
      "time": "08:50",
      "title": "요즘 쇼핑 유리네",
      "schedule_raw": "📍1호점 일 08:50 📍2호점 수 19:35",
      "thumbnail": "https://...",
      "detail_link": "https://...",
      "upcoming_products": [...]
    },
    ...
  ],
  "unscheduled": [
    {"company": "LT", "title": "엘라이브 가구단지", "schedule_raw": "엘단지로 완성하는 우리집 스타일링"},
    ...
  ]
}

== 사용법 ==
  python build_fixed_pgm.py
"""

import os
import re
import json
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

INPUT_DIR = os.path.join("homeshopping", "fixed_programs")
OUTPUT_PATH = os.path.join(INPUT_DIR, "merged.json")

COMPANIES = ["HD", "GS", "CJ", "LT"]

WEEKDAYS_KR = ["월", "화", "수", "목", "금", "토", "일"]
WEEKDAY_FULL_TO_SHORT = {
    "월요일": "월", "화요일": "화", "수요일": "수", "목요일": "목",
    "금요일": "금", "토요일": "토", "일요일": "일",
}

# "일 08:50", "목20:45", "수 21:45", "토요일 17시 45분", "오전 09시25분" 등
# 다양한 표기에서 "요일 한 글자 + 시:분"을 한 쌍씩 뽑아내는 정규식.
# 요일 글자 뒤에 오는 첫 번째 시각 표현만 그 요일의 시각으로 간주한다.
DAY_CHAR_RE = r'[월화수목금토일]'

# 시각 표현 패턴 (아래 순서대로 시도):
#   1) "08:50" / "20:45" 같은 콜론 표기
#   2) "09시25분" / "17시 45분" / "10시" 같은 한글 표기
TIME_COLON_RE = re.compile(r'(\d{1,2}):(\d{2})')
TIME_KOREAN_RE = re.compile(r'(\d{1,2})\s*시\s*(\d{1,2})?\s*분?')


def normalize_schedule_text(text: str) -> str:
    """'매주 목요일' 같은 접두사를 보존한 채로, 요일 전체 표기를
    한 글자로 통일해 이후 파싱을 단순하게 만든다."""
    if not text:
        return ""
    normalized = text
    for full, short in WEEKDAY_FULL_TO_SHORT.items():
        normalized = normalized.replace(full, short)
    return normalized


def find_time_after(text: str, start_idx: int) -> str:
    """start_idx 이후 가장 가까운 위치에서 "HH:MM" 형태의 정확한 시각을
    찾아 'HH:MM' 문자열로 반환한다. '오후'가 시각 앞에 붙어 있으면
    12시간제를 24시간제로 보정한다 (오후 5시30분 -> 17:30).
    못 찾으면 None."""
    window = text[start_idx:start_idx + 20]  # 요일 글자 바로 다음 20자 내에서만 탐색

    # '오후' 외에도 '저녁'/'밤'은 관용적으로 오후~심야 시각을 가리킨다
    # (예: '저녁 8시 45분' = 20:45, '밤 9시 35분' = 21:35).
    # '낮'은 정오 근처(오전~초오후)를 가리키는 모호한 표현이라 보정하지
    # 않는다 - 대부분 오전 11시~오후 1시 사이로 쓰이고, 이미 12시간제
    # 입력 자체가 그 범위 안에 있어 추가 보정이 오히려 틀릴 수 있다.
    is_pm = bool(re.search(r'^\s*(오후|저녁|밤)', window))
    is_am = bool(re.search(r'^\s*오전', window))

    m = TIME_COLON_RE.search(window)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if is_pm and hh < 12:
            hh += 12
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"

    m = TIME_KOREAN_RE.search(window)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2)) if m.group(2) else 0
        if is_pm and hh < 12:
            hh += 12
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"

    return None


def parse_day_time_pairs(schedule_raw: str) -> list:
    """자유 형식 편성 텍스트에서 (요일, 'HH:MM') 쌍을 모두 추출한다.
    같은 요일이 중복 등장하면 처음 찾은 시각만 채택한다.
    하나도 못 찾으면 빈 리스트를 반환한다."""
    if not schedule_raw:
        return []

    text = normalize_schedule_text(schedule_raw)
    pairs = []
    seen_days = set()

    for m in re.finditer(DAY_CHAR_RE, text):
        day = m.group(0)
        if day in seen_days:
            continue
        time_str = find_time_after(text, m.end())
        if time_str:
            pairs.append((day, time_str))
            seen_days.add(day)

    return pairs


def load_company_json(company: str) -> dict:
    path = os.path.join(INPUT_DIR, f"{company}.json")
    if not os.path.exists(path):
        print(f"  [경고] {path} 없음, {company} 건너뜀")
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_program(company: str, raw_program: dict) -> dict:
    """회사마다 다른 필드명을 공통 스키마로 변환."""

    if company == "LT":
        return {
            "title": raw_program.get("title", ""),
            "schedule_raw": raw_program.get("schedule_raw", ""),
            "thumbnail": raw_program.get("thumbnail", ""),
            "detail_link": raw_program.get("detail_link", ""),
            "upcoming_products": raw_program.get("upcoming_products", []),
        }

    if company == "CJ":
        return {
            "title": raw_program.get("title", ""),
            "schedule_raw": raw_program.get("schedule_raw", ""),
            "thumbnail": raw_program.get("thumbnail", ""),
            "detail_link": raw_program.get("pgmshop_link", ""),
            "upcoming_products": raw_program.get("upcoming_products", []),
        }

    if company == "HD":
        return {
            "title": raw_program.get("title", ""),
            "schedule_raw": raw_program.get("schedule_raw", ""),
            "thumbnail": raw_program.get("thumbnail", ""),
            "detail_link": raw_program.get("detail_link", ""),
            "upcoming_products": raw_program.get("upcoming_products", []),
        }

    if company == "GS":
        return {
            "title": raw_program.get("title", ""),
            "schedule_raw": raw_program.get("schedule_raw", ""),
            "thumbnail": raw_program.get("thumbnail", ""),
            "detail_link": raw_program.get("detail_link", ""),
            "upcoming_products": raw_program.get("upcoming_products", []),
        }

    return None


def build_slots_and_unscheduled(company: str, programs: list) -> tuple:
    slots = []
    unscheduled = []

    for raw_program in programs:
        norm = normalize_program(company, raw_program)
        if not norm or not norm["title"]:
            continue

        # HD는 스크래퍼 단계에서 이미 day(요일 한 글자)/time(HH:MM)을
        # 파싱해서 내려준다. 이 값이 있으면 재파싱보다 우선 신뢰한다
        # (schedule_raw가 여러 요일을 담은 자유 텍스트라도, day/time은
        # 그 중 첫 번째 요일만 가리키는 경우가 있어 재파싱과 결과가
        # 다를 수 있기 때문).
        pre_day = raw_program.get("day") if company == "HD" else None
        pre_time = raw_program.get("time") if company == "HD" else None

        if pre_day and pre_time:
            pairs = [(pre_day, pre_time)]
        else:
            pairs = parse_day_time_pairs(norm["schedule_raw"])

        if not pairs:
            unscheduled.append({
                "company": company,
                "title": norm["title"],
                "schedule_raw": norm["schedule_raw"],
            })
            continue

        for day, time_str in pairs:
            slots.append({
                "company": company,
                "day": day,
                "time": time_str,
                "title": norm["title"],
                "schedule_raw": norm["schedule_raw"],
                "thumbnail": norm["thumbnail"],
                "detail_link": norm["detail_link"],
                "upcoming_products": norm["upcoming_products"],
            })

    return slots, unscheduled


def main():
    all_slots = []
    all_unscheduled = []
    loaded_companies = []

    for company in COMPANIES:
        data = load_company_json(company)
        if data is None:
            continue

        programs = data.get("programs", [])
        slots, unscheduled = build_slots_and_unscheduled(company, programs)

        all_slots.extend(slots)
        all_unscheduled.extend(unscheduled)
        loaded_companies.append(company)

        print(f"  [{company}] 원본 {len(programs)}개 -> 그리드 슬롯 {len(slots)}개, "
              f"미표시(시각 불명확) {len(unscheduled)}개")

    # 보기 좋은 순서로 정렬: 요일(월~일) -> 시각 -> 회사
    day_order = {d: i for i, d in enumerate(WEEKDAYS_KR)}
    all_slots.sort(key=lambda s: (day_order.get(s["day"], 99), s["time"], s["company"]))

    payload = {
        "collectedAt": datetime.now(KST).isoformat(),
        "companies": loaded_companies,
        "slots": all_slots,
        "unscheduled": all_unscheduled,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n저장 완료: {OUTPUT_PATH}")
    print(f"  전체 그리드 슬롯: {len(all_slots)}개")
    print(f"  미표시(시각 불명확) 항목: {len(all_unscheduled)}개")
    if all_unscheduled:
        print("  -> 미표시 목록:")
        for u in all_unscheduled:
            print(f"     [{u['company']}] {u['title']} | {u['schedule_raw']!r}")


if __name__ == "__main__":
    main()
