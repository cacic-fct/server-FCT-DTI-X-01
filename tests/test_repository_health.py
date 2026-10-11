import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from test_ara import ROOT, telemetry

spec = importlib.util.spec_from_file_location(
    "repository_health", ROOT / "roles/ansible_pull/files/repository-deployment-health.py"
)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)

REVISION = "a" * 40
INFRA_REVISION = "b" * 40
LOG_URL = "https://ansible.cacic.com.br/playbooks/71.html"
ENTRY = {
    "repository": "cacic-fct/example",
    "directory": "example",
    "project": "example-project",
    "revision_service": "backend",
    "services": {
        "backend": "ghcr.io/cacic-fct/example/backend",
        "frontend": "ghcr.io/cacic-fct/example/frontend",
    },
    "dependencies": ["db"],
}
CONFIG = {
    "schema": 1,
    "server_id": "FCT-DTI-X-01",
    "runner_hostname": "secompp",
    "compose_root": "/home/shared/docker-compose",
    "repositories": [ENTRY],
}


def container(service, number, state=None, image=None):
    return {
        "Id": f"{number:064x}",
        "Image": f"sha256:{number:064x}",
        "Config": {
            "Image": image or f"ghcr.io/cacic-fct/example/{service}:latest",
            "Env": ["TOKEN=private-inspection-value"],
            "Labels": {
                "com.docker.compose.project": "example-project",
                "com.docker.compose.service": service,
                "com.docker.compose.project.working_dir": "/home/shared/docker-compose/example",
                "com.docker.compose.oneoff": "False",
            },
        },
        "State": state or {"Running": True, "Status": "running", "Health": {"Status": "healthy"}},
    }


class RepositoryReports(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "GITHUB_REPOSITORY_HEALTH_CONFIG": "/test/config.json",
                "SERVER_ID": "FCT-DTI-X-01",
                "INVOCATION_ID": "c" * 32,
                "ANSIBLE_PULL_ARA_VENV": "/test/ara",
                "GITHUB_DEPLOYMENT_ARA_RUN_LABEL": "ansible-pull-" + "d" * 32,
                "ANSIBLE_PULL_CHECKOUT": "/test/checkout",
                "ANSIBLE_PULL_REPO": "https://github.com/cacic-fct/server.git",
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.api = SimpleNamespace(
            WRAPPER_SHA256="e" * 64,
            warn=Mock(),
            checkout_revision=Mock(return_value=INFRA_REVISION),
            repo_owner_name=telemetry.repo_owner_name,
            github_token=Mock(return_value="test-token"),
            request_json=Mock(side_effect=self.request),
        )
        self.config = copy.deepcopy(CONFIG)
        self.containers = [
            container("backend", 1),
            container("frontend", 2),
            container("db", 3, image="postgres:18"),
        ]
        self.images = {}
        for item in self.containers[:2]:
            path = health.image_repository(item["Config"]["Image"])
            self.images[item["Image"]] = {
                "Id": item["Image"],
                "RepoDigests": [path + "@sha256:" + "f" * 64],
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.source": "https://github.com/cacic-fct/example",
                        "org.opencontainers.image.revision": REVISION,
                    }
                },
            }
        self.command_patch = patch.object(health, "command", side_effect=self.command)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    def request(self, method, path, token, payload=None, **kwargs):
        if method == "GET":
            if "/commits/" not in path:
                return {"full_name": path.removeprefix("/repos/")}
            return {"sha": path.rsplit("/", 1)[1]}
        return {"id": 123}

    def command(self, argv, **kwargs):
        if argv[:2] == ["docker", "ps"]:
            return "\n".join(item["Id"] for item in self.containers)
        if argv[:3] == ["docker", "container", "inspect"]:
            return json.dumps(self.containers)
        if argv[:3] == ["docker", "image", "inspect"]:
            return json.dumps([self.images[argv[3]]])
        if argv[0] == "git":
            if "status" in argv:
                return ""
            return "https://github.com/cacic-fct/server.git"
        if argv[0] == "systemctl":
            return f"InvocationID={'c' * 32}\nMainPID={os.getpid()}"
        return "71"

    def reporter(self, failed=None):
        with patch.object(
            health, "load_config", return_value=copy.deepcopy(self.config)
        ), patch.object(health.socket, "gethostname", return_value="secompp"), patch.object(
            health, "fingerprint", return_value="1" * 64
        ):
            reporter = health.Reporter(self.api)
        self.addCleanup(reporter.close)
        reporter.deadline = time.monotonic() + 90
        reporter.results_path.write_text(
            json.dumps(
                {
                    "run_id": reporter.run_id,
                    "projects": ["example"],
                    "failed_projects": failed or [],
                }
            )
        )
        return reporter

    def posts(self):
        return [call for call in self.api.request_json.call_args_list if call.args[0] == "POST"]

    def test_one_overall_record_contains_only_safe_attributable_metadata(self):
        reporter = self.reporter()
        reporter.report()
        deployment, status = self.posts()
        payload = deployment.args[3]
        self.assertEqual(payload["environment"], "production/FCT-DTI-X-01")
        self.assertEqual(payload["ref"], REVISION)
        identity = payload["payload"]
        self.assertEqual(identity["run_id"], reporter.run_id)
        self.assertEqual(identity["systemd_invocation_id"], "c" * 32)
        self.assertEqual(identity["wrapper_sha256"], "e" * 64)
        self.assertEqual(identity["infrastructure_revision"], INFRA_REVISION)
        self.assertEqual(identity["ara_playbook_id"], 71)
        self.assertEqual(identity["ara_url"], LOG_URL)
        self.assertEqual(len(identity["services"]), 3)
        self.assertNotIn("private-inspection-value", json.dumps(payload))
        self.assertEqual(status.args[3]["state"], "success")
        self.assertEqual(status.args[3]["log_url"], LOG_URL)
        self.assertFalse(status.args[3]["auto_inactive"])
        self.api.github_token.assert_called_once_with(
            "https://github.com/cacic-fct/example.git", timeout=unittest.mock.ANY
        )

    def test_unhealthy_dependency_and_missing_service_fail_overall_health(self):
        reporter = self.reporter()
        for condition in ["unhealthy", "missing", "restarting", "oom"]:
            with self.subTest(condition=condition):
                self.api.request_json.reset_mock()
                original = copy.deepcopy(self.containers)
                if condition == "missing":
                    self.containers.pop(1)
                elif condition == "restarting":
                    self.containers[2]["State"] = {"Running": True, "Status": "restarting"}
                elif condition == "oom":
                    self.containers[2]["State"]["OOMKilled"] = True
                else:
                    self.containers[2]["State"]["Health"]["Status"] = "unhealthy"
                reporter.report()
                self.assertEqual(self.posts()[1].args[3]["state"], "failure")
                self.containers = original

    def test_starting_healthcheck_reports_pending(self):
        self.containers[0]["State"]["Health"]["Status"] = "starting"
        self.reporter().report()
        self.assertEqual(self.posts()[1].args[3]["state"], "pending")

    def test_failed_apply_reports_observed_revision_instead_of_guessing_attempted_ref(self):
        self.reporter(failed=["example"]).report()
        deployment, status = self.posts()
        self.assertEqual(deployment.args[3]["ref"], REVISION)
        self.assertTrue(deployment.args[3]["payload"]["compose_apply_failed"])
        self.assertEqual(status.args[3]["state"], "failure")
        self.assertIn("observed container image", status.args[3]["description"])

    def test_completed_migration_is_healthy_only_for_explicit_oneshot(self):
        self.config["repositories"][0]["completed_services"] = ["frontend"]
        self.containers[1]["State"] = {"Running": False, "Status": "exited", "ExitCode": 0}
        reporter = self.reporter()
        reporter.report()
        self.assertEqual(self.posts()[1].args[3]["state"], "success")
        self.containers[1]["State"]["ExitCode"] = 1
        self.api.request_json.reset_mock()
        reporter.report()
        self.assertEqual(self.posts()[1].args[3]["state"], "failure")

    def test_different_package_revisions_are_recorded_under_one_repository(self):
        self.images[self.containers[1]["Image"]]["Config"]["Labels"][
            "org.opencontainers.image.revision"
        ] = ("9" * 40)
        self.reporter().report()
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.posts()[0].args[3]["ref"], REVISION)
        self.assertEqual(
            {
                item["revision"]
                for item in self.posts()[0].args[3]["payload"]["services"]
                if "revision" in item
            },
            {REVISION, "9" * 40},
        )

    def test_unknown_image_source_revision_digest_or_compose_owner_is_never_published(self):
        reporter = self.reporter()
        for condition in [
            "source",
            "revision",
            "digest",
            "image",
            "directory",
            "primary_missing",
            "replica_revision",
        ]:
            with self.subTest(condition=condition):
                original_containers, original_images = copy.deepcopy(
                    self.containers
                ), copy.deepcopy(self.images)
                image = self.images[self.containers[0]["Image"]]
                if condition == "source":
                    image["Config"]["Labels"][
                        "org.opencontainers.image.source"
                    ] = "https://github.com/other/example"
                elif condition == "revision":
                    image["Config"]["Labels"]["org.opencontainers.image.revision"] = "latest"
                elif condition == "digest":
                    image["RepoDigests"] = ["ghcr.io/other/example@sha256:" + "f" * 64]
                elif condition == "image":
                    self.containers[0]["Config"]["Image"] = "ghcr.io/cacic-fct/unmapped:latest"
                elif condition == "directory":
                    self.containers[0]["Config"]["Labels"][
                        "com.docker.compose.project.working_dir"
                    ] = "/unrelated"
                elif condition == "primary_missing":
                    self.containers.pop(0)
                else:
                    replica = container("backend", 4)
                    self.containers.append(replica)
                    self.images[replica["Image"]] = copy.deepcopy(image)
                    self.images[replica["Image"]]["Id"] = replica["Image"]
                    self.images[replica["Image"]]["Config"]["Labels"][
                        "org.opencontainers.image.revision"
                    ] = ("9" * 40)
                self.api.request_json.reset_mock()
                reporter.report()
                self.assertEqual(self.posts(), [])
                self.containers, self.images = original_containers, original_images

    def test_unknown_containers_and_oneoff_jobs_do_not_create_repository_entries(self):
        self.containers.append(container("unmapped", 8, image="ghcr.io/other/unmapped:latest"))
        oneoff = container("backend", 9)
        oneoff["Config"]["Labels"]["com.docker.compose.oneoff"] = "True"
        self.containers.append(oneoff)
        self.reporter().report()
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(len(self.posts()[0].args[3]["payload"]["services"]), 3)

    def test_missing_or_stale_handoff_and_missing_ara_prevent_false_records(self):
        reporter = self.reporter()
        for condition in ["stale", "missing", "unattempted", "ara_missing"]:
            with self.subTest(condition=condition):
                reporter.results_path.write_text(
                    json.dumps(
                        {
                            "run_id": reporter.run_id,
                            "projects": ["example"],
                            "failed_projects": [],
                        }
                    )
                )
                if condition == "missing":
                    reporter.results_path.unlink()
                elif condition == "stale":
                    reporter.results_path.write_text('{"run_id":"previous-run"}')
                elif condition == "unattempted":
                    reporter.results_path.write_text(
                        json.dumps(
                            {"run_id": reporter.run_id, "projects": [], "failed_projects": []}
                        )
                    )
                else:
                    os.environ.pop("GITHUB_DEPLOYMENT_ARA_RUN_LABEL")
                self.api.request_json.reset_mock()
                try:
                    reporter.report()
                except (ValueError, FileNotFoundError):
                    pass
                self.assertEqual(self.posts(), [])

    def test_unresolvable_commit_never_falls_back_to_branch(self):
        self.api.request_json.side_effect = lambda method, path, *args, **kwargs: (
            {"sha": "another-commit"} if "/commits/" in path else {"full_name": "cacic-fct/example"}
        )
        self.reporter().report()
        self.assertEqual(self.posts(), [])

    def test_unmapped_owned_services_prevent_incomplete_overall_success(self):
        self.containers.append(container("new-unmapped-app", 8))
        self.reporter().report()
        self.assertEqual(self.posts(), [])

    def test_wrong_systemd_invocation_or_dirty_checkout_prevents_publication(self):
        reporter = self.reporter()
        for condition in ["invocation", "checkout"]:

            def output(argv, **kwargs):
                if condition == "invocation" and argv[0] == "systemctl":
                    return "InvocationID=another-run\nMainPID=123"
                if condition == "checkout" and "status" in argv:
                    return " M site.yml"
                return self.command(argv)

            with self.subTest(condition=condition), patch.object(
                health, "command", side_effect=output
            ):
                with self.assertRaises(ValueError):
                    reporter.report()
                self.assertEqual(self.posts(), [])

    def test_validation_failure_is_attributed_only_to_failed_projects(self):
        reporter = self.reporter()
        reporter.results_path.write_text(
            json.dumps(
                {
                    "run_id": reporter.run_id,
                    "phase": "validation",
                    "projects": ["example"],
                    "failed_projects": ["example"],
                }
            )
        )
        reporter.report()
        deployment, status = self.posts()
        self.assertFalse(deployment.args[3]["payload"]["compose_apply_failed"])
        self.assertTrue(deployment.args[3]["payload"]["compose_validation_failed"])
        self.assertEqual(status.args[3]["state"], "failure")
        self.assertIn("validation failed", status.args[3]["description"])

    def test_changed_github_repository_owner_is_never_attributed_to_cacic(self):
        self.api.request_json.side_effect = lambda *args, **kwargs: {"full_name": "other/example"}
        self.reporter().report()
        self.assertEqual(self.posts(), [])

    def test_github_failure_is_private_and_does_not_prevent_other_repository_reports(self):
        second = copy.deepcopy(ENTRY)
        second.update(repository="cacic-fct/second", directory="second", project="second-project")
        self.config["repositories"].append(second)
        reporter = self.reporter()
        reporter.results_path.write_text(
            json.dumps(
                {
                    "run_id": reporter.run_id,
                    "projects": ["example", "second"],
                    "failed_projects": [],
                }
            )
        )
        with patch.object(
            reporter, "report_repository", side_effect=[RuntimeError("private-api-response"), None]
        ) as report:
            reporter.report()
        self.assertEqual(report.call_count, 2)
        self.assertNotIn("private-api-response", str(self.api.warn.call_args_list))

    def test_reporting_budget_bounds_docker_and_api_operations(self):
        reporter = self.reporter()
        reporter.deadline = time.monotonic() - 1
        with self.assertRaises(ValueError):
            reporter.inspect_repository(ENTRY, False)
        self.assertEqual(self.posts(), [])

    def test_unknown_runner_or_missing_invocation_rejects_initialization(self):
        for hostname, invocation in [("unknown", "c" * 32), ("secompp", "")]:
            with self.subTest(hostname=hostname, invocation=invocation), patch.object(
                health, "load_config", return_value=copy.deepcopy(CONFIG)
            ), patch.object(health.socket, "gethostname", return_value=hostname), patch.dict(
                os.environ, {"INVOCATION_ID": invocation}
            ):
                with self.assertRaises(ValueError):
                    health.Reporter(self.api)


class MappingValidation(unittest.TestCase):
    def test_mapping_covers_every_current_owned_package_and_actual_compose_identity(self):
        variables = yaml.safe_load(
            (ROOT / "host_vars/fct-dti-x-01/95-deployment-health.yml").read_text()
        )
        entries = variables["ansible_pull_repository_health_repositories"]
        health.validate_config({**CONFIG, "repositories": entries})
        mapped = set()
        found = set()
        active = set(
            yaml.safe_load((ROOT / "host_vars/fct-dti-x-01/90-compose-projects.yml").read_text())[
                "compose_projects"
            ]
        )
        for entry in entries:
            compose = yaml.safe_load(
                (ROOT / "docker-compose" / entry["directory"] / "docker-compose.yml").read_text()
            )
            self.assertEqual(entry["project"], compose.get("name", entry["directory"]))
            self.assertTrue(set(entry["dependencies"]) <= set(compose["services"]))
            for service, image in entry["services"].items():
                self.assertEqual(
                    health.image_repository(compose["services"][service]["image"]), image
                )
                mapped.add((entry["directory"], service))
        for path in (ROOT / "docker-compose").glob("*/docker-compose.yml"):
            if path.parent.name not in active:
                continue
            for service, values in yaml.safe_load(path.read_text())["services"].items():
                if values.get("image", "").startswith("ghcr.io/cacic-fct/"):
                    found.add((path.parent.name, service))
        self.assertEqual(mapped, found)

    def test_unknown_owner_duplicate_attribution_and_path_traversal_are_rejected(self):
        for field, value in [
            ("repository", "other/example"),
            ("directory", "../example"),
            ("project", "--option"),
            ("revision_service", "unknown"),
        ]:
            with self.subTest(field=field):
                config = copy.deepcopy(CONFIG)
                config["repositories"][0][field] = value
                with self.assertRaises(ValueError):
                    health.validate_config(config)
        duplicate = copy.deepcopy(CONFIG)
        duplicate["repositories"].append(copy.deepcopy(ENTRY))
        with self.assertRaises(ValueError):
            health.validate_config(duplicate)


class OutcomeHandoff(unittest.TestCase):
    def test_actual_ansible_handoff_is_fresh_and_nonblocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy.yml").write_text((ROOT / "roles/compose/tasks/deploy.yml").read_text())
            # Exercise the real orchestration/handoff without accessing production Docker.
            (root / "deploy-project.yml").write_text("""---
- name: Simulate a terminal project result
  ansible.builtin.set_fact:
    compose_project_deployment_failures: "{{ compose_project_deployment_failures + [{'project': compose_project}] }}"
  when: simulate_failure and compose_project == 'second'
""")
            (root / "playbook.yml").write_text("""---
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    compose_projects: [example, second]
  tasks:
    - ansible.builtin.include_tasks: deploy.yml
""")
            for failed, writable in [(False, True), (True, True), (False, False), (True, False)]:
                with self.subTest(failed=failed, writable=writable):
                    destination = root / (
                        "outcomes.json" if writable else "nonexistent/outcomes.json"
                    )
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "ansible.cli.playbook",
                            "-i",
                            "localhost,",
                            "playbook.yml",
                            "-e",
                            json.dumps({"simulate_failure": failed}),
                        ],
                        cwd=root,
                        env={
                            **os.environ,
                            "ANSIBLE_BECOME": "false",
                            "GITHUB_DEPLOYMENT_RUN_ID": "fresh-run",
                            "GITHUB_DEPLOYMENT_RESULTS_PATH": str(destination),
                        },
                        capture_output=True,
                        text=True,
                        timeout=90,
                    )
                    self.assertEqual(
                        result.returncode, 2 if failed else 0, result.stdout + result.stderr
                    )
                    if writable:
                        data = json.loads(destination.read_text())
                        self.assertEqual(
                            data,
                            {
                                "run_id": "fresh-run",
                                "phase": "apply",
                                "projects": ["example", "second"],
                                "failed_projects": ["second"] if failed else [],
                            },
                        )


class WrapperFailsafe(unittest.TestCase):
    def test_reporting_and_recorder_failures_preserve_success_and_failure_exit_codes(self):
        for outcome in [0, 2]:
            with self.subTest(outcome=outcome), patch.dict(
                os.environ, {"GITHUB_DEPLOYMENT_TELEMETRY_ENABLED": "true"}, clear=True
            ), patch.object(telemetry, "configure_ara", side_effect=RuntimeError), patch.object(
                telemetry, "configure_repository_health"
            ) as configure, patch.object(
                telemetry, "run_ansible_pull", return_value=outcome
            ), patch.object(
                telemetry, "checkout_revision", side_effect=OSError
            ):
                reporter = configure.return_value
                reporter.report.side_effect = TimeoutError
                reporter.close.side_effect = OSError
                self.assertEqual(telemetry.main(), outcome)
                reporter.report.assert_called_once()
                reporter.close.assert_called_once()

    def test_original_wrapper_exception_survives_telemetry_and_cleanup_errors(self):
        original = OSError("deployment launch failed")
        with patch.dict(
            os.environ, {"GITHUB_DEPLOYMENT_TELEMETRY_ENABLED": "true"}, clear=True
        ), patch.object(telemetry, "configure_ara"), patch.object(
            telemetry, "configure_repository_health"
        ) as configure, patch.object(
            telemetry, "run_ansible_pull", side_effect=original
        ), patch.object(
            telemetry, "checkout_revision", side_effect=RuntimeError
        ):
            configure.return_value.close.side_effect = OSError
            with self.assertRaises(OSError) as caught:
                telemetry.main()
            self.assertIs(caught.exception, original)

    def test_helper_import_failure_does_not_interrupt_deployment(self):
        with patch.dict(
            os.environ, {"GITHUB_REPOSITORY_HEALTH_CONFIG": "/missing"}, clear=True
        ), patch.object(telemetry.importlib.util, "spec_from_file_location", side_effect=OSError):
            self.assertIsNone(telemetry.configure_repository_health())

    def test_failed_diagnostics_do_not_replace_deployment_failure(self):
        with patch.object(
            telemetry.subprocess, "run", return_value=SimpleNamespace(returncode=2)
        ), patch.object(telemetry, "print_failure_diagnostics", side_effect=RuntimeError):
            self.assertEqual(telemetry.run_ansible_pull(), 2)


if __name__ == "__main__":
    unittest.main()
