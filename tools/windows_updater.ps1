param(
    [switch]$Library,
    [string]$Root,
    [string]$Channel,
    [string]$Revision
)

$ErrorActionPreference = 'Stop'
$script:HeldInstallationLocks = @{}
$script:AllowedHosts = @(
    'api.github.com',
    'github.com',
    'codeload.github.com',
    'release-assets.githubusercontent.com',
    'objects.githubusercontent.com',
    'github-releases.githubusercontent.com'
)
$script:ManagedDirectories = @('src', 'assets', 'data', 'docs', 'licenses', 'tools')
$script:ManagedFiles = @(
    'LICENSE',
    'README.md',
    'CHANGELOG.md',
    'pyproject.toml',
    'requirements.txt',
    'requirements-ci.txt',
    'requirements-dev.txt',
    'start_edfc.bat',
    'install_requirements.bat',
    'fix_keybinds.bat'
)
$script:RequiredPackageFiles = @(
    'src/core/version.py',
    'src/main.py',
    'src/gui/main_window.py',
    'assets/icons/edfc.png',
    'data/configs/resolution.json',
    'LICENSE',
    'start_edfc.bat',
    'install_requirements.bat',
    'requirements.txt'
)
$script:SemVerPattern = '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$'

function Get-NormalizedRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "Installation directory does not exist: $resolved"
    }
    if ($resolved -eq [System.IO.Path]::GetPathRoot($resolved)) {
        return $resolved
    }
    $trimmed = $resolved.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    if (-not $trimmed) {
        return [System.IO.Path]::GetPathRoot($resolved)
    }
    return $trimmed
}

function Assert-NotGitCheckout {
    param([Parameter(Mandatory = $true)][string]$Root)

    $current = Get-NormalizedRoot $Root
    while ($current) {
        $gitPath = Join-Path $current '.git'
        if (Test-Path -LiteralPath $gitPath) {
            throw "Refusing to update a Git checkout (found $gitPath)."
        }
        $parent = [System.IO.Directory]::GetParent($current)
        if ($null -eq $parent -or $parent.FullName -eq $current) {
            break
        }
        $current = $parent.FullName
    }
}

function Assert-InvocationArguments {
    param(
        [AllowEmptyString()][string]$Channel,
        [AllowEmptyString()][string]$Revision
    )

    if ([string]::IsNullOrEmpty($Channel) -and [string]::IsNullOrEmpty($Revision)) {
        return [pscustomobject]@{ Interactive = $true; Channel = ''; Revision = '' }
    }
    if ($Channel -cnotin @('release', 'main')) {
        throw "Invalid updater channel '$Channel'. Expected 'release' or 'main'."
    }
    if ([string]::IsNullOrEmpty($Revision)) {
        throw 'A channel supplied by another program must include its exact revision.'
    }
    if ($Channel -eq 'release' -and $Revision -cnotmatch '^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') {
        throw "Invalid release tag '$Revision'. Expected a stable vMAJOR.MINOR.PATCH tag."
    }
    if ($Channel -eq 'main' -and $Revision -cnotmatch '^[0-9a-f]{40}$') {
        throw "Invalid main revision '$Revision'. Expected its exact 40-character lowercase commit SHA."
    }
    return [pscustomobject]@{ Interactive = $false; Channel = $Channel; Revision = $Revision }
}

function Test-InstallationLockHeld {
    param([Parameter(Mandatory = $true)][string]$Root)

    $key = (Get-NormalizedRoot $Root).ToLowerInvariant()
    return $script:HeldInstallationLocks.ContainsKey($key)
}

function Acquire-InstallationLock {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [ValidateRange(0, 3600)][int]$WaitSeconds = 300
    )

    $normalized = Get-NormalizedRoot $Root
    $updateDirectory = Join-Path $normalized '.update'
    [void][System.IO.Directory]::CreateDirectory($updateDirectory)
    $lockPath = Join-Path $updateDirectory 'installation.lock'
    $key = $normalized.ToLowerInvariant()
    if ($script:HeldInstallationLocks.ContainsKey($key)) {
        throw 'This updater process already holds the installation lock.'
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
    $announced = $false
    while ($true) {
        try {
            $stream = [System.IO.File]::Open(
                $lockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
            $script:HeldInstallationLocks[$key] = $stream
            return $stream
        } catch [System.IO.IOException] {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "Could not acquire $lockPath within $WaitSeconds seconds. Close EDFC and any other updater, then retry."
            }
            if (-not $announced) {
                Write-Host 'EDFC or another updater is using this installation. Close EDFC; the updater will retry automatically.'
                $announced = $true
            }
            Start-Sleep -Seconds 2
        }
    }
}

function Release-InstallationLock {
    param([Parameter(Mandatory = $true)][string]$Root)

    $normalized = Get-NormalizedRoot $Root
    $key = $normalized.ToLowerInvariant()
    if ($script:HeldInstallationLocks.ContainsKey($key)) {
        $stream = $script:HeldInstallationLocks[$key]
        $script:HeldInstallationLocks.Remove($key)
        $stream.Dispose()
    }
}

function Assert-NoLegacyAppProcess {
    param([Parameter(Mandatory = $true)][string]$Root)

    $mainPath = [System.IO.Path]::GetFullPath((Join-Path $Root 'src\main.py')).Replace('/', '\').TrimEnd('\')
    $venvExecutables = @(
        [System.IO.Path]::GetFullPath((Join-Path $Root 'venv\Scripts\python.exe')),
        [System.IO.Path]::GetFullPath((Join-Path $Root 'venv\Scripts\pythonw.exe'))
    )
    try {
        $processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    } catch {
        throw "Unable to query Windows processes to check for a legacy EDFC instance: $($_.Exception.Message)"
    }
    foreach ($process in $processes) {
        if ($process.Name -notin @('python.exe', 'pythonw.exe')) {
            continue
        }
        $localInterpreter = $process.ExecutablePath -and $process.ExecutablePath -in $venvExecutables
        if ([string]::IsNullOrEmpty($process.CommandLine)) {
            if ($localInterpreter) {
                throw "Cannot inspect command line of the installation's Python process (PID $($process.ProcessId)); close it and retry."
            }
            continue
        }
        $commandLine = $process.CommandLine.Replace('/', '\')
        $index = $commandLine.IndexOf($mainPath, [System.StringComparison]::OrdinalIgnoreCase)
        while ($index -ge 0) {
            $after = $index + $mainPath.Length
            if (($index -eq 0 -or $commandLine[$index - 1] -match '[\s"]') -and
                ($after -eq $commandLine.Length -or $commandLine[$after] -match '[\s"]')) {
                throw "A legacy EDFC process is running from this installation (PID $($process.ProcessId)). Close EDFC and retry."
            }
            $index = $commandLine.IndexOf($mainPath, $index + 1, [System.StringComparison]::OrdinalIgnoreCase)
        }
        if ($commandLine -match '(?:^|[\s"])(?:\.\\)?src\\main\.py(?=$|[\s"])') {
            if ($localInterpreter) {
                throw "A legacy EDFC process is running from this installation (PID $($process.ProcessId)). Close EDFC and retry."
            }
            throw "Python process PID $($process.ProcessId) runs a relative src\main.py; its working directory cannot be verified. Close it and retry."
        }
    }
}

function Acquire-UpdaterLock {
    param([Parameter(Mandatory = $true)][string]$Root)

    $updateDirectory = Get-UpdateDirectory $Root
    [void][System.IO.Directory]::CreateDirectory($updateDirectory)
    try {
        return [System.IO.File]::Open(
            (Join-Path $updateDirectory 'updater.lock'),
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        throw 'Another updater is already running for this installation. Close it before starting another.'
    }
}

function Assert-LockHeld {
    param([Parameter(Mandatory = $true)][string]$Root)
    if (-not (Test-InstallationLockHeld $Root)) {
        throw 'The installation lock must be held before reading or changing updater transaction state.'
    }
}

function Write-AtomicText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )

    $tempPath = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($tempPath, $Text, $encoding)
    if (Test-Path -LiteralPath $Path) {
        # Windows PowerShell converts $null to an empty path for .NET string parameters.
        [System.IO.File]::Replace($tempPath, $Path, [System.Management.Automation.Language.NullString]::Value)
    } else {
        [System.IO.File]::Move($tempPath, $Path)
    }
}

function Write-AtomicJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    Write-AtomicText -Path $Path -Text (ConvertTo-Json -InputObject $Value -Depth 12 -Compress)
}

function Get-UpdateDirectory {
    param([Parameter(Mandatory = $true)][string]$Root)
    return Join-Path (Get-NormalizedRoot $Root) '.update'
}

function Get-PendingPath {
    param([Parameter(Mandatory = $true)][string]$Root)
    return Join-Path (Get-UpdateDirectory $Root) 'pending.json'
}

function Write-UpdaterLog {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet('INFO', 'WARNING', 'ERROR')][string]$Level = 'INFO'
    )

    $updateDirectory = Get-UpdateDirectory $Root
    [void][System.IO.Directory]::CreateDirectory($updateDirectory)
    $line = '{0} [{1}] {2}{3}' -f [DateTime]::UtcNow.ToString('o'), $Level, ($Message -replace '[\r\n]+', ' '), [Environment]::NewLine
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($line)
    $stream = [System.IO.File]::Open(
        (Join-Path $updateDirectory 'updater.log'),
        [System.IO.FileMode]::Append,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::Read
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
    } finally {
        $stream.Dispose()
    }
}

function Get-TransactionDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][ValidateSet('backups', 'staging')][string]$Kind
    )
    if ($Id -notmatch '^[0-9a-f]{32}$') {
        throw 'Invalid updater transaction identifier.'
    }
    return Join-Path (Join-Path (Get-UpdateDirectory $Root) $Kind) $Id
}

function Get-AllowedEntryNames {
    return @($script:ManagedDirectories + $script:ManagedFiles)
}

function Assert-TransactionRecord {
    param([Parameter(Mandatory = $true)]$Record)

    if ($Record.id -notmatch '^[0-9a-f]{32}$') {
        throw 'Pending updater journal has an invalid transaction identifier.'
    }
    if ($Record.channel -notin @('release', 'main') -or $Record.version -notmatch $script:SemVerPattern) {
        throw 'Pending updater journal has invalid target metadata.'
    }
    if (($Record.channel -eq 'release' -and $Record.revision -cnotmatch '^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') -or
        ($Record.channel -eq 'main' -and $Record.revision -cnotmatch '^[0-9a-f]{40}$')) {
        throw 'Pending updater journal has an invalid immutable revision.'
    }
    $allowed = Get-AllowedEntryNames
    if (@($Record.entries).Count -eq 0) {
        throw 'Pending updater journal has no managed entries.'
    }
    foreach ($entry in @($Record.entries)) {
        if ($entry.name -notin $allowed -or $entry.kind -notin @('Directory', 'File')) {
            throw 'Pending updater journal contains an unmanaged path.'
        }
        if ($entry.hadOriginal -isnot [bool] -or $entry.hasNew -isnot [bool]) {
            throw 'Pending updater journal contains invalid exchange state.'
        }
        if (($entry.kind -eq 'Directory' -and $entry.name -notin $script:ManagedDirectories) -or
            ($entry.kind -eq 'File' -and $entry.name -notin $script:ManagedFiles)) {
            throw 'Pending updater journal contains a path of the wrong kind.'
        }
    }
}

function Restore-Transaction {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)]$Record
    )

    Assert-TransactionRecord $Record
    $backupRoot = Get-TransactionDirectory -Root $Root -Id $Record.id -Kind backups
    $stageRoot = Get-TransactionDirectory -Root $Root -Id $Record.id -Kind staging
    $entries = @($Record.entries)
    for ($index = $entries.Count - 1; $index -ge 0; $index--) {
        $entry = $entries[$index]
        $livePath = Join-Path $Root $entry.name
        $backupPath = Join-Path $backupRoot $entry.name
        $stagePath = Join-Path (Join-Path $stageRoot 'new') $entry.name
        if (Test-Path -LiteralPath $backupPath) {
            if (Test-Path -LiteralPath $livePath) {
                Remove-Item -LiteralPath $livePath -Recurse -Force
            }
            [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($livePath))
            Move-Item -LiteralPath $backupPath -Destination $livePath
        } elseif (-not $entry.hadOriginal -and $entry.hasNew -and
            -not (Test-Path -LiteralPath $stagePath) -and (Test-Path -LiteralPath $livePath)) {
            Remove-Item -LiteralPath $livePath -Recurse -Force
        } elseif ($entry.hadOriginal -and -not (Test-Path -LiteralPath $livePath)) {
            throw "Cannot restore '$($entry.name)': both the installed path and its backup are missing."
        }
    }
    $metadataPath = Join-Path (Get-UpdateDirectory $Root) 'installed.json'
    if ($null -eq $Record.previousMetadataBase64) {
        if (Test-Path -LiteralPath $metadataPath) {
            Remove-Item -LiteralPath $metadataPath -Force
        }
    } else {
        $metadataBytes = [Convert]::FromBase64String([string]$Record.previousMetadataBase64)
        [System.IO.File]::WriteAllBytes($metadataPath, $metadataBytes)
    }
}

function Recover-PendingTransaction {
    param([Parameter(Mandatory = $true)][string]$Root)

    $normalized = Get-NormalizedRoot $Root
    Assert-LockHeld $normalized
    $pendingPath = Get-PendingPath $normalized
    if (-not (Test-Path -LiteralPath $pendingPath -PathType Leaf)) {
        return $false
    }
    $record = Get-Content -LiteralPath $pendingPath -Raw | ConvertFrom-Json
    Assert-TransactionRecord $record
    Write-Host "Recovering interrupted update $($record.id); restoring the previous installation."
    Restore-Transaction -Root $normalized -Record $record
    Remove-Item -LiteralPath $pendingPath -Force
    foreach ($kind in @('backups', 'staging')) {
        $path = Get-TransactionDirectory -Root $normalized -Id $record.id -Kind $kind
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }
    return $true
}

function New-TransactionRecord {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][ValidateSet('release', 'main')][string]$Channel,
        [Parameter(Mandatory = $true)][string]$Revision,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][object[]]$Entries
    )

    $normalized = Get-NormalizedRoot $Root
    Assert-LockHeld $normalized
    if (Test-Path -LiteralPath (Get-PendingPath $normalized)) {
        throw 'A pending update must be recovered before another exchange begins.'
    }
    if ($Version -notmatch $script:SemVerPattern) {
        throw "Invalid target package version '$Version'."
    }
    if (($Channel -eq 'release' -and $Revision -cnotmatch '^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') -or
        ($Channel -eq 'main' -and $Revision -cnotmatch '^[0-9a-f]{40}$')) {
        throw 'Target revision does not match the selected update channel.'
    }
    $metadataPath = Join-Path (Get-UpdateDirectory $normalized) 'installed.json'
    $previousMetadata = $null
    if (Test-Path -LiteralPath $metadataPath -PathType Leaf) {
        $previousMetadata = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($metadataPath))
    }
    $record = [pscustomobject]@{
        id = $Id
        channel = $Channel
        revision = $Revision
        version = $Version
        installedAt = [DateTime]::UtcNow.ToString('o')
        previousMetadataBase64 = $previousMetadata
        entries = @($Entries)
    }
    Assert-TransactionRecord $record
    Write-AtomicJson -Path (Get-PendingPath $normalized) -Value $record
    return $record
}

function Expand-SafeArchive {
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$Destination,
        [long]$MaximumExpandedBytes = 2147483648,
        [int]$MaximumEntries = 100000
    )

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $destinationRoot = [System.IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    if (Test-Path -LiteralPath $Destination) {
        throw "Archive destination already exists: $Destination"
    }
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        if ($archive.Entries.Count -eq 0 -or $archive.Entries.Count -gt $MaximumEntries) {
            throw "Archive has an invalid entry count ($($archive.Entries.Count))."
        }
        $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
        $kinds = @{}
        $topNames = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
        $expanded = [long]0
        foreach ($entry in $archive.Entries) {
            $name = [string]$entry.FullName
            if ([string]::IsNullOrEmpty($name) -or $name.Contains('\') -or $name.StartsWith('/') -or
                $name -match '^[A-Za-z]:' -or $name.Contains(':') -or $name -match '[\x00-\x1f]') {
                throw "Archive contains an unsafe path: '$name'."
            }
            $isDirectory = $name.EndsWith('/')
            $trimmed = $name.TrimEnd('/')
            $parts = $trimmed.Split('/')
            $invalidPart = @($parts | Where-Object {
                $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' -or
                $_ -match '[<>:"|?*]' -or $_ -match '[\. ]$' -or
                $_ -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)'
            })
            if (($parts.Length -lt 2 -and -not ($isDirectory -and $parts.Length -eq 1)) -or $invalidPart.Count -gt 0) {
                throw "Archive paths must remain beneath one package directory: '$name'."
            }
            if (-not $seen.Add($trimmed)) {
                throw "Archive contains duplicate case-insensitive paths: '$name'."
            }
            $topNames.Add($parts[0]) | Out-Null
            $unixMode = ($entry.ExternalAttributes -shr 16) -band 0xF000
            if ($unixMode -eq 0xA000) {
                throw "Archive contains a symbolic link: '$name'."
            }
            if (-not $isDirectory) {
                if ($entry.Length -lt 0 -or $entry.Length -gt 536870912) {
                    throw "Archive file exceeds the per-file extraction limit: '$name'."
                }
                $expanded += $entry.Length
                if ($expanded -gt $MaximumExpandedBytes) {
                    throw 'Archive exceeds the total extraction limit.'
                }
                $kinds[$trimmed] = 'File'
            } else {
                $kinds[$trimmed] = 'Directory'
            }
        }
        if ($topNames.Count -ne 1) {
            throw 'Archive must contain exactly one top-level package directory.'
        }
        foreach ($path in @($kinds.Keys)) {
            $parent = $path
            while ($parent.Contains('/')) {
                $parent = $parent.Substring(0, $parent.LastIndexOf('/'))
                if ($kinds.ContainsKey($parent) -and $kinds[$parent] -eq 'File') {
                    throw "Archive path is nested beneath a file: '$parent'."
                }
            }
        }
        [void][System.IO.Directory]::CreateDirectory($Destination)
        foreach ($entry in $archive.Entries) {
            $relative = $entry.FullName.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
            $path = [System.IO.Path]::GetFullPath((Join-Path $Destination $relative))
            if (-not $path.StartsWith($destinationRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Archive path escapes the extraction directory: '$($entry.FullName)'."
            }
            if ($entry.FullName.EndsWith('/')) {
                [void][System.IO.Directory]::CreateDirectory($path)
                continue
            }
            [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($path))
            $inputStream = $entry.Open()
            $outputStream = [System.IO.File]::Open($path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            try {
                $buffer = New-Object byte[] 65536
                $copied = [long]0
                while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $copied += $read
                    if ($copied -gt $entry.Length -or $copied -gt 536870912) {
                        throw "Archive entry exceeded its declared extraction size: '$($entry.FullName)'."
                    }
                    $outputStream.Write($buffer, 0, $read)
                }
                if ($copied -ne $entry.Length) {
                    throw "Archive entry size did not match its header: '$($entry.FullName)'."
                }
            } finally {
                $outputStream.Dispose()
                $inputStream.Dispose()
            }
        }
        $topName = @($topNames.GetEnumerator())[0]
        return Join-Path $Destination $topName
    } finally {
        $archive.Dispose()
    }
}

function Assert-PackageStructure {
    param(
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][string]$Channel,
        [Parameter(Mandatory = $true)][string]$Revision,
        [AllowEmptyString()][string]$ReleaseVersion = ''
    )

    foreach ($relative in $script:RequiredPackageFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot $relative) -PathType Leaf)) {
            throw "Update package is missing required file '$relative'."
        }
    }
    $updaterPath = Join-Path $PackageRoot 'tools\windows_updater.ps1'
    if (Test-Path -LiteralPath $updaterPath -PathType Leaf) {
        $tokens = $null
        $parseErrors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($updaterPath, [ref]$tokens, [ref]$parseErrors)
        if ($parseErrors.Count -gt 0) {
            throw "Downloaded updater has invalid PowerShell syntax: $($parseErrors[0].Message)"
        }
    }
    $versionPath = Join-Path $PackageRoot 'src\core\version.py'
    $source = [System.IO.File]::ReadAllText($versionPath)
    $matches = [regex]::Matches($source, '(?m)^\s*__version__\s*=\s*[''"]([^''"]+)[''"]')
    if ($matches.Count -ne 1) {
        throw 'Could not identify exactly one __version__ in src/core/version.py.'
    }
    $version = $matches[0].Groups[1].Value
    if ($version -notmatch $script:SemVerPattern) {
        throw "Package contains an invalid semantic version '$version'."
    }
    if ($Channel -eq 'release' -and $ReleaseVersion -and $version -ne $ReleaseVersion) {
        throw "Release tag version $ReleaseVersion does not match package version $version."
    }
    return $version
}

function Get-ManagedExchangeEntries {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][string]$Id
    )

    $stageRoot = Get-TransactionDirectory -Root $Root -Id $Id -Kind staging
    $newRoot = Join-Path $stageRoot 'new'
    [void][System.IO.Directory]::CreateDirectory($newRoot)
    $entries = New-Object System.Collections.Generic.List[object]
    foreach ($directory in $script:ManagedDirectories) {
        $source = Join-Path $PackageRoot $directory
        if (-not (Test-Path -LiteralPath $source -PathType Container)) {
            continue
        }
        $destination = Join-Path $newRoot $directory
        Copy-Item -LiteralPath $source -Destination $destination -Recurse
        if ($directory -eq 'tools' -and
            -not (Test-Path -LiteralPath (Join-Path $destination 'windows_updater.ps1') -PathType Leaf)) {
            $installedUpdater = Join-Path $Root 'tools\windows_updater.ps1'
            if (-not (Test-Path -LiteralPath $installedUpdater -PathType Leaf)) {
                throw 'Cannot retain the updater: the target has no updater and the installed copy is missing.'
            }
            Copy-Item -LiteralPath $installedUpdater -Destination (Join-Path $destination 'windows_updater.ps1')
        }
        $entries.Add([pscustomobject]@{ name = $directory; kind = 'Directory'; hadOriginal = (Test-Path -LiteralPath (Join-Path $Root $directory)); hasNew = $true })
    }
    foreach ($file in $script:ManagedFiles) {
        $source = Join-Path $PackageRoot $file
        $hasNew = Test-Path -LiteralPath $source -PathType Leaf
        if ($hasNew) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $newRoot $file)
        }
        $entries.Add([pscustomobject]@{ name = $file; kind = 'File'; hadOriginal = (Test-Path -LiteralPath (Join-Path $Root $file)); hasNew = $hasNew })
    }
    return [pscustomobject]@{ Id = $Id; Root = $stageRoot; NewRoot = $newRoot; Entries = $entries.ToArray() }
}

function Invoke-ManagedExchange {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][ValidateSet('release', 'main')][string]$Channel,
        [Parameter(Mandatory = $true)][string]$Revision,
        [Parameter(Mandatory = $true)][string]$Version,
        [AllowEmptyString()][string]$CommitDate = '',
        [AllowEmptyString()][string]$CommitMessage = '',
        [int]$FaultAfterMoves = 0
    )

    $normalized = Get-NormalizedRoot $Root
    Assert-LockHeld $normalized
    foreach ($required in @('tools\windows_updater.ps1', 'update_edfc.bat')) {
        if (-not (Test-Path -LiteralPath (Join-Path $normalized $required) -PathType Leaf)) {
            throw "Installed program is missing required file '$required'."
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot 'requirements.txt') -PathType Leaf)) {
        throw "Update package is missing required file 'requirements.txt'."
    }
    if ($Channel -eq 'main') {
        $parsedCommitDate = [DateTimeOffset]::MinValue
        if (-not $CommitDate -or
            -not [DateTimeOffset]::TryParse($CommitDate, [ref]$parsedCommitDate) -or
            [string]::IsNullOrWhiteSpace($CommitMessage)) {
            throw 'Main update metadata must include the GitHub commit date and first-line message.'
        }
    }
    $newRequirements = [System.IO.File]::ReadAllText((Join-Path $PackageRoot 'requirements.txt')).Replace("`r`n", "`n")
    $oldRequirementsPath = Join-Path $normalized 'requirements.txt'
    $requirementsChanged = $true
    if (Test-Path -LiteralPath $oldRequirementsPath -PathType Leaf) {
        $oldRequirements = [System.IO.File]::ReadAllText($oldRequirementsPath).Replace("`r`n", "`n")
        $requirementsChanged = $oldRequirements -cne $newRequirements
    }
    $recoveryDirectory = Join-Path (Get-UpdateDirectory $normalized) 'recovery'
    [void][System.IO.Directory]::CreateDirectory($recoveryDirectory)
    Copy-Item -LiteralPath (Join-Path $normalized 'tools\windows_updater.ps1') `
        -Destination (Join-Path $recoveryDirectory 'windows_updater.ps1') -Force
    $id = [Guid]::NewGuid().ToString('N')
    $plan = Get-ManagedExchangeEntries -Root $normalized -PackageRoot $PackageRoot -Id $id
    $entries = @($plan.Entries)
    $record = New-TransactionRecord -Root $normalized -Id $id -Channel $Channel -Revision $Revision -Version $Version -Entries $entries
    $backupRoot = Get-TransactionDirectory -Root $normalized -Id $id -Kind backups
    [void][System.IO.Directory]::CreateDirectory($backupRoot)
    $moves = 0
    try {
        foreach ($entry in $entries) {
            $livePath = Join-Path $normalized $entry.name
            $backupPath = Join-Path $backupRoot $entry.name
            $newPath = Join-Path $plan.NewRoot $entry.name
            if ($entry.hadOriginal) {
                Move-Item -LiteralPath $livePath -Destination $backupPath
                $moves++
                if ($FaultAfterMoves -gt 0 -and $moves -ge $FaultAfterMoves) {
                    throw 'Injected exchange failure.'
                }
            }
            if ($entry.hasNew) {
                Move-Item -LiteralPath $newPath -Destination $livePath
                $moves++
                if ($FaultAfterMoves -gt 0 -and $moves -ge $FaultAfterMoves) {
                    throw 'Injected exchange failure.'
                }
            }
        }
        $metadata = [pscustomobject]@{
            channel = $Channel
            revision = $Revision
            version = $Version
            installedAt = $record.installedAt
        }
        if ($Channel -eq 'main') {
            $commitFirstLine = ($CommitMessage -split '\r?\n')[0]
            $metadata | Add-Member -NotePropertyName commitDate -NotePropertyValue $CommitDate
            $metadata | Add-Member -NotePropertyName commitMessage -NotePropertyValue $commitFirstLine
        }
        Write-AtomicJson -Path (Join-Path (Get-UpdateDirectory $normalized) 'installed.json') -Value $metadata
        Remove-Item -LiteralPath (Get-PendingPath $normalized) -Force
    } catch {
        $exchangeError = $_
        try {
            Restore-Transaction -Root $normalized -Record $record
            Remove-Item -LiteralPath (Get-PendingPath $normalized) -Force
            if (Test-Path -LiteralPath $plan.Root) {
                Remove-Item -LiteralPath $plan.Root -Recurse -Force
            }
        } catch {
            throw "Update failed ($($exchangeError.Exception.Message)); rollback also failed ($($_.Exception.Message)). The pending journal was retained for next-run recovery."
        }
        throw $exchangeError
    }
    if (Test-Path -LiteralPath $plan.Root) {
        Remove-Item -LiteralPath $plan.Root -Recurse -Force
    }
    if ($requirementsChanged) {
        Write-Host ''
        Write-Host 'IMPORTANT: Python requirements changed. Before starting EDFC, run install_requirements.bat (or install requirements.txt in your active environment).' -ForegroundColor Yellow
    } else {
        Write-Host 'Requirements are unchanged.'
    }
    Write-Host "Installed $Channel target $Revision (version $Version)."
    return [pscustomobject]@{ RequirementsChanged = $requirementsChanged; Metadata = $metadata }
}

function Test-AllowedGitHubUri {
    param([Parameter(Mandatory = $true)][Uri]$Uri)
    return $Uri.Scheme -eq 'https' -and $Uri.Host.ToLowerInvariant() -in $script:AllowedHosts
}

function Get-SafeHttpBytes {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][long]$MaximumBytes,
        [string]$Destination
    )

    $uri = [Uri]$Url
    $redirects = 0
    while ($true) {
        if (-not (Test-AllowedGitHubUri $uri)) {
            throw "Refusing non-HTTPS or non-GitHub download URL: $uri"
        }
        $request = [System.Net.HttpWebRequest]::Create($uri)
        $request.AllowAutoRedirect = $false
        $request.UserAgent = 'EDFlightControl-Updater/1'
        $request.Timeout = 30000
        $request.ReadWriteTimeout = 30000
        try {
            $response = [System.Net.HttpWebResponse]$request.GetResponse()
        } catch [System.Net.WebException] {
            if ($null -eq $_.Exception.Response) {
                throw "GitHub request failed for $uri`: $($_.Exception.Message)"
            }
            $response = [System.Net.HttpWebResponse]$_.Exception.Response
        }
        try {
            $status = [int]$response.StatusCode
            if ($status -ge 300 -and $status -lt 400) {
                $location = $response.Headers['Location']
                if (-not $location -or $redirects -ge 5) {
                    throw "GitHub returned an invalid or excessive redirect for $uri."
                }
                $uri = [Uri]::new($uri, $location)
                $redirects++
                continue
            }
            if ($status -lt 200 -or $status -ge 300) {
                throw "GitHub returned HTTP $status for $uri."
            }
            if ($response.ContentLength -gt $MaximumBytes) {
                throw "Download exceeds the $MaximumBytes-byte limit."
            }
            $inputStream = $response.GetResponseStream()
            try {
                if ($Destination) {
                    $outputStream = [System.IO.File]::Open($Destination, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
                    try {
                        $buffer = New-Object byte[] 65536
                        $total = [long]0
                        while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                            $total += $read
                            if ($total -gt $MaximumBytes) {
                                throw 'Download exceeded its maximum allowed size.'
                            }
                            $outputStream.Write($buffer, 0, $read)
                        }
                    } finally {
                        $outputStream.Dispose()
                    }
                    return $null
                }
                $memory = New-Object System.IO.MemoryStream
                try {
                    $buffer = New-Object byte[] 65536
                    $total = [long]0
                    while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $total += $read
                        if ($total -gt $MaximumBytes) {
                            throw 'GitHub response exceeded its maximum allowed size.'
                        }
                        $memory.Write($buffer, 0, $read)
                    }
                    return $memory.ToArray()
                } finally {
                    $memory.Dispose()
                }
            } finally {
                $inputStream.Dispose()
            }
        } finally {
            $response.Dispose()
        }
    }
}

function Get-GitHubJson {
    param([Parameter(Mandatory = $true)][string]$Url)
    $bytes = Get-SafeHttpBytes -Url $Url -MaximumBytes 10485760
    return ([System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json)
}

function Get-ResolvedTarget {
    param(
        [Parameter(Mandatory = $true)][string]$Channel,
        [AllowEmptyString()][string]$Revision
    )

    if ($Channel -eq 'release') {
        if ($Revision) {
            $url = 'https://api.github.com/repos/DopeEx/EDFlightControl/releases/tags/' + [Uri]::EscapeDataString($Revision)
        } else {
            $url = 'https://api.github.com/repos/DopeEx/EDFlightControl/releases/latest'
        }
        $release = Get-GitHubJson $url
        $tag = [string]$release.tag_name
        if ($release.draft -or $release.prerelease -or $tag -cnotmatch '^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') {
            throw 'GitHub returned a draft, prerelease, or non-semantic stable release.'
        }
        if ($Revision -and $tag -cne $Revision) {
            throw "GitHub resolved a different release tag ('$tag') than requested ('$Revision')."
        }
        $version = $tag.Substring(1)
        $assetName = "EDFlightControl-$version.zip"
        $asset = @($release.assets | Where-Object { $_.name -ceq $assetName }) | Select-Object -First 1
        if ($null -eq $asset -or -not $asset.browser_download_url) {
            throw "Release $tag does not provide the required $assetName asset."
        }
        if ($asset.digest -and $asset.digest -notmatch '^sha256:[0-9a-fA-F]{64}$') {
            throw "Release asset '$assetName' has an unsupported digest format."
        }
        return [pscustomobject]@{
            Channel = 'release'; Revision = $tag; Version = $version
            DownloadUrl = [string]$asset.browser_download_url
            Digest = [string]$asset.digest
            SourceVersion = $version
            CommitDate = ''
            CommitMessage = ''
        }
    }
    if ($Channel -ne 'main') {
        throw "Invalid update channel '$Channel'."
    }
    $endpoint = if ($Revision) { $Revision } else { 'main' }
    $commit = Get-GitHubJson "https://api.github.com/repos/DopeEx/EDFlightControl/commits/$endpoint"
    $sha = [string]$commit.sha
    if ($sha -cnotmatch '^[0-9a-f]{40}$' -or ($Revision -and $sha -cne $Revision)) {
        throw 'GitHub did not resolve the exact requested main commit.'
    }
    $commitDate = [string]$commit.commit.committer.date
    $commitMessage = [string]$commit.commit.message
    $parsedCommitDate = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse($commitDate, [ref]$parsedCommitDate) -or
        [string]::IsNullOrWhiteSpace($commitMessage)) {
        throw "GitHub did not provide valid commit metadata for $sha."
    }
    $commitMessage = ($commitMessage -split '\r?\n')[0]
    return [pscustomobject]@{
        Channel = 'main'; Revision = $sha; Version = ''
        DownloadUrl = "https://api.github.com/repos/DopeEx/EDFlightControl/zipball/$sha"
        Digest = ''
        SourceVersion = ''
        CommitDate = $commitDate
        CommitMessage = $commitMessage
    }
}

function Get-PackageRootFromArchive {
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    return Expand-SafeArchive -ArchivePath $ArchivePath -Destination $Destination
}

function Invoke-UpdaterCore {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [AllowEmptyString()][string]$Channel = '',
        [AllowEmptyString()][string]$Revision = ''
    )

    $normalized = Get-NormalizedRoot $Root
    Assert-NotGitCheckout $normalized
    $arguments = Assert-InvocationArguments -Channel $Channel -Revision $Revision
    if ($arguments.Interactive) {
        Write-Host 'Choose an update channel:'
        Write-Host '  1. Latest stable release (default)'
        Write-Host '  2. Development build (main; unreleased, may contain bugs)'
        $choice = Read-Host 'Selection [1]'
        if ([string]::IsNullOrWhiteSpace($choice) -or $choice -eq '1') {
            $Channel = 'release'
            $Revision = ''
        } elseif ($choice -eq '2') {
            Write-Host 'WARNING: main is development source and may be unstable.'
            $Channel = 'main'
            $Revision = ''
        } else {
            throw "Invalid channel selection '$choice'."
        }
    } else {
        $Channel = $arguments.Channel
        $Revision = $arguments.Revision
    }

    $lock = Acquire-InstallationLock -Root $normalized
    $stageRoot = $null
    try {
        Assert-NoLegacyAppProcess $normalized
        Write-UpdaterLog -Root $normalized -Message "Starting $Channel update request."
        [void](Recover-PendingTransaction $normalized)
        $target = Get-ResolvedTarget -Channel $Channel -Revision $Revision
        Write-UpdaterLog -Root $normalized -Message "Resolved $($target.Channel) target $($target.Revision)."
        $id = [Guid]::NewGuid().ToString('N')
        $stageRoot = Get-TransactionDirectory -Root $normalized -Id $id -Kind staging
        [void][System.IO.Directory]::CreateDirectory($stageRoot)
        $archivePath = Join-Path $stageRoot 'download.zip'
        Write-Host "Downloading $($target.Channel) target $($target.Revision)..."
        Write-UpdaterLog -Root $normalized -Message "Downloading target $($target.Revision)."
        Get-SafeHttpBytes -Url $target.DownloadUrl -MaximumBytes 1073741824 -Destination $archivePath
        if ($target.Digest) {
            $expected = $target.Digest.Substring(7).ToLowerInvariant()
            $actual = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -cne $expected) {
                throw 'The release asset SHA-256 digest does not match GitHub metadata.'
            }
        }
        $packageRoot = Get-PackageRootFromArchive -ArchivePath $archivePath -Destination (Join-Path $stageRoot 'unpacked')
        $version = Assert-PackageStructure -PackageRoot $packageRoot -Channel $Channel -Revision $target.Revision -ReleaseVersion $target.SourceVersion
        $target.Version = $version
        Write-Host ''
        Write-Host "Target: $($target.Channel)"
        Write-Host "Revision: $($target.Revision)"
        Write-Host "Version: $($target.Version)"
        if (-not (Test-Path -LiteralPath (Join-Path $packageRoot 'tools\windows_updater.ps1') -PathType Leaf)) {
            Write-Host 'This snapshot has no bundled updater. The installed standalone updater will be retained.'
        }
        if ($Channel -eq 'main') {
            Write-Host 'Warning: This is an unreleased development build and may contain bugs.'
        }
        Write-UpdaterLog -Root $normalized -Message "Validated $Channel target $($target.Revision), version $version."
        $answer = Read-Host 'Replace the managed program files with this exact target? [y/N]'
        if ($answer -cne 'y' -and $answer -cne 'yes') {
            throw 'Update cancelled; installed program files were not changed.'
        }
        Assert-LockHeld $normalized
        Assert-NoLegacyAppProcess $normalized
        $result = Invoke-ManagedExchange -Root $normalized -PackageRoot $packageRoot -Channel $Channel -Revision $target.Revision -Version $version -CommitDate $target.CommitDate -CommitMessage $target.CommitMessage
        Write-UpdaterLog -Root $normalized -Message "Installed $Channel target $($target.Revision), version $version."
    } catch {
        $errorMessage = $_.Exception.Message
        try {
            Write-UpdaterLog -Root $normalized -Message $errorMessage -Level ERROR
        } catch {
            Write-Host "Could not write updater.log: $($_.Exception.Message)" -ForegroundColor Red
        }
        throw
    } finally {
        try {
            if ($stageRoot -and (Test-Path -LiteralPath $stageRoot)) {
                Remove-Item -LiteralPath $stageRoot -Recurse -Force
            }
        } finally {
            Release-InstallationLock $normalized
        }
    }
    if ($result.RequirementsChanged) {
        Write-Host 'Do not start EDFC until its Python requirements are installed.'
    } else {
        $restart = Read-Host 'Start EDFC now using start_edfc.bat? [y/N]'
        if ($restart -ceq 'y' -or $restart -ceq 'yes') {
            Start-Process -FilePath (Join-Path $normalized 'start_edfc.bat') -WorkingDirectory $normalized
        }
    }
}

function Invoke-Updater {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [AllowEmptyString()][string]$Channel = '',
        [AllowEmptyString()][string]$Revision = ''
    )

    $normalized = Get-NormalizedRoot $Root
    Assert-NotGitCheckout $normalized
    [void](Assert-InvocationArguments -Channel $Channel -Revision $Revision)
    $updaterLock = Acquire-UpdaterLock $normalized
    try {
        Invoke-UpdaterCore -Root $normalized -Channel $Channel -Revision $Revision
    } finally {
        $updaterLock.Dispose()
    }
}

if (-not $Library) {
    Invoke-Updater -Root $Root -Channel $Channel -Revision $Revision
}
