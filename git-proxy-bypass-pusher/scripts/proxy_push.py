#!/usr/bin/env python3
import argparse
import base64
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_OWNER = "<user>"
DEFAULT_REPO = "<repo>"
DEFAULT_BRANCH = "main"

class RateLimitError(Exception):
    pass

def get_git_path(cwd):
    # Try finding git on PATH
    git_bin = shutil.which("git")
    if git_bin:
        return git_bin
    
    # Try common portable git locations relative to the workspace directory
    candidates = [
        os.path.abspath(os.path.join(cwd, "..", "python", "Git", "cmd", "git.exe")),
        os.path.abspath(os.path.join(cwd, "..", "Git", "cmd", "git.exe")),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "git"

def make_request(url, method="GET", headers=None, data=None, secure=False):
    # Setup SSL Context (by default bypass SSL check behind proxy)
    ctx = ssl.create_default_context()
    if not secure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method=method
    )

    # Retry loop for rate limits and transient errors
    max_retries = 5
    delay = 1.0
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, context=ctx) as response:
                return response.read(), response.info()
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")
            except Exception:
                pass

            if e.code == 429:
                print(f"Warning: Rate limited (429) by GitHub API. Retrying in {delay}s...", file=sys.stderr)
                time.sleep(delay)
                delay *= 2
                continue
            elif e.code in [500, 502, 503, 504]:
                print(f"Warning: Remote server error ({e.code}). Retrying in {delay}s...", file=sys.stderr)
                time.sleep(delay)
                delay *= 2
                continue
            else:
                # Raise other HTTP errors with response body
                error_msg = f"HTTP Error {e.code}: {e.reason}"
                if body:
                    error_msg += f"\nResponse Body: {body}"
                raise Exception(error_msg)
        except urllib.error.URLError as e:
            if attempt == max_retries - 1:
                raise e
            print(f"Warning: Connection error ({e.reason}). Retrying in {delay}s...", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
            continue
    raise RateLimitError("GitHub API rate limit exceeded and max retries reached.")

def sync_repo(cwd, branch):
    git_bin = get_git_path(cwd)
    print("Reconciling Git history...")
    
    # Run fetch
    fetch_cmd = [git_bin, "fetch", "origin"]
    print(f"Executing: {' '.join(fetch_cmd)}")
    subprocess.run(fetch_cmd, cwd=cwd, check=True)
    
    # Run reset --hard
    reset_cmd = [git_bin, "reset", "--hard", f"origin/{branch}"]
    print(f"Executing: {' '.join(reset_cmd)}")
    subprocess.run(reset_cmd, cwd=cwd, check=True)

def handle_push(args):
    cwd = os.getcwd()
    
    # Resolve token
    token = args.token or os.environ.get("GITHUB_TOKEN")
    if not token and os.path.exists(".env"):
        with open(".env", "r") as env_file:
            for line in env_file:
                if line.strip().startswith("GITHUB_TOKEN="):
                    token = line.strip().split("=", 1)[1].strip('"\' ')
                    break
    if not token:
        print("Error: GitHub token is required. Pass with -t, set GITHUB_TOKEN environment variable, or add GITHUB_TOKEN=your_token to .env", file=sys.stderr)
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "GitProxyPusher/1.0",
        "Accept": "application/vnd.github+json"
    }

    # Normalize files paths
    resolved_files = []
    for f in args.files:
        abs_path = os.path.abspath(f)
        if not os.path.exists(abs_path):
            print(f"Error: Local file '{f}' does not exist.")
            sys.exit(1)
        # Ensure it is inside workspace
        rel_path = os.path.relpath(abs_path, cwd)
        if rel_path.startswith("../"):
            print(f"Error: File '{f}' is outside the current workspace directory.")
            sys.exit(1)
        resolved_files.append((abs_path, rel_path))

    suffix = args.suffix or ""

    try:
        # Step 1: Get latest commit and base tree info from GitHub
        print("Fetching latest commit info...")
        branch_url = f"https://api.github.com/repos/{args.owner}/{args.repo}/branches/{args.branch}"
        branch_info_content, _ = make_request(branch_url, headers=headers, secure=args.secure)
        branch_info = json.loads(branch_info_content.decode("utf-8"))
        parent_commit_sha = branch_info["commit"]["sha"]
        base_tree_sha = branch_info["commit"]["commit"]["tree"]["sha"]
        print(f"  Parent commit: {parent_commit_sha}")
        print(f"  Base tree: {base_tree_sha}")

        # Step 2: Upload files as blobs
        print("Uploading files as blobs to GitHub via Git Database API...")
        tree_entries = []
        for abs_path, rel_path in resolved_files:
            with open(abs_path, "rb") as f:
                file_bytes = f.read()
            encoded_content = base64.b64encode(file_bytes).decode("utf-8")
            
            blob_url = f"https://api.github.com/repos/{args.owner}/{args.repo}/git/blobs"
            blob_payload = {
                "content": encoded_content,
                "encoding": "base64"
            }
            blob_headers = headers.copy()
            blob_headers["Content-Type"] = "application/json"
            
            remote_path = rel_path + suffix
            git_path = remote_path.replace("\\", "/")
            
            print(f"  Uploading blob for '{git_path}'...")
            blob_res_content, _ = make_request(
                blob_url, method="POST", headers=blob_headers,
                data=json.dumps(blob_payload).encode("utf-8"), secure=args.secure
            )
            blob_res = json.loads(blob_res_content.decode("utf-8"))
            blob_sha = blob_res["sha"]
            
            tree_entries.append({
                "path": git_path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha
            })

        # Step 3: Create Git Tree
        print("Creating Git tree on GitHub...")
        tree_url = f"https://api.github.com/repos/{args.owner}/{args.repo}/git/trees"
        tree_payload = {
            "base_tree": base_tree_sha,
            "tree": tree_entries
        }
        tree_headers = headers.copy()
        tree_headers["Content-Type"] = "application/json"
        tree_res_content, _ = make_request(
            tree_url, method="POST", headers=tree_headers,
            data=json.dumps(tree_payload).encode("utf-8"), secure=args.secure
        )
        tree_res = json.loads(tree_res_content.decode("utf-8"))
        new_tree_sha = tree_res["sha"]
        print(f"  Created tree SHA: {new_tree_sha}")

        # Step 4: Create Commit
        print("Creating commit on GitHub...")
        commit_url = f"https://api.github.com/repos/{args.owner}/{args.repo}/git/commits"
        commit_payload = {
            "message": args.message,
            "tree": new_tree_sha,
            "parents": [parent_commit_sha]
        }
        commit_headers = headers.copy()
        commit_headers["Content-Type"] = "application/json"
        commit_res_content, _ = make_request(
            commit_url, method="POST", headers=commit_headers,
            data=json.dumps(commit_payload).encode("utf-8"), secure=args.secure
        )
        commit_res = json.loads(commit_res_content.decode("utf-8"))
        new_commit_sha = commit_res["sha"]
        print(f"  Created commit SHA: {new_commit_sha}")

        # Step 5: Update Branch Reference
        print("Updating branch reference on GitHub...")
        ref_url = f"https://api.github.com/repos/{args.owner}/{args.repo}/git/refs/heads/{args.branch}"
        ref_payload = {
            "sha": new_commit_sha,
            "force": True
        }
        ref_headers = headers.copy()
        ref_headers["Content-Type"] = "application/json"
        make_request(
            ref_url, method="PATCH", headers=ref_headers,
            data=json.dumps(ref_payload).encode("utf-8"), secure=args.secure
        )
        print("  Successfully updated branch reference.")

        # Step 6: Reconcile local history
        sync_repo(cwd, args.branch)
        print("Workspace successfully pushed and synchronized!")

    except Exception as e:
        print(f"\nError occurred: {e}", file=sys.stderr)
        sys.exit(1)

def handle_sync(args):
    cwd = os.getcwd()
    try:
        sync_repo(cwd, args.branch)
        print("Synchronization completed successfully!")
    except Exception as e:
        print(f"Error during synchronization: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Git Proxy/Firewall Bypass Pusher via GitHub REST API (Git Database API)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Push command
    push_parser = subparsers.add_parser("push", help="Push files through REST API using Git Database API")
    push_parser.add_argument("-f", "--files", nargs="+", required=True, help="List of files to push")
    push_parser.add_argument("-m", "--message", required=True, help="Commit message")
    push_parser.add_argument("--suffix", default="", help="Firewall bypass suffix (default: empty, using Git Database API to upload directly)")
    push_parser.add_argument("-o", "--owner", default=DEFAULT_OWNER, help=f"Repo owner (default: {DEFAULT_OWNER})")
    push_parser.add_argument("-r", "--repo", default=DEFAULT_REPO, help=f"Repo name (default: {DEFAULT_REPO})")
    push_parser.add_argument("-b", "--branch", default=DEFAULT_BRANCH, help=f"Branch (default: {DEFAULT_BRANCH})")
    push_parser.add_argument("-t", "--token", help="GitHub Personal Access Token")
    push_parser.add_argument("--secure", action="store_true", help="Enable SSL verification (disabled by default for proxy)")

    # Sync command
    sync_parser = subparsers.add_parser("sync", help="Synchronize local repository with remote branch safely")
    sync_parser.add_argument("-o", "--owner", default=DEFAULT_OWNER, help=f"Repo owner (default: {DEFAULT_OWNER})")
    sync_parser.add_argument("-r", "--repo", default=DEFAULT_REPO, help=f"Repo name (default: {DEFAULT_REPO})")
    sync_parser.add_argument("-b", "--branch", default=DEFAULT_BRANCH, help=f"Branch (default: {DEFAULT_BRANCH})")

    args = parser.parse_args()

    if args.command == "push":
        handle_push(args)
    elif args.command == "sync":
        handle_sync(args)

if __name__ == "__main__":
    main()
