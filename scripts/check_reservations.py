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
import re
import sys

import requests
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build
from pywebpush import WebPushException, webpush

BASE_URL = "http://pscamp.hana-pnc.co.kr"
# GitHub Actions는 등록 안 된 시크릿도 빈 문자열 env로 넘기므로, os.environ.get의
# 기본값 인자가 아니라 `or`로 빈 문자열도 기본값으로 대체되게 처리한다.
LOGIN_URL = os.environ.get("LOGIN_URL") or f"{BASE_URL}/index.php?act=procMemberLogin"
# 관리자 페이지에서 상태 필터를 "예약완료"(status=1)로 걸고 확인한 실제 목록 URL
LIST_URL = os.environ.get("LIST_URL") or (
    f"{BASE_URL}/index.php?mid=&vid=&module=admin&act=dispYeyakAdminResList"
    "&islog=&order_type=desc&sort_index=res_srl&list_count=20&hide_room_block="
    "&cate_srl=&status=1&start_date=&end_date=&searchType=&searchStr="
)
STATE_FILE = os.environ.get("STATE_FILE", "data/notified_ids.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
SEED_ONLY = os.environ.get("SEED_ONLY") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
CONFIRMED_KEYWORDS = ["예약완료", "결제완료"]
MAX_PAGES = int(os.environ.get("MAX_PAGES", "30"))
# 구글 캘린더 고정 팔레트(1~11) 중 서로 잘 구분되는 색상을 사이트 종류별로 배정
SITE_TYPE_COLORS = {
    "민박": "5",     # Banana (노랑)
    "카라반": "7",   # Peacock (청록)
    "A사이트": "9",  # Blueberry (파랑)
    "B사이트": "3",  # Grape (보라)
    "평상": "10",    # Basil (초록)
    "테이블": "4",   # Flamingo (분홍)
}
DEFAULT_COLOR_ID = "6"  # 매칭 실패 시 기존 고정색(Tangerine)으로 대체


def login(session: requests.Session) -> None:
    admin_id = os.environ["ADMIN_ID"]
    admin_pw = os.environ["ADMIN_PW"]
    # 로그인 폼을 먼저 방문해 세션 쿠키를 받아와야 로그인 POST가 같은 세션으로 처리된다.
    login_form_url = f"{BASE_URL}/index.php?mid=index&act=dispMemberLoginForm"
    warmup = session.get(login_form_url, timeout=15)
    warmup.raise_for_status()
    if DEBUG:
        print(
            f"[DEBUG] warmup status={warmup.status_code} "
            f"cookie_count={len(session.cookies.get_dict())}",
            file=sys.stderr,
        )
    payload = {
        "mid": "index",
        "vid": "",
        "ruleset": "@login",
        "act": "procMemberLogin",
        "xe_validator_id": "modules/member/skins",
        "user_id": admin_id,
        "password": admin_pw,
        "keep_signed": "Y",
        "success_return_url": "",
        "error_return_url": "/index.php?mid=index&act=dispMemberLoginForm",
    }
    # XE는 Referer가 로그인 폼과 다르면 CSRF로 간주해 "잘못된 요청입니다"로 거부한다.
    resp = session.post(LOGIN_URL, data=payload, timeout=15, headers={"Referer": login_form_url})
    resp.raise_for_status()
    if DEBUG:
        # 개인정보(예약자명 등)가 로그에 남지 않도록 원문 내용은 출력하지 않고
        # 로그인 성공 여부를 판단할 수 있는 구조적 정보만 남긴다.
        looks_like_login_page = "procMemberLogin" in resp.text or "dispMemberLoginForm" in resp.text
        print(
            f"[DEBUG] login status={resp.status_code} final_url={resp.url} "
            f"body_len={len(resp.text)} looks_like_login_page={looks_like_login_page} "
            f"cookie_names={list(session.cookies.get_dict().keys())}",
            file=sys.stderr,
        )
        error_hints = [
            kw
            for kw in ["일치하지", "존재하지", "차단", "잠금", "잘못된", "제한", "탈퇴", "승인"]
            if kw in resp.text
        ]
        print(f"[DEBUG] login error_hints={error_hints}", file=sys.stderr)


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

        name = ""
        if idx_name is not None and idx_name < len(cells):
            # 이름 칸에는 문자보내기/전화걸기 등 숨은 드롭다운 메뉴 글자가 같이 들어있어서
            # get_text()로 그냥 뽑으면 오염된다. 실제 이름은 a-send-sms-btn span에 들어있다.
            name_span = cells[idx_name].find("span", class_="a-send-sms-btn")
            name = normalize(name_span.get_text()) if name_span else normalize(cells[idx_name].get_text())

        rows.append(
            {
                "id": reservation_id,
                "room": normalize(cells[idx_room].get_text()) if idx_room is not None and idx_room < len(cells) else "",
                "date": normalize(cells[idx_date].get_text()) if idx_date is not None and idx_date < len(cells) else "",
                "price": normalize(cells[idx_price].get_text()) if idx_price is not None and idx_price < len(cells) else "",
                "name": name,
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
        if DEBUG:
            # 원문 HTML(고객 이름 등 개인정보 포함)은 절대 출력하지 않고 구조적 정보만 남긴다.
            looks_like_login_page = "procMemberLogin" in resp.text or "dispMemberLoginForm" in resp.text
            print(
                f"[DEBUG] page={page} status={resp.status_code} final_url={resp.url} "
                f"len={len(resp.text)} has_marker={'예약번호' in resp.text} rows={len(rows)} "
                f"looks_like_login_page={looks_like_login_page}",
                file=sys.stderr,
            )
        if not rows:
            # 더 이상 결과가 없는 진짜 끝. 이 목록은 상태=예약완료 필터가 걸려 있어
            # 입실완료(이미 체크인) 행이 섞여 있어도 is_confirmed()가 걸러내므로,
            # 중간에 입실완료가 나온다고 페이지를 중단하면 안 된다 (과거엔 이 조건으로
            # 조기 종료해서 2페이지 이후의 신규 확정 건을 놓치는 버그가 있었다).
            break
        confirmed.extend(r for r in rows if is_confirmed(r["status"]))
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


def get_color_id(room: str) -> str:
    # 평상/테이블/카라반/민박 키워드를 A/B사이트 매칭보다 먼저 확인해야 한다.
    # "평상/A-9 평상"처럼 평상 사이트명이 A/B사이트와 같은 표기를 공유하기 때문이다.
    if "카라반" in room:
        return SITE_TYPE_COLORS["카라반"]
    if "민박" in room:
        return SITE_TYPE_COLORS["민박"]
    if "테이블" in room:
        return SITE_TYPE_COLORS["테이블"]
    if "평상" in room:
        return SITE_TYPE_COLORS["평상"]
    # 관리자 객실 필드는 "서숲A사이트/A-6"처럼 카테고리명이 앞에 붙어 오므로
    # 접두사가 아니라 "A사이트"/"B사이트" 부분 문자열로 매칭한다.
    if "A사이트" in room or re.match(r"^A[\s-]", room):
        return SITE_TYPE_COLORS["A사이트"]
    if "B사이트" in room or re.match(r"^B[\s-]", room):
        return SITE_TYPE_COLORS["B사이트"]
    return DEFAULT_COLOR_ID


def create_calendar_event(reservation: dict) -> None:
    try:
        start_date, end_date = parse_date_range(reservation["date"])
    except Exception as exc:  # noqa: BLE001
        print(f"날짜 파싱 실패, 캘린더 등록 건너뜀: 예약번호={reservation['id']} ({exc})", file=sys.stderr)
        return

    nights = (end_date - start_date).days
    stay_label = f"{nights}박{nights + 1}일"

    service = get_calendar_service()
    event = {
        "summary": f"[예약확정] {reservation['name']} · {reservation['room']} ({stay_label})",
        "description": (
            f"성함: {reservation['name']}\n"
            f"사이트 구역 및 번호: {reservation['room']}\n"
            f"특이사항: "
        ),
        "start": {"date": start_date.isoformat()},
        # 구글 캘린더 종일 일정의 end.date는 배타적(그 날은 포함 안 됨)이라
        # 체크아웃 당일까지 색이 칠해지도록 하루를 더해서 넣는다.
        "end": {"date": (end_date + datetime.timedelta(days=1)).isoformat()},
        # 사이트 종류별로 캘린더에서 한눈에 구분되도록 색상 지정
        "colorId": get_color_id(reservation["room"]),
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
        print(f"  -> 신규 확정: 예약번호={r['id']}")
        if not DRY_RUN:
            try:
                send_push(r)
                create_calendar_event(r)
            except Exception as exc:  # noqa: BLE001
                # 한 건에서 실패해도 나머지 신규 확정 건은 계속 처리해야 한다.
                # 이 건은 notified에 추가하지 않아 다음 실행에서 다시 시도된다.
                print(f"알림/캘린더 처리 실패, 다음 실행에서 재시도: 예약번호={r['id']} ({exc})", file=sys.stderr)
                continue
        notified.add(r["id"])

    if not DRY_RUN and new_ones:
        save_notified_ids(notified)


if __name__ == "__main__":
    main()
