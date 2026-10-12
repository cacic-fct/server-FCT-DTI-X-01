#!/usr/bin/env python3
"""Retry isolated network operations; never replay an applied playbook."""

import argparse
import errno
import http.client
import json
import os
from pathlib import Path
import random
import signal
import socket
import subprocess
import sys
import time
import urllib.error

TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}


def command_deadline():
    value = os.environ.get("NETWORK_RETRY_DEADLINE_MONOTONIC")
    if value:
        return float(value)
    return time.monotonic() + int(
        os.environ.get("NETWORK_RETRY_HTTP_BUDGET_SECONDS", "360")
    )


def bounded_command(argv):
    """Share a deadline with nested helpers and terminate the whole process group."""
    deadline = command_deadline()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        print("Network operation time budget exhausted", file=sys.stderr)
        return 75
    environment = {**os.environ, "NETWORK_RETRY_DEADLINE_MONOTONIC": str(deadline)}
    with subprocess.Popen(argv, env=environment, start_new_session=True) as process:
        try:
            return process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            # Give Git a chance to release its lock files before forcing exit.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            print("Network operation time budget exhausted", file=sys.stderr)
            return 75


def retry_settings():
    attempts = int(os.environ.get("NETWORK_RETRY_ATTEMPTS", "10"))
    delay = int(os.environ.get("NETWORK_RETRY_DELAY_SECONDS", "30"))
    if attempts < 1 or delay < 0:
        raise ValueError(
            "Network retry attempts must be positive and delay nonnegative"
        )
    return attempts, delay


def pause(attempt, attempts, delay, remaining=None):
    wait = delay + random.uniform(0, min(delay, 10))
    if remaining is not None:
        wait = min(wait, remaining)
    print(
        f"Network operation failed (attempt {attempt}/{attempts}); retrying in {wait:.0f}s",
        file=sys.stderr,
        flush=True,
    )
    time.sleep(wait)


def retry_command(argv, extra_environment=None):
    attempts, delay = retry_settings()
    environment = os.environ.copy()
    environment.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_HTTP_LOW_SPEED_LIMIT="1",
        GIT_HTTP_LOW_SPEED_TIME="60",
    )
    environment.update(extra_environment or {})
    for attempt in range(1, attempts + 1):
        result = subprocess.run(argv, env=environment, check=False)
        if result.returncode == 0 or attempt == attempts:
            return result.returncode
        pause(attempt, attempts, delay)


def failed_before_request(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    return isinstance(reason, socket.gaierror) or (
        isinstance(reason, OSError)
        and reason.errno in {errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH}
    )


def retry_http(operation, timeout=360):
    """Retry transient transport/HTTP failures, with a total time budget.

    Callers may retry POST only when duplicates are safe or the operation
    reconciles an ambiguous outcome before doing any further writes.
    The callback receives the remaining per-request timeout on every attempt.
    """
    attempts, delay = retry_settings()
    deadline = time.monotonic() + timeout
    for attempt in range(1, attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Network retry time budget exhausted")
        try:
            return operation(min(30, remaining))
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP_STATUSES:
                raise
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            requested_delay = int(retry_after) if retry_after.isdecimal() else delay
            exc.close()
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
        ):
            requested_delay = delay
        remaining = deadline - time.monotonic()
        if attempt == attempts or remaining <= requested_delay:
            raise TimeoutError("Network retries exhausted") from None
        pause(attempt, attempts, requested_delay, remaining)


def pull(args):
    # Use Ansible's git module, retaining force/shallow/submodule behavior from
    # ansible-pull. The checkout is retried before any playbook task can run.
    executable_dir = Path(args.executable).parent
    checkout = str(Path(args.directory).resolve())
    parameters = {
        "repo": args.url,
        "dest": checkout,
        "version": args.checkout,
        "force": args.clean,
        "depth": 1,
    }
    result = retry_command(
        [
            str(executable_dir / "ansible"),
            "localhost",
            "--inventory",
            "localhost,",
            "--connection",
            "local",
            "--module-name",
            "ansible.builtin.git",
            "--args",
            json.dumps(parameters),
            "--extra-vars",
            json.dumps({"ansible_python_interpreter": sys.executable}),
        ],
        extra_environment={"ANSIBLE_LOAD_CALLBACK_PLUGINS": "0"},
    )
    if result:
        return result

    # Same host restriction as ansible-pull; execute exactly once, from the
    # freshly checked-out directory so its ansible.cfg and roles take effect.
    hostnames = {socket.getfqdn(), socket.gethostname()}
    hostnames.update(host.split(".")[0] for host in list(hostnames))
    argv = [
        str(executable_dir / "ansible-playbook"),
        "--connection",
        "local",
        "--inventory",
        args.inventory,
        "--limit",
        ",".join(sorted({"localhost", "127.0.0.1", *hostnames})),
    ]
    if args.verbosity:
        argv.append("-" + "v" * args.verbosity)
    if args.diff:
        argv.append("--diff")
    argv.append(args.playbook)
    return subprocess.run(argv, cwd=checkout, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("deadline", help="Start a shared network operation budget")
    bounded_parser = commands.add_parser(
        "bounded-command", help="Run once within a shared network operation budget"
    )
    bounded_parser.add_argument("argv", nargs=argparse.REMAINDER)
    command_parser = commands.add_parser(
        "command", help="Retry a safe download/install command"
    )
    command_parser.add_argument("argv", nargs=argparse.REMAINDER)
    pull_parser = commands.add_parser("pull", help="Retry checkout, then apply once")
    pull_parser.add_argument(
        "executable", help="Original ansible-pull executable (selects its sibling CLIs)"
    )
    pull_parser.add_argument("--url", required=True)
    pull_parser.add_argument("--checkout", required=True)
    pull_parser.add_argument("--directory", required=True)
    pull_parser.add_argument("--inventory", required=True)
    pull_parser.add_argument("--clean", action="store_true")
    pull_parser.add_argument("--diff", action="store_true")
    pull_parser.add_argument("-v", dest="verbosity", action="count", default=0)
    pull_parser.add_argument("playbook")
    args = parser.parse_args()
    if args.mode == "deadline":
        print(command_deadline())
        return 0
    if args.mode == "pull":
        return pull(args)
    if not args.argv:
        parser.error("command requires a command to run")
    if args.mode == "bounded-command":
        return bounded_command(args.argv)
    return retry_command(args.argv)


if __name__ == "__main__":
    sys.exit(main())
