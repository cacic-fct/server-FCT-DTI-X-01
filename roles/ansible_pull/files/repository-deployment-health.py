"""Publish an attributable health snapshot, once per explicitly mapped repository.

This is observational telemetry: the ref is the revision of revision_service's
actual container image, including when Compose failed to replace that image.
It is never an inferred revision of a failed pull or an undeployed Git branch.
"""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import uuid

SHA = re.compile(r"[0-9a-f]{40}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")
REPOSITORY = re.compile(r"cacic-fct/[a-zA-Z0-9][a-zA-Z0-9_.-]*")


def command(argv, timeout=180):
    # Docker inspection includes environment variables. Capture them privately;
    # never echo stdout/stderr (or exception text containing either) to ARA/logs.
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ValueError(f"{argv[0]} inspection unavailable") from None
    if result.returncode:
        raise ValueError(f"{argv[0]} inspection failed (exit {result.returncode})")
    return result.stdout.strip()


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes().strip()).hexdigest()


def load_config(path):
    config_path = Path(path)
    stat = config_path.stat()
    if config_path.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
        raise ValueError("health mapping must be root-owned and not writable by group/others")
    config = json.loads(config_path.read_text())
    validate_config(config)
    return config


def validate_config(config):
    if config.get("schema") != 1 or not NAME.fullmatch(config.get("server_id", "")):
        raise ValueError("invalid health mapping schema/server identity")
    if not config.get("runner_hostname") or not Path(config.get("compose_root", "")).is_absolute():
        raise ValueError("expected runner and absolute Compose root are required")
    for name, default in [("timeout_seconds", 900), ("operation_timeout_seconds", 180)]:
        if type(config.get(name, default)) is not int or config.get(name, default) <= 0:
            raise ValueError("reporting timeouts must be positive integers")
    repositories = set()
    projects = set()
    for entry in config["repositories"]:
        repo = entry["repository"]
        if not REPOSITORY.fullmatch(repo) or repo in repositories:
            raise ValueError("repository must be unique and explicitly belong to cacic-fct")
        repositories.add(repo)
        if not NAME.fullmatch(entry["directory"]) or not NAME.fullmatch(entry["project"]):
            raise ValueError("invalid Compose directory/project")
        if entry["project"] in projects:
            raise ValueError("a Compose project cannot be attributed to multiple repositories")
        projects.add(entry["project"])
        services = entry["services"]
        if entry["revision_service"] not in services:
            raise ValueError("revision service must be an explicitly mapped application")
        dependencies = entry.get("dependencies", [])
        completed = entry.get("completed_services", [])
        if set(dependencies) & set(services) or not set(completed) <= set(services):
            raise ValueError("invalid dependency/completed service mapping")
        for service in [*services, *dependencies]:
            if not NAME.fullmatch(service):
                raise ValueError("invalid service name")
        for image in services.values():
            if not re.fullmatch(r"ghcr\.io/cacic-fct/[a-z0-9][a-z0-9_./-]*", image):
                raise ValueError("images must be explicit untagged cacic-fct GHCR paths")


def image_repository(reference):
    return reference.split("@", 1)[0].split(":", 1)[0]


def container_health(container, completed=False):
    state = container["State"]
    if state.get("OOMKilled") or state.get("Dead"):
        return "failure"
    if completed:
        if state.get("Status") == "exited" and state.get("ExitCode") == 0:
            return "success"
        return "pending" if state.get("Running") else "failure"
    if not state.get("Running") or state.get("Status") != "running":
        return "failure"
    health = state.get("Health", {}).get("Status")
    if health == "starting":
        return "pending"
    return "success" if health in (None, "healthy") else "failure"


def aggregate_health(states):
    if "failure" in states:
        return "failure"
    return "pending" if "pending" in states else "success"


class Reporter:
    def __init__(self, telemetry):
        self.api = telemetry
        self.config = load_config(os.environ["GITHUB_REPOSITORY_HEALTH_CONFIG"])
        if socket.gethostname() != self.config["runner_hostname"]:
            raise ValueError("runner hostname does not match the explicit mapping")
        invocation = os.environ.get("INVOCATION_ID", "")
        if not re.fullmatch(r"[0-9a-f]{32}", invocation):
            raise ValueError("repository health requires a systemd invocation identity")
        if os.environ.get("SERVER_ID") != self.config["server_id"]:
            raise ValueError("server identity does not match the explicit mapping")
        self.run_id = uuid.uuid4().hex
        self.identity = {
            "source": "ansible-pull-repository-health-v1",
            "run_id": self.run_id,
            "server_id": self.config["server_id"],
            "runner_hostname": socket.gethostname(),
            "runner_machine_id_sha256": fingerprint("/etc/machine-id"),
            "runner_boot_id_sha256": fingerprint("/proc/sys/kernel/random/boot_id"),
            "systemd_unit": "ansible-pull-fct-dti-x-01.service",
            "systemd_invocation_id": invocation,
            "wrapper_sha256": telemetry.WRAPPER_SHA256,
            "reporter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "mapping_sha256": hashlib.sha256(
                json.dumps(self.config, sort_keys=True).encode()
            ).hexdigest(),
        }
        # Snapshot config and reporter before Ansible replaces their installed files.
        self.directory = tempfile.TemporaryDirectory(prefix="ansible-repository-health-")
        self.results_path = Path(self.directory.name) / "outcomes.json"
        os.environ["GITHUB_DEPLOYMENT_RUN_ID"] = self.run_id
        os.environ["GITHUB_DEPLOYMENT_RESULTS_PATH"] = str(self.results_path)

    def close(self):
        os.environ.pop("GITHUB_DEPLOYMENT_RUN_ID", None)
        os.environ.pop("GITHUB_DEPLOYMENT_RESULTS_PATH", None)
        self.directory.cleanup()

    def remaining_timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("repository health reporting time budget exhausted")
        return min(self.config.get("operation_timeout_seconds", 180), remaining)

    def command(self, argv):
        return command(argv, timeout=self.remaining_timeout())

    def request(self, method, path, token, payload=None):
        return self.api.request_json(method, path, token, payload, timeout=self.remaining_timeout())

    def ara_report_url(self):
        label = os.environ.get("GITHUB_DEPLOYMENT_ARA_RUN_LABEL", "")
        if not re.fullmatch(r"ansible-pull-[0-9a-f]{32}", label):
            raise ValueError("no ARA recorder identity for this run")
        # Resolve the actual recorded entry, rather than link to a guessed run.
        python = str(Path(os.environ["ANSIBLE_PULL_ARA_VENV"]) / "bin/python")
        script = """import os, sys
os.environ['DJANGO_SETTINGS_MODULE'] = 'ara.server.settings'
import django
django.setup()
from ara.api.models import Playbook
ids = list(Playbook.objects.filter(labels__name=sys.argv[1]).values_list('pk', flat=True))
if len(ids) != 1:
    sys.exit(1)
print(ids[0])
"""
        record_id = self.command([python, "-c", script, label])
        if not record_id.isdecimal():
            raise ValueError("invalid ARA playbook identity")
        self.identity["ara_playbook_id"] = int(record_id)
        self.identity["ara_run_label"] = label
        return f"https://ansible.cacic.com.br/playbooks/{record_id}.html"

    def inspect_repository(self, entry, compose_failed):
        expected = entry["services"]
        dependencies = entry.get("dependencies", [])
        by_service = {service: [] for service in [*expected, *dependencies]}
        ids = self.command(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={entry['project']}",
            ]
        ).splitlines()
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in ids):
            raise ValueError("invalid Docker container identity")
        containers = (
            json.loads(self.command(["docker", "container", "inspect", *ids])) if ids else []
        )
        working_dir = str(Path(self.config["compose_root"]) / entry["directory"])
        for container in containers:
            labels = container["Config"].get("Labels") or {}
            service = labels.get("com.docker.compose.service")
            if labels.get("com.docker.compose.oneoff") == "True":
                continue
            if service not in by_service:
                if container["Config"].get("Image", "").startswith("ghcr.io/cacic-fct/"):
                    raise ValueError(
                        "an owned service in this project is absent from the explicit mapping"
                    )
                continue
            if (
                labels.get("com.docker.compose.project") != entry["project"]
                or labels.get("com.docker.compose.project.working_dir") != working_dir
            ):
                raise ValueError("container Compose provenance does not match the mapping")
            by_service[service].append(container)

        snapshots = []
        images = {}
        for service, members in by_service.items():
            if not members:
                snapshots.append({"service": service, "state": "failure", "reason": "missing"})
            for container in members:
                snapshot = {
                    "service": service,
                    "container_id": container["Id"],
                    "state": container_health(
                        container, service in entry.get("completed_services", [])
                    ),
                }
                if service in expected:
                    image_id = container["Image"]
                    if (
                        not IMAGE_ID.fullmatch(image_id)
                        or image_repository(container["Config"]["Image"]) != expected[service]
                    ):
                        raise ValueError(
                            "container image does not match its explicitly mapped package"
                        )
                    if image_id not in images:
                        images[image_id] = json.loads(
                            self.command(["docker", "image", "inspect", image_id])
                        )[0]
                    image = images[image_id]
                    labels = image["Config"].get("Labels") or {}
                    revision = labels.get("org.opencontainers.image.revision", "")
                    source = labels.get("org.opencontainers.image.source", "").removesuffix(".git")
                    digests = [
                        digest
                        for digest in image.get("RepoDigests", [])
                        if re.fullmatch(
                            re.escape(expected[service]) + r"@sha256:[0-9a-f]{64}", digest
                        )
                    ]
                    if (
                        image["Id"] != image_id
                        or not SHA.fullmatch(revision)
                        or not digests
                        or source != f"https://github.com/{entry['repository']}"
                    ):
                        raise ValueError(
                            "image source, full revision, or registry digest is unverified"
                        )
                    snapshot.update(
                        image_id=image_id, image_digests=digests, revision=revision, source=source
                    )
                snapshots.append(snapshot)

        primary = [item for item in snapshots if item["service"] == entry["revision_service"]]
        revisions = {item.get("revision") for item in primary}
        if len(revisions) != 1 or None in revisions:
            raise ValueError("no unambiguous observed revision for the configured revision service")
        states = [item["state"] for item in snapshots]
        if compose_failed:
            states.append("failure")
        return revisions.pop(), aggregate_health(states), snapshots

    def report(self):
        # This runs after all Compose work and must not keep the unit busy indefinitely.
        self.deadline = time.monotonic() + self.config.get("timeout_seconds", 900)
        outcomes = json.loads(self.results_path.read_text())
        if outcomes.get("run_id") != self.run_id:
            raise ValueError("Compose outcomes belong to a different invocation")
        attempted = outcomes["projects"]
        failed = outcomes["failed_projects"]
        phase = outcomes.get("phase", "apply")
        if (
            not isinstance(attempted, list)
            or not isinstance(failed, list)
            or not all(isinstance(item, str) for item in [*attempted, *failed])
            or not set(failed) <= set(attempted)
            or phase not in ("apply", "validation")
            or (phase == "validation" and set(attempted) != set(failed))
        ):
            raise ValueError("invalid Compose outcome handoff")
        log_url = self.ara_report_url()
        unit = self.identity["systemd_unit"]
        properties = dict(
            line.split("=", 1)
            for line in self.command(
                [
                    "systemctl",
                    "show",
                    unit,
                    "--property=InvocationID",
                    "--property=MainPID",
                ]
            ).splitlines()
            if "=" in line
        )
        if properties.get("InvocationID") != self.identity[
            "systemd_invocation_id"
        ] or properties.get("MainPID") != str(os.getpid()):
            raise ValueError("this process is not the active systemd deployment invocation")
        self.identity["runner_pid"] = os.getpid()
        revision = self.api.checkout_revision(timeout=self.remaining_timeout())
        if not revision or not SHA.fullmatch(revision):
            raise ValueError("no full infrastructure checkout revision")
        origin = self.command(
            ["git", "-C", os.environ["ANSIBLE_PULL_CHECKOUT"], "remote", "get-url", "origin"]
        )
        repo_url = os.environ["ANSIBLE_PULL_REPO"]
        if self.api.repo_owner_name(origin) != self.api.repo_owner_name(repo_url):
            raise ValueError("infrastructure checkout origin does not match the configured source")
        if self.command(
            [
                "git",
                "-C",
                os.environ["ANSIBLE_PULL_CHECKOUT"],
                "status",
                "--porcelain",
                "--untracked-files=no",
            ]
        ):
            raise ValueError("infrastructure checkout has unrecorded tracked changes")
        self.identity.update(infrastructure_repository=repo_url, infrastructure_revision=revision)
        self.identity["compose_phase"] = phase
        for entry in self.config["repositories"]:
            try:
                if entry["directory"] not in attempted:
                    raise ValueError("project was not attempted in this invocation")
                self.report_repository(entry, entry["directory"] in failed, log_url)
            except Exception:
                # Do not include API bodies or Docker inspection data in warnings.
                self.api.warn(
                    f"repository health skipped for {entry['repository']}; attribution/reporting unavailable; {log_url}"
                )

    def report_repository(self, entry, compose_failed, log_url):
        revision, state, services = self.inspect_repository(entry, compose_failed)
        observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        owner, repo = entry["repository"].split("/")
        token = self.api.github_token(
            f"https://github.com/{entry['repository']}.git",
            timeout=self.remaining_timeout(),
        )
        repository = self.request("GET", f"/repos/{owner}/{repo}", token)
        if repository.get("full_name", "").lower() != entry["repository"].lower():
            raise ValueError("GitHub repository ownership does not match the explicit mapping")
        for observed in {item["revision"] for item in services if "revision" in item}:
            commit = self.request("GET", f"/repos/{owner}/{repo}/commits/{observed}", token)
            if commit.get("sha") != observed:
                raise ValueError(
                    "observed image revision does not resolve in the mapped repository"
                )
        environment = f"production/{self.config['server_id']}"
        deployment = self.request(
            "POST",
            f"/repos/{owner}/{repo}/deployments",
            token,
            {
                "ref": revision,
                "environment": environment,
                "description": f"Overall repository health on {self.config['server_id']}",
                "auto_merge": False,
                "required_contexts": [],
                "production_environment": True,
                "transient_environment": False,
                "payload": {
                    **self.identity,
                    "observed_at": observed_at,
                    "ref_service": entry["revision_service"],
                    "compose_project": entry["project"],
                    "compose_apply_failed": compose_failed
                    and self.identity["compose_phase"] == "apply",
                    "compose_validation_failed": compose_failed
                    and self.identity["compose_phase"] == "validation",
                    "services": services,
                    "ara_url": log_url,
                },
            },
        )
        description = "Overall health: " + state
        if compose_failed:
            description += f"; Compose {self.identity['compose_phase']} failed (ref identifies the observed container image)"
        self.request(
            "POST",
            f"/repos/{owner}/{repo}/deployments/{deployment['id']}/statuses",
            token,
            {
                "state": state,
                "description": description[:140],
                "log_url": log_url,
                "environment_url": log_url,
                "auto_inactive": False,
            },
        )
