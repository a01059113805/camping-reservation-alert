#!/usr/bin/env python3
"""구글 캘린더에서 check_reservations.py가 만든 캠핑 예약 일정을 전부 삭제한다.

캘린더 ID가 개인 캘린더라 예약과 무관한 개인 일정이 섞여 있으므로, backfill 스크립트와
똑같이 아래 세 조건을 모두 만족하는 일정만 삭제 대상으로 삼는다 (개인 일정은 절대 건드리지 않음).
  1) 서비스 계정이 만든 일정 (creator.email == 서비스 계정 이메일)
  2) 종일 일정 (start.date 존재)
  3) 설명에 '사이트 구역 및 번호:' 줄이 있음

사용법:
  python3 scripts/delete_all_reservation_events.py            # 확인만 (기본 dry-run)
  python3 scripts/delete_all_reservation_events.py --apply    # 실제 삭제
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_reservations as cr  # noqa: E402


def load_secrets_if_needed() -> None:
    if os.environ.get("GOOGLE_CALENDAR_ID") and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        return
    import run_local

    run_local.load_secrets()


def fetch_our_events(service, calendar_id: str, service_account_email: str) -> list[dict]:
    events, page_token = [], None
    while True:
        resp = (
            service.events()
            .list(calendarId=calendar_id, maxResults=2500, pageToken=page_token, showDeleted=False)
            .execute()
        )
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    ours = []
    for event in events:
        created_by_bot = (event.get("creator") or {}).get("email") == service_account_email
        is_all_day = bool((event.get("start") or {}).get("date"))
        has_our_description = "사이트 구역 및 번호:" in (event.get("description") or "")
        if created_by_bot and is_all_day and has_our_description:
            ours.append(event)
    return ours


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제로 삭제한다 (기본은 확인만)")
    args = parser.parse_args()

    load_secrets_if_needed()
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
    service_account_email = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])["client_email"]

    service = cr.get_calendar_service()
    events = fetch_our_events(service, calendar_id, service_account_email)
    print(f"삭제 대상(스크립트가 만든 예약 일정): {len(events)}건")
    # 개인정보가 로그에 남지 않도록 날짜만 출력한다.
    for event in sorted(events, key=lambda e: (e.get("start") or {}).get("date", "")):
        start = (event.get("start") or {}).get("date", "")
        print(f"  - {start} (id={event['id']})")

    if not events:
        return
    if not args.apply:
        print("\n확인만 함 (dry-run). 실제로 지우려면 --apply 를 붙여 다시 실행.")
        return

    deleted = 0
    for event in events:
        try:
            service.events().delete(calendarId=calendar_id, eventId=event["id"]).execute()
            deleted += 1
        except HttpError as exc:
            if exc.resp.status in (404, 410):
                deleted += 1  # 이미 삭제됨
                continue
            print(f"삭제 실패 (id={event['id']}): {exc}", file=sys.stderr)
    print(f"\n삭제 완료: {deleted}/{len(events)}건")


if __name__ == "__main__":
    main()
