#!/usr/bin/env python3
"""대시보드 집계를 그대로 구글 캘린더에 반영한다.

예전 방식("예약 1건 = 일정 1개")을 폐기하고, 대시보드(dispYeyakAdminIndex)에 찍히는
날짜별 사이트 타입 점유를 기준으로 삼는다. 캘린더에는 (날짜 × 사이트 타입)마다 종일 일정
1개만 만들고(예약이 1건 이상 있는 것만), 그 일정 설명에 그 밤에 실제로 찬 자리번호 +
예약자 이름 + (방문/취소) 횟수를 적는다. 전화 응대 때 "A-13 비었나?"를 바로 확인하기 위함.

동작 개요:
  1) 대시보드(이번 달 + 다음 달)에서 사이트 타입별 '총 개수(분모)'와 '예약 수'를 읽는다.
     (총 개수는 캘린더 제목 분모로 쓰고, 예약 수는 재구성 검증용 기준값으로 쓴다)
  2) 예약 리스트를 '체크인 날짜 범위'로 모은다. 이 리스트는 start=end=D가 "그날 체크인하는
     예약"만 주므로(점유 전체가 아님), 과거로 넉넉히(기본 185일) 당겨 모은 뒤 각 예약의
     숙박기간을 밤별로 펼쳐서 '그 밤에 찬 자리'를 재구성한다. 이렇게 하면 대시보드 점유수와
     정확히 일치한다(검증으로 확인).
  3) 재구성 결과를 캘린더에 upsert 한다. 각 일정에 extendedProperties.private 로
     dash_date/dash_site 표식을 심어, 매 실행마다 날짜·사이트 키로 찾아 업데이트/생성하고,
     0건이 되거나 사라진 (날짜×사이트)는 일정을 삭제한다.

개인정보 주의(저장소 공개): 이름 등 PII는 '구글 캘린더 일정 설명'에만 들어간다.
콘솔/로그/커밋에는 절대 출력하지 않는다(дry-run 샘플도 이름은 가린다).

필요 환경변수:
  ADMIN_ID, ADMIN_PW            예약 관리자 로그인 계정
  GOOGLE_SERVICE_ACCOUNT_JSON   구글 서비스 계정 키(JSON) 전체 문자열
  GOOGLE_CALENDAR_ID            일정을 등록할 구글 캘린더 ID
  LOOKBACK_DAYS                 체크인 조회를 며칠 과거까지 당길지 (기본 185)
  DEBUG                         "1"이면 구조적 디버그 정보 출력(PII 제외)

사용법:
  python3 scripts/sync_dashboard_calendar.py            # 확인만 (기본 dry-run, 이름 가림)
  python3 scripts/sync_dashboard_calendar.py --apply    # 실제 캘린더 반영
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_reservations as cr  # 로그인/캘린더 서비스/BASE_URL 재사용  # noqa: E402

BASE_URL = cr.BASE_URL
LIST_ACT = f"{BASE_URL}/index.php?module=admin&act=dispYeyakAdminResList"
DASH_ACT = f"{BASE_URL}/index.php?module=admin&act=dispYeyakAdminIndex"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "185"))
MAX_LIST_PAGES = int(os.environ.get("MAX_LIST_PAGES", "100"))
DEBUG = os.environ.get("DEBUG") == "1"

# 우리가 만든 일정만 골라/갱신/삭제하기 위한 표식 (개인 캘린더에 섞인 개인 일정 보호)
TAG_KEY = "pscamp_dash"
TAG_VAL = "1"

# 사이트 타입: (정규명, 제목 약칭, 구글 캘린더 colorId)
#   colorId 팔레트: 3=Grape(보라) 4=Flamingo(분홍) 5=Banana(노랑) 7=Peacock(청록)
#                   9=Blueberry(파랑) 10=Basil(초록) 11=Tomato(빨강)
SITE_TYPES = [
    ("서숲A사이트", "A", "9"),
    ("서숲B사이트", "B", "3"),
    ("서숲카라반", "카라반", "7"),
    ("서숲민박", "민박", "5"),
    ("★서숲장박★", "장박", "11"),
    ("평상", "평상", "10"),
    ("야외테이블", "야외", "4"),
]
SHORT_LABEL = {name: short for name, short, _ in SITE_TYPES}
COLOR_ID = {name: color for name, _, color in SITE_TYPES}
CANON_ORDER = [name for name, _, _ in SITE_TYPES]


def canon_site(label: str) -> str:
    """객실/대시보드 라벨을 7개 정규 사이트 타입명으로 통일한다."""
    l = label.replace(" ", "")
    if l.startswith("서숲A"):
        return "서숲A사이트"
    if l.startswith("서숲B"):
        return "서숲B사이트"
    if "카라" in l:
        return "서숲카라반"
    if "민박" in l:
        return "서숲민박"
    if "장박" in l:
        return "★서숲장박★"
    if "평상" in l:
        return "평상"
    if "야외" in l:
        return "야외테이블"
    return label


def is_cancel(status: str) -> bool:
    return "취소" in status.replace(" ", "")


# ---------- 대시보드 ----------

def fetch_dashboard_month(session: requests.Session, year: int, month: int) -> dict[datetime.date, dict[str, tuple[int, int]]]:
    """월별 대시보드에서 {날짜: {정규사이트명: (예약수, 총개수)}} 를 뽑는다."""
    url = f"{DASH_ACT}&setyear={year}&setmonth={month}"
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out: dict[datetime.date, dict[str, tuple[int, int]]] = {}
    for td in soup.find_all("td"):
        text = " ".join(td.get_text().split())
        m = re.match(r"^(\d+)\s", text)
        if not m or "(" not in text:
            continue
        pairs = re.findall(r"(\S+?)\s*\((\d+)/(\d+)\)", text)
        if not pairs:
            continue
        day = int(m.group(1))
        try:
            d = datetime.date(year, month, day)
        except ValueError:
            continue
        out[d] = {canon_site(lbl): (int(b), int(t)) for lbl, b, t in pairs}
    return out


# ---------- 예약 리스트 ----------

def parse_list_rows(html: str) -> list[dict]:
    """예약현황 리스트 표에서 한 행씩 뽑는다.

    check_reservations.parse_reservations 와 달리 '이름 옆 (방문/취소) 횟수'까지 가져온다.
    이름은 span.a-send-sms-btn 에, 방문/취소는 그 칸 텍스트의 첫 괄호에 들어있다.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for t in soup.find_all("table"):
        head = " ".join(t.get_text().split())
        if "예약번호" in head and "이름" in head and "객실" in head:
            table = t
            break
    if table is None:
        return []
    headers = [" ".join(th.get_text().split()) for th in table.find_all("th")]

    def col(name: str):
        for i, h in enumerate(headers):
            if name in h:
                return i
        return None

    idx_id, idx_room, idx_date = col("예약번호"), col("객실"), col("예약일")
    idx_name, idx_status = col("이름"), col("상태")

    rows = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds or idx_id is None or idx_id >= len(tds):
            continue
        rid = cr.normalize(tds[idx_id].get_text())
        if not rid:
            continue
        room = cr.normalize(tds[idx_room].get_text()) if idx_room is not None and idx_room < len(tds) else ""
        date = cr.normalize(tds[idx_date].get_text()) if idx_date is not None and idx_date < len(tds) else ""
        status = cr.normalize(tds[idx_status].get_text()) if idx_status is not None and idx_status < len(tds) else ""

        name, visit, cancel = "", None, None
        if idx_name is not None and idx_name < len(tds):
            cell = tds[idx_name]
            span = cell.find("span", class_="a-send-sms-btn")
            name = cr.normalize(span.get_text()) if span else ""
            mm = re.search(r"\((\d+)(?:\s*/\s*(\d+))?\)", cr.normalize(cell.get_text()))
            if mm:
                visit = int(mm.group(1))
                cancel = int(mm.group(2)) if mm.group(2) else 0
        rows.append({"id": rid, "room": room, "date": date, "name": name,
                     "visit": visit, "cancel": cancel, "status": status})
    return rows


def fetch_checkin_rows(session: requests.Session, start: datetime.date, end: datetime.date) -> list[dict]:
    """체크인 날짜가 [start, end] 안인 예약을 전부(페이지 순회) 모은다."""
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    rows = []
    for page in range(1, MAX_LIST_PAGES + 1):
        url = f"{LIST_ACT}&start_date={s}&end_date={e}&page={page}"
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        page_rows = parse_list_rows(resp.text)
        if DEBUG:
            print(f"[DEBUG] list page={page} rows={len(page_rows)}", file=sys.stderr)
        if not page_rows:
            break
        rows.extend(page_rows)
    return rows


def parse_stay(date_str: str, range_start: datetime.date, range_end: datetime.date):
    """'MM-DD~MM-DD'(연도 없음)를 (체크인, 체크아웃) date로 푼다.

    체크인은 조회 범위 [range_start, range_end] 안에 있으므로, 그 구간에 들어오는 연도를
    골라 연도 추정 오차(연말/연초, 장기체류)를 없앤다. 체크아웃은 체크인과 같은 해,
    더 이르면 다음 해로 본다.
    """
    parts = date_str.split("~")
    ci_md = parts[0].strip()
    co_md = parts[1].strip() if len(parts) > 1 else ci_md
    try:
        cm, cd = (int(x) for x in ci_md.split("-"))
    except ValueError:
        return None
    checkin = None
    for y in (range_start.year, range_end.year, range_start.year + 1):
        try:
            cand = datetime.date(y, cm, cd)
        except ValueError:
            continue
        if range_start <= cand <= range_end:
            checkin = cand
            break
    if checkin is None:
        return None
    try:
        om, od = (int(x) for x in co_md.split("-"))
        checkout = datetime.date(checkin.year, om, od)
    except ValueError:
        return None
    if checkout <= checkin:
        try:
            checkout = datetime.date(checkin.year + 1, om, od)
        except ValueError:
            checkout = checkin + datetime.timedelta(days=1)
    return checkin, checkout


# ---------- 재구성 ----------

def site_number(room: str) -> str:
    """'서숲A사이트/A-3' -> 'A-3'. '/'가 없으면 통째로."""
    return room.split("/", 1)[1].strip() if "/" in room else room.strip()


def num_sort_key(num: str):
    m = re.search(r"(\d+)", num)
    return (0, int(m.group(1)), num) if m else (1, 0, num)


def build_occupancy(rows: list[dict], win_start: datetime.date, win_end: datetime.date,
                    range_start: datetime.date, range_end: datetime.date):
    """{(날짜, 정규사이트명): [ {num,name,visit,cancel} ... ]} 로 밤별 점유를 재구성.

    창(win_start~win_end) 안의 밤만 담는다. 취소 건은 제외.
    """
    occ: dict[tuple[datetime.date, str], list[dict]] = defaultdict(list)
    for row in rows:
        if is_cancel(row["status"]):
            continue
        stay = parse_stay(row["date"], range_start, range_end)
        if not stay:
            continue
        checkin, checkout = stay
        stype = canon_site(row["room"].split("/")[0] if "/" in row["room"] else row["room"])
        num = site_number(row["room"])
        night = max(checkin, win_start)
        last = min(checkout, win_end + datetime.timedelta(days=1))
        while night < last:
            occ[(night, stype)].append({
                "num": num, "name": row["name"],
                "visit": row["visit"], "cancel": row["cancel"],
            })
            night += datetime.timedelta(days=1)
    return occ


def format_description(stype: str, entries: list[dict], total: int) -> str:
    """일정 설명: 첫 줄 요약 + 자리번호별 이름/(방문/취소). PII 포함(캘린더 전용)."""
    lines = [f"{stype} {len(entries)}/{total}"]
    for e in sorted(entries, key=lambda x: num_sort_key(x["num"])):
        who = e["name"] or "(이름없음)"
        if e["visit"] is not None:
            vc = f" (방문{e['visit']}/취소{e['cancel']})"
        else:
            vc = ""
        lines.append(f"{e['num']} {who}{vc}")
    return "\n".join(lines)


# ---------- 캘린더 upsert ----------

def list_existing(service, calendar_id: str, win_start: datetime.date, win_end: datetime.date) -> dict[tuple[str, str], dict]:
    """우리 표식이 있는 기존 일정을 (dash_date, dash_site) -> event 로 모은다."""
    existing: dict[tuple[str, str], dict] = {}
    page_token = None
    time_min = datetime.datetime.combine(win_start, datetime.time()).isoformat() + "Z"
    time_max = datetime.datetime.combine(win_end + datetime.timedelta(days=2), datetime.time()).isoformat() + "Z"
    while True:
        resp = service.events().list(
            calendarId=calendar_id, privateExtendedProperty=f"{TAG_KEY}={TAG_VAL}",
            timeMin=time_min, timeMax=time_max, maxResults=2500,
            pageToken=page_token, showDeleted=False, singleEvents=True,
        ).execute()
        for ev in resp.get("items", []):
            priv = (ev.get("extendedProperties") or {}).get("private") or {}
            key = (priv.get("dash_date", ""), priv.get("dash_site", ""))
            existing[key] = ev
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return existing


def sync_calendar(occ, dashboard, win_start, win_end, apply_changes):
    service = cr.get_calendar_service()
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
    existing = list_existing(service, calendar_id, win_start, win_end) if apply_changes else {}

    # 이번에 있어야 할 (날짜×사이트) 목록
    desired = {}
    for (d, stype), entries in occ.items():
        if not entries:
            continue
        total = dashboard.get(d, {}).get(stype, (len(entries), len(entries)))[1]
        desired[(d.isoformat(), stype)] = (d, stype, entries, total)

    created = updated = deleted = 0
    for key, (d, stype, entries, total) in sorted(desired.items()):
        summary = f"{SHORT_LABEL.get(stype, stype)} {len(entries)}/{total}"
        description = format_description(stype, entries, total)
        body = {
            "summary": summary,
            "description": description,
            "start": {"date": d.isoformat()},
            "end": {"date": (d + datetime.timedelta(days=1)).isoformat()},
            "colorId": COLOR_ID.get(stype, "8"),
            "extendedProperties": {"private": {TAG_KEY: TAG_VAL, "dash_date": d.isoformat(), "dash_site": stype}},
            "transparency": "transparent",
        }
        ev = existing.pop(key, None)
        if ev is None:
            if apply_changes:
                service.events().insert(calendarId=calendar_id, body=body).execute()
            created += 1
        else:
            need = (ev.get("summary") != summary or ev.get("description") != description
                    or ev.get("colorId") != body["colorId"])
            if need:
                if apply_changes:
                    service.events().patch(calendarId=calendar_id, eventId=ev["id"], body=body).execute()
                updated += 1
    # desired에 없는데 남아있는 기존 일정 = 취소/0건 -> 삭제
    for key, ev in existing.items():
        if apply_changes:
            service.events().delete(calendarId=calendar_id, eventId=ev["id"]).execute()
        deleted += 1
    return created, updated, deleted, len(desired)


# ---------- 검증/출력 ----------

def validate_against_dashboard(occ, dashboard, win_start, win_end):
    """재구성 점유수가 대시보드 예약수와 맞는지 확인. 불일치 셀 수를 돌려준다(PII 없음)."""
    mismatches = []
    d = win_start
    while d <= win_end:
        for stype in CANON_ORDER:
            dash_booked = dashboard.get(d, {}).get(stype, (0, 0))[0]
            recon = len(occ.get((d, stype), []))
            if dash_booked != recon:
                mismatches.append((d.isoformat(), stype, recon, dash_booked))
        d += datetime.timedelta(days=1)
    return mismatches


def mask_name(name: str) -> str:
    return re.sub(r"[가-힣]{2,}", lambda m: m.group()[0] + "*" * (len(m.group()) - 1), name)


def run(apply_changes: bool, months: int = 2) -> None:
    """대시보드를 읽어 캘린더에 반영(또는 dry-run 미리보기)한다.

    apply_changes=False 면 실제 변경 없이 미리보기(이름 가림)만 출력한다.
    months=2 는 이번 달 + 다음 달. launchd 래퍼(run_local)가 apply_changes=True로 부른다.
    """
    today = datetime.date.today()
    win_start = today
    # 창의 끝 = (이번 달 포함 months개월) 중 마지막 달의 말일
    y, m = today.year, today.month + (months - 1)
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    if m == 12:
        win_end = datetime.date(y, 12, 31)
    else:
        win_end = datetime.date(y, m + 1, 1) - datetime.timedelta(days=1)

    range_start = today - datetime.timedelta(days=LOOKBACK_DAYS)
    range_end = win_end

    session = requests.Session()
    cr.login(session)

    # 대시보드(창에 걸치는 모든 달)
    dashboard: dict[datetime.date, dict[str, tuple[int, int]]] = {}
    ym = (today.year, today.month)
    for _ in range(months):
        dashboard.update(fetch_dashboard_month(session, ym[0], ym[1]))
        ny, nm = ym[0] + (ym[1] // 12), (ym[1] % 12) + 1
        ym = (ny, nm)

    rows = fetch_checkin_rows(session, range_start, range_end)
    occ = build_occupancy(rows, win_start, win_end, range_start, range_end)

    mismatches = validate_against_dashboard(occ, dashboard, win_start, win_end)

    print(f"창: {win_start} ~ {win_end} (체크인 조회 {range_start}~{range_end}, 수집 {len(rows)}건)")
    print(f"대시보드 대조 불일치 셀: {len(mismatches)}")
    if mismatches and DEBUG:
        for d, st, recon, dash in mismatches[:30]:
            print(f"  [MISMATCH] {d} {st} 재구성={recon} 대시보드={dash}", file=sys.stderr)

    # 요약(일정 개수) — 날짜×사이트 중 예약>0
    cells = sum(1 for v in occ.values() if v)
    print(f"만들/유지할 일정(날짜×사이트, 예약>0): {cells}개")

    # dry-run 샘플: 가장 가까운 날 몇 개를 이름 가려서 미리보기
    if not apply_changes:
        print("\n--- 미리보기 (이름 가림, 실제 캘린더엔 이름 그대로 저장됨) ---")
        shown = 0
        for (d, stype) in sorted(occ.keys()):
            entries = occ[(d, stype)]
            if not entries:
                continue
            total = dashboard.get(d, {}).get(stype, (len(entries), len(entries)))[1]
            print(f"\n[{d} · {SHORT_LABEL.get(stype, stype)} {len(entries)}/{total}]  color={COLOR_ID.get(stype)}")
            desc = format_description(stype, entries, total)
            for line in desc.split("\n"):
                print("   " + mask_name(line))
            shown += 1
            if shown >= 6:
                break
        print("\n실제 반영하려면 --apply 를 붙여 실행.")
        return

    created, updated, deleted, total_cells = sync_calendar(occ, dashboard, win_start, win_end, apply_changes=True)
    print(f"\n반영 완료: 생성 {created} · 수정 {updated} · 삭제 {deleted} (대상 {total_cells}개)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제로 캘린더에 반영 (기본은 dry-run)")
    parser.add_argument("--months", type=int, default=2, help="이번 달부터 몇 개월치 (기본 2 = 이번 달+다음 달)")
    args = parser.parse_args()
    run(apply_changes=args.apply, months=args.months)


if __name__ == "__main__":
    main()
