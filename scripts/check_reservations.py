#!/usr/bin/env python3
"""30분마다 실행되어 예약현황 페이지에서 새로 '예약완료'된 건을 찾아
웹푸시로 알리고 구글 캘린더에 일정을 등록한다. 반대로 이미 알렸던 예약이 목록에서
사라지면(취소) 웹푸시로 알리고 등록해뒀던 캘린더 일정을 지운다. 단, 이 기능이
추가되기 전부터 있던 예약(체크인 날짜/캘린더 일정 id를 모름)은 취소 감지 대상에서
제외되며, 이 기능 배포 이후 새로 확정되는 예약부터 적용된다.

이름/객실/예약일/요금/전화번호는 목록 페이지에서 한 번에 얻지만, 인원(성인/아동/유아)은
목록에 없어서 신규 확정 건에 대해서만 예약 상세 페이지를 한 번 더 열어 가져온다.

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
from googleapiclient.errors import HttpError
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
# 예약 채널이 전화/쇼핑몰(OTA)/현장결제/방막기인 행은 "예약번호" 칸에 고유 번호 대신
# 채널명이 그대로 찍혀 나온다. 이 값들을 id로 그대로 쓰면 서로 다른 예약이 같은 키로
# 겹쳐버리므로, 이런 행은 내부 관리번호(res_srl)를 붙여 고유하게 만든다.
NON_UNIQUE_ID_LABELS = {"전화", "쇼핑몰", "방막기", "현장결제"}
MAX_PAGES = int(os.environ.get("MAX_PAGES", "200"))
# 한 번 실행에서 이 값을 넘는 취소가 감지되면 뭔가 잘못된 것(목록 조회 실패, 로그인 실패,
# 사이트 구조 변경 등)으로 보고 취소 처리를 건너뛴다. 사이트 통상적인 취소 빈도보다
# 훨씬 크게 잡아 정상적인 취소는 절대 막지 않으면서도, 대량 오탐이 실제로 푸시/캘린더
# 삭제까지 실행되는 사고(2026-08-23 실제 발생)를 막는 안전장치다.
MAX_CANCELLATIONS_PER_RUN = int(os.environ.get("MAX_CANCELLATIONS_PER_RUN", "15"))
# 같은 이유로 신규 확정도 한 번에 너무 많이 잡히면(예: MAX_PAGES를 늘렸더니 그동안
# 추적 안 된 오래된 예약이 무더기로 "신규"로 잡히는 경우) 실제 발송 대신 건너뛴다.
# 이런 배치는 SEED_ONLY로 조용히 기록해야 한다.
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "15"))
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
# 목록에는 인원 컬럼이 없어서 예약 상세 페이지를 한 번 더 열어야 한다.
DETAIL_URL_TEMPLATE = f"{BASE_URL}/index.php?module=admin&act=dispYeyakAdminResView&res_srl={{res_srl}}"
# 상세 페이지 인원 입력칸의 라벨과 input name 대응 (관리자 페이지에서 확인한 값).
# 사이트가 어른/아이 2단계가 아니라 성인/아동/유아 3단계로 관리하므로 그대로 따라간다.
HEADCOUNT_FIELDS = [("성인", "n_user_count"), ("아동", "a_user_count"), ("유아", "u_user_count")]
# 값을 못 가져왔을 때 빈칸으로 두면 "정보 없음"과 "긁기 실패"를 구분할 수 없어 명시한다.
UNKNOWN_LABEL = "확인 실패 (예약페이지 확인 필요)"


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


def parse_res_srl(row) -> str:
    """목록 행에서 상세 페이지 키(res_srl)를 뽑는다.

    행마다 상세보기 링크(act=dispYeyakAdminResView&res_srl=...)가 있고, 같은 행의
    삭제 링크(ResDelete)도 같은 형식이라 반드시 ResView 링크만 골라야 한다.
    """
    link = row.find("a", href=re.compile(r"dispYeyakAdminResView"))
    if link is None:
        return ""
    match = re.search(r"res_srl=(\d+)", link.get("href", ""))
    return match.group(1) if match else ""


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
        res_srl = parse_res_srl(tr)
        if reservation_id in NON_UNIQUE_ID_LABELS and res_srl:
            reservation_id = f"{reservation_id}-{res_srl}"
        status = normalize(cells[idx_status].get_text()) if idx_status is not None and idx_status < len(cells) else ""

        name = ""
        phone = ""
        if idx_name is not None and idx_name < len(cells):
            # 이름 칸에는 문자보내기/전화걸기 등 숨은 드롭다운 메뉴 글자가 같이 들어있어서
            # get_text()로 그냥 뽑으면 오염된다. 실제 이름은 a-send-sms-btn span에 들어있다.
            name_span = cells[idx_name].find("span", class_="a-send-sms-btn")
            name = normalize(name_span.get_text()) if name_span else normalize(cells[idx_name].get_text())
            # 전화번호도 같은 칸의 "전화걸기" 드롭다운에 data-tel 속성으로 이미 들어있어서
            # 상세 페이지를 열지 않고 목록에서 바로 가져올 수 있다 (010-0000-0000 형식).
            tel_el = cells[idx_name].find(attrs={"data-tel": True})
            phone = normalize(tel_el["data-tel"]) if tel_el else ""

        rows.append(
            {
                "id": reservation_id,
                "room": normalize(cells[idx_room].get_text()) if idx_room is not None and idx_room < len(cells) else "",
                "date": normalize(cells[idx_date].get_text()) if idx_date is not None and idx_date < len(cells) else "",
                "price": normalize(cells[idx_price].get_text()) if idx_price is not None and idx_price < len(cells) else "",
                "name": name,
                "phone": phone,
                "res_srl": res_srl,
                "status": status,
            }
        )
    return rows


def is_confirmed(status: str) -> bool:
    compact = status.replace(" ", "")
    return any(keyword in compact for keyword in CONFIRMED_KEYWORDS)


# 예약-대시보드 화면의 사이트별 일자 칸이 실제로 호출하는 조회 방식과 동일하다
# (대시보드 HTML의 data-url1에서 확인). "상태" 글자만 보는 기존 방식과 달리 이 방식은
# 사이트 자체가 유효하다고 판단한 예약만 돌려준다 — 2026-08-23에 실제로 발견된 사례로,
# 같은 사람 이름이 겹치는 날짜에 여러 사이트로 잡혀있던 유령/중복 예약(상태 텍스트는
# "예약완료"로 정상처럼 보였음)이 날짜범위+islog=Y 조회에서는 아예 나오지 않았다.
CATEGORY_IDS = {
    "서숲A사이트": "8",
    "서숲B사이트": "9",
    "서숲카라반": "10",
    "서숲장박": "11",
    "서숲민박": "12",
    "평상": "13",
    "야외테이블": "14",
}
# 오늘부터 이 개월 수 뒤까지만 조회한다. 과거 체크인 건은 취소 감지 대상도 아니고(위
# find_cancelled_ids 참고) 신규로 잡을 필요도 없어 애초에 범위에 넣지 않는다.
# 기본값 2(이번 달 + 다음 달)는 사용자 지시(2026-08-23): 겨울 장박은 아직 접수 시작 전이라
# 너무 먼 미래까지 캘린더에 넣을 필요 없고, 매 실행이 "오늘" 기준으로 범위를 다시 계산하니
# 달이 바뀌면 자동으로 다음 달이 굴러 들어온다(별도 수동 조정 불필요).
ACTIVE_LOOKAHEAD_MONTHS = int(os.environ.get("ACTIVE_LOOKAHEAD_MONTHS", "2"))


def _month_ranges(start: datetime.date, months: int) -> list[tuple[str, str]]:
    """start가 속한 달부터 months개월 분량의 (YYYYMMDD 시작일, YYYYMMDD 종료일) 목록.

    dispYeyakAdminResList는 start_date~end_date 범위가 너무 넓으면(직접 확인: 오늘부터
    400일) 조용히 일부만 돌려주는 문제가 있어(예: 8월 한 달만 조회하면 54건인데 8월+
    이후 400일을 한 번에 조회하면 오히려 24건으로 줄어듦), 대시보드가 실제로 쓰는
    것처럼 한 달 단위로 나눠서 조회해야 안전하다.
    """
    ranges = []
    year, month = start.year, start.month
    for _ in range(months):
        month_start = datetime.date(year, month, 1)
        if month == 12:
            next_month_start = datetime.date(year + 1, 1, 1)
        else:
            next_month_start = datetime.date(year, month + 1, 1)
        month_end = next_month_start - datetime.timedelta(days=1)
        # 오늘이 속한 첫 달은 지나간 날짜부터 조회할 필요 없으니 시작일을 오늘로 당긴다.
        effective_start = max(month_start, start)
        ranges.append((effective_start.strftime("%Y%m%d"), month_end.strftime("%Y%m%d")))
        year, month = next_month_start.year, next_month_start.month
    return ranges


def fetch_active_reservation_rows(session: requests.Session, today: datetime.date | None = None) -> list[dict]:
    """대시보드와 같은 카테고리별/월별 날짜범위(+islog=Y) 조회로 실제 유효한 예약만 모은다.

    fetch_reservation_rows()(상태 필터 없는 전체 페이지 조회, backfill 스크립트 전용)와
    달리 유령/중복 예약이 섞이지 않는다 — 2026-08-23에 실제로 확인: 같은 사람 이름이
    겹치는 날짜에 여러 사이트로 잡혀있던 예약(상태 텍스트는 "예약완료"로 정상처럼
    보였음)이 이 방식에서는 처음부터 나오지 않았다.
    """
    today = today or datetime.date.today()
    month_ranges = _month_ranges(today, ACTIVE_LOOKAHEAD_MONTHS)

    all_rows: list[dict] = []
    seen_ids: set[str] = set()
    for cate_srl in CATEGORY_IDS.values():
        for start_str, end_str in month_ranges:
            for page in range(1, MAX_PAGES + 1):
                url = (
                    f"{BASE_URL}/index.php?module=admin&act=dispYeyakAdminResList"
                    f"&start_date={start_str}&end_date={end_str}&cate_srl={cate_srl}"
                    f"&islog=Y&page={page}"
                )
                resp = session.get(url, timeout=15)
                resp.raise_for_status()
                rows = parse_reservations(resp.text)
                if not rows:
                    break
                for r in rows:
                    if r["id"] not in seen_ids:
                        seen_ids.add(r["id"])
                        all_rows.append(r)
    return all_rows


def fetch_reservation_rows(session: requests.Session) -> list[dict]:
    """목록 전체 페이지의 행을 상태 필터 없이 그대로 모아 온다.

    기존 캘린더 일정을 갱신하는 backfill 스크립트는 이미 입실완료로 바뀐 예약도
    찾아야 하므로 확정 필터를 걸지 않은 원본 행이 필요하다.
    """
    all_rows = []
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
        all_rows.extend(rows)
    return all_rows


def load_notified_ids() -> dict[str, dict]:
    """id -> {'event_id': 캘린더 일정 id 또는 None, 'checkin_date': 'YYYY-MM-DD' 또는 None}.

    취소 감지 기능 도입 전에는 단순 id 배열이었다. 그 형식으로 저장된 id는
    event_id/checkin_date를 몰라 아래에서 None으로 채우며, checkin_date가 없으므로
    find_cancelled_ids()의 취소 후보에서 자연히 제외된다.
    """
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {rid: {"event_id": None, "checkin_date": None} for rid in data}
    return data


def save_notified_ids(notified: dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(notified.items())), f, ensure_ascii=False, indent=2)


def load_subscriptions() -> list[dict]:
    subs = json.loads(os.environ["PUSH_SUBSCRIPTIONS"])
    return [subs] if isinstance(subs, dict) else subs


def fetch_headcount(session: requests.Session, res_srl: str, expected_id: str = "") -> dict[str, int | None]:
    """예약 상세 페이지에서 성인/아동/유아 인원을 가져온다.

    인원은 목록 표에 없고 상세 페이지의 input value에만 들어있다(칸 텍스트는 '명'뿐이라
    get_text()로는 못 뽑는다). 신규 확정 건에 대해서만 호출하므로 요청 수는 거의 안 늘어난다.

    없는 res_srl로 요청해도 이 페이지는 200과 함께 인원이 0인 빈 양식을 돌려주기 때문에,
    'keynum' 값(=예약번호)이 기대한 예약과 같은지 확인해서 엉뚱한 0명을 정상값으로
    믿는 일이 없게 한다.
    """
    resp = session.get(DETAIL_URL_TEMPLATE.format(res_srl=res_srl), timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    keynum_el = soup.find("input", attrs={"name": "keynum"})
    keynum = normalize(keynum_el.get("value") or "") if keynum_el is not None else ""
    if expected_id and keynum != expected_id:
        raise ValueError(f"상세 페이지의 예약번호가 목록과 다름 (res_srl={res_srl})")

    counts: dict[str, int | None] = {}
    for label, field in HEADCOUNT_FIELDS:
        el = soup.find("input", attrs={"name": field})
        digits = re.sub(r"[^0-9]", "", el.get("value") or "") if el is not None else ""
        counts[label] = int(digits) if digits else (0 if el is not None else None)
    return counts


def format_headcount(counts: dict[str, int | None]) -> str:
    """{'성인': 2, '아동': 2, '유아': 0} -> '총 4명 (성인 2, 아동 2, 유아 0)'.

    유아는 요금을 받지 않기 때문에 세 구분이 각각 몇 명인지가 요금 확인에 필요하다.
    그래서 0명인 구분도 생략하지 않고 성인/아동/유아를 항상 다 적는다.

    실제 예약에 0명은 있을 수 없으므로 총합이 0이거나 값을 하나도 못 읽었으면
    빈 문자열(=확인 실패)을 반환해서 잘못 읽은 값이 정상처럼 보이지 않게 한다.
    """
    if not counts or all(v is None for v in counts.values()):
        return ""
    total = sum(v for v in counts.values() if v)
    if total <= 0:
        return ""
    parts = [f"{label} {counts[label] or 0}" for label, _ in HEADCOUNT_FIELDS]
    return f"총 {total}명 ({', '.join(parts)})"


def enrich_with_headcount(session: requests.Session, reservation: dict) -> None:
    """예약 dict에 'headcount' 문자열을 채운다. 실패해도 예외를 밖으로 던지지 않는다.

    인원을 못 가져온 것 때문에 알림/캘린더 등록 자체를 놓치는 게 더 큰 손해라서,
    실패 시엔 빈 값으로 두고 나머지 정보로 계속 진행한다.
    """
    if not reservation.get("res_srl"):
        reservation["headcount"] = ""
        return
    try:
        counts = fetch_headcount(session, reservation["res_srl"], expected_id=reservation["id"])
        reservation["headcount"] = format_headcount(counts)
    except Exception as exc:  # noqa: BLE001
        # 개인정보가 로그에 남지 않도록 예약번호만 출력한다.
        print(f"인원 정보 조회 실패, 나머지 정보로 계속 진행: 예약번호={reservation['id']} ({exc})", file=sys.stderr)
        reservation["headcount"] = ""


def _dispatch_push(payload: str) -> None:
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


def send_push(reservation: dict) -> None:
    # 요금은 알림창에서 굳이 확인할 필요가 없어 캘린더 일정 설명에만 넣는다.
    # 빈 항목은 걸러서 ' · ' 구분자가 겹쳐 보이지 않게 한다.
    body_parts = [
        f"{reservation['name']}님",
        reservation["room"],
        reservation["date"],
        reservation.get("headcount", ""),
        reservation.get("phone", ""),
    ]
    payload = json.dumps(
        {
            "title": "새 예약 확정",
            "body": " · ".join(part for part in body_parts if part),
            "tag": reservation["id"],
        },
        ensure_ascii=False,
    )
    _dispatch_push(payload)


def send_cancel_push(reservation_id: str, fields: dict[str, str], checkin_date: str) -> None:
    """취소 알림을 보낸다. fields는 삭제 전 캘린더 일정 설명에서 읽어온 성함/사이트/연락처
    (event_id를 몰라 캘린더 일정을 못 찾은 경우엔 빈 dict가 들어와 예약번호만 표시된다).
    """
    name = fields.get("성함", "")
    body_parts = [
        f"{name}님" if name else "",
        fields.get("사이트 구역 및 번호", ""),
        checkin_date,
        fields.get("연락처", ""),
    ]
    body = " · ".join(part for part in body_parts if part) or f"예약번호 {reservation_id}"
    payload = json.dumps(
        {"title": "예약 취소", "body": body, "tag": reservation_id},
        ensure_ascii=False,
    )
    _dispatch_push(payload)


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


def build_event_summary(reservation: dict) -> str:
    """캘린더 일정 제목을 만든다.

    사이트(=색상)를 맨 앞에 두어야 구글 캘린더 목록의 제목 가나다순 정렬에서
    같은 사이트(같은 색)끼리 한 덩어리로 붙는다. 이름은 뒤에 붙인다.
    신규 등록과 기존 일정 제목 정렬(reorder)이 같은 형식을 쓰도록 여기 한 곳에만 둔다.
    """
    return f"{reservation['room']} · {reservation['name']}"


def build_event_description(reservation: dict, memo: str = "") -> str:
    """캘린더 일정 설명을 만든다.

    신규 등록과 기존 일정 갱신(backfill)이 똑같은 문구를 쓰도록 여기 한 곳에만 둔다.
    memo는 사람이 직접 적어 넣은 특이사항으로, 갱신할 때 그대로 보존해서 넘겨야 한다.
    """
    return (
        f"성함: {reservation['name']}\n"
        f"연락처: {reservation.get('phone') or UNKNOWN_LABEL}\n"
        f"인원: {reservation.get('headcount') or UNKNOWN_LABEL}\n"
        f"사이트 구역 및 번호: {reservation['room']}\n"
        f"요금: {reservation['price']}\n"
        f"특이사항: {memo}"
    )


def parse_event_description(description: str) -> dict[str, str]:
    """일정 설명을 '라벨 -> 값' dict로 되돌린다 (특이사항 보존용).

    특이사항 값 안에 ':'가 들어있어도 첫 콜론만 기준으로 잘라 값을 온전히 남긴다.
    """
    fields = {}
    for line in (description or "").split("\n"):
        label, sep, value = line.partition(":")
        if sep:
            fields[label.strip()] = value.strip()
    return fields


def checkin_date_iso(reservation: dict) -> str | None:
    try:
        start, _ = parse_date_range(reservation["date"])
    except Exception:  # noqa: BLE001
        return None
    return start.isoformat()


def create_calendar_event(reservation: dict) -> str | None:
    try:
        start_date, end_date = parse_date_range(reservation["date"])
    except Exception as exc:  # noqa: BLE001
        print(f"날짜 파싱 실패, 캘린더 등록 건너뜀: 예약번호={reservation['id']} ({exc})", file=sys.stderr)
        return None

    service = get_calendar_service()
    event = {
        "summary": build_event_summary(reservation),
        "description": build_event_description(reservation),
        "start": {"date": start_date.isoformat()},
        # 구글 캘린더 종일 일정의 end.date는 배타적(그 날은 포함 안 됨)이라
        # 체크아웃 당일까지 색이 칠해지도록 하루를 더해서 넣는다.
        "end": {"date": (end_date + datetime.timedelta(days=1)).isoformat()},
        # 사이트 종류별로 캘린더에서 한눈에 구분되도록 색상 지정
        "colorId": get_color_id(reservation["room"]),
    }
    created = service.events().insert(calendarId=os.environ["GOOGLE_CALENDAR_ID"], body=event).execute()
    return created["id"]


def active_window_end(today: datetime.date | None = None) -> datetime.date:
    """fetch_active_reservation_rows()가 실제로 조회하는 마지막 날짜(월 단위 범위의 끝)."""
    today = today or datetime.date.today()
    _, last_end = _month_ranges(today, ACTIVE_LOOKAHEAD_MONTHS)[-1]
    return datetime.datetime.strptime(last_end, "%Y%m%d").date()


def find_cancelled_ids(notified: dict[str, dict], active_ids: set[str]) -> list[str]:
    """notified 중 체크인일이 조회 범위 안(오늘~active_window_end)인데 목록에서 사라진 id를
    취소 후보로 본다.

    체크인일이 이미 지난 id까지 검사하면 MAX_PAGES 페이지네이션 한계로 목록 뒤로
    밀려난 오래된 완료 건을 취소로 오판할 수 있어 과거는 제외한다. 조회 범위 밖(먼 미래)의
    id도 마찬가지로 제외해야 한다 — active_ids 자체가 ACTIVE_LOOKAHEAD_MONTHS 범위만
    보므로, 범위 밖 id는 목록에 없는 게 당연하고 취소가 아니다.
    """
    today_iso = datetime.date.today().isoformat()
    window_end_iso = active_window_end().isoformat()
    return [
        rid
        for rid, meta in notified.items()
        if meta.get("checkin_date")
        and today_iso <= meta["checkin_date"] <= window_end_iso
        and rid not in active_ids
    ]


def handle_cancellation(reservation_id: str, meta: dict) -> None:
    event_id = meta.get("event_id")
    fields: dict[str, str] = {}
    if event_id:
        service = get_calendar_service()
        calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
        try:
            event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            if exc.resp.status not in (404, 410):
                raise
            event = None  # 이미 삭제된 일정. 취소 알림만 보내고 넘어간다.
        if event is not None:
            fields = parse_event_description(event.get("description", ""))
            service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    send_cancel_push(reservation_id, fields, meta.get("checkin_date") or "")


def main() -> None:
    session = requests.Session()
    login(session)

    raw_rows = fetch_active_reservation_rows(session)
    confirmed = [r for r in raw_rows if is_confirmed(r["status"])]
    # 취소된 예약은 애초에 목록 응답에서 빠지지만, 입실완료(체크인 완료)로 넘어간 예약은
    # 그대로 남아있다. 그래서 취소 판정 기준은 confirmed가 아니라 raw_rows 전체여야
    # "예약완료 -> 입실완료" 전환을 취소로 오판하지 않는다.
    active_ids = {r["id"] for r in raw_rows}
    notified = load_notified_ids()

    if SEED_ONLY:
        # 최초 1회: 지금까지 쌓인 예약완료 건을 전부 '이미 알림' 상태로만 기록하고 푸시는 보내지 않는다.
        before = len(notified)
        for r in confirmed:
            notified.setdefault(r["id"], {"event_id": None, "checkin_date": checkin_date_iso(r)})
        print(f"시드 완료: {len(notified) - before}건을 신규 알림 없이 기록함 (총 {len(notified)}건)")
        save_notified_ids(notified)
        return

    new_ones = [r for r in confirmed if r["id"] not in notified]
    changed = False

    print(f"확인된 예약완료 건수: {len(confirmed)}, 신규: {len(new_ones)}")
    if len(new_ones) > MAX_NEW_PER_RUN:
        print(
            f"신규 확정 건수가 비정상적으로 많음({len(new_ones)}건 > {MAX_NEW_PER_RUN}건) - "
            "MAX_PAGES 변경 등으로 오래된 미추적 예약이 한꺼번에 잡혔을 수 있어 이번 실행에서는 "
            "건너뜀 (SEED_ONLY=1로 조용히 기록하거나 원인 확인 후 다시 실행 필요)",
            file=sys.stderr,
        )
        new_ones = []
    for r in new_ones:
        print(f"  -> 신규 확정: 예약번호={r['id']}")
        # 인원은 상세 페이지에만 있어서 신규 건마다 한 번씩 더 조회한다.
        # DRY_RUN에서도 조회해 형식을 확인할 수 있게 발송 여부와 무관하게 먼저 채운다.
        enrich_with_headcount(session, r)
        if DEBUG:
            print(
                f"[DEBUG] 예약번호={r['id']} has_phone={bool(r.get('phone'))} "
                f"has_headcount={bool(r.get('headcount'))} has_res_srl={bool(r.get('res_srl'))}",
                file=sys.stderr,
            )
        event_id = None
        if not DRY_RUN:
            try:
                send_push(r)
                event_id = create_calendar_event(r)
            except Exception as exc:  # noqa: BLE001
                # 한 건에서 실패해도 나머지 신규 확정 건은 계속 처리해야 한다.
                # 이 건은 notified에 추가하지 않아 다음 실행에서 다시 시도된다.
                print(f"알림/캘린더 처리 실패, 다음 실행에서 재시도: 예약번호={r['id']} ({exc})", file=sys.stderr)
                continue
        notified[r["id"]] = {"event_id": event_id, "checkin_date": checkin_date_iso(r)}
        changed = True

    cancelled_ids = find_cancelled_ids(notified, active_ids)
    print(f"취소 감지: {len(cancelled_ids)}건")
    if len(cancelled_ids) > MAX_CANCELLATIONS_PER_RUN:
        print(
            f"취소 감지 건수가 비정상적으로 많음({len(cancelled_ids)}건 > "
            f"{MAX_CANCELLATIONS_PER_RUN}건) - 목록 조회가 일부만 됐거나 사이트 문제일 수 있어 "
            "이번 실행에서는 취소 처리를 전부 건너뜀 (다음 실행에서 다시 시도됨)",
            file=sys.stderr,
        )
        cancelled_ids = []
    for rid in cancelled_ids:
        print(f"  -> 취소 감지: 예약번호={rid}")
        if not DRY_RUN:
            try:
                handle_cancellation(rid, notified[rid])
            except Exception as exc:  # noqa: BLE001
                # 이 건은 notified에서 지우지 않아 다음 실행에서 다시 시도된다.
                print(f"취소 처리 실패, 다음 실행에서 재시도: 예약번호={rid} ({exc})", file=sys.stderr)
                continue
        del notified[rid]
        changed = True

    if not DRY_RUN and changed:
        save_notified_ids(notified)


if __name__ == "__main__":
    main()
