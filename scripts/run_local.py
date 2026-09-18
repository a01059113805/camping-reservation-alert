#!/usr/bin/env python3
"""launchd가 1시간마다 호출하는 로컬 실행 래퍼.

GitHub Actions는 이 사이트에서 차단되어 있어(IP 문제), 실제 스케줄 실행은
이 컴퓨터(Mac)에서 launchd로 돌린다. secrets.local.md와 local-secrets/의
서비스 계정 키를 읽어 환경변수로 세팅한 뒤 check_reservations.main()을
호출하고, 상태 파일이 바뀌었으면 git에 커밋/푸시한다.

배터리 절약(2026-09-18 사용자 요청): 맥이 켜져 있어도 사람이 자리를 비워
키보드/마우스 입력이 없으면(=화면만 켜둔 채 방치) 사이트 로그인/전체 스캔
같은 무거운 작업을 아예 건너뛴다. launchd 틱(1시간) 자체는 그대로 두되,
매번 유휴시간만 가볍게 확인해서 실제 작업 여부를 결정한다.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT, "data", "notified_ids.json")
IDLE_THRESHOLD_SECONDS = int(os.environ.get("IDLE_THRESHOLD_SECONDS", "600"))


def idle_seconds() -> float:
    """마지막 키보드/마우스 입력 이후 지난 시간(초). macOS HIDIdleTime 기반."""
    out = subprocess.run(
        ["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, check=True
    ).stdout
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            nanoseconds = int(line.split("=")[-1].strip())
            return nanoseconds / 1_000_000_000
    return 0.0


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
    idle = idle_seconds()
    if idle > IDLE_THRESHOLD_SECONDS:
        print(f"자리비움으로 판단(마지막 입력 후 {idle:.0f}초 경과) - 이번 실행은 건너뜀")
        return

    load_secrets()
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import check_reservations as cr

    cr.main()
    commit_state_if_changed()


if __name__ == "__main__":
    main()
