#!/usr/bin/env python3
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


GITHUB_API = "https://api.github.com"
USER_AGENT = "server-FCT-DTI-X-01-ansible-pull-deployment-telemetry"


def env(name, default=""):
    return os.environ.get(name, default).strip()


def enabled(value):
    return value.lower() in {"1", "true", "yes", "on"}


def env_int(name, default=0):
    value = env(name, str(default))
    try:
        return int(value)
    except ValueError:
        return default


def repo_owner_name(repo_url):
    if repo_url.startswith("git@github.com:"):
        path = repo_url.removeprefix("git@github.com:")
    else:
        parsed = urllib.parse.urlparse(repo_url)
        if parsed.netloc != "github.com":
            raise ValueError(f"Unsupported GitHub repository URL: {repo_url}")
        path = parsed.path.lstrip("/")

    match = re.match(r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$", path)
    if not match:
        raise ValueError(f"Could not parse GitHub repository owner/name from: {repo_url}")
    return match.group("owner"), match.group("repo")


def request_json(method, path, token, payload=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        f"{GITHUB_API}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API {method} {path} failed: {exc.code} {exc.reason}: {detail[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API {method} {path} failed: {exc.reason}") from exc


def github_token():
    helper = env("GITHUB_DEPLOYMENT_TOKEN_HELPER")
    private_key = env("GITHUB_DEPLOYMENT_APP_PRIVATE_KEY")
    if not helper or not private_key:
        raise ValueError("GitHub App token helper and private key path are required")

    argv = [helper]
    app_id = env("GITHUB_DEPLOYMENT_APP_ID")
    app_slug = env("GITHUB_DEPLOYMENT_APP_SLUG")
    if app_id:
        argv += ["--app-id", app_id]
    elif app_slug:
        argv += ["--app-slug", app_slug]
    else:
        raise ValueError("GitHub App id or slug is required")

    installation_id = env("GITHUB_DEPLOYMENT_APP_INSTALLATION_ID")
    if installation_id:
        argv += ["--installation-id", installation_id]
    else:
        argv += ["--repo-url", env("GITHUB_DEPLOYMENT_REPO", env("ANSIBLE_PULL_REPO"))]

    argv += ["--private-key", private_key]

    result = subprocess.run(
        argv,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def create_deployment(token, owner, repo):
    environment = env("GITHUB_DEPLOYMENT_ENVIRONMENT", "production")
    payload = {
        "ref": env("GITHUB_DEPLOYMENT_REF", env("ANSIBLE_PULL_VERSION", "production")),
        "environment": environment,
        "description": f"Apply {environment} on {env('SERVER_ID', 'server')}",
        "auto_merge": False,
        "required_contexts": [],
        "production_environment": enabled(env("GITHUB_DEPLOYMENT_PRODUCTION_ENVIRONMENT", "true")),
        "transient_environment": enabled(env("GITHUB_DEPLOYMENT_TRANSIENT_ENVIRONMENT", "false")),
    }
    return request_json("POST", f"/repos/{owner}/{repo}/deployments", token, payload)


def create_status(token, owner, repo, deployment_id, state, description):
    payload = {
        "state": state,
        "description": description[:140],
    }
    environment_url = env("GITHUB_DEPLOYMENT_ENVIRONMENT_URL")
    log_url = env("GITHUB_DEPLOYMENT_LOG_URL")
    if environment_url:
        payload["environment_url"] = environment_url
    if log_url:
        payload["log_url"] = log_url

    return request_json(
        "POST",
        f"/repos/{owner}/{repo}/deployments/{deployment_id}/statuses",
        token,
        payload,
    )


def ansible_pull_argv():
    argv = [
        "/usr/bin/ansible-pull",
        "--url",
        env("ANSIBLE_PULL_REPO"),
        "--checkout",
        env("ANSIBLE_PULL_VERSION"),
        "--directory",
        env("ANSIBLE_PULL_CHECKOUT"),
        "--clean",
        "--inventory",
        env("ANSIBLE_PULL_INVENTORY"),
    ]
    verbosity = max(0, min(env_int("ANSIBLE_PULL_VERBOSITY", 0), 6))
    if verbosity:
        argv.append("-" + ("v" * verbosity))
    if enabled(env("ANSIBLE_PULL_SHOW_DIFF", "false")):
        argv.append("--diff")
    argv.append(env("ANSIBLE_PULL_PLAYBOOK"))
    return argv


def print_diagnostic_command(title, argv, cwd=None):
    print(f"\n--- {title} ---", file=sys.stderr)
    print(f"$ {shlex.join(argv)}", file=sys.stderr)
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        print(f"diagnostic command could not run: {exc}", file=sys.stderr)
        return

    print(f"exit code: {result.returncode}", file=sys.stderr)
    if result.stdout:
        print("stdout:", file=sys.stderr)
        print(result.stdout.rstrip(), file=sys.stderr)
    if result.stderr:
        print("stderr:", file=sys.stderr)
        print(result.stderr.rstrip(), file=sys.stderr)


def print_failure_diagnostics(return_code):
    checkout = env("ANSIBLE_PULL_CHECKOUT")
    inventory = env("ANSIBLE_PULL_INVENTORY")
    playbook = env("ANSIBLE_PULL_PLAYBOOK")

    print("\n=== ansible-pull failure diagnostics ===", file=sys.stderr)
    print(f"exit code: {return_code}", file=sys.stderr)
    print(f"repo: {env('ANSIBLE_PULL_REPO')}", file=sys.stderr)
    print(f"ref: {env('ANSIBLE_PULL_VERSION')}", file=sys.stderr)
    print(f"checkout: {checkout}", file=sys.stderr)
    print(f"inventory: {inventory}", file=sys.stderr)
    print(f"playbook: {playbook}", file=sys.stderr)
    print(f"ansible-pull command: {shlex.join(ansible_pull_argv())}", file=sys.stderr)

    print_diagnostic_command("ansible version", ["/usr/bin/ansible", "--version"])
    if checkout:
        print_diagnostic_command("checkout git revision", ["git", "-C", checkout, "rev-parse", "--short", "HEAD"])
        print_diagnostic_command("checkout git status", ["git", "-C", checkout, "status", "--short"])
    if checkout and inventory and playbook:
        print_diagnostic_command(
            "inventory host match",
            [
                "/usr/bin/ansible-playbook",
                "--inventory",
                inventory,
                "--list-hosts",
                playbook,
            ],
            cwd=checkout,
        )
    print("=== end ansible-pull failure diagnostics ===\n", file=sys.stderr)


def run_ansible_pull():
    argv = ansible_pull_argv()
    print(f"Running ansible-pull: {shlex.join(argv)}", file=sys.stderr, flush=True)
    return_code = subprocess.run(argv).returncode
    if return_code != 0:
        print_failure_diagnostics(return_code)
    return return_code


def warn(message):
    print(f"GitHub deployment telemetry warning: {message}", file=sys.stderr)


def main():
    if not enabled(env("GITHUB_DEPLOYMENT_TELEMETRY_ENABLED", "false")):
        return run_ansible_pull()

    token = None
    owner = None
    repo = None
    deployment_id = None

    try:
        owner, repo = repo_owner_name(env("GITHUB_DEPLOYMENT_REPO", env("ANSIBLE_PULL_REPO")))
        token = github_token()
        deployment = create_deployment(token, owner, repo)
        deployment_id = deployment["id"]
        create_status(
            token,
            owner,
            repo,
            deployment_id,
            "in_progress",
            "ansible-pull started applying this ref",
        )
    except Exception as exc:
        warn(f"could not create an in-progress deployment telemetry record: {exc}")
        return run_ansible_pull()

    try:
        return_code = run_ansible_pull()
    except Exception as exc:
        try:
            create_status(token, owner, repo, deployment_id, "error", f"ansible-pull wrapper errored: {exc}")
        except Exception as status_exc:
            warn(f"could not mark deployment as error: {status_exc}")
        raise

    state = "success" if return_code == 0 else "failure"
    description = (
        "ansible-pull finished successfully"
        if return_code == 0
        else f"ansible-pull failed with exit code {return_code}"
    )
    try:
        create_status(token, owner, repo, deployment_id, state, description)
    except Exception as exc:
        warn(f"could not mark deployment as {state}: {exc}")

    return return_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
