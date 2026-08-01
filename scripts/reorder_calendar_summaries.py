#!/usr/bin/env python3
"""이미 등록된 구글 캘린더 일정의 제목을 '사이트 먼저' 형식으로 바꾼다.

예전 형식은 '{성함} · {사이트}'라 목록이 이름 가나다순으로 섞여 색이 흩어져 보였다.
새 형식은 '{사이트} · {성함}'(build_event_summary)이라, 제목 가나다순 정렬에서
같은 사이트(=같은 색)끼리 한 덩어리로 붙는다.

check_reservations.py가 만든 일정만 골라서 제목만 바꾼다. 설명/날짜/색상/특이사항은
건드리지 않는다. 이름·사이트 값은 예약 목록을 다시 조회하지 않고 일정 설명에 이미
들어 있는 '성함'·'사이트 구역 및 번호' 줄에서 그대로 읽어 쓴다.

사용법:
  python3 scripts/reorder_calendar_summaries.py            # 확인만 (기본 dry-run)
  python3 scripts/reorder_calendar_summaries.py --apply    # 실제 갱신
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_reservations as cr  # noqa: E402
from backfill_calendar_descriptions import fetch_our_events, load_secrets_if_needed  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제로 캘린더를 갱신한다 (기본은 확인만)")
    parser.add_argument("--dry-run", action="store_true", help="확인만 한다 (기본 동작)")
    args = parser.parse_args()
    apply_changes = args.apply and not args.dry_run

    load_secrets_if_needed()
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
    service_account_email = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])["client_email"]

    service = cr.get_calendar_service()
    all_events, events = fetch_our_events(service, calendar_id, service_account_email)
    print(f"캘린더 전체 일정 {len(all_events)}건 중 스크립트가 만든 일정 {len(events)}건을 대상으로 함")
    if not events:
        return

    updated, unchanged, skipped = 0, 0, 0
    for event in events:
        fields = cr.parse_event_description(event.get("description", ""))
        name = fields.get("성함", "")
        room = fields.get("사이트 구역 및 번호", "")
        if not name or not room:
            # 성함/사이트를 못 읽으면 제목을 안전하게 재구성할 수 없으므로 건너뛴다.
            skipped += 1
            continue

        new_summary = cr.build_event_summary({"name": name, "room": room})
        if new_summary == event.get("summary"):
            unchanged += 1
            continue

        if apply_changes:
            service.events().patch(
                calendarId=calendar_id, eventId=event["id"], body={"summary": new_summary}
            ).execute()
        else:
            # 개인정보(성함)가 로그에 남지 않도록 사이트/이벤트ID만 출력한다.
            print(f"  - eventId={event['id']} {room} · (성함 생략)")
        updated += 1

    verb = "제목 갱신함" if apply_changes else "제목 갱신 예정(dry-run)"
    print(f"\n{verb}: {updated}건")
    print(f"이미 새 형식: {unchanged}건")
    print(f"성함/사이트 못 읽어 건너뜀: {skipped}건")
    if not apply_changes and updated:
        print("\n실제로 반영하려면 --apply 를 붙여 다시 실행하세요.")


if __name__ == "__main__":
    main()
