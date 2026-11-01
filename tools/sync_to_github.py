# -*- coding: utf-8 -*-
"""把本地提交同步到 GitHub。

本机 `git push` 走 HTTPS 代理会失败（Empty reply / CONNECT 502），
但 GitHub REST API 走同一个代理是通的 —— 所以这里直接用 Git Data API
复刻提交历史。

用法：
    python tools/sync_to_github.py

依赖：仓库根目录已配置好 git remote origin，且系统有 GitHub 凭据
      （git credential-manager github login --browser 授权过）。
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO = os.environ.get("GITHUB_REPO", "22759/software-backup-manager")
API = "https://api.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_token():
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, cwd=ROOT,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    return ""


TOKEN = get_token()
if not TOKEN:
    print("未找到 GitHub 凭据，请先执行：")
    print("  git credential-manager github login --browser")
    sys.exit(1)

urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.HTTPSHandler()))


def api(method, path, payload=None):
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read().decode("utf-8")
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def main():
    files = [f for f in subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT
    ).stdout.splitlines() if f.strip()]
    print(f"待同步文件 {len(files)} 个")

    st, ref = api("GET", f"/repos/{REPO}/git/ref/heads/main")
    if st == 200:
        base_tree = ref["object"]["sha"]
    else:
        base_tree = None

    if base_tree is None:
        # 空仓库：先用 Contents API 建一个 README 把仓库点亮
        print("远端为空，先初始化...")
        with open(os.path.join(ROOT, "README.md"), "rb") as f:
            seed = base64.b64encode(f.read()).decode("ascii")
        st, r = api("PUT", f"/repos/{REPO}/contents/README.md", {
            "message": "chore: 初始化仓库",
            "content": seed, "branch": "main"})
        if st not in (200, 201):
            print("初始化失败:", st, str(r)[:300])
            return 1
        st, ref = api("GET", f"/repos/{REPO}/git/ref/heads/main")
        base_tree = ref["object"]["sha"]

    entries = []
    for i, rel in enumerate(files, 1):
        with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        st, blob = api("POST", f"/repos/{REPO}/git/blobs",
                       {"content": b64, "encoding": "base64"})
        if st != 201:
            print(f"blob 失败 {rel}: {st} {str(blob)[:200]}")
            return 1
        entries.append({"path": rel, "mode": "100644",
                        "type": "blob", "sha": blob["sha"]})
        print(f"  [{i}/{len(files)}] {rel}")

    st, tree = api("POST", f"/repos/{REPO}/git/trees",
                   {"base_tree": base_tree, "tree": entries})
    if st != 201:
        print("tree 失败:", st, str(tree)[:300])
        return 1

    msg = subprocess.run(["git", "log", "-1", "--pretty=%B"],
                         capture_output=True, text=True, cwd=ROOT).stdout.strip()
    st, commit = api("POST", f"/repos/{REPO}/git/commits", {
        "message": msg or "update",
        "tree": tree["sha"], "parents": [base_tree]})
    if st != 201:
        print("commit 失败:", st, str(commit)[:300])
        return 1

    st, res = api("PATCH", f"/repos/{REPO}/git/refs/heads/main",
                  {"sha": commit["sha"], "force": True})
    if st != 200:
        print("ref 失败:", st, str(res)[:300])
        return 1

    print("已推送 ->", f"https://github.com/{REPO}")
    print("commit:", commit["sha"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
