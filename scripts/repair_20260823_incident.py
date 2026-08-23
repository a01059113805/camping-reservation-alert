#!/usr/bin/env python3
"""일회성 복구 스크립트 (2026-08-23 사고 전용).

MAX_PAGES=30 한도 때문에 활성 예약 1919건 중 뒤쪽 페이지의 예약들이 목록 조회에서
빠졌고, 그걸 취소로 오판해 468건을 잘못 처리했다(푸시 발송 + 그중 12건은 캘린더
일정까지 삭제). 삭제된 12건은 구글 캘린더 API로 직접 조회해(성함/사이트/체크인일)
정확히 특정해뒀다(/tmp/deleted_events.json).

이 스크립트는:
  1. 삭제된 12건을 지금 살아있는 예약 정보로 새 캘린더 일정으로 재생성
  2. 오판된 468개 예약번호 전부를 notified_ids.json에 복구
     (현재 목록 기준 checkin_date 재계산, event_id는 재생성한 일정 또는 기존에
     남아있는 일정과 이름/사이트/체크인일로 매칭)

--apply 없이는 무엇을 할지만 보여주고 아무것도 바꾸지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_reservations as cr  # noqa: E402
from backfill_calendar_descriptions import fetch_our_events, load_secrets_if_needed  # noqa: E402


def build_event_index(events: list[dict]) -> dict[tuple[str, str, str], str]:
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
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--flagged-file", default="/tmp/flagged_ids.txt")
    parser.add_argument("--deleted-events-file", default="/tmp/deleted_events.json")
    args = parser.parse_args()

    with open(args.flagged_file, encoding="utf-8") as f:
        flagged_ids = [line.strip() for line in f if line.strip()]
    with open(args.deleted_events_file, encoding="utf-8") as f:
        deleted_events = json.load(f)
    print(f"오판된 예약번호: {len(flagged_ids)}건, 실제로 삭제됐던 캘린더 일정: {len(deleted_events)}건")

    load_secrets_if_needed()
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
    service_account_email = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])["client_email"]
    service = cr.get_calendar_service()

    session = requests.Session()
    cr.login(session)
    rows = cr.fetch_reservation_rows(session)
    row_by_id = {r["id"]: r for r in rows}
    # 이름/사이트/체크인일 -> 행. 여러 행이 같은 키를 쓰면(중복 예약번호) 먼저 나온 걸 쓴다.
    row_by_key: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        checkin = cr.checkin_date_iso(r)
        if checkin:
            row_by_key.setdefault((r["name"], r["room"], checkin), r)
    print(f"현재 목록에서 {len(rows)}행 조회")

    _, our_events = fetch_our_events(service, calendar_id, service_account_email)
    event_index = build_event_index(our_events)
    print(f"현재 남아있는 스크립트 캘린더 일정: {len(our_events)}건")

    # 1) 삭제된 12건 재생성
    recreated_index: dict[tuple[str, str, str], str] = {}
    for item in deleted_events:
        key = (item["name"], item["room"], item["checkin"])
        row = row_by_key.get(key)
        if row is None:
            print(f"  [경고] 재생성 대상인데 현재 목록에서 못 찾음: {item['event_id']} (checkin={item['checkin']})")
            continue
        if args.apply:
            new_id = cr.create_calendar_event(row)
        else:
            new_id = f"<APPLY 시 새로 생성: {item['event_id']} 대체>"
        recreated_index[key] = new_id
        print(f"  재생성: checkin={item['checkin']} room={item['room']} -> {new_id}")

    # 2) 468건 전체 notified_ids 복구
    notified = cr.load_notified_ids()
    restored, unmatched_in_site = 0, []
    for rid in flagged_ids:
        row = row_by_id.get(rid)
        if row is None:
            unmatched_in_site.append(rid)
            continue
        checkin = cr.checkin_date_iso(row)
        key = (row["name"], row["room"], checkin) if checkin else None
        event_id = (recreated_index.get(key) or event_index.get(key)) if key else None
        notified[rid] = {"event_id": event_id, "checkin_date": checkin}
        restored += 1

    print(f"\n복구됨: {restored}건")
    print(f"현재 목록에서도 안 보임(수동 확인 필요): {len(unmatched_in_site)}건")
    for rid in unmatched_in_site:
        print(f"  - {rid}")

    if args.apply:
        cr.save_notified_ids(notified)
        print("\nnotified_ids.json 저장 완료 (커밋은 하지 않음)")
    else:
        print("\n--apply 없이 실행함: 아무것도 저장/생성하지 않았습니다.")


if __name__ == "__main__":
    main()
