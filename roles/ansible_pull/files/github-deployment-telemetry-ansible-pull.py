#!/usr/bin/env python3
import hashlib
import http.client
import importlib.util
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request
import uuid

from network_retry import TRANSIENT_HTTP_STATUSES, failed_before_request, retry_http


GITHUB_API = "https://api.github.com"
USER_AGENT = "server-FCT-DTI-X-01-ansible-pull-deployment-telemetry"
# The playbook can replace this file while this process is still running.
with open(__file__, "rb") as source:
    WRAPPER_SHA256 = hashlib.sha256(source.read()).hexdigest()


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


def request_json(method, path, token, payload=None, timeout=None):
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
    if timeout is None:
        timeout = env_int("NETWORK_RETRY_HTTP_BUDGET_SECONDS", 360)

    creating_deployment = method == "POST" and path.endswith("/deployments")
    uncertain_creation = False

    def send(remaining):
        nonlocal uncertain_creation
        if uncertain_creation:
            # A lost POST response may mean the deployment already exists.
            # Reconcile by this invocation's exact payload; never POST again
            # after an ambiguous result, even if the listing is still empty.
            query = urllib.parse.urlencode({
                "environment": payload["environment"], "sha": payload["ref"], "per_page": 100,
            })
            lookup = urllib.request.Request(
                f"{GITHUB_API}{path}?{query}", headers=headers, method="GET",
            )
            with urllib.request.urlopen(lookup, timeout=remaining) as response:
                deployments = json.loads(response.read().decode("utf-8"))
            for deployment in deployments:
                if deployment.get("payload") == payload.get("payload"):
                    return deployment
            raise urllib.error.URLError("Awaiting confirmation of deployment creation")
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # A rate-limit rejection did not create a deployment. Retry that
            # POST after Retry-After; reconcile only potentially accepted writes.
            if creating_deployment and exc.code in TRANSIENT_HTTP_STATUSES - {429}:
                uncertain_creation = True
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            if creating_deployment and not failed_before_request(exc):
                uncertain_creation = True
            raise

    try:
        return retry_http(send, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub API {method} failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise RuntimeError(f"GitHub API {method} failed after network retry budget") from exc


def github_token(repo_url=None, timeout=None):
    if timeout is None:
        timeout = env_int("NETWORK_RETRY_HTTP_BUDGET_SECONDS", 360) + 30
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
        argv += ["--repo-url", repo_url or env("GITHUB_DEPLOYMENT_REPO", env("ANSIBLE_PULL_REPO"))]

    argv += ["--private-key", private_key]

    result = subprocess.run(
        argv,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def create_deployment(token, owner, repo, revision):
    environment = env("GITHUB_DEPLOYMENT_ENVIRONMENT", "production")
    payload = {
        "ref": revision,
        "environment": environment,
        "description": f"Apply {environment} on {env('SERVER_ID', 'server')}",
        "auto_merge": False,
        "required_contexts": [],
        "payload": {
            "report_id": uuid.uuid4().hex,
            "runner_hostname": socket.gethostname(),
            "wrapper_sha256": WRAPPER_SHA256,
            "systemd_invocation_id": env("INVOCATION_ID"),
        },
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
        env("ANSIBLE_PULL_EXECUTABLE", "/usr/bin/ansible-pull"),
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


def configure_ara():
    """Enable the offline recorder only after its repository-driven bootstrap."""
    os.environ.pop("GITHUB_DEPLOYMENT_ARA_RUN_LABEL", None)
    if not enabled(env("ANSIBLE_PULL_ARA_ENABLED", "false")):
        return

    venv = env("ANSIBLE_PULL_ARA_VENV", "/opt/ara/venv")
    try:
        result = subprocess.run(
            [f"{venv}/bin/python", "-m", "ara.setup.callback_plugins"],
            check=True, capture_output=True, text=True, timeout=180,
        )
        callback_path = result.stdout.strip()
        if not callback_path or not os.path.isfile(f"{venv}/bin/ansible-pull"):
            raise RuntimeError("ARA Ansible entry points are not installed")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        warn(f"ARA recorder unavailable; using distribution Ansible: {exc}")
        return

    os.environ["ANSIBLE_PULL_EXECUTABLE"] = f"{venv}/bin/ansible-pull"
    os.environ["ANSIBLE_PULL_ARA_VENV"] = venv
    previous_paths = env("ANSIBLE_CALLBACK_PLUGINS")
    os.environ["ANSIBLE_CALLBACK_PLUGINS"] = os.pathsep.join(
        path for path in (callback_path, previous_paths) if path
    )
    run_label = f"ansible-pull-{uuid.uuid4().hex}"
    os.environ["GITHUB_DEPLOYMENT_ARA_RUN_LABEL"] = run_label
    os.environ["ARA_DEFAULT_LABELS"] = ",".join(
        label for label in (env("ARA_DEFAULT_LABELS"), run_label) if label
    )
    report_url = env("GITHUB_DEPLOYMENT_ENVIRONMENT_URL")
    if report_url:
        os.environ["GITHUB_DEPLOYMENT_LOG_URL"] = (
            f"{report_url.rstrip('/')}/?{urllib.parse.urlencode({'label': run_label})}"
        )


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
    runner = env("NETWORK_RETRY_HELPER", "/usr/local/libexec/ansible-pull/network_retry.py")
    return_code = subprocess.run([runner, "pull", *argv]).returncode
    if return_code != 0:
        try:
            print_failure_diagnostics(return_code)
        except Exception:
            warn("failure diagnostics unavailable; preserving ansible-pull exit code")
    return return_code


def checkout_revision(timeout=180):
    checkout = env("ANSIBLE_PULL_CHECKOUT")
    if not checkout:
        return None

    result = subprocess.run(
        ["git", "-C", checkout, "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def warn(message):
    print(f"GitHub deployment telemetry warning: {message}", file=sys.stderr)


def report_deployment(revision, state, description):
    try:
        owner, repo = repo_owner_name(
            env("GITHUB_DEPLOYMENT_REPO", env("ANSIBLE_PULL_REPO"))
        )
        token = github_token()
        deployment = create_deployment(token, owner, repo, revision)
        create_status(
            token,
            owner,
            repo,
            deployment["id"],
            state,
            description,
        )
    except Exception as exc:
        warn(f"could not report deployment telemetry: {exc}")


def configure_repository_health():
    if not env("GITHUB_REPOSITORY_HEALTH_CONFIG"):
        return None
    try:
        path = os.path.join(os.path.dirname(__file__), "repository-deployment-health.py")
        spec = importlib.util.spec_from_file_location("repository_health", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        api = SimpleNamespace(
            WRAPPER_SHA256=WRAPPER_SHA256, warn=warn, checkout_revision=checkout_revision,
            repo_owner_name=repo_owner_name, github_token=github_token, request_json=request_json,
        )
        return module.Reporter(api)
    except Exception:
        warn("repository health initialization unavailable; deployment continues")
        return None


def report_server_result(state, description):
    try:
        revision = checkout_revision()
        if revision:
            report_deployment(revision, state, description)
        else:
            warn("no checkout revision available; skipping server deployment telemetry")
    except Exception:
        warn("server deployment telemetry unavailable; preserving ansible-pull outcome")


def main():
    try:
        configure_ara()
    except Exception:
        warn("ARA setup unavailable; deployment continues")
    telemetry_enabled = enabled(env("GITHUB_DEPLOYMENT_TELEMETRY_ENABLED", "false"))
    reporter = configure_repository_health() if telemetry_enabled else None

    try:
        return_code = run_ansible_pull()
    except Exception as exc:
        if telemetry_enabled:
            report_server_result("error", f"ansible-pull wrapper errored: {exc}")
        if reporter:
            try:
                reporter.close()
            except Exception:
                warn("repository health cleanup unavailable")
        raise

    if not telemetry_enabled:
        return return_code

    report_server_result(
        "success" if return_code == 0 else "failure",
        "ansible-pull finished successfully"
        if return_code == 0
        else f"ansible-pull failed with exit code {return_code}",
    )

    if reporter:
        try:
            reporter.report()
        except Exception:
            warn("repository health reporting unavailable; preserving ansible-pull outcome")
        finally:
            try:
                reporter.close()
            except Exception:
                warn("repository health cleanup unavailable")

    return return_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
