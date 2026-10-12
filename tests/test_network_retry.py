import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import network_retry

spec = importlib.util.spec_from_file_location(
    "token_helper",
    ROOT / "roles/compose/files/github-app-installation-token.py",
)
token_helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(token_helper)

from test_ara import telemetry


def response(payload):
    result = Mock()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.read.return_value = json.dumps(payload).encode()
    return result


class HTTPRetries(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(
            os.environ,
            {
                "NETWORK_RETRY_ATTEMPTS": "3",
                "NETWORK_RETRY_DELAY_SECONDS": "0",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_dns_timeout_disconnect_and_server_failure_recover(self):
        for error in [
            urllib.error.URLError(socket.gaierror("offline")),
            TimeoutError(),
            ConnectionResetError(),
            urllib.error.HTTPError(
                "https://example.test", 503, "offline", {}, io.BytesIO()
            ),
        ]:
            with self.subTest(error=type(error)), patch.object(
                network_retry, "pause"
            ) as pause:
                operation = Mock(side_effect=[error, {"ok": True}])
                self.assertEqual(network_retry.retry_http(operation), {"ok": True})
                self.assertEqual(operation.call_count, 2)
                pause.assert_called_once()

    def test_bad_credentials_fail_immediately(self):
        error = urllib.error.HTTPError(
            "https://example.test", 401, "bad credentials", {}, None
        )
        operation = Mock(side_effect=error)
        with self.assertRaises(urllib.error.HTTPError):
            network_retry.retry_http(operation)
        operation.assert_called_once()
        error.close()

    def test_exhaustion_is_failure_and_respects_time_budget(self):
        operation = Mock(side_effect=TimeoutError())
        with patch.object(network_retry, "pause") as pause, self.assertRaises(
            TimeoutError
        ):
            network_retry.retry_http(operation)
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(pause.call_count, 2)
        operation.reset_mock()
        with self.assertRaises(TimeoutError):
            network_retry.retry_http(operation, timeout=0)
        operation.assert_not_called()

    def test_rate_limit_wait_cannot_exceed_budget(self):
        error = urllib.error.HTTPError(
            "https://example.test",
            429,
            "slow down",
            {"Retry-After": "600"},
            None,
        )
        with patch.object(network_retry, "pause") as pause, self.assertRaises(
            TimeoutError
        ):
            network_retry.retry_http(Mock(side_effect=error), timeout=10)
        pause.assert_not_called()

    def test_token_retry_refreshes_jwt_and_keeps_stdout_clean(self):
        credentials = Mock(
            side_effect=[
                {"Authorization": "Bearer first"},
                {"Authorization": "Bearer fresh"},
            ]
        )
        with patch.object(
            token_helper.urllib.request,
            "urlopen",
            side_effect=[
                TimeoutError(),
                response({"token": "test-token"}),
            ],
        ) as request, patch.object(network_retry, "pause"), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            self.assertEqual(
                token_helper.request_json(
                    "POST", "https://example.test", credentials, {}
                ),
                {"token": "test-token"},
            )
        self.assertEqual(
            request.call_args_list[0].args[0].get_header("Authorization"),
            "Bearer first",
        )
        self.assertEqual(
            request.call_args_list[1].args[0].get_header("Authorization"),
            "Bearer fresh",
        )
        self.assertEqual(output.getvalue(), "")

    def test_token_http_requests_cannot_reset_the_parent_deadline(self):
        for remaining in [2, -1]:
            deadline = time.monotonic() + remaining
            with self.subTest(remaining=remaining), patch.dict(
                os.environ, {"NETWORK_RETRY_DEADLINE_MONOTONIC": str(deadline)}
            ):
                spec = importlib.util.spec_from_file_location(
                    "bounded_token_helper",
                    ROOT / "roles/compose/files/github-app-installation-token.py",
                )
                helper = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(helper)
                self.assertEqual(helper.REQUEST_DEADLINE, deadline)
                with patch.object(
                    helper.urllib.request,
                    "urlopen",
                    return_value=response({"ok": True}),
                ) as request:
                    if remaining > 0:
                        self.assertEqual(
                            helper.request_json("GET", "https://example.test"),
                            {"ok": True},
                        )
                        self.assertLessEqual(
                            request.call_args.kwargs["timeout"], remaining
                        )
                    else:
                        with self.assertRaises(SystemExit):
                            helper.request_json("GET", "https://example.test")
                        request.assert_not_called()

    def test_telemetry_recovers_without_duplicate_deployment(self):
        payload = {
            "ref": "a" * 40,
            "environment": "production",
            "payload": {"invocation": "unique"},
        }
        with patch.object(
            telemetry.urllib.request,
            "urlopen",
            side_effect=[
                TimeoutError(),
                response([{**payload, "id": 42}]),
            ],
        ) as request, patch.object(network_retry, "pause"):
            self.assertEqual(
                telemetry.request_json(
                    "POST", "/repos/example/server/deployments", "token", payload
                )["id"],
                42,
            )
        self.assertEqual(
            [call.args[0].method for call in request.call_args_list], ["POST", "GET"]
        )

    def test_rate_limit_rejection_retries_deployment_creation(self):
        payload = {
            "ref": "a" * 40,
            "environment": "production",
            "payload": {"report_id": "unique"},
        }
        error = urllib.error.HTTPError(
            "https://example.test", 429, "rate limited", {"Retry-After": "0"}, None
        )
        with patch.object(
            telemetry.urllib.request,
            "urlopen",
            side_effect=[error, response({"id": 42})],
        ) as request, patch.object(network_retry, "pause"):
            self.assertEqual(
                telemetry.request_json(
                    "POST", "/repos/example/server/deployments", "token", payload
                )["id"],
                42,
            )
        self.assertEqual(
            [call.args[0].method for call in request.call_args_list], ["POST", "POST"]
        )

    def test_token_output_preserves_plain_mode_and_includes_expiry_in_json_mode(self):
        token = {"token": "test-token", "expires_at": "2099-01-01T00:00:00Z"}
        for json_mode in [False, True]:
            argv = [
                "helper",
                "--app-id",
                "123",
                "--installation-id",
                "456",
                "--private-key",
                "unused",
            ] + (["--json"] if json_mode else [])
            with self.subTest(json_mode=json_mode), patch.object(
                sys, "argv", argv
            ), patch.object(
                token_helper, "installation_token", return_value=token
            ), patch(
                "sys.stdout", new_callable=io.StringIO
            ) as output:
                token_helper.main()
                self.assertEqual(
                    (
                        json.loads(output.getvalue())
                        if json_mode
                        else output.getvalue().strip()
                    ),
                    token if json_mode else token["token"],
                )

    def test_missing_confirmation_never_reposts_deployment(self):
        payload = {
            "ref": "a" * 40,
            "environment": "production",
            "payload": {"invocation": "unique"},
        }
        with patch.object(
            telemetry.urllib.request,
            "urlopen",
            side_effect=[
                TimeoutError(),
                response([]),
                response([]),
            ],
        ) as request, patch.object(network_retry, "pause"), self.assertRaises(
            RuntimeError
        ):
            telemetry.request_json(
                "POST", "/repos/example/server/deployments", "token", payload
            )
        self.assertEqual(
            [call.args[0].method for call in request.call_args_list],
            ["POST", "GET", "GET"],
        )

    def test_deployment_creation_retries_dns_failure(self):
        payload = {
            "ref": "a" * 40,
            "environment": "production",
            "payload": {"invocation": "unique"},
        }
        with patch.object(
            telemetry.urllib.request,
            "urlopen",
            side_effect=[
                urllib.error.URLError(socket.gaierror("offline")),
                response({"id": 42}),
            ],
        ) as request, patch.object(network_retry, "pause"):
            self.assertEqual(
                telemetry.request_json(
                    "POST", "/repos/example/server/deployments", "token", payload
                )["id"],
                42,
            )
        self.assertEqual(
            [call.args[0].method for call in request.call_args_list], ["POST", "POST"]
        )


class CommandBudgets(unittest.TestCase):
    def test_expired_deadline_does_not_start_another_attempt(self):
        with patch.dict(
            os.environ, {"NETWORK_RETRY_DEADLINE_MONOTONIC": "1"}
        ), patch.object(network_retry.subprocess, "Popen") as process, patch(
            "sys.stderr", new_callable=io.StringIO
        ):
            self.assertEqual(network_retry.bounded_command(["unused"]), 75)
        process.assert_not_called()

    def test_nested_commands_share_the_original_deadline(self):
        deadline = str(time.monotonic() + 10)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/network_retry.py"),
                "bounded-command",
                sys.executable,
                "-c",
                "import os; print(os.environ['NETWORK_RETRY_DEADLINE_MONOTONIC'])",
            ],
            env={**os.environ, "NETWORK_RETRY_DEADLINE_MONOTONIC": deadline},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), deadline)

    def test_budget_kills_nested_processes_as_well_as_the_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-survived"
            child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"
            parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
            started = time.monotonic()
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/network_retry.py"),
                    "bounded-command",
                    sys.executable,
                    "-c",
                    parent,
                ],
                env={
                    **os.environ,
                    "NETWORK_RETRY_DEADLINE_MONOTONIC": str(time.monotonic() + 0.4),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertLess(time.monotonic() - started, 3)
            time.sleep(1.1)
            self.assertFalse(marker.exists())


class PrivateGitBudgets(unittest.TestCase):
    def test_git_retries_share_the_token_deadline_and_stop_terminal_failures(self):
        for permanent_failure in [True, False]:
            with self.subTest(
                permanent_failure=permanent_failure
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calls_path = root / "calls.json"
                runner = root / "network_retry.py"
                runner.write_text((ROOT / "scripts/network_retry.py").read_text())
                runner.chmod(0o755)
                helper = root / "token-helper"
                helper.write_text(f"""#!{sys.executable}
import json, os
from pathlib import Path
Path({str(calls_path)!r}).write_text(json.dumps([{{'stage': 'token', 'deadline': os.environ['NETWORK_RETRY_DEADLINE_MONOTONIC']}}]))
print(json.dumps({{'token': 'test-token', 'expires_at': '2099-01-01T00:00:00Z'}}))
""")
                helper.chmod(0o755)
                fake_git = root / "fake-git.py"
                fake_git.write_text(f"""
import json, os, sys, time
from pathlib import Path
path = Path({str(calls_path)!r})
calls = json.loads(path.read_text())
calls.append({{'stage': 'git', 'deadline': os.environ['NETWORK_RETRY_DEADLINE_MONOTONIC']}})
path.write_text(json.dumps(calls))
assert os.environ['GITHUB_APP_INSTALLATION_TOKEN'] == 'test-token'
if {permanent_failure!r}:
    print('fatal: Authentication failed', file=sys.stderr)
    sys.exit(1)
time.sleep(20)
""")
                git_wrapper = root / "network-git"
                git_wrapper.write_text(f"""#!{sys.executable}
import os, sys
os.execv(sys.executable, [sys.executable, {str(runner)!r}, 'bounded-command', sys.executable, {str(fake_git)!r}, *sys.argv[1:]])
""")
                git_wrapper.chmod(0o755)
                original = yaml.safe_load(
                    (ROOT / "roles/compose/tasks/secrets-source.yml").read_text()
                )
                tasks = [
                    task
                    for task in original
                    if task["name"]
                    in [
                        "Start the shared private Git and authentication retry budget",
                        "Ensure a valid GitHub App token before private Git operations",
                    ]
                    or (
                        task["name"] == "Verify private secrets repository is reachable"
                        and "become_user" in task
                    )
                ]
                for task in tasks:
                    if "ansible.builtin.include_tasks" in task:
                        task["ansible.builtin.include_tasks"] = str(
                            ROOT / "roles/compose/tasks/github-app-token.yml"
                        )
                variables = {
                    **yaml.safe_load(
                        (ROOT / "group_vars/all/network-retry.yml").read_text()
                    ),
                    "network_retry_attempts": 10,
                    "network_retry_delay_seconds": 0,
                    "network_retry_http_budget_seconds": 8,
                    "network_retry_helper_path": str(runner),
                    "network_retry_git_executable": str(git_wrapper),
                    "server_secrets_auth_method": "github_app",
                    "server_secrets_github_app_token_helper_path": str(helper),
                    "server_secrets_github_app_id": "123",
                    "server_secrets_github_app_installation_id": "456",
                    "server_secrets_repo_url": "https://example.test/unused.git",
                    "server_secrets_github_app_private_key_path": str(root / "unused"),
                    "server_secrets_github_app_user": os.environ.get("USER", "root"),
                    "server_secrets_github_app_home": str(root),
                    "server_secrets_github_app_askpass_path": str(
                        root / "unused-askpass"
                    ),
                }
                config = root / "ansible.cfg"
                config.write_text("[defaults]\nretry_files_enabled = false\n")
                playbook = root / "play.yml"
                playbook.write_text(
                    yaml.safe_dump(
                        [
                            {
                                "hosts": "localhost",
                                "gather_facts": False,
                                "vars": variables,
                                "environment": {
                                    "NETWORK_RETRY_HTTP_BUDGET_SECONDS": "8"
                                },
                                "tasks": tasks,
                            }
                        ]
                    )
                )
                started = time.monotonic()
                result = subprocess.run(
                    [
                        shutil.which("ansible-playbook"),
                        "--inventory",
                        "localhost,",
                        "--connection",
                        "local",
                        "--extra-vars",
                        json.dumps({"ansible_python_interpreter": sys.executable}),
                        str(playbook),
                    ],
                    cwd=root,
                    env={**os.environ, "ANSIBLE_CONFIG": str(config)},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertLess(time.monotonic() - started, 16)
                calls = json.loads(calls_path.read_text())
                self.assertEqual([call["stage"] for call in calls], ["token", "git"])
                self.assertEqual(calls[0]["deadline"], calls[1]["deadline"])
                self.assertNotIn("test-token", result.stdout)


class CheckoutRetries(unittest.TestCase):
    def test_only_checkout_is_replayed_and_failure_stops_apply(self):
        for codes, expected in [([1, 1, 0, 0], 0), ([1, 1, 1], 1), ([0, 2], 2)]:
            with self.subTest(codes=codes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log = root / "calls"
                counter = root / "counter"
                for name, stage in [
                    ("ansible", "checkout"),
                    ("ansible-playbook", "apply"),
                ]:
                    executable = root / name
                    executable.write_text(f"""#!{sys.executable}
from pathlib import Path
import sys
log = Path({str(log)!r})
counter = Path({str(counter)!r})
index = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(index + 1))
with log.open('a') as output:
    output.write({stage!r} + '\\n')
sys.exit({codes!r}[index])
""")
                    executable.chmod(0o755)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/network_retry.py"),
                        "pull",
                        str(root / "ansible-pull"),
                        "--url",
                        "https://example.test/repo.git",
                        "--checkout",
                        "production",
                        "--directory",
                        str(root),
                        "--inventory",
                        str(root / "inventory.yml"),
                        "--clean",
                        "--diff",
                        "-vv",
                        "site.yml",
                    ],
                    env={
                        **os.environ,
                        "NETWORK_RETRY_ATTEMPTS": "3",
                        "NETWORK_RETRY_DELAY_SECONDS": "0",
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                stages = log.read_text().splitlines()
                self.assertEqual(stages.count("apply"), int(0 in codes[:-1]))
                self.assertEqual(
                    stages,
                    ["checkout"] * (len(codes) - stages.count("apply"))
                    + ["apply"] * stages.count("apply"),
                )


class ComposeRetries(unittest.TestCase):
    def run_projects(
        self,
        images,
        pull_failures=None,
        login_failures=0,
        token_failure=False,
        extra_variables=None,
    ):
        """Run real orchestration against local doubles without production access."""
        ansible = shutil.which("ansible-playbook")
        self.assertIsNotNone(ansible)
        pull_failures = pull_failures or {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls_path = root / "calls.json"
            modules = (
                root
                / "collections/ansible_collections/community/docker/plugins/modules"
            )
            modules.mkdir(parents=True)
            for name, stage in [
                ("docker_login", "login"),
                ("docker_compose_v2_pull", "pull"),
                ("docker_compose_v2", "apply"),
            ]:
                (modules / f"{name}.py").write_text(f"""
import json
import os
from pathlib import Path
from ansible.module_utils.basic import AnsibleModule
module = AnsibleModule(argument_spec={{
    'registry_url': {{'type': 'str'}}, 'username': {{'type': 'str'}},
    'password': {{'type': 'str', 'no_log': True}}, 'reauthorize': {{'type': 'bool'}},
    'project_src': {{'type': 'str'}}, 'policy': {{'type': 'str'}},
    'pull': {{'type': 'str'}}, 'remove_orphans': {{'type': 'bool'}}, 'state': {{'type': 'str'}},
}})
path = Path({str(calls_path)!r})
calls = json.loads(path.read_text()) if path.exists() else []
stage = {stage!r}
project = Path(module.params['project_src']).name if module.params['project_src'] else None
calls.append({{'stage': stage, 'project': project}})
path.write_text(json.dumps(calls))
attempt = sum(call == calls[-1] for call in calls)
if stage == 'login' and attempt <= {login_failures} or stage == 'pull' and attempt <= {pull_failures!r}.get(project, 0):
    module.fail_json(msg='TLS handshake timeout')
if stage in ('pull', 'apply') and os.environ.get('COMPOSE_PARALLEL_LIMIT') != '1':
    module.fail_json(msg='HDD download concurrency was not limited')
if stage == 'apply' and module.params['pull'] != 'never':
    module.fail_json(msg='Apply unexpectedly attempted another pull')
module.exit_json(changed=True)
""")
            runner = root / "network_retry.py"
            runner.write_text((ROOT / "scripts/network_retry.py").read_text())
            runner.chmod(0o755)
            helper = root / "token-helper"
            helper.write_text(f"""#!{sys.executable}
import json
from pathlib import Path
import sys
path = Path({str(calls_path)!r})
calls = json.loads(path.read_text()) if path.exists() else []
calls.append({{'stage': 'token', 'project': None}})
path.write_text(json.dumps(calls))
if {token_failure!r}:
    print('GitHub API request failed: HTTP 401', file=sys.stderr)
    sys.exit(1)
print(json.dumps({{'token': 'test-token', 'expires_at': '2099-01-01T00:00:00Z'}}))
""")
            helper.chmod(0o755)
            (root / "ansible.cfg").write_text(
                "[defaults]\nretry_files_enabled = false\n"
            )
            variables = {
                "docker_compose_root": str(root),
                "compose_projects": list(images),
                "compose_project_config_validation": {
                    "results": [
                        {"item": project, "stdout_lines": values}
                        for project, values in images.items()
                    ]
                },
                "compose_pull_before_up": True,
                "network_retry_attempts": 3,
                "network_retry_delay_seconds": 0,
                "network_retry_http_budget_seconds": 10,
                "network_retry_helper_path": str(runner),
                "server_secrets_auth_method": "github_app",
                "server_secrets_github_app_token_helper_path": str(helper),
                "server_secrets_github_app_id": "123",
                "server_secrets_github_app_installation_id": "456",
                "server_secrets_repo_url": "https://github.com/example/test.git",
                "server_secrets_github_app_private_key_path": str(root / "unused-key"),
                "server_secrets_github_app_user": os.environ.get("USER", "root"),
                "server_secrets_github_app_home": str(root),
                **(extra_variables or {}),
            }
            playbook = [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": variables,
                    "tasks": [
                        {
                            "block": [
                                {
                                    "ansible.builtin.include_tasks": str(
                                        ROOT / "roles/compose/tasks/deploy.yml"
                                    )
                                },
                            ],
                            "rescue": [
                                {
                                    "ansible.builtin.debug": {
                                        "msg": "Expected terminal project failure"
                                    }
                                },
                            ],
                        },
                        {
                            "ansible.builtin.copy": {
                                "content": "{{ compose_project_deployment_failures | to_json }}",
                                "dest": str(root / "failures.json"),
                                "mode": "0600",
                            }
                        },
                    ],
                }
            ]
            (root / "play.yml").write_text(yaml.safe_dump(playbook))
            result = subprocess.run(
                [
                    ansible,
                    "--inventory",
                    "localhost,",
                    "--connection",
                    "local",
                    "--extra-vars",
                    json.dumps({"ansible_python_interpreter": sys.executable}),
                    str(root / "play.yml"),
                ],
                cwd=root,
                env={
                    **os.environ,
                    "ANSIBLE_CONFIG": str(root / "ansible.cfg"),
                    "ANSIBLE_COLLECTIONS_PATH": str(root / "collections"),
                    "GITHUB_DEPLOYMENT_RUN_ID": "",
                    "GITHUB_DEPLOYMENT_RESULTS_PATH": "",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            failures = json.loads((root / "failures.json").read_text())
            calls = json.loads(calls_path.read_text())
            self.assertNotIn("test-token", result.stdout)
            return calls, failures

    def test_outage_retries_login_and_pull_without_replaying_apply(self):
        for pull_failures, expected_failure in [(2, False), (3, True)]:
            with self.subTest(pull_failures=pull_failures):
                calls, failures = self.run_projects(
                    {"example": ["ghcr.io/example/app:latest"]},
                    pull_failures={"example": pull_failures},
                    login_failures=2,
                )
                stages = [call["stage"] for call in calls]
                self.assertEqual(stages.count("token"), 1)
                self.assertEqual(stages.count("login"), 3)
                self.assertEqual(stages.count("pull"), 3)
                self.assertEqual(stages.count("apply"), int(not expected_failure))
                self.assertEqual(
                    [failure["project"] for failure in failures],
                    ["example"] if expected_failure else [],
                )

    def test_two_ghcr_projects_share_one_token_and_login(self):
        calls, failures = self.run_projects(
            {
                "first": ["ghcr.io/example/first:latest"],
                "second": ["ghcr.io/example/second:latest"],
            }
        )
        stages = [call["stage"] for call in calls]
        self.assertEqual(stages.count("token"), 1)
        self.assertEqual(stages.count("login"), 1)
        self.assertEqual(stages.count("apply"), 2)
        self.assertEqual(failures, [])

    def test_dockerhub_projects_do_not_depend_on_github_authentication(self):
        calls, failures = self.run_projects(
            {"public": ["docker.io/library/postgres:18"]},
            token_failure=True,
        )
        self.assertEqual([call["stage"] for call in calls], ["pull", "apply"])
        self.assertEqual(failures, [])

    def test_failed_ghcr_auth_does_not_block_a_later_public_project(self):
        calls, failures = self.run_projects(
            {
                "private": ["ghcr.io/example/app:latest"],
                "public": ["docker.io/library/postgres:18"],
            },
            token_failure=True,
        )
        self.assertEqual([call["stage"] for call in calls], ["token", "pull", "apply"])
        self.assertEqual([failure["project"] for failure in failures], ["private"])

    def test_expired_token_is_renewed_and_docker_is_reauthenticated(self):
        calls, failures = self.run_projects(
            {"private": ["ghcr.io/example/app:latest"]},
            extra_variables={
                "server_secrets_github_app_installation_token": {
                    "token": "expired",
                    "expires_at": "2000-01-01T00:00:00Z",
                },
                "ghcr_authenticated_token_expiry": "2000-01-01T00:00:00Z",
            },
        )
        self.assertEqual(
            [call["stage"] for call in calls], ["token", "login", "pull", "apply"]
        )
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
