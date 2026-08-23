#!/usr/bin/env python3
"""취소 감지 기능(2026-08-16) 배포 이전부터 있던 notified_ids.json 항목은
event_id/checkin_date를 몰라 취소 감지 대상에서 빠진다. 아직 사이트 목록에
남아있는(=취소되지 않은) 예약이라면 지금이라도 이름/사이트/체크인일로 캘린더
일정과 매칭해서 event_id/checkin_date를 채워 넣어, 앞으로 그 예약이 취소될 때
정상적으로 푸시+캘린더 삭제가 동작하게 한다.

이미 event_id/checkin_date가 있는 항목이나, 목록에서 이미 사라진(과거에 취소/
완료됐을 예약) 항목은 건드리지 않는다.

사용법:
  python3 scripts/backfill_checkin_metadata.py            # 확인만 (기본 dry-run)
  python3 scripts/backfill_checkin_metadata.py --apply    # 실제 저장
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_reservations as cr  # noqa: E402
from backfill_calendar_descriptions import (  # noqa: E402
    build_reservation_index,
    fetch_our_events,
    load_secrets_if_needed,
)


def build_event_index(events: list[dict]) -> dict[tuple[str, str, str], str]:
    """(성함, 사이트 구역 및 번호, 체크인일) -> eventId. 키가 겹치면 먼저 나온 것을 유지한다."""
    index: dict[tuple[str, str, str], str] = {}
    for event in events:
        fields = cr.parse_event_description(event.get("description", ""))
        name = fields.get("성함", "")
        room = fields.get("사이트 구역 및 번호", "")
        start = (event.get("start") or {}).get("date", "")
        if not name or not room or not start:
            continue
        index.setdefault((name, room, start), event["id"])
    return index


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제로 notified_ids.json을 갱신한다 (기본은 확인만)")
    parser.add_argument("--dry-run", action="store_true", help="확인만 한다 (기본 동작)")
    parser.add_argument("--max-pages", type=int, default=200, help="목록을 몇 페이지까지 읽을지")
    args = parser.parse_args()
    apply_changes = args.apply and not args.dry_run
    cr.MAX_PAGES = args.max_pages

    load_secrets_if_needed()
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
    service_account_email = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])["client_email"]

    service = cr.get_calendar_service()
    all_events, our_events = fetch_our_events(service, calendar_id, service_account_email)
    print(f"캘린더 전체 일정 {len(all_events)}건 중 스크립트가 만든 일정 {len(our_events)}건")
    event_index = build_event_index(our_events)

    session = requests.Session()
    cr.login(session)
    rows = cr.fetch_reservation_rows(session)
    reservation_index = build_reservation_index(rows)
    print(f"예약 목록에서 {len(rows)}행 조회 (현재 목록에 남아있는 예약만 대상)")

    notified = cr.load_notified_ids()
    targets = [rid for rid, meta in notified.items() if not meta.get("checkin_date")]
    print(f"체크인일 정보가 없는 기존 항목: {len(targets)}건")

    matched_both, matched_checkin_only, still_missing, not_in_list = 0, 0, 0, 0
    for rid in targets:
        row = next((r for r in rows if r["id"] == rid), None)
        if row is None:
            not_in_list += 1
            continue
        checkin = cr.checkin_date_iso(row)
        if not checkin:
            still_missing += 1
            continue
        key = (row["name"], row["room"], checkin)
        event_id = event_index.get(key)
        if event_id:
            notified[rid] = {"event_id": event_id, "checkin_date": checkin}
            matched_both += 1
        else:
            notified[rid] = {"event_id": None, "checkin_date": checkin}
            matched_checkin_only += 1

    print(f"\n체크인일+캘린더 일정 모두 매칭: {matched_both}건")
    print(f"체크인일만 채움(캘린더 일정 못 찾음): {matched_checkin_only}건")
    print(f"현재 목록에 이미 없음(과거 완료/취소로 추정, 건드리지 않음): {not_in_list}건")
    print(f"날짜 파싱 실패: {still_missing}건")

    if apply_changes:
        cr.save_notified_ids(notified)
        print("\nnotified_ids.json 갱신 완료")
    else:
        print("\n실제로 반영하려면 --apply 를 붙여 다시 실행하세요.")


if __name__ == "__main__":
    main()
