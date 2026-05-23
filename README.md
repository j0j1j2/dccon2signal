# dccon2signal

디시인사이드 디시콘(DCcon) 패키지를 Signal Messenger 스티커 팩으로 변환·업로드하는 CLI.

- 디시콘 패키지 ID 만 입력하면 자동으로 스크래핑 → 변환 → Signal 업로드
- 정적 콘은 **512×512 PNG**, GIF 콘은 **소스 크기 APNG** (300 KiB 안에 들어가는 한 모든 프레임 보존)
- 원본 배경 유지가 기본 (디시콘은 보통 흰배경이 의도된 경우가 많음). 투명화 원하면 `--remove-bg`
- 변환 결과는 항상 `out/<package_idx>/` 에 저장돼서 실패해도 안전

## 설치

```bash
git clone https://github.com/j0j1j2/dccon2signal.git
cd dccon2signal
uv sync
```

`uv` 가 없으면: `curl -LsSf https://astral.sh/uv/install.sh | sh`

**필수: `apngasm` + `oxipng` 설치.** 애니메이션 스티커를 Signal 이 재생하는 APNG 포맷으로 인코딩합니다. 둘 다 없으면 변환이 실패합니다.

```bash
# macOS
brew install apngasm oxipng
# Debian/Ubuntu 22.04+
sudo apt install apngasm oxipng
# Arch
sudo pacman -S apngasm oxipng
```

## 빠른 시작 — 변환만 (인증 불필요)

```bash
uv run dccon2signal 170660 --no-upload
```

결과:
```
out/170660/
├── cover.png            # 512×512 표지
├── stickers/
│   ├── 1.png            # 정적 스티커 (PNG)
│   ├── 2.apng           # 애니메이션 스티커 (APNG)
│   └── ...
└── manifest.json        # 팩 메타데이터
```

이 폴더를 Signal Desktop 의 *Create / Upload Sticker Pack* 마법사에 수동으로 올리면 끝. 자동 업로드까지 필요 없으면 여기서 멈추세요.

## 자동 업로드 — Signal 인증 설정

자동 업로드는 Signal 계정의 `username` + `password` 가 필요합니다.

⚠️ **최신 Signal Desktop (6.x+) 은 `config.json` 에 비밀번호를 평문 저장하지 않습니다** (`encryptedKey` 만 존재). 대신 `signal-cli` 를 폰 Signal 계정의 **보조 디바이스로 링크**해서 자격증명을 얻습니다. 새 전화번호나 SMS 인증 필요 없음.

### 1. signal-cli 설치

```bash
brew install signal-cli jq
```

### 2. 폰 Signal 계정에 보조 디바이스로 링크

```bash
signal-cli link -n "dccon2signal"
```

→ `sgnl://linkdevice?uuid=...&pub_key=...` 형식의 URI 가 출력됩니다 (또는 QR 코드).

### 3. 휴대폰 Signal 앱에서 디바이스 연결

1. Settings → **Linked Devices** → **+** (오른쪽 위)
2. 카메라로 터미널의 QR 스캔 (또는 URI 직접 입력)
3. 디바이스 이름 확인 → **Link**

링크 성공하면 터미널의 `signal-cli` 가 자동으로 완료 처리되고, 자격증명 파일이 `~/.local/share/signal-cli/data/` 에 생성됩니다.

### 4. auth.json 생성

⚠️ **중요**: 보조 디바이스 (`isMultiDevice: true`) 인 경우 Signal 의 chat 서비스는 username 으로 **`{ACI}.{deviceId}`** 형식을 기대합니다. 일반 전화번호 (`+82...`) 나 `{number}.{deviceId}` 를 쓰면 `401 Invalid authentication` 이 납니다.

`ACI` (Account Identifier) 는 `aciAccountData.serviceId` 에, `deviceId` 는 최상위에 있습니다:

```bash
# signal-cli 가 생성한 데이터 파일 (숫자 이름) 찾기
SIGNAL_DATA=$(jq -r '.accounts[0].path' ~/.local/share/signal-cli/data/accounts.json)
SIGNAL_FILE="$HOME/.local/share/signal-cli/data/$SIGNAL_DATA"

mkdir -p ~/.config/dccon2signal
jq '{username: "\(.aciAccountData.serviceId).\(.deviceId|tostring)", password}' "$SIGNAL_FILE" \
  > ~/.config/dccon2signal/auth.json
chmod 600 ~/.config/dccon2signal/auth.json

# 확인 (password 는 길이만 표시, 노출 안함)
jq '{username, password: (.password | "[" + (.|length|tostring) + " chars]")}' \
  ~/.config/dccon2signal/auth.json
```

수동으로 만들 경우 `~/.local/share/signal-cli/data/<숫자>` 파일을 열어서:
- `aciAccountData.serviceId` → ACI (UUID 형식, 예: `54ab151d-141a-49cd-94e5-01e4a25ce625`)
- `deviceId` → 정수 (보통 2 이상, 폰이 1번)
- `password` → 24자 랜덤 문자열

조합해서 `auth.json` 작성:

```json
{
  "username": "54ab151d-141a-49cd-94e5-01e4a25ce625.3",
  "password": "<24자 password>"
}
```

### 5. 풀 자동 업로드

```bash
uv run dccon2signal 170660
```

출력 마지막에:
```
Install: https://signal.art/addstickers/#pack_id=abc123&pack_key=def456
```

이 링크를 폰에서 열면 Signal 앱이 알아서 팩을 설치합니다. 끝.

### auth 안 풀릴 때 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `401 Invalid authentication` | username 형식 잘못. `{aci}.{deviceId}` 사용 확인. `{number}` 단독은 작동 안함 |
| `403 Forbidden` | 디바이스가 unlink 됐을 가능성. `signal-cli list-devices` 로 확인 후 재링크 |
| `Auth file not found` | `~/.config/dccon2signal/auth.json` 부재. 4단계 다시 |
| `accounts.json` 에 `accounts: []` | 링크가 완료되지 않음. signal-cli link 단계 다시 |

## 자주 쓰는 옵션

```bash
uv run dccon2signal <package_idx> [<package_idx> ...] [OPTIONS]
```

| 옵션 | 설명 |
|---|---|
| `--no-upload` | 변환만, Signal 업로드 생략 (인증 불필요) |
| `--static-only` | GIF 도 첫 프레임만 PNG 로 (정적) |
| `--remove-bg` | 흰 배경을 알파로 자동 제거 (기본 OFF — 캐릭터의 흰 부분도 같이 사라질 수 있어서) |
| `--title TEXT` | 팩 제목 오버라이드 (기본: 디시콘 페이지 제목) |
| `--author TEXT` | 작성자 오버라이드 (기본: 디시콘 판매자명) |
| `--out-dir PATH` | 출력 디렉토리 (기본: `./out`) |
| `--emoji-map PATH` | `{"1": "😀", "2": "🐱", ...}` JSON 으로 스티커별 이모지 지정 |
| `--auth PATH` | auth.json 경로 (기본: `~/.config/dccon2signal/auth.json`) |
| `-v` | 상세 로그 |

여러 개 한꺼번에:
```bash
uv run dccon2signal 170660 12345 99999
```

## 디시콘 package_idx 찾는 법

디시콘 상세 페이지 URL 의 마지막 숫자. 예: `https://dccon.dcinside.com/...#170660` → `170660`.

## 동작 원리

1. **Scraper** — `POST https://dccon.dcinside.com/index/package_detail` 로 패키지 메타데이터 + 스티커 이미지 경로 목록 받음
2. **Downloader** — 이미지를 병렬 다운로드 (Referer 헤더 필수)
3. **Image Processor** — 정적은 Pillow 로 512×512 캔버스에 업스케일 (Lanczos) 한 PNG. 애니메이션은 소스 크기 유지하고 공유 팔레트 양자화 → `apngasm` 어셈블 → `oxipng` 후처리 (300 KiB 제한에 맞춰 컬러 256 → 8 단계로 내려가며 탐색, 그래도 안 맞으면 stride 증가). `--remove-bg` 옵션 시 흰배경 → 알파
4. **Pack Builder** — `signalstickers-client` 의 `LocalStickerPack` 으로 변환
5. **Uploader** — Signal 서버에 업로드 → `pack_id` + `pack_key` 받아서 설치 링크 조립

## 제한 사항

- 디시콘 원본은 **200×200 JPEG/GIF** 미리보기 화질이라 512×512 업스케일 시 약간 블러 발생 (Lanczos 리샘플링). 원본보다 약 2.5배 확대돼서 Signal 표시 사이즈와 맞음.
- 디시콘 비공개 / 삭제된 팩은 변환 불가
- Signal 스티커 팩 최대 200 개 (디시콘은 보통 20~50 개라 문제 없음)
- 스티커별 이모지 태그는 기본 `😀` placeholder. `--emoji-map` 또는 업로드 후 Signal Desktop 에서 편집

## 텔레그램 봇 (선택 사항)

`dccon2signal` 을 텔레그램으로 호출할 수 있는 봇이 같이 들어있어요. 큐가 직렬이라 봇 하나로 여러 사람이 써도 Signal rate limit 에 안 걸립니다.

### 1. BotFather 에서 봇 생성

폰 텔레그램에서 [@BotFather](https://t.me/BotFather) 와 채팅:
- `/newbot` → 봇 이름 + username 입력
- 받은 **HTTP API token** (e.g. `123456:ABC-...`) 을 저장

### 2. 환경 변수 파일

```bash
cp deploy/dccon2signal-bot.env.example ~/.config/dccon2signal-bot.env
chmod 600 ~/.config/dccon2signal-bot.env
# 그리고 ~/.config/dccon2signal-bot.env 열어서 TELEGRAM_BOT_TOKEN 채우기
```

`DCCON2SIGNAL_AUTH` 는 `auth.json` 셋업이 이미 끝난 상태여야 동작합니다 (위 "자동 업로드 — Signal 인증 설정" 섹션 참조).

### 3. systemd 유저 서비스로 실행

```bash
mkdir -p ~/.config/systemd/user
cp deploy/dccon2signal-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dccon2signal-bot
systemctl --user status dccon2signal-bot
journalctl --user -u dccon2signal-bot -f      # 실시간 로그
```

수동으로 한번 돌려보고 싶으면:
```bash
set -a; source ~/.config/dccon2signal-bot.env; set +a
uv run dccon2signal-bot
```

### 4. 사용

폰 텔레그램에서 봇을 찾아서 (DM이든 그룹이든 동일):

```
@봇username 170660
```

봇이 보낸 메시지가 단계별로 갱신되다가 마지막에 Signal 설치 링크로 바뀝니다. (`/start` 명령어로 사용법 안내도 가능)

### 5. 트러블슈팅

- 봇이 아무 응답 안함 → 토큰 확인 (`systemctl --user status dccon2signal-bot` 로 로그 보기)
- `❌ 봇 측 인증 문제` → `auth.json` 만료. signal-cli 재링크 + auth.json 재생성
- `Signal rate limit` → 한 번에 한 작업씩 직렬 처리되니 보통 안 걸리지만, 너무 자주 변환했으면 잠시 대기

## 개발

```bash
uv sync
uv run pytest -q          # 테스트 (29개)
uv run ruff check         # 린트
uv run ruff format        # 포맷
uv run mypy               # 타입 체크
```

설계 문서: [`docs/superpowers/specs/`](docs/superpowers/specs/) · 구현 계획: [`docs/superpowers/plans/`](docs/superpowers/plans/)

## License

MIT
