#!/usr/bin/env python3
"""30분마다 실행되어 예약현황 페이지에서 새로 '예약완료'된 건을 찾아
웹푸시로 알리고 구글 캘린더에 일정을 등록한다.

필요 환경변수:
  ADMIN_ID, ADMIN_PW          예약 관리자 로그인 계정
  LOGIN_URL                   로그인 POST 대상 URL (기본값: 홈페이지 XE 로그인)
  LIST_URL                    예약현황 목록 조회 URL (상태=예약완료 필터가 걸린 상태의 URL을 그대로 넣어야 함)
  VAPID_PRIVATE_KEY           웹푸시 VAPID 개인키
  VAPID_SUBJECT               mailto:본인이메일 형식
  PUSH_SUBSCRIPTIONS          구독 페이지에서 복사한 JSON을 기기별로 모은 배열 문자열
                              (기기 1개면 [ {...} ], 여러 기기(아이폰/갤럭시 등)면 [ {...}, {...} ])
  GOOGLE_SERVICE_ACCOUNT_JSON 구글 서비스 계정 키(JSON) 전체 문자열
  GOOGLE_CALENDAR_ID          일정을 등록할 구글 캘린더 ID (서비스 계정과 공유되어 있어야 함)
  STATE_FILE                  이미 알린 예약번호를 저장하는 파일 경로 (기본값: data/notified_ids.json)
  DRY_RUN                     "1"이면 실제 발송/등록/상태저장 없이 콘솔에만 출력
"""
from __future__ import annotations

import datetime
import json
import os
import sys

import requests
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build
from pywebpush import WebPushException, webpush

BASE_URL = "http://pscamp.hana-pnc.co.kr"
LOGIN_URL = os.environ.get("LOGIN_URL", f"{BASE_URL}/index.php?act=procMemberLogin")
# TODO: 실제 로그인 후 개발자도구 Network 탭에서 확인한, "상태=예약완료" 필터가 걸린
# 예약현황 목록 페이지의 정확한 URL로 교체해야 한다. (아래는 임시 추정값)
LIST_URL = os.environ.get("LIST_URL", f"{BASE_URL}/index.php?mid=admin&act=dispYeyakAdminList")
STATE_FILE = os.environ.get("STATE_FILE", "data/notified_ids.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
SEED_ONLY = os.environ.get("SEED_ONLY") == "1"
CONFIRMED_KEYWORDS = ["예약완료", "결제완료"]
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))


def login(session: requests.Session) -> None:
    admin_id = os.environ["ADMIN_ID"]
    admin_pw = os.environ["ADMIN_PW"]
    payload = {
        "mid": "index",
        "act": "procMemberLogin",
        "user_id": admin_id,
        "password": admin_pw,
        "keep_signed": "Y",
        "success_return_url": "",
        "error_return_url": "/index.php?mid=index&act=dispMemberLoginForm",
    }
    resp = session.post(LOGIN_URL, data=payload, timeout=15)
    resp.raise_for_status()


def normalize(text: str) -> str:
    return " ".join(text.split()).strip()


def parse_reservations(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for t in soup.find_all("table"):
        header_text = normalize(t.get_text())
        if "예약번호" in header_text and "상태" in header_text:
            table = t
            break
    if table is None:
        return []

    headers = [normalize(th.get_text()) for th in table.find_all("th")]
    if not headers:
        first_row = table.find("tr")
        headers = [normalize(td.get_text()) for td in first_row.find_all(["td", "th"])]

    def col_index(name: str) -> int | None:
        for i, h in enumerate(headers):
            if name in h:
                return i
        return None

    idx_id = col_index("예약번호")
    idx_room = col_index("객실")
    idx_date = col_index("예약일")
    idx_price = col_index("요금")
    idx_name = col_index("이름")
    idx_status = col_index("상태")

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells or idx_id is None or idx_id >= len(cells):
            continue
        reservation_id = normalize(cells[idx_id].get_text())
        if not reservation_id:
            continue
        status = normalize(cells[idx_status].get_text()) if idx_status is not None and idx_status < len(cells) else ""
        rows.append(
            {
                "id": reservation_id,
                "room": normalize(cells[idx_room].get_text()) if idx_room is not None and idx_room < len(cells) else "",
                "date": normalize(cells[idx_date].get_text()) if idx_date is not None and idx_date < len(cells) else "",
                "price": normalize(cells[idx_price].get_text()) if idx_price is not None and idx_price < len(cells) else "",
                "name": normalize(cells[idx_name].get_text()) if idx_name is not None and idx_name < len(cells) else "",
                "status": status,
            }
        )
    return rows


def is_confirmed(status: str) -> bool:
    compact = status.replace(" ", "")
    return any(keyword in compact for keyword in CONFIRMED_KEYWORDS)


def fetch_confirmed_reservations(session: requests.Session) -> list[dict]:
    confirmed = []
    for page in range(1, MAX_PAGES + 1):
        sep = "&" if "?" in LIST_URL else "?"
        url = f"{LIST_URL}{sep}page={page}"
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        rows = parse_reservations(resp.text)
        if not rows:
            break
        page_confirmed = [r for r in rows if is_confirmed(r["status"])]
        confirmed.extend(page_confirmed)
        if len(page_confirmed) < len(rows):
            # 이 페이지에 확정이 아닌 행이 섞여 있다는 건 최신순 목록의 끝부분에 도달했다는 뜻
            break
    return confirmed


def load_notified_ids() -> set[str]:
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, encoding="utf-8") as f:
        return set(json.load(f))


def save_notified_ids(ids: set[str]) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def load_subscriptions() -> list[dict]:
    subs = json.loads(os.environ["PUSH_SUBSCRIPTIONS"])
    return [subs] if isinstance(subs, dict) else subs


def send_push(reservation: dict) -> None:
    payload = json.dumps(
        {
            "title": "새 예약 확정",
            "body": f"{reservation['name']}님 · {reservation['room']} · {reservation['date']} · {reservation['price']}",
            "tag": reservation["id"],
        },
        ensure_ascii=False,
    )
    for subscription in load_subscriptions():
        try:
            webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                vapid_claims={"sub": os.environ["VAPID_SUBJECT"]},
            )
        except WebPushException as exc:
            # 기기 하나가 만료/구독취소 됐어도 다른 기기에는 계속 보내야 하므로 여기서 중단하지 않음
            print(f"웹푸시 발송 실패 (기기 1개, 계속 진행): {exc}", file=sys.stderr)


def parse_date_range(date_str: str, today: datetime.date | None = None) -> tuple[datetime.date, datetime.date]:
    """'08-02~08-03' 같은 문자열을 (체크인, 체크아웃) date로 변환한다.

    목록에 연도가 표시되지 않으므로 오늘 날짜를 기준으로 연도를 추정한다.
    """
    today = today or datetime.date.today()
    if "~" in date_str:
        start_raw, end_raw = date_str.split("~", 1)
    else:
        start_raw = end_raw = date_str

    def to_date(raw: str) -> datetime.date:
        month, day = (int(x) for x in raw.strip().split("-"))
        year = today.year
        candidate = datetime.date(year, month, day)
        # 오늘보다 6개월 이상 과거로 보이면 연도가 넘어간 것으로 보고 다음 해로 보정
        if (today - candidate).days > 180:
            candidate = datetime.date(year + 1, month, day)
        return candidate

    start_date = to_date(start_raw)
    end_date = to_date(end_raw)
    if end_date <= start_date:
        end_date = start_date + datetime.timedelta(days=1)
    return start_date, end_date


def get_calendar_service():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def create_calendar_event(reservation: dict) -> None:
    try:
        start_date, end_date = parse_date_range(reservation["date"])
    except Exception as exc:  # noqa: BLE001
        print(f"날짜 파싱 실패, 캘린더 등록 건너뜀: {reservation} ({exc})", file=sys.stderr)
        return

    service = get_calendar_service()
    event = {
        "summary": f"[예약확정] {reservation['name']} · {reservation['room']}",
        "description": f"예약번호: {reservation['id']}\n요금: {reservation['price']}\n상태: {reservation['status']}",
        "start": {"date": start_date.isoformat()},
        "end": {"date": end_date.isoformat()},
    }
    service.events().insert(calendarId=os.environ["GOOGLE_CALENDAR_ID"], body=event).execute()


def main() -> None:
    session = requests.Session()
    login(session)

    confirmed = fetch_confirmed_reservations(session)
    notified = load_notified_ids()

    if SEED_ONLY:
        # 최초 1회: 지금까지 쌓인 예약완료 건을 전부 '이미 알림' 상태로만 기록하고 푸시는 보내지 않는다.
        before = len(notified)
        notified.update(r["id"] for r in confirmed)
        print(f"시드 완료: {len(notified) - before}건을 신규 알림 없이 기록함 (총 {len(notified)}건)")
        save_notified_ids(notified)
        return

    new_ones = [r for r in confirmed if r["id"] not in notified]

    print(f"확인된 예약완료 건수: {len(confirmed)}, 신규: {len(new_ones)}")
    for r in new_ones:
        print(f"  -> 신규 확정: {r}")
        if not DRY_RUN:
            send_push(r)
            create_calendar_event(r)
        notified.add(r["id"])

    if not DRY_RUN and new_ones:
        save_notified_ids(notified)


if __name__ == "__main__":
    main()
