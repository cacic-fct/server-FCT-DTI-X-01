import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "telemetry", ROOT / "roles/ansible_pull/files/github-deployment-telemetry-ansible-pull.py"
)
telemetry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(telemetry)


class DeploymentLinks(unittest.TestCase):
    def test_deployment_identifies_runner_and_executed_revision(self):
        with patch.dict(os.environ, {
            "ANSIBLE_PULL_VERSION": "production",
            "INVOCATION_ID": "test-invocation",
        }, clear=True), patch.object(telemetry.socket, "gethostname", return_value="secompp"), \
                patch.object(telemetry, "request_json") as request:
            telemetry.create_deployment("test-token", "example", "server", "executed-sha")

        payload = request.call_args.args[3]
        self.assertEqual(payload["ref"], "executed-sha")
        self.assertEqual(payload["payload"]["runner_hostname"], "secompp")
        self.assertEqual(payload["payload"]["systemd_invocation_id"], "test-invocation")
        self.assertRegex(payload["payload"]["wrapper_sha256"], r"^[0-9a-f]{64}$")

    def test_wrapper_identity_survives_playbook_replacing_installed_file(self):
        original_hash = telemetry.WRAPPER_SHA256
        with patch("builtins.open", side_effect=FileNotFoundError), \
                patch.object(telemetry, "request_json") as request:
            telemetry.create_deployment("test-token", "example", "server", "executed-sha")
        self.assertEqual(request.call_args.args[3]["payload"]["wrapper_sha256"], original_hash)

    def test_success_failure_and_wrapper_error_link_to_ara(self):
        for outcome, expected in [(0, "success"), (2, "failure"), (OSError("test"), "error")]:
            with self.subTest(outcome=outcome), patch.dict(os.environ, {
                "ANSIBLE_PULL_ARA_ENABLED": "true",
                "GITHUB_DEPLOYMENT_TELEMETRY_ENABLED": "true",
                "GITHUB_DEPLOYMENT_ENVIRONMENT_URL": "https://ansible.cacic.com.br",
                "GITHUB_DEPLOYMENT_REPO": "https://github.com/example/server.git",
            }, clear=True), patch.object(telemetry.subprocess, "run") as run, \
                    patch.object(telemetry.os.path, "isfile", return_value=True), \
                    patch.object(telemetry, "checkout_revision", return_value="current"), \
                    patch.object(telemetry, "run_ansible_pull") as pull, \
                    patch.object(telemetry, "github_token", return_value="test-token"), \
                    patch.object(telemetry, "request_json", return_value={"id": 42}) as request:
                run.return_value.stdout = "/test/callbacks\n"
                if isinstance(outcome, Exception):
                    pull.side_effect = outcome
                    with self.assertRaises(OSError):
                        telemetry.main()
                else:
                    pull.return_value = outcome
                    self.assertEqual(telemetry.main(), outcome)
                payload = request.call_args.args[3]
                self.assertEqual(payload["state"], expected)
                self.assertEqual(payload["environment_url"], "https://ansible.cacic.com.br")
                label = parse_qs(urlparse(payload["log_url"]).query)["label"][0]
                self.assertIn(label, os.environ["ARA_DEFAULT_LABELS"].split(","))
                self.assertEqual(telemetry.ansible_pull_argv()[0], "/opt/ara/venv/bin/ansible-pull")

    def test_unchanged_revision_is_reported_on_retry(self):
        with patch.dict(os.environ, {"GITHUB_DEPLOYMENT_TELEMETRY_ENABLED": "true"}, clear=True), \
                patch.object(telemetry, "checkout_revision", return_value="same-revision"), \
                patch.object(telemetry, "run_ansible_pull", side_effect=[2, 0]), \
                patch.object(telemetry, "report_deployment") as report:
            self.assertEqual(telemetry.main(), 2)
            self.assertEqual(telemetry.main(), 0)
            self.assertEqual([call.args[1] for call in report.call_args_list], ["failure", "success"])

    def test_missing_recorder_keeps_distribution_ansible(self):
        with patch.dict(os.environ, {"ANSIBLE_PULL_ARA_ENABLED": "true"}, clear=True), \
                patch.object(telemetry.subprocess, "run", side_effect=FileNotFoundError):
            telemetry.configure_ara()
            self.assertEqual(telemetry.ansible_pull_argv()[0], "/usr/bin/ansible-pull")
            self.assertNotIn("ANSIBLE_CALLBACK_PLUGINS", os.environ)


class Recording(unittest.TestCase):
    def test_offline_failure_details_privacy_and_web_permissions(self):
        # Exercise the actual callback and database, without a host or GitHub access.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            private = root / "server-FCT-DTI-X-01-secrets"
            private.mkdir()
            (private / "vars.yml").write_text("private_value: ara-test-private-value\n")
            (root / "ansible.cfg").write_text("[defaults]\nretry_files_enabled = false\n")
            (root / "playbook.yml").write_text("""---
- hosts: localhost
  gather_facts: false
  vars_files:
    - server-FCT-DTI-X-01-secrets/vars.yml
  tasks:
    - name: Secret task
      ansible.builtin.debug:
        msg: "{{ private_value }}"
      no_log: true
    - name: Ordinary task
      ansible.builtin.debug:
        msg: visible-result
    - name: Intentional failure
      ansible.builtin.fail:
        msg: visible-failure-detail
""")
            callback = subprocess.check_output(
                [sys.executable, "-m", "ara.setup.callback_plugins"], text=True
            ).strip()
            environment = {
                **os.environ,
                **yaml.safe_load((ROOT / "docker-compose/ara/docker-compose.yml").read_text())[
                    "services"
                ]["ara"]["environment"],
                "ANSIBLE_CONFIG": str(root / "ansible.cfg"),
                "ANSIBLE_CALLBACK_PLUGINS": callback,
                "ANSIBLE_BECOME": "false",
                "ARA_BASE_DIR": str(data),
                "ARA_API_CLIENT": "offline",
                "ARA_WRITE_LOGIN_REQUIRED": "false",
                "ARA_DEFAULT_LABELS": "test-run",
                "ARA_IGNORED_FACTS": "all",
                "ARA_IGNORED_ARGUMENTS": "extra_vars,vault_password_files",
                "ARA_IGNORED_FILES": ".ansible/tmp,/server-FCT-DTI-X-01-secrets/,.env,.pem",
                "ARA_RECORD_TASK_CONTENT": "true",
            }
            result = subprocess.run(
                [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
                 "playbook.yml", "-e", "cli_secret=ara-test-cli-secret"],
                env=environment, cwd=root, capture_output=True, text=True, timeout=90,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            # A separate process loads the web settings, just like the Compose API.
            check = subprocess.run([sys.executable, "-c", """
import json, os
os.environ['DJANGO_SETTINGS_MODULE'] = 'ara.server.settings'
import django
django.setup()
from django.test import Client
from ara.api.models import Playbook, Result, File
from ara.api.fields import CompressedObjectField
client = Client(HTTP_HOST='localhost')
playbook = Playbook.objects.get(labels__name='test-run')
assert playbook.status == 'failed', playbook.status
decode = CompressedObjectField().to_representation
results = [decode(value) for value in Result.objects.values_list('content', flat=True)]
assert 'visible-failure-detail' in json.dumps(results), results
assert 'visible-result' in json.dumps(results), results
assert 'ara-test-private-value' not in json.dumps(results), results
assert 'ara-test-cli-secret' not in json.dumps(decode(playbook.arguments))
assert not File.objects.filter(path__contains='server-FCT-DTI-X-01-secrets').exists()
assert client.get('/?label=test-run').status_code == 200
assert client.get(f'/playbooks/{playbook.pk}.html').status_code == 200
assert client.post('/api/v1/playbooks', data={}, content_type='application/json').status_code in (401, 403)
print('ARA callback records failure details, suppresses secrets, and denies web API writes.')
"""], env={**environment, "ARA_WRITE_LOGIN_REQUIRED": "true"},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


if __name__ == "__main__":
    unittest.main()
