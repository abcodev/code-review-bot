#!/usr/bin/env python3
"""Bitbucket Cloud PR 조회·댓글 CLI — pr-review-comment 스킬 전용.

토큰은 키체인 `bitbucket-api-token` 항목(계정=Atlassian 이메일)에서 읽는다.
머지·승인·거절은 일부러 구현하지 않는다. 토큰 스코프(write:pullrequest)는 허용하지만
git_guard 훅이 API 호출을 가로채지 못하므로, 이 스크립트가 그 동작의 유일한 통로가 되지 않게 한다.

쓰기(comment·reply·edit)는 기본이 미리보기다. `--post` 를 붙여야 실제로 게시한다.

사용:
  bb.py prs <repo> [--state OPEN|MERGED|DECLINED]
  bb.py pr <repo> <id> | <PR URL>
  bb.py diff <repo> <id> [--stat]
  bb.py comments <repo> <id>
  bb.py comment <repo> <id> --body-file F [--path P (--to N | --from N)] [--post]
  bb.py reply <repo> <id> --parent CID --body-file F [--post]
  bb.py edit <repo> <id> --comment CID --body-file F [--post]
"""
import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.bitbucket.org/2.0"
WORKSPACE = os.environ.get("BB_WORKSPACE", "pay-n")
KEYCHAIN_SERVICE = "bitbucket-api-token"
PR_URL = re.compile(r"bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)")


def credentials():
    try:
        token = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, check=True).stdout.strip()
        meta = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        sys.exit(f"키체인에 '{KEYCHAIN_SERVICE}' 항목이 없습니다.")
    m = re.search(r'"acct"<blob>="([^"]+)"', meta)
    if not m:
        sys.exit("키체인 항목에 계정(이메일)이 없습니다.")
    return m.group(1), token


def request(method, url, body=None, raw=False):
    email, token = credentials()
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url if url.startswith("http") else API + url,
                                 data=data, method=method)
    req.add_header("Authorization", f"Basic {auth}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            text = res.read().decode()
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} {method} {url}\n{e.read().decode()[:500]}")
    return text if raw else json.loads(text)


def paged(url):
    while url:
        page = request("GET", url)
        yield from page.get("values", [])
        url = page.get("next")


def pr_path(repo, pr_id):
    return f"/repositories/{WORKSPACE}/{repo}/pullrequests/{pr_id}"


def resolve_target(args):
    """`<repo> <id>` 또는 PR URL 하나를 받는다."""
    m = PR_URL.search(args.repo)
    if m:
        global WORKSPACE
        WORKSPACE = m.group(1)
        return m.group(2), m.group(3)
    if not args.id:
        sys.exit("PR 번호가 필요합니다: <repo> <id> 또는 PR URL")
    return args.repo, args.id


def cmd_prs(args):
    url = f"/repositories/{WORKSPACE}/{args.repo}/pullrequests?state={args.state}&pagelen=50"
    for p in paged(url):
        print(f"#{p['id']}\t{p['state']}\t{p['source']['branch']['name']} -> "
              f"{p['destination']['branch']['name']}\t{p['author']['display_name']}\t{p['title']}")


def cmd_pr(args):
    repo, pr_id = resolve_target(args)
    p = request("GET", pr_path(repo, pr_id))
    src, dst = p["source"], p["destination"]
    print(f"#{p['id']} [{p['state']}] {p['title']}")
    print(f"작성자: {p['author']['display_name']}")
    print(f"소스:   {src['branch']['name']} @ {src['commit']['hash'][:12]}")
    print(f"타깃:   {dst['branch']['name']} @ {dst['commit']['hash'][:12]}")
    print(f"댓글:   {p.get('comment_count', 0)}개")
    for r in p.get("participants", []):
        mark = "승인" if r.get("approved") else (r.get("state") or "")
        print(f"참여자: {r['user']['display_name']} ({r['role']}) {mark}".rstrip())
    print(f"링크:   {p['links']['html']['href']}")
    if p.get("description"):
        print("\n" + p["description"])


def cmd_diff(args):
    repo, pr_id = resolve_target(args)
    if args.stat:
        for f in paged(pr_path(repo, pr_id) + "/diffstat?pagelen=100"):
            path = (f.get("new") or f.get("old"))["path"]
            print(f"{f['status']:<9} +{f['lines_added']:<5} -{f['lines_removed']:<5} {path}")
    else:
        sys.stdout.write(request("GET", pr_path(repo, pr_id) + "/diff", raw=True))


def cmd_comments(args):
    repo, pr_id = resolve_target(args)
    for c in paged(pr_path(repo, pr_id) + "/comments?pagelen=100"):
        if c.get("deleted"):
            continue
        where = ""
        if c.get("inline"):
            i = c["inline"]
            line = f"+{i['to']}" if i.get("to") else f"-{i.get('from')}"
            where = f" {i['path']}:{line}"
        parent = f" ↳#{c['parent']['id']}" if c.get("parent") else ""
        resolved = " [해결됨]" if c.get("resolution") else ""
        print(f"--- #{c['id']}{parent} {c['user']['display_name']} "
              f"{c['created_on'][:16]}{where}{resolved}")
        print(c["content"]["raw"])


def diff_lines(repo, pr_id, path):
    """PR diff 에서 path 의 새 파일 줄번호(추가+문맥)와 옛 파일 줄번호(삭제+문맥)를 모은다."""
    diff = request("GET", pr_path(repo, pr_id) + "/diff", raw=True)
    new, old, in_file = set(), set(), False
    o = n = 0
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            in_file = line.endswith(f" b/{path}") or f" a/{path} " in line
            continue
        if not in_file:
            continue
        h = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if h:
            o, n = int(h.group(1)), int(h.group(2))
        elif line.startswith("+") and not line.startswith("+++"):
            new.add(n); n += 1
        elif line.startswith("-") and not line.startswith("---"):
            old.add(o); o += 1
        elif line.startswith(" "):
            new.add(n); old.add(o); n += 1; o += 1
    return new, old


def read_body(path):
    with open(path, encoding="utf-8") as f:
        body = f.read().strip()
    if not body:
        sys.exit("본문이 비어 있습니다.")
    return body


def send(args, method, url, payload, label):
    print(f"[{label}] {method} {url}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not args.post:
        print("\n미리보기입니다. 게시하려면 --post 를 붙이세요.")
        return
    c = request(method, url, payload)
    print(f"\n게시됨: #{c['id']} {c['links']['html']['href']}")


def cmd_comment(args):
    repo, pr_id = resolve_target(args)
    payload = {"content": {"raw": read_body(args.body_file)}}
    if args.path:
        if (args.to is None) == (args.from_ is None):
            sys.exit("인라인 댓글은 --to(새 파일 줄) 또는 --from(삭제된 줄) 중 하나만 지정합니다.")
        new, old = diff_lines(repo, pr_id, args.path)
        if not new and not old:
            sys.exit(f"{args.path} 는 이 PR diff 에 없습니다.")
        if args.to is not None and args.to not in new:
            sys.exit(f"{args.path}:{args.to} 는 diff 에 나온 줄이 아닙니다. 가장 가까운 변경 줄에 다세요.")
        if args.from_ is not None and args.from_ not in old:
            sys.exit(f"{args.path}:-{args.from_} 는 diff 에 나온 삭제 줄이 아닙니다.")
        payload["inline"] = {"path": args.path}
        payload["inline"]["to" if args.to is not None else "from"] = args.to or args.from_
    send(args, "POST", pr_path(repo, pr_id) + "/comments", payload,
         "인라인 댓글" if args.path else "전체 댓글")


def cmd_reply(args):
    repo, pr_id = resolve_target(args)
    payload = {"content": {"raw": read_body(args.body_file)}, "parent": {"id": args.parent}}
    send(args, "POST", pr_path(repo, pr_id) + "/comments", payload, f"답글 → #{args.parent}")


def cmd_edit(args):
    repo, pr_id = resolve_target(args)
    payload = {"content": {"raw": read_body(args.body_file)}}
    send(args, "PUT", pr_path(repo, pr_id) + f"/comments/{args.comment}", payload,
         f"댓글 수정 #{args.comment}")


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(description="Bitbucket PR 조회·댓글")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def target(p):
        p.add_argument("repo", help="저장소 slug 또는 PR URL")
        p.add_argument("id", nargs="?")

    p = sub.add_parser("prs"); p.add_argument("repo")
    p.add_argument("--state", default="OPEN", choices=["OPEN", "MERGED", "DECLINED", "SUPERSEDED"])
    p.set_defaults(fn=cmd_prs)
    p = sub.add_parser("pr"); target(p); p.set_defaults(fn=cmd_pr)
    p = sub.add_parser("diff"); target(p); p.add_argument("--stat", action="store_true")
    p.set_defaults(fn=cmd_diff)
    p = sub.add_parser("comments"); target(p); p.set_defaults(fn=cmd_comments)

    p = sub.add_parser("comment"); target(p)
    p.add_argument("--body-file", required=True)
    p.add_argument("--path")
    p.add_argument("--to", type=int)
    p.add_argument("--from", dest="from_", type=int)
    p.add_argument("--post", action="store_true")
    p.set_defaults(fn=cmd_comment)
    p = sub.add_parser("reply"); target(p)
    p.add_argument("--parent", type=int, required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--post", action="store_true")
    p.set_defaults(fn=cmd_reply)
    p = sub.add_parser("edit"); target(p)
    p.add_argument("--comment", type=int, required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--post", action="store_true")
    p.set_defaults(fn=cmd_edit)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
