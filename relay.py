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
  relay.py requeue [ts…]    # 데몬을 멈춘 상태에서 — 최근 24시간 상대방 요청 중 끝나지 않은 것(과 지정한 ts)을 처리 목록에 다시 넣는다
"""
import importlib.util
import json
import os
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
UPDATE_SECONDS = 30 * 60
SOURCE_FILE = BASE / "source"   # install.sh 가 남기는 원본 저장소 경로 — 없으면 자동 업데이트를 하지 않는다
CODE_FILES = ("relay.py", "prompt.md", "schema.json", "bb.py", "posting.md")
THREAD_POLL_EVERY = 3     # 스레드 재리뷰 확인은 1분마다
THREAD_DAYS = 7

PR_LINK = re.compile(r"https://bitbucket\.org/pay-n/([\w.-]+)/pull-requests/(\d+)")
REQUESTER = re.compile(r"^<@(U\w+)> 님 코드리뷰 요청")
NOT_CODE = re.compile(r"/test/|\.md$|^docs/")
RE_REVIEW = re.compile(r"리뷰|\d+\s*차")  # 요청 스레드 안 상대방 답글에 「리뷰」가 있으면 다시 돈다(10-07)
ROUND = re.compile(r"(\d+)\s*차")

LIMIT = {"timeout": 90 * 60, "budget": "25"}
LIMIT_RETRY_SECONDS = 10 * 60
USAGE_LIMIT = re.compile(r"hit your (session|weekly|monthly spend) limit", re.I)
# 셸은 전부 허용하고 위험한 것만 막는다 — 형태별 허용 목록은 명령 조합이 끝이 없어 거부가 계속 났다(10-07).
# 금지 목록(DENY)이 허용보다 우선한다. 파일 쓰기는 작업 폴더와 워크트리 안으로만.
TOOLS = ["Read", "Grep", "Glob", "Skill", "Agent", "Bash", "TodoWrite", "ToolSearch",
         "Write(//private/tmp/slack-review/**)", "Edit(//private/tmp/slack-review/**)",
         f"Write(/{WT_ROOT}/**)", f"Edit(/{WT_ROOT}/**)"]
DENY = ["Bash(git commit *)", "Bash(git push *)", "Bash(git merge *)", "Bash(git rebase *)",
        "Bash(git reset --hard *)", "Bash(git tag *)",
        "Bash(curl *)", "Bash(wget *)", "Bash(nc *)", "Bash(scp *)", "Bash(ssh *)",
        "Bash(security *)", "Bash(sudo *)", "Bash(rm -rf /)", "Bash(rm -rf ~*)", "Bash(rm -rf /Users*)"]

log = logging.getLogger("slack-review")
_locks, _locks_guard = {}, threading.Lock()


def named_lock(name):
    """같은 PR 리뷰·같은 저장소 git 작업이 동시에 돌지 않게 하는 이름별 잠금."""
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())


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
        self.threads = data.get("threads", {})   # 요청 스레드 → {channel, links, cursor}
        self.rounds = data.get("rounds", {})     # PR → 마지막 리뷰 회차
        self.pending = data.get("pending", [])   # 발견했지만 끝나지 않은 요청 — 재시작해도 이어서 처리한다
        self.lock = threading.RLock()  # 일꾼 여럿과 확인 스레드가 함께 고친다

    def save(self):
        with self.lock:
            cutoff = time.time() - THREAD_DAYS * 86400
            self.threads = {ts: v for ts, v in self.threads.items() if float(ts) >= cutoff}
            STATE.write_text(json.dumps({"reviewed": self.reviewed, "handled": self.handled[-500:],
                                         "cursor": self.cursor, "threads": self.threads,
                                         "rounds": self.rounds, "pending": self.pending}, indent=1))

    def add_pending(self, job):
        with self.lock:
            self.pending.append(list(job))
            self.save()

    def add_thread(self, channel, root, links):
        with self.lock:
            self.threads.setdefault(root, {"channel": channel, "links": links, "cursor": root})
            self.save()

    def reviewed_commit(self, key):
        return self.reviewed.get(key)

    def record(self, key, commit, round_no):
        with self.lock:
            self.reviewed[key] = commit
            self.rounds[key] = round_no or max(self.rounds.get(key, 1), 2)
            self.save()

    def mark_handled(self, ts):
        with self.lock:
            self.handled.append(ts)
            self.pending = [job for job in self.pending if job[1] != ts]
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
    with state.lock:
        oldest = state.cursor.setdefault(channel, f"{time.time():.6f}")
    messages = slack.conversations_history(channel=channel, oldest=oldest, limit=100)["messages"]
    found = []
    with state.lock:
        found = _pick_requests(state, me, partners, channel, messages)
        state.save()
    return found


def _pick_requests(state, me, partners, channel, messages):
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
    return found


def thread_requests(slack, state, me, partners):
    """최근 요청 스레드 안에서 상대방이 「재리뷰」·「N차」로 다시 부탁한 답글."""
    found = []
    for root, info in list(state.threads.items()):
        replies = slack.conversations_replies(channel=info["channel"], ts=root, oldest=info["cursor"])["messages"]
        with state.lock:
            found += _pick_rereviews(state, me, partners, root, info, replies)
    state.save()
    return found


def _pick_rereviews(state, me, partners, root, info, replies):
    found = []
    for m in replies:
        if m["ts"] == root or float(m["ts"]) <= float(info["cursor"]):
            continue
        info["cursor"] = max(info["cursor"], m["ts"], key=float)
        user, text = m.get("user"), m.get("text", "")
        if m.get("bot_id") or user == me or user not in partners or m["ts"] in state.handled:
            continue
        if RE_REVIEW.search(text):
            hint = ROUND.search(text)
            links = [tuple(link) for link in info["links"]]
            found.append((info["channel"], m["ts"], links, root, int(hint.group(1)) if hint else None))
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
    with named_lock(f"git:{repo}"), named_lock("git:payn_domain") if repo == "payn_backend" else _NO_LOCK:
        return _prepare_worktree(repo, pr)


class _NoLock:
    def __enter__(self): return self
    def __exit__(self, *exc): return False


_NO_LOCK = _NoLock()


def _prepare_worktree(repo, pr):
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
    "discussion": ("재리뷰 요청이지만 지난 리뷰(`{base}`) 이후 새 커밋은 없다. 코드 대신 **지난 리뷰 이후 PR 에 달린 댓글·답글**을 본다. "
                   "작성자나 다른 사람이 이 계정의 지적에 반박·설명·질문을 남겼으면 코드로 확인해 그 댓글에 답글로 답한다 — "
                   "맞으면 「확인했습니다, 이 지적은 철회합니다」처럼 수긍하고, 아니면 근거(파일:라인)를 보강한다. "
                   "새 코드 지적은 하지 않는다. 답할 댓글이 없으면 아무것도 게시하지 않고 posted=true, counts 전부 0, "
                   "headline 은 「새 커밋·새 의견이 없어 기존 리뷰를 유지합니다」."),
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
           "--allowedTools", *TOOLS, "--disallowedTools", *DENY]
    SCRATCH.mkdir(exist_ok=True)
    out = subprocess.run(cmd, input=prompt, cwd=wt, capture_output=True, text=True, timeout=LIMIT["timeout"])
    (BASE / "logs" / f"{wt.name}-{int(time.time())}.json").write_text(out.stdout or out.stderr)
    result = json.loads(out.stdout)
    if result.get("is_error") or "structured_output" not in result:
        message = str(result.get("result"))
        if USAGE_LIMIT.search(message):
            raise UsageLimit(message[:200])
        raise RuntimeError(f"claude 실패: {result.get('subtype')} {message[:200]}")
    return {**result["structured_output"], "denied": len(result.get("permission_denials") or [])}


# ── 답글 ───────────────────────────────────────────────────

def tally(counts):
    marks = (("🔴", "red"), ("🟡", "yellow"), ("🟢", "green"), ("❓", "question"))
    return " · ".join(f"{m} {counts[k]}" for m, k in marks if counts.get(k))


def reply_text(link, mode, res, round_no=None):
    if not res.get("posted"):  # 내부 사유는 스레드에 쓰지 않는다 — 로그·알림으로만
        return f"{link} 자동 리뷰를 마치지 못했습니다. 확인 후 다시 진행하겠습니다."
    new = tally(res["counts"])
    label = f"{round_no}차 리뷰" if round_no and round_no >= 2 else ("리뷰" if mode == "first" else "재리뷰")
    if mode == "first":
        if not new:
            return f"{link} {label} 완료했습니다 — 지적 없음, PR 에 리뷰 댓글 남겼습니다.\n{res['headline']}"
        return f"{link} {label} 완료했습니다 — PR 에 댓글 남겼습니다 ({new})\n{res['headline']}"
    if mode == "discussion":
        return f"{link} {label} 완료했습니다 — 새 커밋이 없어 PR 의견에 답했습니다\n{res['headline']}"
    p = res.get("prior") or {}
    parts = [f"이전 지적 {p.get('total', 0)}건 중 {p.get('resolved', 0)}건 반영"]
    if p.get("unresolved"):
        parts.append(f"미반영 {p['unresolved']}건")
    parts.append(f"새 지적 {new}" if new else "새 지적 없음")
    return f"{link} {label} 완료했습니다 — {' · '.join(parts)}\n{res['headline']}"


# ── 작업 ───────────────────────────────────────────────────

def review(cfg, slack, state, repo, pr_id, channel=None, root=None, round_hint=None, dry=False):
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

    wt = prepare_worktree(repo, pr)
    try:
        if same_commit(base, head):  # 새 커밋이 없어도 재리뷰 요청이면 PR 댓글 반박·설명에 답한다
            mode, rows = "discussion", []
        else:
            mode, rows = scope(wt, pr["destination"]["branch"]["name"], base)
        prompt = build_prompt(cfg, repo, pr, mode, base, size(rows))
        if dry:
            print(f"[{pr['state']}] mode={mode} base={base} ({size(rows)})\n\n{prompt}")
            return None
        log.info("리뷰 시작 %s %s (%s)", key, mode, size(rows))
        res = run_claude(prompt, wt)
        if not res.get("posted"):
            log.warning("게시 안 됨 — 한 번 다시 실행 %s: %s", key, res.get("skipped_reason"))
            res = run_claude(prompt, wt)
        if not res.get("posted"):
            notify(f"{key} 자동 리뷰 미게시 — {res.get('skipped_reason', '')}"[:200])
        round_no = round_hint or (state.rounds[key] + 1 if key in state.rounds else 1 if mode == "first" else None)
        if res.get("posted"):
            state.record(key, head, round_no)
        if res["denied"]:  # 스레드에는 올리지 않는다 — 허용 목록 보강용으로 로그만
            log.warning("권한 거부 %d건 %s — logs/*.json 의 permission_denials 참고", res["denied"], key)
        if res.get("posted"):  # 실패·미게시는 스레드에 올리지 않는다 — 로그·Mac 알림으로만
            done(reply_text(link, mode, res, round_no))
    finally:
        with named_lock(f"git:{repo}"):
            git(REPO_ROOT / repo, "worktree", "remove", "--force", str(wt), check=False)


def self_update():
    """원본 저장소에 새 커밋이 있으면 받아 코드 파일만 덮어쓴다. True 면 호출자가 끝내고 launchd 가 새 코드로 다시 띄운다."""
    if not SOURCE_FILE.exists():
        return False
    src = Path(SOURCE_FILE.read_text().strip())
    git(src, "fetch", "-q", "origin", "main")
    if git(src, "rev-parse", "HEAD").stdout == git(src, "rev-parse", "origin/main").stdout:
        return False
    git(src, "pull", "-q", "--ff-only", "origin", "main")
    for name in CODE_FILES:
        shutil.copy2(src / name, BASE / name)
    return True


class UsageLimit(RuntimeError):
    """Claude 구독 한도 — 리뷰 실패가 아니라 나중에 다시 할 일이다."""


def handle_request(cfg, slack, state, channel, ts, links, root=None, round_hint=None):
    """ts 는 👀 를 달 요청(명령 메시지 또는 스레드 재리뷰 답글), root 는 답글을 달 스레드.

    한도에 걸리면 끝남으로 기록하지 않고 UsageLimit 을 올린다 — 처리 목록에 남아 나중에 다시 돈다.
    """
    root = root or ts
    state.add_thread(channel, root, links)
    react(slack, channel, ts, "eyes")
    for repo, pr_id in links:
        try:
            with named_lock(f"pr:{repo}#{pr_id}"):  # 같은 PR 은 앞 리뷰가 끝난 뒤에 — 작업 폴더가 겹친다
                review(cfg, slack, state, repo, pr_id, channel=channel, root=root, round_hint=round_hint)
        except UsageLimit:
            raise
        except Exception as e:  # noqa: BLE001 — 한 PR 실패가 다른 PR 을 막지 않게 한다
            log.exception("리뷰 실패 %s#%s", repo, pr_id)
            notify(f"{repo}#{pr_id} 자동 리뷰 실패 — {e}"[:200])
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
    active, active_lock = [0], threading.Lock()

    def worker():
        while True:
            channel, ts, links, root, round_hint = jobs.get()
            with active_lock:
                active[0] += 1
            try:
                handle_request(cfg, slack, state, channel, ts, links, root, round_hint)
            except UsageLimit as e:
                log.warning("Claude 한도 — %d분 뒤 다시 시도 %s: %s", LIMIT_RETRY_SECONDS // 60, ts, e)
                notify(f"Claude 한도로 자동 리뷰를 미룹니다 — {e}"[:200])
                job = (channel, ts, links, root, round_hint)
                threading.Timer(LIMIT_RETRY_SECONDS, jobs.put, args=(job,)).start()
            except Exception as e:  # noqa: BLE001
                log.exception("요청 처리 실패 %s", ts)
                notify(f"자동 리뷰 요청 처리 실패 — {e}"[:200])
            finally:
                with active_lock:
                    active[0] -= 1

    def updater():
        while True:
            time.sleep(UPDATE_SECONDS)
            if active[0] or not jobs.empty():
                continue  # 리뷰 중에는 바꾸지 않는다 — 다음 주기에 다시 본다
            try:
                if self_update():
                    log.info("새 버전을 받아 재시작합니다")
                    os._exit(0)  # KeepAlive 라 launchd 가 새 코드로 다시 띄운다
            except Exception:  # noqa: BLE001 — 업데이트 실패로 리뷰를 멈추지 않는다
                log.exception("자동 업데이트 실패")

    def poller():
        dms, refreshed, tick = {}, 0.0, 0
        while True:
            try:
                if time.time() - refreshed > 600:
                    dms, refreshed = partner_dms(slack, cfg["partners"]), time.time()
                found = [(c, ts, links, None, None) for channel in dms.values()
                         for c, ts, links in new_requests(slack, state, me, cfg["partners"], channel)]
                if tick % THREAD_POLL_EVERY == 0:
                    found += thread_requests(slack, state, me, cfg["partners"])
                tick += 1
                for job in found:
                    if job[1] not in queued:
                        log.info("요청 발견 %s %s 스레드=%s 회차=%s", job[1], job[2], job[3], job[4])
                        queued.add(job[1])
                        state.add_pending(job)
                        jobs.put(job)
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

    for job in state.pending:  # 재시작 전에 발견했지만 끝내지 못한 요청
        log.info("이어서 처리 %s %s", job[1], job[2])
        queued.add(job[1])
        jobs.put(tuple(job))
    for _ in range(cfg.get("workers", 2)):  # 심층 리뷰는 빌드까지 돌려 무겁다 — 기본 2개
        threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=updater, daemon=True).start()
    socket = SocketModeClient(app_token=keychain("slack-review-app-token"), web_client=slack)
    socket.socket_mode_request_listeners.append(on_request)
    socket.connect()
    log.info("연결됨 me=%s partners=%s", me, sorted(cfg["partners"]))
    threading.Event().wait()


def requeue(hours=24, force=()):
    """재시작으로 잃은 요청을 되살린다. 👀 가 달려 있어도 끝난 기록(handled)이 없으면 다시 넣는다.

    force 에 준 요청 ts 는 이미 끝난 기록이 있어도 다시 넣는다 — 게시에 실패한 리뷰를 다시 돌릴 때.
    """
    from slack_sdk import WebClient
    cfg, state = load_config(), State()
    state.handled = [ts for ts in state.handled if ts not in force]
    slack = WebClient(token=keychain("slack-review-user-token"))
    me, pending = slack.auth_test()["user_id"], {job[1] for job in state.pending}
    for channel in partner_dms(slack, cfg["partners"]).values():
        history = slack.conversations_history(channel=channel, oldest=str(time.time() - hours * 3600), limit=200)
        for m in history["messages"]:
            requester = REQUESTER.match(m.get("text", ""))
            if m.get("bot_id") != APP_BOT_ID or not requester or requester.group(1) in (me,) \
                    or requester.group(1) not in cfg["partners"] or m["ts"] in state.handled or m["ts"] in pending:
                continue
            links = list(dict.fromkeys((r, int(i)) for r, i in PR_LINK.findall(m["text"])))
            state.add_pending((channel, m["ts"], links, None, None))
            print("다시 넣음", m["ts"], links)


def main():
    (BASE / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        serve()
    elif cmd == "requeue":
        requeue(force=tuple(sys.argv[2:]))
    elif cmd == "dry" and len(sys.argv) > 2 and PR_LINK.search(sys.argv[2]):
        repo, pr_id = PR_LINK.search(sys.argv[2]).groups()
        review(load_config(), None, State(), repo, int(pr_id), dry=True)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
