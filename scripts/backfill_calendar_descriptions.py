#!/usr/bin/env python3
"""이미 등록된 구글 캘린더 일정의 설명을 현재 형식(연락처/인원/요금 포함)으로 갱신한다.

check_reservations.py가 만든 일정만 골라서 설명만 바꾼다. 제목/날짜/색상은 건드리지 않고,
사람이 직접 적어 넣은 특이사항도 그대로 보존한다.

캘린더 ID가 개인 캘린더인 경우 예약과 무관한 일정이 섞여 있으므로, 아래 세 조건을 모두
만족하는 일정만 대상으로 삼는다.
  1) 서비스 계정이 만든 일정 (creator.email == 서비스 계정 이메일)
  2) 종일 일정 (start.date 존재)
  3) 설명에 '사이트 구역 및 번호:' 줄이 있음

사용법:
  python3 scripts/backfill_calendar_descriptions.py            # 확인만 (기본 dry-run)
  python3 scripts/backfill_calendar_descriptions.py --apply    # 실제 갱신
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_reservations as cr  # noqa: E402


def load_secrets_if_needed() -> None:
    """launchd 실행과 같은 방식으로 로컬 시크릿을 채운다 (이미 있으면 그대로 사용)."""
    if os.environ.get("ADMIN_ID") and os.environ.get("GOOGLE_CALENDAR_ID"):
        return
    import run_local

    run_local.load_secrets()


def redact(text: str) -> str:
    """샘플 출력용. 이름은 첫 글자만, 전화번호는 가운데 자리를 가린다."""
    out = []
    for line in (text or "").split("\n"):
        label, sep, value = line.partition(":")
        value = value.strip()
        if sep and label.strip() == "성함" and value:
            value = value[0] + "*" * (len(value) - 1)
        elif sep and label.strip() == "연락처" and value.count("-") == 2:
            head, _, tail = value.rpartition("-")
            value = f"{head.split('-')[0]}-****-{tail}"
        out.append(f"{label}: {value}" if sep else line)
    return "\n".join(out)


def checkin_date(reservation: dict) -> str | None:
    try:
        start, _ = cr.parse_date_range(reservation["date"])
    except Exception:  # noqa: BLE001
        return None
    return start.isoformat()


def build_reservation_index(rows: list[dict]) -> dict[tuple[str, str, str], dict]:
    """(성함, 객실, 체크인일) -> 예약 행. 같은 키가 겹치면 뒤(더 오래된 행)를 버린다."""
    index: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        start = checkin_date(row)
        if not start or not row["name"] or not row["room"]:
            continue
        index.setdefault((row["name"], row["room"], start), row)
    return index


def fetch_our_events(service, calendar_id: str, service_account_email: str) -> tuple[list[dict], list[dict]]:
    """(캘린더 전체 일정, 그중 스크립트가 만든 일정) 을 돌려준다."""
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
    return events, ours


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제로 캘린더를 갱신한다 (기본은 확인만)")
    parser.add_argument("--dry-run", action="store_true", help="확인만 한다 (기본 동작)")
    # 목록은 예약 접수순 내림차순이라 오래전에 예약한 미래 숙박 건은 뒤쪽 페이지에 있다.
    # 신규 감지용 기본값(30페이지)으로는 그런 건을 못 찾으므로 여기서는 끝까지 넘긴다.
    parser.add_argument("--max-pages", type=int, default=200, help="목록을 몇 페이지까지 읽을지")
    parser.add_argument("--sample", type=int, default=0, help="변경 전/후를 몇 건 보여줄지 (이름/전화 일부 가림)")
    args = parser.parse_args()
    apply_changes = args.apply and not args.dry_run
    cr.MAX_PAGES = args.max_pages

    load_secrets_if_needed()
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
    service_account_email = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])["client_email"]

    service = cr.get_calendar_service()
    all_events, events = fetch_our_events(service, calendar_id, service_account_email)
    print(f"캘린더 전체 일정 {len(all_events)}건 중 스크립트가 만든 일정 {len(events)}건을 대상으로 함")
    if not events:
        return

    session = requests.Session()
    cr.login(session)
    rows = cr.fetch_reservation_rows(session)
    index = build_reservation_index(rows)
    print(f"예약 목록에서 {len(rows)}행 조회, 매칭 키 {len(index)}개 생성")

    updated, unchanged, unmatched, memo_kept = 0, 0, [], 0
    headcount_cache: dict[str, str] = {}

    for event in events:
        fields = cr.parse_event_description(event.get("description", ""))
        name = fields.get("성함", "")
        room = fields.get("사이트 구역 및 번호", "")
        memo = fields.get("특이사항", "")
        start = (event.get("start") or {}).get("date", "")

        reservation = index.get((name, room, start))
        if reservation is None:
            unmatched.append((event["id"], start))
            continue

        # 같은 예약을 두 번 조회하지 않도록 res_srl 기준으로 캐시한다.
        res_srl = reservation.get("res_srl", "")
        if res_srl not in headcount_cache:
            cr.enrich_with_headcount(session, reservation)
            headcount_cache[res_srl] = reservation.get("headcount", "")
        reservation["headcount"] = headcount_cache[res_srl]

        new_description = cr.build_event_description(reservation, memo=memo)
        if new_description == event.get("description"):
            unchanged += 1
            continue
        if memo:
            memo_kept += 1

        if args.sample and updated < args.sample:
            print(f"\n--- eventId={event['id']} start={start} ---")
            print("[변경 전]")
            print(redact(event.get("description", "")))
            print("[변경 후]")
            print(redact(new_description))

        if apply_changes:
            service.events().patch(
                calendarId=calendar_id, eventId=event["id"], body={"description": new_description}
            ).execute()
        updated += 1

    verb = "갱신함" if apply_changes else "갱신 예정(dry-run)"
    print(f"\n{verb}: {updated}건")
    print(f"이미 최신 형식: {unchanged}건")
    print(f"직접 적은 특이사항 보존: {memo_kept}건")
    print(f"예약 목록에서 못 찾아 건너뜀: {len(unmatched)}건")
    for event_id, start in unmatched:
        # 개인정보가 로그에 남지 않도록 일정 ID와 날짜만 출력한다.
        print(f"  - eventId={event_id} start={start}")
    if not apply_changes and updated:
        print("\n실제로 반영하려면 --apply 를 붙여 다시 실행하세요.")


if __name__ == "__main__":
    main()
