# 관리자 UI 브라우저 smoke

정적 Svelte 산출물의 실제 렌더링을 확인할 때 사용하는 최소 센서다. 이 검사는
인증 전 화면, 모바일 폭, 200% 배율에서 빈 화면·JS 로딩 실패·기본 레이아웃
회귀를 잡는다. API 인증 이후의 운영 동선은 실제 gateway와 별도 smoke에서
검증한다.

```sh
bun install --cwd apps/admin --frozen-lockfile --ignore-scripts --no-progress
bun run --cwd apps/admin check
bun run --cwd apps/admin build
root="$(mktemp -d)"
mkdir -p "$root/admin"
cp -a apps/admin/build/. "$root/admin/"
busybox httpd -f -p 4173 -h "$root" &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT

chrome --headless=new --no-sandbox --disable-gpu --virtual-time-budget=5000 \
  --window-size=375,800 --screenshot=/tmp/nvidia-build-lb-admin-375.png \
  http://127.0.0.1:4173/admin/
chrome --headless=new --no-sandbox --disable-gpu --virtual-time-budget=5000 \
  --force-device-scale-factor=2 --window-size=750,1600 \
  --screenshot=/tmp/nvidia-build-lb-admin-200.png \
  http://127.0.0.1:4173/admin/
```

성공 증거는 두 PNG가 생성되고, `--dump-dom` 결과에 `관리자 로그인`,
`admin-token`, `인증하고 확인`이 포함되며 Chrome 콘솔에 페이지 JS 오류가 없는
것이다. 테스트 후 gateway·tunnel을 이 정적 서버로 대체하지 않는다.

