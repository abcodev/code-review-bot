# 코드리뷰 봇

Slack DM 에서 `/코드리뷰 <PR 링크>` 로 리뷰를 부탁하면, **부탁받은 사람의 Mac** 에서 그 사람의 Claude Code 가
PR 을 리뷰하고 Bitbucket 에 댓글을 남긴 뒤 Slack 스레드에 결과를 알려 줍니다.

## 이렇게 쓰게 됩니다

**리뷰를 부탁할 때**
1. 리뷰어와의 DM **본문**에서 `/코드리뷰 https://bitbucket.org/pay-n/<저장소>/pull-requests/<번호>` 를 칩니다.
   (스레드 안에서는 Slack 이 슬래시 명령을 막아 둬서 안 됩니다.)
2. DM 에 「@나 님 코드리뷰 요청 — 링크」 메시지가 남고, 리뷰어 Mac 이 시작하면 그 메시지에 👀 가 달립니다.
3. 끝나면 그 메시지 **스레드 안에** 「리뷰 완료했습니다 — 🔴 1 · 🟡 2」 같은 답글이 달립니다.
4. 수정한 뒤 다시 보고 싶으면 그 스레드에 「재리뷰 부탁드려요」·「3차 리뷰 부탁드려요」처럼 답글을 남깁니다.
   지난 리뷰 이후 바뀐 부분만 보고 같은 스레드에 「3차 리뷰 완료」로 답합니다(최근 7일 스레드).

**리뷰를 부탁받았을 때** — 할 일이 없습니다. 내 Mac 이 켜져 있으면 알아서 돌고, 꺼져 있던 동안 온 요청은 켜지면 처리합니다.

- 리뷰는 **내 Claude Code 로그인**(내 구독 사용량)과 **내가 쓰는 리뷰 명령**으로 돕니다.
- PR 댓글·Slack 답글은 **내 이름**으로 달립니다. 지적이 없어도 PR 에 리뷰 범위와 「지적 없음」을 남깁니다.
- 내가 친 `/코드리뷰` 는 내 Mac 에서 돌지 않습니다. 상대 Mac 이 처리합니다.
- 머지·승인·커밋·푸시는 하지 않습니다.

## 설치 — Claude Code 에게 맡기기

터미널에서 아래를 실행합니다.

```bash
git clone https://github.com/abcodev/code-review-bot.git && cd code-review-bot && claude "README 의 「AI 설치 절차」대로 코드리뷰 봇을 설치해줘"
```

Claude 가 점검·설정·설치를 진행하고, 아래 세 가지만 직접 부탁합니다.

| 부탁받는 것 | 이유 |
|---|---|
| 토큰을 Copy 한 뒤, Claude 가 보여 준 키체인 명령을 **직접** 실행하기 | 토큰 값은 Claude 가 보거나 다루지 않습니다 |
| Slack 앱 설치 화면에서 「허용」 누르기 | 권한 허용은 본인이 해야 합니다 |
| 내 Mac 에서 리뷰를 돌려도 되는 **요청자 Slack 멤버 ID** 알려 주기 | 상대 프로필 → ⋮ → 「멤버 ID 복사」 |

미리 필요한 것: Claude Code 로그인, Bitbucket SSH 키(git@bitbucket.org clone 가능), 「코드리뷰」 Slack 앱 협업자 초대 수락,
앱 관리자에게서 받은 팀 공용 앱 토큰(`xapp-`).

## 운영

- 업데이트: **자동**입니다. 데몬이 30분마다 이 저장소의 새 커밋을 받아, 진행 중인 리뷰가 없을 때 스스로 재시작합니다.
  설정(`config.json`)은 그대로 둡니다. 바로 받고 싶으면 저장소 폴더에서 `git pull && ./install.sh`.
- 저장소 폴더를 옮기거나 지우면 자동 업데이트가 멈춥니다. 옮긴 곳에서 `./install.sh` 를 다시 실행하면 됩니다.
- 로그: `tail -f ~/.claude/slack-review/logs/relay.log`
- 끄기: `launchctl bootout gui/$(id -u)/com.paynstore.slack-review`
- 설정: `~/.claude/slack-review/config.json` (바꾼 뒤 `launchctl kickstart -k gui/$(id -u)/com.paynstore.slack-review`)

---

## AI 설치 절차

> 이 절은 설치를 맡은 Claude Code 가 따른다. 사람에게는 위 「설치」 절의 세 가지만 부탁한다.

**지킬 것**
- 토큰 값을 채팅으로 받지 않고, 출력하지 않는다. `security find-generic-password … -w` 처럼 값을 읽는 명령을 쓰지 않는다.
  키체인에 넣는 명령은 사람에게 보여 주고 **사람이 직접** 실행하게 한다(Claude Code 입력창에서 `!` 로 시작하면 바로 실행된다).
- launchd 등록은 `./install.sh` 로만 한다. 이미 있는 `~/.claude/slack-review/config.json` 은 묻지 않고 덮어쓰지 않는다.
- 봇이 쓰는 저장소 클론은 `~/.claude/slack-review/repos/` 다. launchd 는 `~/Desktop` 을 읽지 못하니 그 아래 경로를 설정에 넣지 않는다.

**1. 사전 점검** — 결과를 표로 보여 준다.
```bash
claude auth status | grep -q '"loggedIn": true' && echo "Claude 로그인 OK"
ssh -o BatchMode=yes -o ConnectTimeout=10 -T git@bitbucket.org 2>&1 | grep -qi authenticated && echo "Bitbucket SSH OK"
for s in bitbucket-api-token slack-review-app-token slack-review-user-token; do
  security find-generic-password -s "$s" >/dev/null 2>&1 && echo "$s 있음" || echo "$s 없음"; done
```

**2. 빠진 토큰 안내** — 없는 항목만. 명령은 「먼저 입력창에 붙여 두고 → 토큰 Copy → Enter」 순서로 안내한다(명령을 복사하면 클립보드가 바뀐다).

| 항목 | 받는 곳 | 사람이 실행할 명령 |
|---|---|---|
| `bitbucket-api-token` | 이미 쓰는 **Atlassian API 토큰**이 있으면 그것. 없으면 https://id.atlassian.com/manage-profile/security/api-tokens 에서 Bitbucket 권한 read:repository·read:pullrequest·read:user·write:pullrequest 로 발급. 앱 비밀번호(app password)는 인증 방식이 달라 쓸 수 없다 | `security add-generic-password -s bitbucket-api-token -a <Atlassian 로그인 이메일> -w "$(pbpaste)"` |
| `slack-review-app-token` | 앱 관리자에게 받은 `xapp-` 토큰 | `security add-generic-password -s slack-review-app-token -a slack -w "$(pbpaste)"` |
| `slack-review-user-token` | https://api.slack.com/apps/A0C7DT4A8F6/install-on-team 에서 설치·허용 → https://api.slack.com/apps/A0C7DT4A8F6/oauth 의 「User OAuth Token」(`xoxp-`) | `security add-generic-password -s slack-review-user-token -a slack -w "$(pbpaste)"` |

User OAuth Token 이 보이지 않으면 멈추고 앱 관리자에게 문의하라고 안내한다.

**3. 설정 파일** — `~/.claude/slack-review/config.json` 이 없을 때만 만든다.
- `partners`: 사람에게 요청자 Slack 멤버 ID(`U` 로 시작)를 묻는다. 여러 명 가능.
- `review_command`: `~/.claude/commands/` 와 `~/.claude/skills/` 에서 이름에 review 가 들어간 파일을 찾아 후보로 보여 주고 고르게 한다.
  없거나 고르지 않으면 `""` — 저장소 CLAUDE.md 와 가진 에이전트로 깊게 리뷰한다. 사람에게 질문하는 방식의 명령은 무인 실행에 맞지 않는다고 알려 준다.
```json
{
  "partners": ["U..."],
  "repos": ["pos_backend", "payn_backend", "payn_domain", "payin_backend"],
  "review_command": ""
}
```

**4. 설치** — 저장소 폴더에서 `./install.sh`. 모든 항목이 ✅ 이고 「로그인 확인 — Bitbucket=… · Slack=…」과 「데몬 실행 중」이 나오면 끝이다.
❌ 가 있으면 메시지대로 고친 뒤 다시 실행한다. 「토큰으로 로그인 실패」는 대개 토큰 종류(앱 비밀번호)나 키체인 계정 칸(Atlassian 이메일)이 틀린 경우다.

**5. 마무리 안내** — 사람에게 「설치 완료, 상대가 DM 에서 `/코드리뷰 PR링크` 를 치면 이 Mac 에서 돕니다」와 로그 확인 명령을 알려 준다.
