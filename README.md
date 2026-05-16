# dccon2signal

디시인사이드 디시콘(DCcon) 패키지를 Signal Messenger 스티커 팩으로 변환·업로드하는 CLI.

- 디시콘 패키지 ID 만 입력하면 자동으로 스크래핑 → 변환 → Signal 업로드
- GIF 디시콘은 **APNG** 로 변환 (움직임 유지)
- 흰 배경 자동 투명화 (다크모드 호환)
- 변환 결과는 항상 `out/<package_idx>/` 에 저장돼서 실패해도 안전

## 설치

```bash
git clone https://github.com/j0j1j2/dccon2signal.git
cd dccon2signal
uv sync
```

`uv` 가 없으면: `curl -LsSf https://astral.sh/uv/install.sh | sh`

## 빠른 시작 — 변환만 (인증 불필요)

```bash
uv run dccon2signal 170660 --no-upload
```

결과:
```
out/170660/
├── cover.png            # 512×512 표지
├── stickers/
│   ├── 1.png            # 정적 스티커
│   ├── 2.apng           # 애니메이션 스티커
│   └── ...
└── manifest.json        # 팩 메타데이터
```

이 폴더를 Signal Desktop 의 *Create / Upload Sticker Pack* 마법사에 수동으로 올리면 끝. 자동 업로드까지 필요 없으면 여기서 멈추세요.

## 자동 업로드 — Signal 인증 설정

자동 업로드는 Signal 계정의 `username` (전화번호) + `password` (Signal 프로토콜용 비밀번호, 앱 잠금 번호 아님) 가 필요합니다.

⚠️ **최신 Signal Desktop (6.x+) 은 `config.json` 에 비밀번호를 평문 저장하지 않습니다.** 대신 `signal-cli` 를 폰 Signal 계정의 **보조 디바이스로 링크**해서 자격증명을 얻습니다. 새 전화번호나 SMS 인증 필요 없음.

### 1. signal-cli 설치

```bash
brew install signal-cli
```

### 2. 폰 Signal 계정에 보조 디바이스로 링크

```bash
signal-cli link -n "dccon2signal"
```

→ `tsdevice:/?uuid=...&pub_key=...` 형식의 URI 가 출력됩니다 (또는 QR 코드).

### 3. 휴대폰 Signal 앱에서 디바이스 연결

1. Settings → **Linked Devices** → **+** (오른쪽 위)
2. 카메라로 터미널의 QR 스캔 (또는 URI 직접 입력)
3. 디바이스 이름 확인 → **Link**

링크 성공하면 터미널의 `signal-cli` 가 자동으로 완료 처리됩니다.

### 4. auth.json 생성

```bash
SIGNAL_DATA=$(ls ~/.local/share/signal-cli/data/+*.json | head -1)
mkdir -p ~/.config/dccon2signal
jq '{username: .username // .number, password: .password}' "$SIGNAL_DATA" \
  > ~/.config/dccon2signal/auth.json
chmod 600 ~/.config/dccon2signal/auth.json
```

`jq` 없으면 `brew install jq`. 또는 `~/.local/share/signal-cli/data/+xxx.json` 을 직접 열어서 `username` / `number` 와 `password` 값을 보고 수동으로 만들어도 됩니다:

```json
{
  "username": "+821012345678",
  "password": "32-char-random-string"
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

## 자주 쓰는 옵션

```bash
uv run dccon2signal <package_idx> [<package_idx> ...] [OPTIONS]
```

| 옵션 | 설명 |
|---|---|
| `--no-upload` | 변환만, Signal 업로드 생략 (인증 불필요) |
| `--static-only` | GIF 도 첫 프레임만 PNG 로 (정적) |
| `--no-bg-removal` | 흰 배경 자동 투명화 끄기 |
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
3. **Image Processor** — Pillow 로 512×512 캔버스에 fit, 흰 배경 → 알파, GIF → APNG (300KB 제한에 맞춰 프레임 스트라이드 자동 조절)
4. **Pack Builder** — `signalstickers-client` 의 `LocalStickerPack` 으로 변환
5. **Uploader** — Signal 서버에 업로드 → `pack_id` + `pack_key` 받아서 설치 링크 조립

## 제한 사항

- 디시콘 원본은 **200×200 JPEG/GIF** 미리보기 화질이라 512×512 업스케일 시 약간 블러 발생 (Lanczos 리샘플링)
- 디시콘 비공개 / 삭제된 팩은 변환 불가
- Signal 스티커 팩 최대 200 개 (디시콘은 보통 20~50 개라 문제 없음)
- 스티커별 이모지 태그는 기본 `😀` placeholder. `--emoji-map` 또는 업로드 후 Signal Desktop 에서 편집

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
