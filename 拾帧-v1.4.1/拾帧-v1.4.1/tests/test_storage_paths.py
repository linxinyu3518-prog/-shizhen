"""Project-local storage routing, tested with temporary fixtures on E: only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from framekeep.config import Settings
from framekeep.desktop_browser import build_browser_command
from framekeep.storage import configure_storage, default_data_dir, storage_paths, worker_environment


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECTS = (
    Path("E:/拾帧/拾帧1.3"),
    Path("E:/拾帧/mp4-avi-h-264-aac-48000hz-2/outputs/拾帧-media-extractor"),
)
ROUTED_ENV = (
    "TEMP", "TMP", "TMPDIR", "MEDIA_DATA_DIR", "XDG_CACHE_HOME", "TORCH_HOME",
    "TORCH_EXTENSIONS_DIR", "CUDA_CACHE_PATH", "HF_HOME", "HUGGINGFACE_HUB_CACHE",
    "MPLCONFIGDIR", "PIP_CACHE_DIR", "PLAYWRIGHT_BROWSERS_PATH", "TRITON_CACHE_DIR",
    "TORCHINDUCTOR_CACHE_DIR", "TRITON_DUMP_DIR", "TRITON_OVERRIDE_DIR", "HF_HUB_CACHE",
    "TRANSFORMERS_CACHE", "NUMBA_CACHE_DIR", "PYTHONPYCACHEPREFIX",
)


class StoragePathTests(unittest.TestCase):
    def setUp(self):
        # Explicit dir prevents even fixture creation from using stale C: TEMP.
        fixture_root = PROJECT_ROOT / "deployment" / "storage-test-temp"
        fixture_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=fixture_root)
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        self.data = self.fixture / "data"
        previous_tempdir = tempfile.tempdir
        previous_bytecode = sys.dont_write_bytecode
        self.addCleanup(setattr, tempfile, "tempdir", previous_tempdir)
        self.addCleanup(setattr, sys, "dont_write_bytecode", previous_bytecode)
        legacy = {key: "C:/old-framekeep/" + key.lower() for key in ROUTED_ENV}
        legacy.update(LOCALAPPDATA="C:/Users/Test/AppData/Local",
                      APPDATA="C:/Users/Test/AppData/Roaming")
        environment = patch.dict(os.environ, legacy)
        environment.start()
        self.addCleanup(environment.stop)

    def test_default_data_directory_ignores_external_environment(self):
        expected = (PROJECT_ROOT / "data").resolve()
        self.assertEqual(default_data_dir(), expected)
        self.assertEqual(Settings().data_dir, expected)

    def test_mapping_is_absolute_and_has_expected_layout(self):
        result = storage_paths(self.data)
        expected = {
            "data": self.data,
            "downloads": self.data / "downloads",
            "temporary": self.data / "tmp" / "runtime",
            "cache": self.data / "cache",
            "browser_profile": self.data / "browser" / "ui",
            "browser_downloads": self.data / "downloads" / "browser",
        }
        for key, directory in expected.items():
            with self.subTest(key=key):
                self.assertIsInstance(result[key], str)
                self.assertEqual(Path(result[key]), directory.resolve())
                self.assertTrue(Path(result[key]).is_absolute())

    def test_configure_overrides_stale_paths_and_preserves_personal_browser_environment(self):
        result = configure_storage(self.data)
        self.assertEqual(result, storage_paths(self.data))
        for value in result.values():
            with self.subTest(directory=value):
                self.assertTrue(Path(value).is_dir())
        for key in ROUTED_ENV:
            with self.subTest(environment=key):
                directory = Path(os.environ[key]).resolve()
                self.assertTrue(directory.is_relative_to(self.data.resolve()))
                self.assertTrue(directory.is_dir())
                self.assertEqual(directory.drive.upper(), "E:")
        for key in ("TEMP", "TMP", "TMPDIR"):
            self.assertEqual(Path(os.environ[key]), Path(result["temporary"]))
        self.assertEqual(Path(os.environ["MEDIA_DATA_DIR"]), self.data.resolve())
        self.assertEqual(os.environ["LOCALAPPDATA"], "C:/Users/Test/AppData/Local")
        self.assertEqual(os.environ["APPDATA"], "C:/Users/Test/AppData/Roaming")
        self.assertTrue(sys.dont_write_bytecode)
        self.assertEqual(Path(tempfile.gettempdir()), Path(result["temporary"]))
        with tempfile.NamedTemporaryFile() as actual:
            self.assertTrue(Path(actual.name).resolve().is_relative_to(Path(result["temporary"])))

    def test_child_process_inherits_local_temp_and_creates_its_file_on_e(self):
        result = configure_storage(self.data)
        script = (
            "import json,os,tempfile; "
            "f=tempfile.NamedTemporaryFile(); "
            "print(json.dumps({'temp':os.environ['TEMP'],"
            "'tmp':os.environ['TMP'],'tmpdir':os.environ['TMPDIR'],"
            "'actual':f.name,'data':os.environ['MEDIA_DATA_DIR']})); f.close()"
        )
        child = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script],
                               cwd=PROJECT_ROOT, capture_output=True, text=True,
                               encoding="utf-8", check=True, timeout=15)
        observed = json.loads(child.stdout)
        expected_temp = Path(result["temporary"])
        for key in ("temp", "tmp", "tmpdir"):
            self.assertEqual(Path(observed[key]), expected_temp)
        self.assertTrue(Path(observed["actual"]).resolve().is_relative_to(expected_temp))
        self.assertEqual(Path(observed["actual"]).drive.upper(), "E:")
        self.assertEqual(Path(observed["data"]), self.data.resolve())

    def test_worker_environment_is_independent_of_parent_globals(self):
        previous_environment = dict(os.environ)
        previous_tempdir = tempfile.tempdir
        previous_bytecode = sys.dont_write_bytecode
        result = worker_environment(self.data)
        self.assertEqual(dict(os.environ), previous_environment)
        self.assertEqual(tempfile.tempdir, previous_tempdir)
        self.assertEqual(sys.dont_write_bytecode, previous_bytecode)
        for key in ROUTED_ENV:
            with self.subTest(environment=key):
                destination = Path(result[key]).resolve()
                self.assertTrue(destination.is_relative_to(self.data.resolve()))
                self.assertTrue(destination.is_dir())
        result["TEMP"] = "changed-only-in-returned-dictionary"
        self.assertEqual(dict(os.environ), previous_environment)

    def test_discover_allows_explicit_test_storage_override(self):
        previous_temp = {name: os.environ[name] for name in ("TEMP", "TMP", "TMPDIR", "MEDIA_DATA_DIR")}
        previous_tempdir = tempfile.tempdir
        previous_bytecode = sys.dont_write_bytecode
        with patch("framekeep.config.resolve_binary", return_value=None), \
                patch("framekeep.config.importlib.util.find_spec", return_value=None), \
                patch("framekeep.config.shutil.which", return_value=None):
            settings = Settings.discover(data_dir=self.data)
        self.assertEqual(settings.data_dir, self.data.resolve())
        self.assertEqual({name: os.environ[name] for name in previous_temp}, previous_temp)
        self.assertEqual(tempfile.tempdir, previous_tempdir)
        self.assertEqual(sys.dont_write_bytecode, previous_bytecode)
        self.assertTrue((self.data / "downloads").is_dir())
        self.assertTrue((self.data / "tmp").is_dir())

    @unittest.skipUnless(os.name == "nt", "Windows drive restriction")
    def test_explicit_c_data_directory_is_rejected_before_any_creation(self):
        with patch("framekeep.storage.Path.mkdir") as mkdir:
            with self.assertRaisesRegex(RuntimeError, "C 盘"):
                configure_storage(Path("C:/framekeep-test-must-not-create"))
            mkdir.assert_not_called()

    def test_versions_keep_separate_default_data_directories(self):
        paths = []
        for project in PROJECTS:
            if not (project / "framekeep" / "storage.py").is_file():
                self.skipTest("Both deployed versions are needed for the isolation check")
            child = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", "-c",
                 "from framekeep.storage import default_data_dir; print(default_data_dir())"],
                cwd=project, capture_output=True, text=True, encoding="utf-8", check=True, timeout=15)
            observed = Path(child.stdout.strip()).resolve()
            self.assertEqual(observed, (project / "data").resolve())
            paths.append(observed)
        self.assertNotEqual(paths[0], paths[1])
        self.assertFalse(paths[0].is_relative_to(paths[1]))
        self.assertFalse(paths[1].is_relative_to(paths[0]))

    def test_managed_browser_downloads_profile_and_cache_are_local(self):
        paths = configure_storage(self.data)
        executable = str(self.fixture / "browser.exe")
        url = "http://127.0.0.1:8773/"
        command = build_browser_command(executable, self.data, url)
        self.assertIsInstance(command, list)
        self.assertEqual(command[0], executable)
        self.assertIn(url, command)
        arguments = {entry.split("=", 1)[0]: entry.split("=", 1)[1]
                     for entry in command[1:] if isinstance(entry, str) and "=" in entry}
        self.assertEqual(Path(arguments["--user-data-dir"]), Path(paths["browser_profile"]))
        for option in ("--disk-cache-dir", "--crash-dumps-dir"):
            self.assertTrue(Path(arguments[option]).resolve().is_relative_to(self.data.resolve()))
            self.assertTrue(Path(arguments[option]).is_dir())
        preferences = Path(paths["browser_profile"]) / "Default" / "Preferences"
        values = json.loads(preferences.read_text(encoding="utf-8"))
        self.assertEqual(Path(values["download"]["default_directory"]),
                         Path(paths["browser_downloads"]))
        for entry in command:
            self.assertNotIn("--no-sandbox", entry)
            self.assertNotIn("--disable-web-security", entry)
            self.assertNotIn("--ignore-certificate-errors", entry)
            self.assertNotIn("--disable-setuid-sandbox", entry)

    def make_symlink(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("This Windows account cannot create symlink fixtures: " + str(exc))

    def test_cache_directory_symlink_cannot_escape_storage_root(self):
        self.data.mkdir()
        outside = self.fixture / "outside-cache"
        outside.mkdir()
        self.make_symlink(self.data / "cache", outside, directory=True)
        with self.assertRaises(RuntimeError):
            worker_environment(self.data)
        self.assertEqual(list(outside.iterdir()), [])

    def test_preferences_file_symlink_cannot_escape_storage_root(self):
        paths = configure_storage(self.data)
        preferences = Path(paths["browser_profile"]) / "Default" / "Preferences"
        preferences.parent.mkdir(parents=True)
        outside = self.fixture / "outside-preferences.json"
        contents = '{"download":{"default_directory":"external"}}'
        outside.write_text(contents, encoding="utf-8")
        self.make_symlink(preferences, outside)
        with self.assertRaises(RuntimeError):
            build_browser_command("browser.exe", self.data, "http://127.0.0.1:8773/")
        self.assertEqual(outside.read_text(encoding="utf-8"), contents)


if __name__ == "__main__":
    unittest.main()
