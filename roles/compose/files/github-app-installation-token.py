#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from network_retry import retry_http

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

GITHUB_API = "https://api.github.com"
USER_AGENT = "server-FCT-DTI-X-01-ansible-github-app"
REQUEST_DEADLINE = time.monotonic() + int(
    os.environ.get("NETWORK_RETRY_HTTP_BUDGET_SECONDS", "360")
)
if os.environ.get("NETWORK_RETRY_DEADLINE_MONOTONIC"):
    REQUEST_DEADLINE = min(
        REQUEST_DEADLINE,
        float(os.environ["NETWORK_RETRY_DEADLINE_MONOTONIC"]),
    )


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def request_json(method, url, headers=None, payload=None):
    def send(timeout):
        request_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        # Refresh the JWT on every attempt; a long outage must not leave us
        # retrying with an expired app credential.
        request_headers.update(headers() if callable(headers) else (headers or {}))
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, data=data, headers=request_headers, method=method
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        return retry_http(
            send,
            timeout=REQUEST_DEADLINE - time.monotonic(),
        )
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"GitHub API request failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise SystemExit(
            "GitHub API request failed after network retry budget"
        ) from exc


def load_private_key(path):
    with open(path, "rb") as key_file:
        return serialization.load_pem_private_key(key_file.read(), password=None)


def create_jwt(app_id, private_key_path):
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - 60,
        "exp": now + 540,
        "iss": str(app_id),
    }

    signing_input = ".".join(
        [
            b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    ).encode("ascii")

    private_key = load_private_key(private_key_path)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode("ascii") + "." + b64url(signature)


def app_id_from_slug(app_slug):
    app = request_json("GET", f"{GITHUB_API}/apps/{urllib.parse.quote(app_slug)}")
    return app["id"]


def repo_owner_name(repo_url):
    if repo_url.startswith("git@github.com:"):
        path = repo_url.removeprefix("git@github.com:")
    else:
        parsed = urllib.parse.urlparse(repo_url)
        if parsed.netloc != "github.com":
            raise SystemExit(f"Unsupported GitHub repository URL: {repo_url}")
        path = parsed.path.lstrip("/")

    match = re.match(r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$", path)
    if not match:
        raise SystemExit(
            f"Could not parse GitHub repository owner/name from: {repo_url}"
        )
    return match.group("owner"), match.group("repo")


def installation_id_for_repo(jwt, repo_url):
    owner, repo = repo_owner_name(repo_url)
    installation = request_json(
        "GET",
        f"{GITHUB_API}/repos/{owner}/{repo}/installation",
        lambda: {"Authorization": f"Bearer {jwt()}"},
    )
    return installation["id"]


def installation_token(jwt, installation_id):
    token = request_json(
        "POST",
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        lambda: {"Authorization": f"Bearer {jwt()}"},
        {},
    )
    return token


def main():
    parser = argparse.ArgumentParser(
        description="Print a short-lived GitHub App installation token."
    )
    app = parser.add_mutually_exclusive_group(required=True)
    app.add_argument("--app-id")
    app.add_argument("--app-slug")
    installation = parser.add_mutually_exclusive_group()
    installation.add_argument("--installation-id")
    installation.add_argument("--repo-url")
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--json", action="store_true", help="Include the token expiry")
    args = parser.parse_args()

    app_id = args.app_id or app_id_from_slug(args.app_slug)

    def jwt():
        return create_jwt(app_id, args.private_key)

    installation_id = args.installation_id or installation_id_for_repo(
        jwt, args.repo_url
    )
    token = installation_token(jwt, installation_id)
    if args.json:
        print(json.dumps({"token": token["token"], "expires_at": token["expires_at"]}))
    else:
        print(token["token"])


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
