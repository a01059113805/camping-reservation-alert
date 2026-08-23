#!/usr/bin/env python3
"""아직 추적되지 않은 예약 중 체크인일이 오늘 이후인 건들을 구글 캘린더에 등록하고
notified_ids.json에 기록한다 (앞으로 취소 감지도 정상 작동하게).

과거 체크인 건들은 사용자 지시에 따라 다루지 않는다. 오래된 백로그라 지금 와서
"새 예약 확정" 푸시를 보내면 스팸이므로 푸시는 보내지 않고 캘린더 등록만 한다.

--apply 없이는 무엇을 등록할지만 보여주고 아무것도 만들지 않는다.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# backfill_calendar_descriptions가 자기 안에서 이미 check_reservations를 import해버리기
# 때문에(그 시점엔 아직 STATE_FILE 환경변수가 안 채워짐), check_reservations.STATE_FILE이
# 잘못된 기본값(상대경로)으로 캐시될 수 있다. load_secrets_if_needed() 이후 명시적으로
# 다시 맞춰준다.
import check_reservations as cr  # noqa: E402
from backfill_calendar_descriptions import load_secrets_if_needed  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    load_secrets_if_needed()
    cr.STATE_FILE = os.environ["STATE_FILE"]
    assert os.path.exists(cr.STATE_FILE), f"STATE_FILE을 못 찾음: {cr.STATE_FILE}"

    session = requests.Session()
    cr.login(session)
    raw_rows = cr.fetch_reservation_rows(session)
    confirmed = [r for r in raw_rows if cr.is_confirmed(r["status"])]
    print(f"확인된 예약완료 건수: {len(confirmed)}")

    notified = cr.load_notified_ids()
    today = datetime.date.today()

    targets = []
    for r in confirmed:
        if r["id"] in notified:
            continue
        checkin = cr.checkin_date_iso(r)
        if not checkin or datetime.date.fromisoformat(checkin) < today:
            continue
        targets.append(r)

    print(f"등록 대상(미추적 + 체크인 {today.isoformat()} 이후): {len(targets)}건")

    created, failed = 0, []
    for r in targets:
        cr.enrich_with_headcount(session, r)
        if args.apply:
            try:
                event_id = cr.create_calendar_event(r)
            except Exception as exc:  # noqa: BLE001
                print(f"  실패: 예약번호={r['id']} ({exc})", file=sys.stderr)
                failed.append(r["id"])
                continue
            notified[r["id"]] = {"event_id": event_id, "checkin_date": cr.checkin_date_iso(r)}
        else:
            print(f"  등록 예정: 예약번호={r['id']} room={r['room']} date={r['date']}")
        created += 1

    print(f"\n{'등록 완료' if args.apply else '등록 예정(dry-run)'}: {created}건")
    if failed:
        print(f"실패(다음에 재시도 필요): {len(failed)}건 -> {failed}")

    if args.apply:
        cr.save_notified_ids(notified)
        print("notified_ids.json 저장 완료 (커밋은 하지 않음, 푸시는 보내지 않음)")
    else:
        print("\n--apply 없이 실행함: 아무것도 만들지 않았습니다.")


if __name__ == "__main__":
    main()
