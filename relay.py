#!/usr/bin/env python3
"""`/코드리뷰 <PR 링크>` 를 DM 상대방 Mac 에서 리뷰하는 데몬 — 각자 Mac 에 하나씩 설치한다.

1. 누가 `/코드리뷰` 를 치든, 켜져 있는 아무 데몬이 받아 DM 에 「요청자 → 코드리뷰 요청」 메시지만 남긴다.
   (같은 앱의 Socket Mode 연결이 여럿이면 Slack 이 아무 연결로나 보낸다 — 받는 쪽을 고를 수 없다.)
2. 각 데몬은 config 의 partners 와의 DM 을 주기적으로 읽어, **상대방이 남긴 요청만** 자기 리뷰 기준으로 처리한다.
   내가 남긴 요청은 무시한다 — 그건 상대 Mac 이 한다.
3. 요청 메시지에 👀, 끝나면 그 스레드 안에 답글. PR 댓글·슬랙 답글은 이 Mac 사용자 이름으로 달린다.

토큰은 키체인 `slack-review-app-token`(xapp-, 팀 공용) · `slack-review-user-token`(xoxp-, 본인) 에서 읽는다.

사용:
  relay.py run              # 상주 (launchd)
  relay.py dry <PR 링크>    # 재리뷰 판별·프롬프트만 출력. 실행·게시하지 않음
"""
import importlib.util
import json
import logging
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
BASE = HOME / ".claude/slack-review"
REPO_ROOT = BASE / "repos"   # launchd 는 TCC 때문에 ~/Desktop 을 못 읽는다 — 봇 전용 클론을 둔다
WT_ROOT = BASE / "wt"
STATE = BASE / "state.json"
CONFIG = BASE / "config.json"
SCRATCH = Path("/private/tmp/slack-review")
CLAUDE = Path(shutil.which("claude") or HOME / ".local/bin/claude")
CONFIG_DATA = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
BB = Path(CONFIG_DATA.get("bb", BASE / "bb.py")).expanduser()
POSTING = Path(CONFIG_DATA.get("posting_rules", BASE / "posting.md")).expanduser()

APP_BOT_ID = "B0C7A354S2W"   # 공용 「코드리뷰」 앱이 남긴 메시지 표식
POLL_SECONDS = 20

PR_LINK = re.compile(r"https://bitbucket\.org/pay-n/([\w.-]+)/pull-requests/(\d+)")
REQUESTER = re.compile(r"^<@(U\w+)> 님 코드리뷰 요청")
NOT_CODE = re.compile(r"/test/|\.md$|^docs/")

LIMIT = {"timeout": 90 * 60, "budget": "25"}
TOOLS = ["Read", "Grep", "Glob", "Skill", "Agent", "Bash(git *)", "Bash(python3 *)", f"Bash({BB} *)",
         "Bash(./gradlew *)", "Bash(./mvnw *)", "Bash(export *)", "Bash(/usr/libexec/java_home *)",
         "Bash(docker info *)", "Bash(colima status *)",
         "Write(//private/tmp/slack-review/**)", "Edit(//private/tmp/slack-review/**)"]

log = logging.getLogger("slack-review")


def load_bb():
    spec = importlib.util.spec_from_file_location("bb", BB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bb = load_bb()


def load_config():
    cfg = json.loads(CONFIG.read_text())
    cfg["partners"] = set(cfg["partners"])
    cfg["repos"] = set(cfg["repos"])
    return cfg


def keychain(service):
    return subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                          capture_output=True, text=True, check=True).stdout.strip()


def notify(text):
    subprocess.run(["osascript", "-e", f'display notification {json.dumps(text)} with title "PR 리뷰 봇"'],
                   capture_output=True)


def git(repo_dir, *args, check=True):
    out = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, text=True)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {args[0]} 실패: {out.stderr.strip()[:300]}")
    return out


class State:
    """리뷰한 커밋(재리뷰 기준), 처리한 요청, DM 별 읽은 위치를 기억한다."""

    def __init__(self):
        data = json.loads(STATE.read_text()) if STATE.exists() else {}
        self.reviewed = data.get("reviewed", {})
        self.handled = data.get("handled", [])
        self.cursor = data.get("cursor", {})
        self.lock = threading.Lock()

    def save(self):
        with self.lock:
            STATE.write_text(json.dumps({"reviewed": self.reviewed, "handled": self.handled[-500:],
                                         "cursor": self.cursor}, indent=1))

    def reviewed_commit(self, key):
        return self.reviewed.get(key)

    def record(self, key, commit):
        self.reviewed[key] = commit
        self.save()

    def mark_handled(self, ts):
        self.handled.append(ts)
        self.save()


# ── 명령: 요청 메시지만 남긴다 ─────────────────────────────

def parse_command(payload):
    """(PR 목록, 거절 사유). 거절 사유는 명령 친 사람에게만 보인다."""
    if not payload.get("channel_id", "").startswith("D"):
        return [], "리뷰어와의 1:1 DM 에서 요청해 주세요."
    links = list(dict.fromkeys((r, int(i)) for r, i in PR_LINK.findall(payload.get("text", ""))))
    if not links:
        return [], "사용법: `/코드리뷰 https://bitbucket.org/pay-n/<저장소>/pull-requests/<번호>`"
    return links, None


def pr_url(repo, pr_id):
    return f"https://bitbucket.org/pay-n/{repo}/pull-requests/{pr_id}"


def request_text(user, links):
    return f"<@{user}> 님 코드리뷰 요청 — " + " ".join(f"<{pr_url(r, i)}|{r}#{i}>" for r, i in links)


# ── 우편함: 상대방 요청 찾기 ───────────────────────────────

def partner_dms(slack, partners):
    dms, cursor = {}, None
    while True:
        page = slack.conversations_list(types="im", limit=200, cursor=cursor)
        dms.update({c["user"]: c["id"] for c in page["channels"] if c.get("user") in partners})
        cursor = page.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return dms


def new_requests(slack, state, me, partners, channel):
    """이 DM 에 새로 올라온, 상대방이 남긴 요청. 처음 보는 DM 은 지금부터 읽는다 — 설치 전 요청은 건드리지 않는다."""
    oldest = state.cursor.setdefault(channel, f"{time.time():.6f}")
    messages = slack.conversations_history(channel=channel, oldest=oldest, limit=100)["messages"]
    found = []
    for m in sorted(messages, key=lambda m: float(m["ts"])):
        state.cursor[channel] = max(state.cursor[channel], m["ts"], key=float)
        requester = REQUESTER.match(m.get("text", ""))
        if m.get("bot_id") != APP_BOT_ID or not requester:
            continue
        user = requester.group(1)
        mine = any(r["name"] == "eyes" and me in r.get("users", []) for r in m.get("reactions", []))
        if user == me or user not in partners or mine or m["ts"] in state.handled:
            continue
        links = list(dict.fromkeys((r, int(i)) for r, i in PR_LINK.findall(m["text"])))
        found.append((channel, m["ts"], links))
    state.save()
    return found


def react(slack, channel, ts, name):
    try:
        slack.reactions_add(channel=channel, timestamp=ts, name=name)
    except Exception:  # noqa: BLE001 — 이미 달려 있으면 무시한다
        pass


# ── 재리뷰 판별 ─────────────────────────────────────────────

def my_review_comments(repo, pr_id):
    me = bb.request("GET", "/user")["account_id"]
    return [c for c in bb.paged(bb.pr_path(repo, pr_id) + "/comments?pagelen=100")
            if c["user"].get("account_id") == me and not c.get("deleted")]


def review_base(state, key, repo, pr_id, mine):
    """지난 리뷰 기준 커밋. 봇 기록이 없으면 마지막 내 댓글 직전 PR 커밋으로 추정한다."""
    if state.reviewed_commit(key):
        return state.reviewed_commit(key)
    if not mine:
        return None
    last = max(c["created_on"] for c in mine)
    commits = list(bb.paged(bb.pr_path(repo, pr_id) + "/commits?pagelen=100"))
    before = [c for c in commits if c["date"] <= last]
    return before[0]["hash"] if before else None


def same_commit(a, b):
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


# ── 워크트리 ────────────────────────────────────────────────

def ensure_clone(repo):
    repo_dir = REPO_ROOT / repo
    if not repo_dir.exists():
        REPO_ROOT.mkdir(exist_ok=True)
        git(REPO_ROOT, "clone", "-q", f"git@bitbucket.org:pay-n/{repo}.git", repo)
    return repo_dir


def prepare_worktree(repo, pr):
    src, dst = pr["source"], pr["destination"]
    repo_dir = ensure_clone(repo)
    wt = WT_ROOT / f"{repo}-pr{pr['id']}"
    WT_ROOT.mkdir(exist_ok=True)
    if repo == "payn_backend":  # composite build 가 ../payn_domain 을 본다
        domain = ensure_clone("payn_domain")
        git(domain, "fetch", "-q", "origin", "develop")
        git(domain, "checkout", "-q", "--detach", "origin/develop")
        if not (WT_ROOT / "payn_domain").exists():
            (WT_ROOT / "payn_domain").symlink_to(domain)
    git(repo_dir, "fetch", "-q", "origin", src["branch"]["name"], dst["branch"]["name"])
    if wt.exists():
        git(repo_dir, "worktree", "remove", "--force", str(wt))
    git(repo_dir, "worktree", "add", "-q", "--detach", str(wt), src["commit"]["hash"])
    return wt


def numstat(wt, *rev):
    rows = []
    for line in git(wt, "diff", "--numstat", *rev).stdout.splitlines():
        added, removed, path = line.split("\t", 2)
        rows.append((path, int(added) if added != "-" else 0, int(removed) if removed != "-" else 0))
    return rows


def scope(wt, dst_branch, base):
    """리뷰할 변경분. 재리뷰면 기준 이후 델타 중 PR 자체 파일만 — 타깃 동기화로 들어온 변경은 뺀다."""
    pr_diff = numstat(wt, f"origin/{dst_branch}...HEAD")
    if not base:
        return "first", pr_diff
    if git(wt, "cat-file", "-e", f"{base}^{{commit}}", check=False).returncode != 0:
        return "base_lost", pr_diff
    pr_files = {p for p, _, _ in pr_diff}
    return "rereview", [row for row in numstat(wt, f"{base}..HEAD") if row[0] in pr_files]


def size(rows):
    lines = sum(a + r for p, a, r in rows if not NOT_CODE.search(p))
    return f"코드 {lines}줄 · 파일 {len(rows)}개"


# ── claude 실행 ─────────────────────────────────────────────

MODE_TEXT = {
    "first": "처음 리뷰다. 리뷰 범위는 `git diff origin/{dst}...HEAD` 다.",
    "rereview": ("재리뷰다. 기준 커밋은 `{base}` 이다. 리뷰 범위는 `git diff {base}..HEAD` 중 "
                 "PR 전체 diff(`origin/{dst}...HEAD`)에 있는 파일만이다 — 나머지는 타깃 동기화로 들어온 변경이다."),
    "base_lost": ("재리뷰다. 기준 커밋 `{base}` 이 사라졌다(강제 푸시 추정). 리뷰 범위는 "
                  "`git diff origin/{dst}...HEAD` 전체지만, 이전 지적과 겹치는 지적은 새로 달지 않는다."),
}


def review_rule(cfg):
    command = Path(cfg.get("review_command", "")).expanduser()
    if cfg.get("review_command") and command.exists():
        return f"`{command}` 를 읽고 그 기준·절차대로 깊게 리뷰한다. 대상 diff 는 위 「대상」의 범위로 대체한다."
    return ("저장소의 CLAUDE.md 와 네가 가진 리뷰 에이전트·스킬을 써서 깊게 리뷰한다. "
            "정확성·보안·트랜잭션·설계를 보고, 근거는 파일:라인으로 댄다.")


def build_prompt(cfg, repo, pr, mode, base, why):
    dst = pr["destination"]["branch"]["name"]
    return (BASE / "prompt.md").read_text().format(
        url=pr["links"]["html"]["href"], repo=repo, pr_id=pr["id"], title=pr["title"],
        src_branch=pr["source"]["branch"]["name"], src_commit=pr["source"]["commit"]["hash"],
        dst_branch=dst, why=why, bb=BB, posting=POSTING, scratch=SCRATCH, review_rule=review_rule(cfg),
        mode=MODE_TEXT[mode].format(base=base, dst=dst))


def run_claude(prompt, wt):
    cmd = [str(CLAUDE), "-p", "--output-format", "json",
           "--json-schema", (BASE / "schema.json").read_text(),
           "--permission-mode", "dontAsk", "--permission-prompts", "none",
           "--max-budget-usd", LIMIT["budget"],
           "--add-dir", str(HOME / ".claude"), "--add-dir", str(SCRATCH),
           "--allowedTools", *TOOLS]
    SCRATCH.mkdir(exist_ok=True)
    out = subprocess.run(cmd, input=prompt, cwd=wt, capture_output=True, text=True, timeout=LIMIT["timeout"])
    (BASE / "logs" / f"{wt.name}-{int(time.time())}.json").write_text(out.stdout or out.stderr)
    result = json.loads(out.stdout)
    if result.get("is_error") or "structured_output" not in result:
        raise RuntimeError(f"claude 실패: {result.get('subtype')} {str(result.get('result'))[:200]}")
    return result["structured_output"]


# ── 답글 ───────────────────────────────────────────────────

def tally(counts):
    marks = (("🔴", "red"), ("🟡", "yellow"), ("🟢", "green"), ("❓", "question"))
    return " · ".join(f"{m} {counts[k]}" for m, k in marks if counts.get(k))


def reply_text(link, mode, res):
    if not res.get("posted"):
        return f"{link} 리뷰를 게시하지 않았습니다 — {res.get('skipped_reason', '사유 미기재')}"
    new = tally(res["counts"])
    if mode == "first":
        if not new:
            return f"{link} 리뷰 완료했습니다 — 지적 없음, PR 에 리뷰 댓글 남겼습니다.\n{res['headline']}"
        return f"{link} 리뷰 완료했습니다 — PR 에 댓글 남겼습니다 ({new})\n{res['headline']}"
    p = res.get("prior") or {}
    parts = [f"이전 지적 {p.get('total', 0)}건 중 {p.get('resolved', 0)}건 반영"]
    if p.get("unresolved"):
        parts.append(f"미반영 {p['unresolved']}건")
    parts.append(f"새 지적 {new}" if new else "새 지적 없음")
    return f"{link} 재리뷰 완료했습니다 — {' · '.join(parts)}\n{res['headline']}"


# ── 작업 ───────────────────────────────────────────────────

def review(cfg, slack, state, repo, pr_id, channel=None, root=None, dry=False):
    key = f"{repo}#{pr_id}"
    pr = bb.request("GET", bb.pr_path(repo, pr_id))
    link = f"<{pr['links']['html']['href']}|{key}>"
    head = pr["source"]["commit"]["hash"]

    def done(text):
        if dry:
            print(text)
        else:
            slack.chat_postMessage(channel=channel, thread_ts=root, text=text)

    if repo not in cfg["repos"]:
        return done(f"{link} 이 저장소는 제 자동 리뷰 대상이 아닙니다.")
    if pr["state"] != "OPEN":
        return done(f"{link} PR 이 {pr['state']} 상태라 리뷰하지 않았습니다.")
    base = review_base(state, key, repo, pr_id, my_review_comments(repo, pr_id))
    if same_commit(base, head):
        return done(f"{link} 지난 리뷰 이후 새 커밋이 없습니다.")

    wt = prepare_worktree(repo, pr)
    try:
        mode, rows = scope(wt, pr["destination"]["branch"]["name"], base)
        prompt = build_prompt(cfg, repo, pr, mode, base, size(rows))
        if dry:
            print(f"[{pr['state']}] mode={mode} base={base} ({size(rows)})\n\n{prompt}")
            return None
        log.info("리뷰 시작 %s %s (%s)", key, mode, size(rows))
        res = run_claude(prompt, wt)
        if res.get("posted"):
            state.record(key, head)
        done(reply_text(link, mode, res))
    finally:
        git(REPO_ROOT / repo, "worktree", "remove", "--force", str(wt), check=False)


def handle_request(cfg, slack, state, channel, ts, links):
    react(slack, channel, ts, "eyes")
    for repo, pr_id in links:
        try:
            review(cfg, slack, state, repo, pr_id, channel=channel, root=ts)
        except Exception as e:  # noqa: BLE001 — 한 PR 실패가 다른 PR 을 막지 않게 한다
            log.exception("리뷰 실패 %s#%s", repo, pr_id)
            notify(f"{repo}#{pr_id} 자동 리뷰 실패 — {e}"[:200])
            slack.chat_postMessage(channel=channel, thread_ts=ts,
                                   text=f"<{pr_url(repo, pr_id)}|{repo}#{pr_id}> 자동 리뷰가 실패했습니다. 확인 후 다시 진행하겠습니다.")
    state.mark_handled(ts)


def serve():
    from slack_sdk import WebClient
    from slack_sdk.webhook import WebhookClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    cfg = load_config()
    slack = WebClient(token=keychain("slack-review-user-token"))
    me = slack.auth_test()["user_id"]
    state, jobs, queued = State(), queue.Queue(), set()

    def worker():
        while True:
            channel, ts, links = jobs.get()
            try:
                handle_request(cfg, slack, state, channel, ts, links)
            except Exception as e:  # noqa: BLE001
                log.exception("요청 처리 실패 %s", ts)
                notify(f"자동 리뷰 요청 처리 실패 — {e}"[:200])

    def poller():
        dms, refreshed = {}, 0.0
        while True:
            try:
                if time.time() - refreshed > 600:
                    dms, refreshed = partner_dms(slack, cfg["partners"]), time.time()
                for channel in dms.values():
                    for channel_, ts, links in new_requests(slack, state, me, cfg["partners"], channel):
                        if ts not in queued:
                            log.info("요청 발견 %s %s", ts, links)
                            queued.add(ts)
                            jobs.put((channel_, ts, links))
            except Exception:  # noqa: BLE001 — 일시적인 Slack 오류로 우편함 확인을 멈추지 않는다
                log.exception("DM 확인 실패")
            time.sleep(POLL_SECONDS)

    def on_request(client, req):
        if req.type != "slash_commands":
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            return
        links, refusal = parse_command(req.payload)
        if refusal:
            client.send_socket_mode_response(SocketModeResponse(
                envelope_id=req.envelope_id, payload={"response_type": "ephemeral", "text": refusal}))
            return
        # 즉시 응답을 in_channel 로 보내면 「/코드리뷰 링크」 명령 줄까지 DM 에 남는다 — 빈 응답 후 따로 보낸다
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        WebhookClient(req.payload["response_url"]).send(
            response_type="in_channel", text=request_text(req.payload["user_id"], links))
        log.info("명령 접수 %s by %s", links, req.payload.get("user_id"))

    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    socket = SocketModeClient(app_token=keychain("slack-review-app-token"), web_client=slack)
    socket.socket_mode_request_listeners.append(on_request)
    socket.connect()
    log.info("연결됨 me=%s partners=%s", me, sorted(cfg["partners"]))
    threading.Event().wait()


def main():
    (BASE / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        serve()
    elif cmd == "dry" and len(sys.argv) > 2 and PR_LINK.search(sys.argv[2]):
        repo, pr_id = PR_LINK.search(sys.argv[2]).groups()
        review(load_config(), None, State(), repo, int(pr_id), dry=True)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
