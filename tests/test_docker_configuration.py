import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class DockerConfiguration(unittest.TestCase):
    def test_download_limit_preserves_existing_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "docker/daemon.json"
            config_path.parent.mkdir()
            original = {
                "log-driver": "local",
                "live-restore": True,
                "max-concurrent-downloads": 3,
            }
            config_path.write_text(json.dumps(original))
            config_path.chmod(0o600)
            validator = root / "validate-json"
            validator.write_text(f"""#!{sys.executable}
import json, sys
from pathlib import Path
config = json.loads(Path(sys.argv[-1]).read_text())
assert config['max-concurrent-downloads'] == 1
assert config['log-driver'] == 'local' and config['live-restore'] is True
""")
            validator.chmod(0o755)
            tasks = yaml.safe_load((ROOT / "roles/docker/tasks/daemon.yml").read_text())
            for task in tasks:
                for module, parameters in task.items():
                    if not isinstance(parameters, dict):
                        continue
                    for field in ["path", "src", "dest"]:
                        if field in parameters:
                            parameters[field] = parameters[field].replace(
                                "/etc/docker", str(config_path.parent)
                            )
                    parameters.pop("owner", None)
                    parameters.pop("group", None)
                    if "validate" in parameters:
                        parameters["validate"] = (
                            f"{validator} --validate --config-file %s"
                        )
            config = root / "ansible.cfg"
            config.write_text("[defaults]\nretry_files_enabled = false\n")
            playbook = root / "play.yml"
            marker = root / "restarted"
            playbook.write_text(
                yaml.safe_dump(
                    [
                        {
                            "hosts": "localhost",
                            "gather_facts": False,
                            "vars": {"docker_max_concurrent_downloads": 1},
                            "tasks": tasks
                            + [{"ansible.builtin.meta": "flush_handlers"}]
                            + tasks,
                            "handlers": [
                                {
                                    "name": "Restart Docker",
                                    "ansible.builtin.shell": f"printf x >> {marker}",
                                    "changed_when": True,
                                }
                            ],
                        }
                    ]
                )
            )
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
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                json.loads(config_path.read_text()),
                {**original, "max-concurrent-downloads": 1},
            )
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(marker.read_text(), "x")


if __name__ == "__main__":
    unittest.main()
