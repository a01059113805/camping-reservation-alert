#!/usr/bin/env python3
"""launchd가 30분마다 호출하는 로컬 실행 래퍼.

GitHub Actions는 이 사이트에서 차단되어 있어(IP 문제), 실제 스케줄 실행은
이 컴퓨터(Mac)에서 launchd로 돌린다. secrets.local.md와 local-secrets/의
서비스 계정 키를 읽어 환경변수로 세팅한 뒤 check_reservations.main()을
호출하고, 상태 파일이 바뀌었으면 git에 커밋/푸시한다.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT, "data", "notified_ids.json")


def load_secrets() -> None:
    with open(os.path.join(ROOT, "secrets.local.md"), encoding="utf-8") as f:
        txt = f.read()
    os.environ["ADMIN_ID"] = re.search(r"ADMIN_ID: (\S+)", txt).group(1)
    os.environ["ADMIN_PW"] = re.search(r"ADMIN_PW: (\S+)", txt).group(1)
    os.environ["VAPID_PRIVATE_KEY"] = re.search(r"VAPID_PRIVATE_KEY: (\S+)", txt).group(1)
    os.environ["VAPID_SUBJECT"] = re.search(r"VAPID_SUBJECT: (\S+)", txt).group(1)

    subs_match = re.search(r"등록된 값\(배열\):\s*```\s*(\[.*?\])\s*```", txt, re.S)
    if subs_match:
        os.environ["PUSH_SUBSCRIPTIONS"] = subs_match.group(1)

    with open(os.path.join(ROOT, "local-secrets", "service-account.json"), encoding="utf-8") as f:
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = f.read()
    # 이 저장소는 공개라서 캘린더 ID(=개인 이메일)를 코드에 두지 않고 secrets.local.md에서 읽는다.
    os.environ["GOOGLE_CALENDAR_ID"] = re.search(r"GOOGLE_CALENDAR_ID: (\S+@\S+)", txt).group(1)
    os.environ["STATE_FILE"] = STATE_FILE


def git(*args: str) -> None:
    subprocess.run(["git", *args], cwd=ROOT, check=True)


def commit_state_if_changed() -> None:
    diff = subprocess.run(
        ["git", "diff", "--quiet", "--", STATE_FILE], cwd=ROOT
    )
    if diff.returncode == 0:
        return  # 변경 없음
    git("add", STATE_FILE)
    git("commit", "-m", "chore: update notified reservation ids (local)")
    git("push")


def main() -> None:
    load_secrets()
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import check_reservations as cr

    cr.main()
    commit_state_if_changed()


if __name__ == "__main__":
    main()
