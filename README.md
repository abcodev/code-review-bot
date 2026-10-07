# 코드리뷰 봇

DM 에서 `/코드리뷰 <PR 링크>` 를 치면 **DM 상대방 Mac 에서** 그 사람의 리뷰 기준으로 리뷰하고,
PR 에 댓글을 남긴 뒤 요청 메시지 스레드에 완료 답글을 다는 로컬 데몬입니다.

- 요청 메시지에 👀 → 리뷰 → PR 인라인 댓글 + 전체 댓글(지적이 없어도 남김) → 스레드 답글
- 내가 친 요청은 내 Mac 에서 돌지 않습니다. 상대방 Mac 이 처리합니다.
- 리뷰는 내 Claude Code 로그인(구독 사용량)으로, 댓글·답글은 내 이름으로 달립니다.
- Mac 이 꺼져 있던 동안 온 요청은 켜지면 처리합니다.

## 설치

1. 아래 「토큰」 세 가지를 키체인에 넣습니다.
2. 받아서 설치합니다. 업데이트도 `git pull && ./install.sh` 로 같습니다.
   ```bash
   git clone https://github.com/abcodev/code-review-bot.git && cd code-review-bot && ./install.sh
   ```
   사전 점검(Claude 로그인, Bitbucket SSH, 토큰 3개)을 통과해야 데몬이 등록됩니다.
3. `~/.claude/slack-review/config.json` 을 확인합니다.
   - `partners`: 내 Mac 에서 리뷰를 돌릴 수 있는 요청자의 Slack 사용자 ID
     (Slack 에서 상대 프로필 → ⋮ → 「멤버 ID 복사」)
   - `review_command`: 내가 쓰는 리뷰 명령 파일 경로(예: `~/.claude/commands/review-deep.md`).
     비워 두면 저장소 CLAUDE.md 와 내 리뷰 에이전트·스킬로 깊게 리뷰합니다.
     사람에게 질문하는 방식의 명령이면 무인 실행이라 결과가 얕아질 수 있습니다.
   - 바꾼 뒤에는 재시작: `launchctl kickstart -k gui/$(id -u)/com.paynstore.slack-review`

## 토큰

토큰 값은 어디에도 붙여 넣지 말고, 아래처럼 클립보드에서 바로 키체인에 넣습니다.
명령을 먼저 터미널에 붙여 둔 뒤 토큰을 Copy 하고 Enter 를 누르세요(명령을 복사하면 클립보드가 바뀝니다).

| 키체인 항목 | 값 | 받는 곳 |
|---|---|---|
| `bitbucket-api-token` (계정=Atlassian 이메일) | 본인 Bitbucket API 토큰 | Atlassian 계정 설정 → API 토큰. 범위: read:repository·pullrequest·user, write:pullrequest |
| `slack-review-app-token` | `xapp-…` 팀 공용 | 앱 관리자에게 안전한 경로로 받기 |
| `slack-review-user-token` | `xoxp-…` 본인 | 앱 협업자로 추가된 뒤 https://api.slack.com/apps/A0C7DT4A8F6/install-on-team 에서 설치 후 User OAuth Token |

```bash
security add-generic-password -s bitbucket-api-token -a <Atlassian 이메일> -w "$(pbpaste)"
security add-generic-password -s slack-review-app-token -a slack -w "$(pbpaste)"
security add-generic-password -s slack-review-user-token -a slack -w "$(pbpaste)"
```

## 운영

- 로그: `tail -f ~/.claude/slack-review/logs/relay.log`
- 끄기: `launchctl bootout gui/$(id -u)/com.paynstore.slack-review`
- 봇 전용 저장소 클론은 `~/.claude/slack-review/repos/` 에 생깁니다(launchd 는 ~/Desktop 을 못 읽습니다).
- 머지·승인·커밋·푸시는 하지 않습니다.
