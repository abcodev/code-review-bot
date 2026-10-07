#!/bin/bash
# 코드리뷰 봇 설치 — 사전 점검 후 ~/.claude/slack-review 에 깔고, 준비가 다 됐을 때만 launchd 에 등록한다.
set -uo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude/slack-review"
LABEL="com.paynstore.slack-review"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
FAIL=0

ok() { echo "  ✅ $1"; }
ng() { echo "  ❌ $1"; FAIL=1; }
has_key() { security find-generic-password -s "$1" >/dev/null 2>&1; }

echo "1) 사전 점검"
if command -v claude >/dev/null && claude auth status 2>/dev/null | grep -q '"loggedIn": true'; then
  ok "Claude Code 로그인"
else
  ng "Claude Code 로그인 필요 — 터미널에서 claude 실행 후 /login"
fi
if ssh -o BatchMode=yes -o ConnectTimeout=10 -T git@bitbucket.org 2>&1 | grep -qi "authenticated"; then
  ok "Bitbucket SSH 접근"
else
  ng "Bitbucket SSH 키 필요 — git@bitbucket.org 로 clone 이 되는지 확인"
fi
has_key bitbucket-api-token    && ok "Bitbucket API 토큰 (키체인 bitbucket-api-token)" || ng "Bitbucket API 토큰 없음 — README 「토큰」 참고"
has_key slack-review-app-token && ok "Slack 앱 토큰 (키체인 slack-review-app-token)"  || ng "Slack 앱 토큰 없음 — README 「토큰」 참고"
has_key slack-review-user-token && ok "Slack 사용자 토큰 (키체인 slack-review-user-token)" || ng "Slack 사용자 토큰 없음 — README 「토큰」 참고"

echo "2) 파일 설치 → $DEST"
mkdir -p "$DEST/logs"
cp "$SRC"/{relay.py,prompt.md,schema.json,bb.py,posting.md} "$DEST/"
chmod +x "$DEST/bb.py"
if [ ! -f "$DEST/config.json" ]; then
  cp "$SRC/config.example.json" "$DEST/config.json"
  echo "  config.json 을 새로 만들었습니다 — partners·review_command 를 확인해 주세요"
fi
[ -d "$DEST/.venv" ] || python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q --disable-pip-version-check slack_sdk && ok "slack_sdk 설치"

CLAUDE_DIR="$(dirname "$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")")"
cat > "$DEST/$LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$DEST/.venv/bin/python</string><string>$DEST/relay.py</string><string>run</string></array>
  <key>WorkingDirectory</key><string>$DEST</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$CLAUDE_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>$HOME</string>
    <key>LANG</key><string>ko_KR.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DEST/logs/relay.log</string>
  <key>StandardErrorPath</key><string>$DEST/logs/relay.log</string>
</dict>
</plist>
EOF

if grep -q '"<' "$DEST/config.json"; then
  ng "config.json 의 partners 가 비어 있습니다 — $DEST/config.json 에 요청자 Slack 멤버 ID 를 넣어 주세요"
fi

if [ "$FAIL" -eq 0 ]; then
  # 키체인에 「있는지」가 아니라 실제로 로그인되는지 본다 — 토큰 종류가 틀리면 여기서 걸린다
  if WHO=$(cd "$DEST" && "$DEST/.venv/bin/python" - 2>&1 <<'PY'
import relay
from slack_sdk import WebClient
for service, prefix in (("slack-review-user-token", "xoxp-"), ("slack-review-app-token", "xapp-")):
    if not relay.keychain(service).startswith(prefix):
        raise SystemExit(f"{service} 자리에 {prefix} 가 아닌 토큰이 들어 있습니다 — 사용자 토큰은 xoxp-, 앱 토큰은 xapp- 입니다")
bb_user = relay.bb.request("GET", "/user")["display_name"]
slack_user = WebClient(token=relay.keychain("slack-review-user-token")).auth_test()["user"]
print(f"Bitbucket={bb_user} · Slack={slack_user}")
PY
  ); then
    ok "로그인 확인 — $WHO"
  else
    ng "토큰으로 로그인 실패 — $(echo "$WHO" | tail -1)"
  fi
fi

echo "3) 실행"
if [ "$FAIL" -ne 0 ]; then
  echo "  ⏸  점검 실패 항목이 있어 데몬을 등록하지 않았습니다. 해결 후 install.sh 를 다시 실행해 주세요."
  exit 1
fi
cp "$DEST/$LABEL.plist" "$PLIST"
BEFORE=$(wc -l < "$DEST/logs/relay.log" 2>/dev/null || echo 0)
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 8
NEW_LOG=$(tail -n +$((BEFORE + 1)) "$DEST/logs/relay.log" 2>/dev/null)
if echo "$NEW_LOG" | grep -q "연결됨"; then
  ok "데몬 실행 중 — $(echo "$NEW_LOG" | grep "연결됨" | tail -1)"
else
  ng "연결 로그가 없습니다 — tail -30 $DEST/logs/relay.log 확인"
fi
