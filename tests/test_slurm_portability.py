import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUBMIT = ROOT / "Slurm/submit_topas_array.sh"
WORKER = ROOT / "Slurm/topas_general_array.sbatch"


class SlurmPortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "Slurm").mkdir()
        shutil.copy2(SUBMIT, self.root / "Slurm/submit_topas_array.sh")
        shutil.copy2(WORKER, self.root / "Slurm/topas_general_array.sbatch")
        (self.root / "study.toml").write_text("[study]\nname = 'test'\n")
        self.task = self.root / "generated/smoke/tasks/task.txt"
        self.task.parent.mkdir(parents=True)
        self.task.write_text(
            'i:Ts/NumberOfThreads = 2\n'
            's:Sc/PatientDose/OutputFile = "output/test/Dose"\n'
        )

    def tearDown(self):
        self.temporary.cleanup()

    def make_environment(self, name="opentopas-env.sh", content="# test environment\n"):
        path = self.root / name
        path.write_text(content)
        return path

    def run_submit(self, arguments, inherited_topas_env=None, path_prefix=None):
        environment = os.environ.copy()
        environment.pop("TOPAS_ENV", None)
        if inherited_topas_env is not None:
            environment["TOPAS_ENV"] = str(inherited_topas_env)
        if path_prefix is not None:
            environment["PATH"] = f"{path_prefix}:{environment['PATH']}"
        return subprocess.run(
            [str(self.root / "Slurm/submit_topas_array.sh"), *map(str, arguments)],
            cwd=self.root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_dry_run_exports_rohpc_or_biohpc_environment(self):
        for cluster in ("rohpc", "biohpc"):
            with self.subTest(cluster=cluster):
                topas_env = self.make_environment(f"{cluster}-opentopas-env.sh")
                result = self.run_submit([
                    "--topas-env", topas_env,
                    "--dry-run",
                    self.task,
                ])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"TOPAS_ENV={topas_env}", result.stdout)
                self.assertIn("--cpus-per-task=2", result.stdout)

    def test_cli_environment_overrides_inherited_and_forwards_scheduler_options(self):
        inherited = self.make_environment("inherited-env.sh")
        requested = self.make_environment("requested-env.sh")
        result = self.run_submit([
            "--topas-env", requested,
            "--account", "radiology",
            "--qos", "normal",
            "--dry-run",
            self.task,
        ], inherited)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"TOPAS_ENV={requested}", result.stdout)
        self.assertNotIn(f"TOPAS_ENV={inherited}", result.stdout)
        self.assertIn("--account=radiology", result.stdout)
        self.assertIn("--qos=normal", result.stdout)

    def test_inherited_environment_is_supported(self):
        topas_env = self.make_environment()
        result = self.run_submit(["--dry-run", self.task], topas_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"TOPAS environment: {topas_env}", result.stdout)

    def test_environment_validation_failures_are_clear(self):
        cases = [
            ([], None, "environment is required"),
            (["--topas-env", "relative-env.sh"], None, "must be absolute"),
            (["--topas-env", self.root / "missing-env.sh"], None, "missing, unreadable, or empty"),
        ]
        empty = self.make_environment("empty-env.sh", "")
        cases.append((["--topas-env", empty], None, "missing, unreadable, or empty"))
        comma = self.make_environment("comma,env.sh")
        cases.append((["--topas-env", comma], None, "may not contain commas"))

        for arguments, inherited, message in cases:
            with self.subTest(message=message):
                result = self.run_submit([*arguments, "--dry-run", self.task], inherited)
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)

        unreadable = self.make_environment("unreadable-env.sh")
        unreadable.chmod(0)
        try:
            if not os.access(unreadable, os.R_OK):
                result = self.run_submit([
                    "--topas-env", unreadable, "--dry-run", self.task,
                ])
                self.assertEqual(result.returncode, 2)
                self.assertIn("unreadable", result.stderr)
        finally:
            unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def make_fake_command(self, directory, name, body):
        path = directory / name
        path.write_text("#!/usr/bin/env bash\nset -e\n" + body)
        path.chmod(0o755)
        return path

    def worker_environment(self, topas_env):
        manifest = self.root / "manifest.txt"
        relative_task = str(self.task.relative_to(self.root))
        manifest.write_text(f"{relative_task}\n")
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        hashes = self.root / "hashes.txt"
        task_digest = hashlib.sha256(self.task.read_bytes()).hexdigest()
        hashes.write_text(f"{task_digest}\t{relative_task}\n")
        hashes_digest = hashlib.sha256(hashes.read_bytes()).hexdigest()
        environment = os.environ.copy()
        environment.update({
            "SLURM_ARRAY_TASK_ID": "1",
            "SLURM_ARRAY_JOB_ID": "123",
            "SLURM_JOB_ID": "123_1",
            "SLURM_CPUS_PER_TASK": "2",
            "SLURM_SUBMIT_DIR": str(self.root),
            "TOPAS_MANIFEST": str(manifest),
            "TOPAS_MANIFEST_SHA256": digest,
            "TOPAS_TASK_HASHES": str(hashes),
            "TOPAS_TASK_HASHES_SHA256": hashes_digest,
            "TOPAS_TASK_COUNT": "1",
            "TOPAS_ENV": str(topas_env),
        })
        return environment

    def test_worker_sources_environment_and_launches_srun(self):
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        self.make_fake_command(fake_bin, "topas", 'echo "FAKE TOPAS $*"\n')
        self.make_fake_command(fake_bin, "srun", 'echo "FAKE SRUN $*"\n')
        self.make_fake_command(
            fake_bin,
            "date",
            'if [[ ${1:-} == "+%s" ]]; then /bin/date +%s; else echo "2026-01-01T00:00:00"; fi\n',
        )
        topas_env = self.make_environment(
            content=f'export PATH="{fake_bin}:$PATH"\nexport TEST_ENV_SOURCED=1\n',
        )
        result = subprocess.run(
            [str(self.root / "Slurm/topas_general_array.sbatch")],
            cwd=self.root,
            env=self.worker_environment(topas_env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"TOPAS environment: {topas_env}", result.stdout)
        self.assertIn(
            "FAKE SRUN --ntasks=1 --cpus-per-task=2 topas generated/smoke/tasks/task.txt",
            result.stdout,
        )

    def test_submission_moves_task_and_writes_receipt(self):
        fake_bin = self.root / "fake-submit-bin"
        fake_bin.mkdir()
        self.make_fake_command(fake_bin, "sbatch", 'echo "98765"\n')
        topas_env = self.make_environment()
        result = self.run_submit([
            "--topas-env", topas_env, self.task,
        ], path_prefix=fake_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.task.exists())
        archived = list((self.task.parent / "submitted").glob("*/*.txt"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].name, "task.txt")
        self.assertEqual(archived[0].stat().st_mode & 0o777, 0o444)
        receipts = list((self.root / "Slurm/logs").glob("topas_submission_*.txt"))
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0].read_text()
        self.assertIn("Slurm job ID: 98765", receipt)
        self.assertIn("TASK\tgenerated/smoke/tasks/task.txt", receipt)

    def test_failed_submission_restores_task(self):
        fake_bin = self.root / "fake-failing-submit-bin"
        fake_bin.mkdir()
        self.make_fake_command(fake_bin, "sbatch", 'exit 1\n')
        topas_env = self.make_environment()
        original = self.task.read_bytes()
        result = self.run_submit([
            "--topas-env", topas_env, self.task,
        ], path_prefix=fake_bin)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.task.read_bytes(), original)
        self.assertFalse(list((self.root / "Slurm/logs").glob("topas_manifest_*.txt")))

    def test_submit_rejects_input_outside_top_level_tasks(self):
        outside = self.root / "outside.txt"
        outside.write_text(self.task.read_text())
        result = self.run_submit([
            "--topas-env", self.make_environment(), "--dry-run", outside,
        ])
        self.assertEqual(result.returncode, 2)
        self.assertIn("top-level file", result.stderr)

    def test_worker_rejects_changed_archived_task(self):
        fake_bin = self.root / "fake-changed-bin"
        fake_bin.mkdir()
        self.make_fake_command(fake_bin, "topas", 'echo topas\n')
        self.make_fake_command(fake_bin, "srun", 'echo srun\n')
        topas_env = self.make_environment(content=f'export PATH="{fake_bin}:$PATH"\n')
        environment = self.worker_environment(topas_env)
        self.task.write_text(self.task.read_text() + "# changed\n")
        result = subprocess.run(
            [str(self.root / "Slurm/topas_general_array.sbatch")], cwd=self.root,
            env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("changed after submission", result.stderr)

    def test_worker_rejects_environment_without_topas(self):
        fake_bin = self.root / "fake-no-topas"
        fake_bin.mkdir()
        self.make_fake_command(
            fake_bin,
            "date",
            'if [[ ${1:-} == "+%s" ]]; then /bin/date +%s; else echo "2026-01-01T00:00:00"; fi\n',
        )
        topas_env = self.make_environment(
            content=f'export PATH="{fake_bin}:/usr/bin:/bin"\n',
        )
        result = subprocess.run(
            [str(self.root / "Slurm/topas_general_array.sbatch")],
            cwd=self.root,
            env=self.worker_environment(topas_env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("did not make the topas command available", result.stderr)

    def test_worker_map_mode_remains_available(self):
        manifest = self.root / "map.txt"
        manifest.write_text("first.txt\nsecond.txt\n")
        result = subprocess.run(
            [str(self.root / "Slurm/topas_general_array.sbatch"), "--map", str(manifest), "2"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "second.txt")


if __name__ == "__main__":
    unittest.main()
