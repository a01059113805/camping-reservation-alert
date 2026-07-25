# 캠핑장 예약확정 자동 알림

포항서숲오토캠핑장(`pscamp.hana-pnc.co.kr`) 예약 관리 페이지를 30분마다 자동으로 확인해서, 새로 "예약완료(입금완료)"된 건이 있으면 휴대폰으로 무료 웹푸시 알림을 보내고, 동시에 구글 캘린더(전용 캘린더)에 일정을 등록한다.

## 구조
- `push/` — GitHub Pages로 배포되는 알림 구독 페이지 (아이폰 홈 화면에 추가해서 사용)
- `scripts/check_reservations.py` — 예약 관리자 페이지 로그인 → 예약완료 목록 확인 → 신규 건 웹푸시 발송 + 구글 캘린더 일정 등록
- `.github/workflows/check-reservations.yml` — 30분마다 위 스크립트를 실행하는 GitHub Actions
- `data/notified_ids.json` — 이미 알림을 보낸 예약번호 기록 (중복 알림/중복 캘린더 등록 방지, Actions가 자동으로 갱신/커밋)

## 설정 순서

### 1. GitHub 저장소 생성 & 푸시
이 폴더를 비공개 GitHub 저장소로 만들고 push한다.

### 2. GitHub Pages 활성화
저장소 Settings → Pages → Source를 `main` 브랜치의 `/push` 폴더로 지정.
배포되면 `https://<사용자명>.github.io/<저장소명>/` 형태의 주소가 생긴다.

### 3. 휴대폰에서 알림 구독 (아이폰·갤럭시 등 기종 무관, 여러 대 등록 가능)
알림을 받을 기기마다 아래를 반복한다 (예: 아이폰 1대 + 갤럭시 1대면 2번 반복).
1. [아이폰] 사파리로 위 주소 접속 → 공유 버튼 → "홈 화면에 추가" → 홈 화면 아이콘으로 다시 열기
   [갤럭시/안드로이드] Chrome으로 위 주소 접속 (그대로 사용 가능, 안정성을 위해 메뉴 → "홈 화면에 추가" 권장)
2. "알림 켜기" 클릭 → 알림 허용
3. 화면에 표시된 JSON 값을 복사해서 기기별로 모아둔다 (다음 단계에서 사용)

### 4. 예약 관리자 페이지의 정확한 URL (확인 완료)
"예약현황" 목록에서 상태 필터를 "예약완료"(`status=1`)로 걸었을 때의 실제 URL을 확인해서
`scripts/check_reservations.py`의 `LIST_URL` 기본값에 반영해뒀다. 별도로 `LIST_URL` 시크릿을
등록하지 않아도 이 기본값이 사용된다 (등록하면 그 값이 우선).

### 5. 구글 캘린더 연동 설정 (서비스 계정 방식, 토큰 갱신 불필요)
1. [Google Cloud Console](https://console.cloud.google.com)에서 새 프로젝트 생성 (무료)
2. "API 및 서비스" → "라이브러리"에서 **Google Calendar API** 활성화
3. "API 및 서비스" → "사용자 인증 정보" → "사용자 인증 정보 만들기" → **서비스 계정** 생성
4. 생성된 서비스 계정 → "키" 탭 → "키 추가" → JSON 다운로드 (이 파일 전체 내용을 통째로 시크릿에 넣을 것)
5. 다운로드한 JSON 안의 `client_email` 값 확인 (예: `xxx@xxx.iam.gserviceaccount.com`)
6. 구글 캘린더 앱에서 새 캘린더 생성 (예: "캠핑장 예약")
7. 그 캘린더 설정 → "특정 사용자와 공유" → 5번의 서비스 계정 이메일 추가, 권한은 **"일정 변경"** 으로 설정
8. 같은 설정 화면에서 "캘린더 통합" → **캘린더 ID** 값 복사 (보통 `xxxxxx@group.calendar.google.com` 형태)

### 6. GitHub Secrets 등록
저장소 Settings → Secrets and variables → Actions → New repository secret 에서 아래 값을 등록:

| 이름 | 값 |
|---|---|
| `ADMIN_ID` | 예약 관리자 로그인 아이디 |
| `ADMIN_PW` | 예약 관리자 로그인 비밀번호 |
| `LOGIN_URL` | (4번에서 확인한 로그인 URL, 기본값과 다르면) |
| `LIST_URL` | (4번에서 확인한 예약완료 목록 URL) |
| `VAPID_PRIVATE_KEY` | `secrets.local.md` 참고 |
| `VAPID_SUBJECT` | `secrets.local.md` 참고 |
| `PUSH_SUBSCRIPTIONS` | 3번에서 기기별로 모은 JSON을 배열로 묶은 문자열 (기기 1대면 `[ {...} ]`, 여러 대면 `[ {...}, {...} ]`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 5번에서 다운로드한 JSON 파일 전체 내용 |
| `GOOGLE_CALENDAR_ID` | 5번에서 복사한 캘린더 ID |

### 7. 최초 1회 시드 실행 (중요 — 과거 예약 폭탄알림 방지)
지금까지 쌓인 예약(2,300건 이상)을 전부 "신규"로 인식해서 한꺼번에 알림이 쏟아지는 걸 막기 위해, 처음 한 번은 알림 없이 현재 상태만 기록해야 한다.

Actions 탭 → "Check reservations" 워크플로우 → "Run workflow" → `seed_only`를 `1`로 설정하고 실행.
(과거 확정건이 많으면 `scripts/check_reservations.py`의 `MAX_PAGES` 환경변수를 늘려서 실행해야 할 수 있음 — 필요하면 워크플로우 env에 `MAX_PAGES: "200"` 추가)

### 8. 정상 동작 확인
1. `dry_run=1`로 수동 실행 → 로그에서 예약완료 건수/신규 건수가 정상적으로 잡히는지 확인
2. 실제 예약 1건을 입금대기 → 예약완료로 바꿔보고, 다음 스케줄(또는 수동 실행)에서 아이폰에 알림이 오고 "캠핑장 예약" 캘린더에 일정이 뜨는지 확인
3. 문제없으면 이후로는 그대로 30분마다 자동 실행됨

## 예약일 → 캘린더 연도 추정 관련 주의사항
목록에는 예약일이 "08-02~08-03"처럼 연도 없이 나오기 때문에, 스크립트는 오늘 날짜를 기준으로 연도를 추정한다(180일 이상 과거로 보이면 다음 해로 간주). 연말/연초에 걸친 예약에서 혹시 연도가 잘못 잡히면 캘린더에서 직접 수정하면 된다.

## 로컬 테스트
```bash
cd scripts
pip install -r requirements.txt
ADMIN_ID=... ADMIN_PW=... LIST_URL=... GOOGLE_SERVICE_ACCOUNT_JSON=... GOOGLE_CALENDAR_ID=... DRY_RUN=1 python check_reservations.py
```
