#!/usr/bin/env python3
"""
tick-sandbox supervisor: a slow 151 Hz loop that checks a queue for new
inputs every tick and dispatches each as a throwaway GitHub Actions job.

Jobs run at full speed in their own runner containers and report lit (success)
or dark (anything else). The supervisor never waits for a job to finish.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TICK_HZ = 151
TICK_PERIOD = 1.0 / TICK_HZ
DEFAULT_INBOX = Path("inbox")
PROCESSED_DIR_NAME = "processed"


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_job(path: Path) -> dict[str, Any] | None:
    """Load a JSON job file. Returns None if invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"dark: failed to read {path.name}: {exc}")
        return None
    if not isinstance(data, dict):
        log(f"dark: {path.name} is not a JSON object")
        return None
    job_id = data.get("job_id") or path.stem
    command = data.get("command")
    if not command or not isinstance(command, str):
        log(f"dark: {path.name} missing string 'command'")
        return None
    return {"job_id": str(job_id), "command": command, "path": path}


def scan_inbox(inbox: Path) -> list[dict[str, Any]]:
    """Return jobs from *.json files in inbox, oldest first."""
    if not inbox.is_dir():
        return []
    files = sorted(
        (p for p in inbox.iterdir() if p.is_file() and p.suffix == ".json"),
        key=lambda p: p.stat().st_mtime,
    )
    jobs: list[dict[str, Any]] = []
    for path in files:
        job = load_job(path)
        if job is not None:
            jobs.append(job)
    return jobs


def mark_processed(job: dict[str, Any], inbox: Path) -> None:
    """Move a consumed job file into inbox/processed/."""
    path: Path = job["path"]
    dest_dir = inbox / PROCESSED_DIR_NAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        dest = dest_dir / f"{path.stem}-{int(time.time())}{path.suffix}"
    try:
        shutil.move(str(path), str(dest))
    except OSError as exc:
        log(f"warning: could not move {path.name}: {exc}")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def resolve_repo() -> tuple[str, str]:
    """Return (owner, repo) from GITHUB_REPOSITORY or gh."""
    env = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env and "/" in env:
        owner, repo = env.split("/", 1)
        return owner, repo
    try:
        out = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if out.returncode == 0 and "/" in out.stdout.strip():
            owner, repo = out.stdout.strip().split("/", 1)
            return owner, repo
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "fitzyracing1", "tick-sandbox"


def dispatch_via_api(owner: str, repo: str, job_id: str, command: str) -> bool:
    """Fire workflow_dispatch via GitHub REST API. Never prints the token."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        # Fall back to gh api (uses stored auth; does not print secrets)
        return dispatch_via_gh(owner, repo, job_id, command)

    url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/actions/workflows/job.yml/dispatches"
    )
    body = json.dumps(
        {
            "ref": "main",
            "inputs": {"job_id": job_id, "command": command},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "tick-sandbox-supervisor",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            # 204 No Content is success for workflow_dispatch
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        log(f"dark: API dispatch HTTP {exc.code} for job_id={job_id}")
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"dark: API dispatch failed for job_id={job_id}: {type(exc).__name__}")
        return False


def dispatch_via_gh(owner: str, repo: str, job_id: str, command: str) -> bool:
    """Fire workflow_dispatch via `gh api`."""
    payload = json.dumps(
        {
            "ref": "main",
            "inputs": {"job_id": job_id, "command": command},
        }
    )
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"/repos/{owner}/{repo}/actions/workflows/job.yml/dispatches",
                "--input",
                "-",
            ],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode == 0:
            return True
        log(f"dark: gh api dispatch failed for job_id={job_id} (rc={result.returncode})")
        return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"dark: gh api dispatch error for job_id={job_id}: {type(exc).__name__}")
        return False


def run_local(job_id: str, command: str) -> str:
    """Run command in a local subprocess; return 'lit' or 'dark'."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            return "lit"
        return "dark"
    except subprocess.TimeoutExpired:
        return "dark"
    except OSError:
        return "dark"


def spawn_local(job_id: str, command: str) -> None:
    """Spawn a local job in a daemon thread so the tick loop never waits."""

    def _worker() -> None:
        status = run_local(job_id, command)
        log(f"{status}: job_id={job_id} (local)")

    t = threading.Thread(target=_worker, name=f"job-{job_id}", daemon=True)
    t.start()


def dispatch_job(
    job: dict[str, Any],
    *,
    dry_run: bool,
    owner: str,
    repo: str,
) -> None:
    """Dispatch one job and log; never blocks waiting for remote completion."""
    job_id = job["job_id"]
    command = job["command"]
    if dry_run:
        log(f"dispatch (dry-run): job_id={job_id}")
        spawn_local(job_id, command)
        return
    ok = dispatch_via_api(owner, repo, job_id, command)
    if ok:
        log(f"dispatched: job_id={job_id} (await lit/dark on Actions)")
    else:
        log(f"dark: dispatch failed job_id={job_id}")


def read_stdin_jobs() -> list[dict[str, Any]]:
    """Non-blocking: drain any complete JSON lines already waiting on stdin."""
    jobs: list[dict[str, Any]] = []
    if not sys.stdin.isatty():
        # Only consume if data is already available; avoid blocking the tick loop.
        import select

        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                break
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                log(f"dark: stdin JSON error: {exc}")
                continue
            if not isinstance(data, dict) or not isinstance(data.get("command"), str):
                log("dark: stdin job missing string 'command'")
                continue
            job_id = str(data.get("job_id") or f"stdin-{int(time.time() * 1000)}")
            jobs.append({"job_id": job_id, "command": data["command"], "path": None})
    return jobs


def tick_once(
    inbox: Path,
    *,
    dry_run: bool,
    owner: str,
    repo: str,
    use_stdin: bool,
) -> int:
    """One supervisor tick: drain queue, dispatch each job, return count."""
    jobs = scan_inbox(inbox)
    if use_stdin:
        jobs.extend(read_stdin_jobs())
    for job in jobs:
        dispatch_job(job, dry_run=dry_run, owner=owner, repo=repo)
        if job.get("path") is not None:
            mark_processed(job, inbox)
    return len(jobs)


def run_loop(
    inbox: Path,
    *,
    dry_run: bool,
    owner: str,
    repo: str,
    use_stdin: bool,
    max_ticks: int | None = None,
) -> None:
    """151 Hz supervisor loop. Continues listening; never waits on jobs."""
    mode = "dry-run (local)" if dry_run else f"remote ({owner}/{repo})"
    log(f"supervisor start: {TICK_HZ} Hz, inbox={inbox}, mode={mode}")
    inbox.mkdir(parents=True, exist_ok=True)
    ticks = 0
    try:
        while max_ticks is None or ticks < max_ticks:
            t0 = time.perf_counter()
            tick_once(inbox, dry_run=dry_run, owner=owner, repo=repo, use_stdin=use_stdin)
            ticks += 1
            elapsed = time.perf_counter() - t0
            sleep_for = TICK_PERIOD - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        log("supervisor stop (interrupt)")
    log(f"supervisor exit after {ticks} ticks")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="151 Hz supervisor that dispatches throwaway Actions jobs"
    )
    p.add_argument(
        "--inbox",
        type=Path,
        default=DEFAULT_INBOX,
        help="Directory of JSON job files (default: inbox/)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run jobs as local subprocesses instead of workflow_dispatch",
    )
    p.add_argument(
        "--stdin",
        action="store_true",
        help="Also accept newline-delimited JSON jobs from stdin",
    )
    p.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="Stop after N ticks (useful for tests)",
    )
    p.add_argument(
        "--owner",
        default=None,
        help="GitHub owner (default: from GITHUB_REPOSITORY or gh)",
    )
    p.add_argument(
        "--repo",
        default=None,
        help="GitHub repo name (default: from GITHUB_REPOSITORY or gh)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    owner, repo = resolve_repo()
    if args.owner:
        owner = args.owner
    if args.repo:
        repo = args.repo
    run_loop(
        args.inbox,
        dry_run=args.dry_run,
        owner=owner,
        repo=repo,
        use_stdin=args.stdin,
        max_ticks=args.max_ticks,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
