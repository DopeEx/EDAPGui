import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "tools" / "windows_updater.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_powershell(command, *, check=True):
    env = os.environ.copy()
    env["EDFC_TEST_UPDATER"] = str(UPDATER)
    encoded_command = base64.b64encode(
        (
            f"try {{ . $env:EDFC_TEST_UPDATER -Library; {command} }} "
            "catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }; exit 0"
        ).encode("utf-16-le")
    ).decode("ascii")
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded_command,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if check and result.returncode:
        raise AssertionError(f"PowerShell failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result


def put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_installation(root):
    put(root / "src" / "core" / "version.py", '__version__ = "1.0.0"\n')
    put(root / "src" / "main.py", "old main\n")
    put(root / "tools" / "windows_updater.ps1", "installed updater\n")
    put(root / "update_edfc.bat", "protocol 1 launcher\n")
    put(root / "start_edfc.bat", "start\n")
    put(root / "install_requirements.bat", "install\n")
    put(root / "requirements.txt", "old-dependency==1\n")
    put(root / "README.md", "old readme\n")
    put(root / "user_data" / "settings.json", "{}\n")
    put(root / "venv" / "marker.txt", "keep\n")
    put(root / "custom.txt", "keep\n")


def make_package(root, *, version="1.1.0", requirements="new-dependency==2\n", updater=True):
    put(root / "src" / "core" / "version.py", f'__version__ = "{version}"\n')
    put(root / "src" / "main.py", "new main\n")
    put(root / "src" / "gui" / "main_window.py", "main window\n")
    put(root / "assets" / "icons" / "edfc.png", "icon\n")
    put(root / "data" / "configs" / "resolution.json", "{}\n")
    put(root / "LICENSE", "license\n")
    put(root / "assets" / "new.dat", "asset\n")
    put(root / "start_edfc.bat", "new start\n")
    put(root / "install_requirements.bat", "new installer\n")
    put(root / "requirements.txt", requirements)
    put(root / "README.md", "new readme\n")
    if updater:
        put(root / "tools" / "windows_updater.ps1", "function Invoke-Updater { }\n")
        put(root / "update_edfc.bat", "new launcher\n")
    return root


@unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "requires Windows PowerShell")
class WindowsUpdaterTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "user_data" / "debug"
        parent.mkdir(parents=True, exist_ok=True)
        workspace = tempfile.TemporaryDirectory(prefix="updater tests-", dir=parent)
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)

    def test_library_flag_and_strict_immutable_arguments(self):
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(UPDATER),
                "-Library",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

        invalid = run_powershell(
            "$null = Assert-InvocationArguments -Channel release -Revision v1.2.3; "
            "try { Assert-InvocationArguments -Channel release -Revision v1.2.4x; exit 9 } "
            "catch { if ($_.Exception.Message -notmatch 'Invalid release tag') { throw }; exit 0 }",
            check=False,
        )
        self.assertEqual(invalid.returncode, 0, invalid.stdout + invalid.stderr)
        invalid_sha = run_powershell(
            "try { Assert-InvocationArguments -Channel main -Revision ('a' * 39); exit 9 } "
            "catch { if ($_.Exception.Message -notmatch 'exact 40-character') { throw }; exit 0 }",
            check=False,
        )
        self.assertEqual(invalid_sha.returncode, 0, invalid_sha.stdout + invalid_sha.stderr)

    def test_launcher_rejects_extra_argument(self):
        result = subprocess.run(
            [os.environ["COMSPEC"], "/d", "/c", str(ROOT / "update_edfc.bat"), "release", "v1.2.3", "extra"],
            cwd=ROOT,
            input="\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid arguments", result.stdout)

    def test_install_preserves_local_state_and_warns_for_changed_requirements(self):
        install = self.workspace / "install"
        package = self.workspace / "package"
        make_installation(install)
        make_package(package)
        put(install / "fix_keybinds.bat", "obsolete\n")
        command = (
            f"$root = {ps_quote(install)}; "
            "Acquire-InstallationLock -Root $root -WaitSeconds 0 | Out-Null; "
            "try { Invoke-ManagedExchange -Root $root "
            f"-PackageRoot {ps_quote(package)} -Channel release -Revision v1.1.0 -Version 1.1.0 | Out-Null "
            "} finally { Release-InstallationLock -Root $root }"
        )
        result = run_powershell(command)
        self.assertIn("IMPORTANT: Python requirements changed", result.stdout)
        self.assertEqual((install / "src" / "main.py").read_text(encoding="utf-8"), "new main\n")
        self.assertTrue((install / "user_data" / "settings.json").exists())
        self.assertTrue((install / "venv" / "marker.txt").exists())
        self.assertEqual((install / "custom.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((install / "fix_keybinds.bat").exists())
        self.assertEqual((install / "update_edfc.bat").read_text(encoding="utf-8"), "protocol 1 launcher\n")
        metadata = json.loads((install / ".update" / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(metadata),
            {"channel", "revision", "version", "installedAt"},
        )
        self.assertEqual(metadata["revision"], "v1.1.0")
        self.assertEqual(metadata["version"], "1.1.0")
        self.assertTrue(metadata["installedAt"])
        backups = list((install / ".update" / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "src" / "main.py").read_text(encoding="utf-8"), "old main\n")
        self.assertEqual(
            (install / ".update" / "recovery" / "windows_updater.ps1").read_text(encoding="utf-8"),
            "installed updater\n",
        )

    def test_failed_exchange_rolls_back_and_interrupted_exchange_recovers_under_lock(self):
        install = self.workspace / "install"
        package = self.workspace / "package"
        make_installation(install)
        make_package(package)
        run_powershell(
            f"$root = {ps_quote(install)}; Acquire-InstallationLock -Root $root -WaitSeconds 0 | Out-Null; "
            "try { try { Invoke-ManagedExchange -Root $root "
            f"-PackageRoot {ps_quote(package)} -Channel release -Revision v1.1.0 -Version 1.1.0 -FaultAfterMoves 1; "
            "exit 9 } catch { if ($_.Exception.Message -notmatch 'Injected exchange failure') { throw } } "
            "} finally { Release-InstallationLock -Root $root }"
        )
        self.assertEqual((install / "src" / "main.py").read_text(encoding="utf-8"), "old main\n")
        self.assertFalse((install / ".update" / "pending.json").exists())

        recovery = run_powershell(
            f"$root = {ps_quote(install)}; "
            "try { Recover-PendingTransaction -Root $root; exit 9 } "
            "catch { if ($_.Exception.Message -notmatch 'lock must be held') { throw } }; "
            "$id = 'a' * 32; $stage = Get-TransactionDirectory -Root $root -Id $id -Kind staging; "
            "$backup = Get-TransactionDirectory -Root $root -Id $id -Kind backups; "
            "[void][IO.Directory]::CreateDirectory((Join-Path $stage 'new')); "
            "[void][IO.Directory]::CreateDirectory($backup); "
            "[IO.File]::WriteAllText((Join-Path (Join-Path $stage 'new') 'src'), 'unused'); "
            "$entries = @([pscustomobject]@{ name='src'; kind='Directory'; hadOriginal=$true; hasNew=$true }); "
            "$record = [pscustomobject]@{ id=$id; channel='release'; revision='v1.1.0'; version='1.1.0'; "
            "installedAt='2026-01-01T00:00:00.0000000Z'; previousMetadataBase64=$null; entries=$entries }; "
            "Write-AtomicJson -Path (Get-PendingPath $root) -Value $record; "
            "Move-Item -LiteralPath (Join-Path $root 'src') -Destination (Join-Path $backup 'src'); "
            "[void][IO.Directory]::CreateDirectory((Join-Path $root 'src')); "
            "[IO.File]::WriteAllText((Join-Path (Join-Path $root 'src') 'main.py'), 'interrupted'); "
            "Acquire-InstallationLock -Root $root -WaitSeconds 0 | Out-Null; "
            "try { Recover-PendingTransaction -Root $root | Out-Null } finally { Release-InstallationLock $root }"
        )
        self.assertIn("Recovering interrupted update", recovery.stdout)
        self.assertEqual((install / "src" / "main.py").read_text(encoding="utf-8"), "old main\n")
        self.assertFalse((install / ".update" / "pending.json").exists())

    def test_lock_contention_blocks_recovery_until_lock_is_held(self):
        install = self.workspace / "install"
        make_installation(install)
        result = run_powershell(
            f"$root = {ps_quote(install)}; "
            "[void][IO.Directory]::CreateDirectory((Join-Path $root '.update')); "
            "$handle = [IO.File]::Open((Join-Path $root '.update\\installation.lock'), "
            "[IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None); "
            "try { try { Acquire-InstallationLock -Root $root -WaitSeconds 0; exit 9 } "
            "catch { if ($_.Exception.Message -notmatch 'Could not acquire') { throw } }; "
            "try { Recover-PendingTransaction -Root $root; exit 8 } "
            "catch { if ($_.Exception.Message -notmatch 'lock must be held') { throw } } "
            "} finally { $handle.Dispose() }; "
            "Acquire-InstallationLock -Root $root -WaitSeconds 0 | Out-Null; "
            "try { if (Recover-PendingTransaction -Root $root) { exit 7 } } "
            "finally { Release-InstallationLock $root }"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_incomplete_recovery_keeps_journal_for_manual_repair(self):
        install = self.workspace / "install"
        make_installation(install)
        run_powershell(
            f"$root = {ps_quote(install)}; "
            "Acquire-InstallationLock -Root $root -WaitSeconds 0 | Out-Null; "
            "try { "
            "$record = [pscustomobject]@{ id=('a' * 32); channel='release'; revision='v1.1.0'; "
            "version='1.1.0'; previousMetadataBase64=$null; "
            "entries=@([pscustomobject]@{name='README.md'; kind='File'; hadOriginal=$true; hasNew=$true}) }; "
            "Write-AtomicJson -Path (Get-PendingPath $root) -Value $record; "
            "Remove-Item -LiteralPath (Join-Path $root 'README.md'); "
            "try { Recover-PendingTransaction $root; throw 'Recovery must fail' } "
            "catch { if ($_.Exception.Message -notmatch 'both the installed path and its backup are missing') { throw } } "
            "} finally { Release-InstallationLock $root }"
        )
        self.assertTrue((install / ".update" / "pending.json").is_file())
        self.assertEqual((install / "src" / "main.py").read_text(encoding="utf-8"), "old main\n")

    def test_safe_archive_rejects_traversal_duplicates_and_accepts_single_root(self):
        archive = self.workspace / "safe.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("EDFlightControl-main/src/main.py", "ok")
        output = self.workspace / "expanded"
        command = (
            f"$root = Expand-SafeArchive -ArchivePath {ps_quote(archive)} -Destination {ps_quote(output)}; "
            "if (-not (Test-Path -LiteralPath (Join-Path $root 'src\\main.py'))) { exit 9 }"
        )
        self.assertEqual(run_powershell(command).returncode, 0)

        for entries in (
            [("../outside.txt", "bad")],
            [("Pkg/src/main.py", "one"), ("pkg/SRC/main.py", "two")],
            [("Pkg/main.py", "one"), ("Other/main.py", "two")],
        ):
            bad_archive = self.workspace / "unsafe.zip"
            with zipfile.ZipFile(bad_archive, "w") as bundle:
                for name, contents in entries:
                    bundle.writestr(name, contents)
            destination = self.workspace / "bad-expanded"
            result = run_powershell(
                f"try {{ Expand-SafeArchive -ArchivePath {ps_quote(bad_archive)} -Destination {ps_quote(destination)}; exit 9 }} "
                "catch { if ($_.Exception.Message -notmatch 'unsafe|duplicate|exactly one|beneath') { throw }; exit 0 }",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if destination.exists():
                shutil.rmtree(destination)

    def test_old_release_without_updater_keeps_installed_bootstrap(self):
        install = self.workspace / "install"
        package = self.workspace / "old-package"
        make_installation(install)
        make_package(package, version="2.0.0", requirements="old-dependency==1\n", updater=False)
        put(package / "tools" / "old-tool.ps1", "Write-Host 'old tool'\n")
        (install / "requirements.txt").write_bytes(b"old-dependency==1\r\n")
        result = run_powershell(
            f"$root = {ps_quote(install)}; Acquire-InstallationLock -Root $root -WaitSeconds 0 | Out-Null; "
            "try { Invoke-ManagedExchange -Root $root "
            f"-PackageRoot {ps_quote(package)} -Channel release -Revision v2.0.0 -Version 2.0.0 | Out-Null "
            "} finally { Release-InstallationLock $root }"
        )
        self.assertIn("Requirements are unchanged", result.stdout)
        self.assertEqual((install / "tools" / "windows_updater.ps1").read_text(encoding="utf-8"), "installed updater\n")
        self.assertEqual((install / "update_edfc.bat").read_text(encoding="utf-8"), "protocol 1 launcher\n")
        self.assertTrue((install / "tools" / "old-tool.ps1").exists())

    def test_main_to_release_and_reinstall_replace_existing_build_metadata(self):
        install = self.workspace / "install"
        package = self.workspace / "package"
        make_installation(install)
        make_package(package, requirements="old-dependency==1\n")
        sha = "a" * 40
        result = run_powershell(
            f"$root = {ps_quote(install)}; Acquire-InstallationLock -Root $root -WaitSeconds 0 | Out-Null; "
            "try { Invoke-ManagedExchange -Root $root "
            f"-PackageRoot {ps_quote(package)} -Channel main -Revision {sha} -Version 1.1.0 "
            "-CommitDate '2026-11-17T11:42:00Z' "
            "-CommitMessage ([string]::Join([Environment]::NewLine, @('Commit subject','body'))) | Out-Null "
            "} finally { Release-InstallationLock -Root $root }"
        )
        self.assertIn("Requirements are unchanged", result.stdout)
        metadata = json.loads((install / ".update" / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["revision"], sha)
        self.assertEqual(metadata["commitDate"], "2026-11-17T11:42:00Z")
        self.assertEqual(metadata["commitMessage"], "Commit subject")

        release_package = make_package(
            self.workspace / "release-package",
            version="2.0.0",
            requirements="old-dependency==1\n",
            updater=False,
        )
        put(release_package / "src" / "main.py", "release main\n")
        for attempt in range(2):
            with self.subTest(release_install=attempt + 1):
                result = run_powershell(
                    f"$root = {ps_quote(install)}; "
                    "Acquire-InstallationLock -Root $root -WaitSeconds 0 | Out-Null; "
                    "try { Invoke-ManagedExchange -Root $root "
                    f"-PackageRoot {ps_quote(release_package)} -Channel release "
                    "-Revision v2.0.0 -Version 2.0.0 | Out-Null "
                    "} finally { Release-InstallationLock -Root $root }"
                )
                self.assertIn("Installed release target v2.0.0", result.stdout)
                self.assertEqual((install / "src" / "main.py").read_text(encoding="utf-8"), "release main\n")
                metadata = json.loads((install / ".update" / "installed.json").read_text(encoding="utf-8"))
                self.assertEqual(metadata["channel"], "release")
                self.assertEqual(metadata["revision"], "v2.0.0")
                self.assertEqual(metadata["version"], "2.0.0")
                self.assertNotIn("commitDate", metadata)
                self.assertNotIn("commitMessage", metadata)
                self.assertFalse((install / ".update" / "pending.json").exists())
                self.assertEqual(list((install / ".update").glob("*.tmp")), [])

    def test_repairs_missing_program_and_requirements_files(self):
        install = self.workspace / "install"
        package = make_package(self.workspace / "package")
        make_installation(install)
        (install / "src" / "main.py").unlink()
        (install / "requirements.txt").unlink()
        result = run_powershell(
            f"$root = {ps_quote(install)}; Acquire-InstallationLock -Root $root -WaitSeconds 0 | Out-Null; "
            "try { Invoke-ManagedExchange -Root $root "
            f"-PackageRoot {ps_quote(package)} -Channel release -Revision v1.1.0 -Version 1.1.0 | Out-Null "
            "} finally { Release-InstallationLock -Root $root }"
        )
        self.assertIn("IMPORTANT: Python requirements changed", result.stdout)
        self.assertEqual((install / "src" / "main.py").read_text(encoding="utf-8"), "new main\n")
        self.assertTrue((install / "requirements.txt").exists())

    def test_package_validation_accepts_missing_updater_but_rejects_broken_code(self):
        package = make_package(self.workspace / "package")
        updater = package / "tools" / "windows_updater.ps1"
        put(updater, "function broken {")
        run_powershell(
            f"$p = {ps_quote(package)}; "
            "try { Assert-PackageStructure -PackageRoot $p -Channel release "
            "-Revision v1.1.0 -ReleaseVersion 1.1.0; throw 'Missing syntax rejection' } "
            "catch { if ($_.Exception.Message -notmatch 'invalid PowerShell syntax') { throw } }; "
            "Remove-Item -LiteralPath (Join-Path $p 'tools\\windows_updater.ps1'); "
            "$v = Assert-PackageStructure -PackageRoot $p -Channel main -Revision ('a' * 40); "
            "if ($v -ne '1.1.0') { throw \"Unexpected main version: $v\" }; "
            "$v = Assert-PackageStructure -PackageRoot $p -Channel release "
            "-Revision v1.1.0 -ReleaseVersion 1.1.0; if ($v -ne '1.1.0') { throw \"Unexpected version: $v\" }; "
            "Set-Content -LiteralPath (Join-Path $p 'tools\\windows_updater.ps1') -Value 'function Invoke-Updater {}'; "
            "Remove-Item -LiteralPath (Join-Path $p 'update_edfc.bat'); "
            "$v = Assert-PackageStructure -PackageRoot $p -Channel main -Revision ('a' * 40); "
            "if ($v -ne '1.1.0') { throw \"Unexpected main version without launcher: $v\" }; "
            "Remove-Item -LiteralPath (Join-Path $p 'src\\gui\\main_window.py'); "
            "try { Assert-PackageStructure -PackageRoot $p -Channel release "
            "-Revision v1.1.0 -ReleaseVersion 1.1.0; throw 'Missing GUI accepted' } "
            "catch { if ($_.Exception.Message -notmatch 'src/gui/main_window.py') { throw } }"
        )

    def test_resolved_targets_use_requested_revision_and_do_not_fallback(self):
        sha = "a" * 40
        run_powershell(
            "$script:requests = @(); "
            "function Get-GitHubJson([string]$Url) { "
            "$script:requests += $Url; "
            "if ($Url -like '*/commits/*') { "
            f"return [pscustomobject]@{{ sha='{sha}'; commit=[pscustomobject]@{{ "
            "committer=[pscustomobject]@{date='2026-11-17T11:42:00Z'}; message=\"Subject`nBody\" } }; "
            "} "
            "return [pscustomobject]@{ tag_name='v1.1.0'; draft=$false; prerelease=$false; "
            "assets=@([pscustomobject]@{name='EDFlightControl-1.1.0.zip'; "
            "browser_download_url='https://github.com/DopeEx/EDFlightControl/releases/download/v1.1.0/EDFlightControl-1.1.0.zip'; digest=''}) } }; "
            f"$main = Get-ResolvedTarget -Channel main -Revision '{sha}'; "
            f"if ($main.Revision -cne '{sha}' -or $main.CommitMessage -cne 'Subject' "
            f"-or $script:requests[-1] -notlike '*/commits/{sha}') {{ throw 'Wrong main target' }}; "
            "$release = Get-ResolvedTarget -Channel release -Revision v1.1.0; "
            "if ($release.Revision -cne 'v1.1.0' -or $script:requests[-1] -notlike '*/releases/tags/v1.1.0') { throw 'Wrong release target' }; "
            "try { Get-ResolvedTarget -Channel release -Revision v1.2.0; throw 'Mismatch accepted' } "
            "catch { if ($_.Exception.Message -notmatch 'different release tag') { throw } }; "
            "try { Get-ResolvedTarget -Channel main -Revision ('b' * 40); throw 'Mismatch accepted' } "
            "catch { if ($_.Exception.Message -notmatch 'exact requested main commit') { throw } }; "
            'if ($script:requests.Count -ne 4) { throw "Unexpected request count: $($script:requests.Count)" }'
        )

    def test_legacy_process_detection_and_updater_lock(self):
        install = self.workspace / "install"
        make_installation(install)
        run_powershell(
            f"$root = {ps_quote(install)}; "
            "function Get-CimInstance { "
            "[pscustomobject]@{ Name='python.exe'; ProcessId=123; "
            "ExecutablePath=(Join-Path $root 'venv\\Scripts\\python.exe'); "
            "CommandLine='\"venv\\Scripts\\python.exe\" src\\main.py' } }; "
            "try { Assert-NoLegacyAppProcess $root; exit 9 } "
            "catch { if ($_.Exception.Message -notmatch 'legacy EDFC process') { throw } }; "
            "function Get-CimInstance { throw 'WMI unavailable' }; "
            "try { Assert-NoLegacyAppProcess $root; exit 8 } "
            "catch { if ($_.Exception.Message -notmatch 'Unable to query Windows processes') { throw } }; "
            "$lock = Acquire-UpdaterLock $root; "
            "try { try { Acquire-UpdaterLock $root; exit 7 } "
            "catch { if ($_.Exception.Message -notmatch 'already running') { throw } } } "
            "finally { $lock.Dispose() }"
        )

    def test_invoke_updater_end_to_end_with_mocked_download(self):
        sha = "a" * 40
        for channel, bundled_updater, tools_directory in (
            ("release", True, True),
            ("main", False, True),
            ("main", False, False),
        ):
            with self.subTest(channel=channel, bundled_updater=bundled_updater, tools_directory=tools_directory):
                case = self.workspace / f"{channel}-{tools_directory}"
                install = case / "install"
                package = make_package(case / "package", updater=bundled_updater)
                if tools_directory:
                    put(package / "tools" / "example.py", "pass\n")
                make_installation(install)
                revision = "v1.1.0" if channel == "release" else sha
                archive = case / "download.zip"
                with zipfile.ZipFile(archive, "w") as bundle:
                    for path in package.rglob("*"):
                        if path.is_file():
                            bundle.write(path, "EDFlightControl-snapshot/" + path.relative_to(package).as_posix())
                result = run_powershell(
                    f"$root = {ps_quote(install)}; $archive = {ps_quote(archive)}; "
                    "function Assert-NotGitCheckout { param($Root) }; "
                    "function Assert-NoLegacyAppProcess { param($Root) }; "
                    "function Get-GitHubJson { param($Url) "
                    "if ($Url -like '*/commits/*') { "
                    f"if ($Url -notlike '*/commits/{sha}') {{ throw 'Wrong main commit' }}; "
                    f"return [pscustomobject]@{{sha='{sha}'; commit=[pscustomobject]@{{ "
                    "committer=[pscustomobject]@{date='2026-11-17T11:42:00Z'}; message='Development snapshot'} } }; "
                    "if ($Url -notlike '*/releases/tags/v1.1.0') { throw 'Wrong release tag' }; "
                    "[pscustomobject]@{tag_name='v1.1.0'; draft=$false; prerelease=$false; "
                    "assets=@([pscustomobject]@{name='EDFlightControl-1.1.0.zip'; "
                    "browser_download_url='https://github.com/download.zip'; digest=''})} }; "
                    "function Get-SafeHttpBytes { param($Url, $MaximumBytes, $Destination) "
                    "Copy-Item -LiteralPath $archive -Destination $Destination }; "
                    "function Read-Host { param($Prompt) return 'y' }; "
                    f"Invoke-Updater -Root $root -Channel {channel} -Revision {revision}"
                )
                self.assertEqual((install / "src" / "main.py").read_text(encoding="utf-8"), "new main\n")
                metadata = json.loads((install / ".update" / "installed.json").read_text(encoding="utf-8"))
                self.assertEqual(metadata["revision"], revision)
                self.assertEqual(metadata["channel"], channel)
                self.assertTrue((install / ".update" / "updater.log").exists())
                self.assertFalse((install / ".update" / "pending.json").exists())
                if not bundled_updater:
                    self.assertIn("The installed standalone updater will be retained", result.stdout)
                    self.assertEqual(
                        (install / "tools" / "windows_updater.ps1").read_text(encoding="utf-8"),
                        "installed updater\n",
                    )
                self.assertEqual((install / "update_edfc.bat").read_text(encoding="utf-8"), "protocol 1 launcher\n")


if __name__ == "__main__":
    unittest.main()
