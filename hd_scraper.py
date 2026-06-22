# -*- coding: utf-8 -*-
"""
현대홈쇼핑(HD) 편성표 수집기

라이브방송(TV쇼핑) / 데이터방송(TV+샵) 편성을 공통 스키마로 변환해 저장한다.

== 저장 구조 ==
homeshopping/
├── HD_live/{YYYY-MM}.json   라이브(TV쇼핑)
└── HD_data/{YYYY-MM}.json   데이터방송(TV+샵)

== 공통 스키마 (월 파일 안에 날짜별 누적) ==
{
  "company": "HD", "broadcast": "live", "month": "2026-06",
  "days": {
    "2026-06-22": [
      {"start":"08:00","end":"09:59","brand":"미우미우","product":"하프문 숄더백",
       "price":39000,"link":"https://..."}
    ]
  }
}

== 수집 정책 ==
오늘 기준 -1일 ~ +5일(7일)을 매번 수집.
과거(오늘 이전) 날짜가 이미 기록돼 있으면 다시 안 건드리고 보존, 오늘+미래만 갱신.

== 사용법 ==
  pip install requests
  python hd_scraper.py
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from categorize import classify_batch
from clean_product import clean_product_name

KST = timezone(timedelta(hours=9))
OUTPUT_DIR = "homeshopping"
REQUEST_DELAY = 0.4
DAYS_RANGE = range(-1, 6)  # 어제 ~ +5일

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def today_kst():
    return datetime.now(KST)


def parse_price(v):
    """가격을 원 단위 정수로 정규화. '69,900' / 69900 / None 등 처리."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def add_categories(programs):
    """
    1) 원본 상품명으로 카테고리 분류 (분류 모델은 원본 패턴으로 학습됨)
    2) 분류가 끝난 뒤 product 필드를 화면 표시용으로 정제
    """
    if not programs:
        return programs
    pairs = [(p["brand"], p["product"]) for p in programs]
    categories = classify_batch(pairs)
    for p, cat in zip(programs, categories):
        p["category"] = cat
        p["product"] = clean_product_name(p["product"])
    return programs


def fetch_hyundai(date_compact, broad_param):
    """
    date_compact: 'YYYYMMDD'
    broad_param: 'etv'(라이브/TV쇼핑) | 'dtv'(데이터/TV+샵)
    페이지 0~7을 순회 후 (시작시각, 상품코드)로 중복 제거.
    """
    headers = {"User-Agent": UA, "Referer": "https://www.hmall.com/"}
    seen = {}
    for page in range(0, 8):
        url = (f"https://www.hmall.com/md/api/cache?url=/api/hf/dp/v1/main-tv-new/tv-list"
               f"&brodDt={date_compact}&brodPrrgPage={page}&brodType={broad_param}&deviceInfo=pc")
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                continue
            items = r.json().get("respData", {}).get("broadItemList", []) or []
            for it in items:
                key = (it.get("brodStrtDtm"), it.get("slitmCd"))
                if key[0] is None:
                    continue
                slitm = it.get("slitmCd")
                seen[key] = {
                    "start": it.get("brodStrtDtm", ""),
                    "end": it.get("brodEndDtm", ""),
                    "brand": it.get("brndNm", "") or "",
                    "product": it.get("convertedSlitmNm") or it.get("slitmNm") or "",
                    "price": parse_price(it.get("sellPrc")),
                    "link": f"https://www.hmall.com/md/pda/itemPtc?slitmCd={slitm}&preview=true" if slitm else "",
                }
        except Exception as e:
            print(f"    [HD] page {page} 오류: {e}")
        time.sleep(0.15)

    programs = sorted(seen.values(), key=lambda x: x["start"])
    return programs


BROADCASTS = [
    ("live", "etv"),
    ("data", "dtv"),
]


def load_month(sub_dir, ym):
    path = os.path.join(sub_dir, f"{ym}.json")
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8")).get("days", {})
        except Exception:
            return {}
    return {}


def main():
    base = today_kst()
    today_str = base.strftime("%Y-%m-%d")

    for broadcast, broad_param in BROADCASTS:
        sub_dir = os.path.join(OUTPUT_DIR, f"HD_{broadcast}")
        os.makedirs(sub_dir, exist_ok=True)
        month_data = {}

        for offset in DAYS_RANGE:
            d = base + timedelta(days=offset)
            date_compact = d.strftime("%Y%m%d")
            date_dash = d.strftime("%Y-%m-%d")
            ym = d.strftime("%Y-%m")
            if ym not in month_data:
                month_data[ym] = load_month(sub_dir, ym)
            days = month_data[ym]

            is_past = date_dash < today_str
            if is_past and days.get(date_dash):
                print(f"[HD_{broadcast}] {date_dash}: 이미 기록됨, 건너뜀")
                continue

            print(f"[HD_{broadcast}] {date_dash} 수집 중...")
            programs = fetch_hyundai(date_compact, broad_param)
            programs = add_categories(programs)

            if is_past and not programs:
                print(f"  -> 0개 (과거, 기존값 유지)")
                time.sleep(REQUEST_DELAY)
                continue

            days[date_dash] = programs
            print(f"  -> {len(programs)}개 편성")
            time.sleep(REQUEST_DELAY)

        for ym, days in month_data.items():
            if not days:
                continue
            out_path = os.path.join(sub_dir, f"{ym}.json")
            sorted_days = {k: days[k] for k in sorted(days)}
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({
                    "company": "HD", "broadcast": broadcast,
                    "month": ym, "days": sorted_days,
                }, f, ensure_ascii=False, indent=2)
            print(f"  저장: {out_path} ({len(sorted_days)}일)")

    print("\n완료.")


if __name__ == "__main__":
    main()
