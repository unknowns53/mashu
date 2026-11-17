# Windows. On macOS, use tools/update.sh.
# The steps and their order are those of the app's own update (desktop/src-tauri/src/update.rs):
# the checkout moves before the app, so the app never runs against an older backend than itself.
# Kept to ASCII, since Windows PowerShell 5.1 reads a script without a BOM in the ANSI code page.

<#
.SYNOPSIS
Updates the Mashu checkout and app to a newer release.

.DESCRIPTION
By default, moves this checkout's main to the latest stable release, runs uv sync and
mashu admin migrate, and reinstalls the app with that release's installer.

.PARAMETER Pre
Uses the most recently published release whose tag starts with v, including prereleases.
#>
[CmdletBinding()]
param(
    [switch]$Pre
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    Write-Host "error: $Message" -ForegroundColor Red
    exit 1
}

# Windows PowerShell 5.1 treats a native command's stderr lines as errors and stops under Stop, and
# git and uv write their progress to stderr, so the exit code decides instead.
function Invoke-Native([string]$File, [string[]]$Arguments, [string]$Hint = '') {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $File @Arguments
    } finally {
        $ErrorActionPreference = $saved
    }
    if ($LASTEXITCODE -ne 0) {
        Fail "$File $($Arguments -join ' ') failed (exit code $LASTEXITCODE).$Hint"
    }
}

function Get-Native([string]$File, [string[]]$Arguments) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $File @Arguments 2>$null
    } finally {
        $ErrorActionPreference = $saved
    }
    [pscustomobject]@{ Ok = ($LASTEXITCODE -eq 0); Out = $out }
}

# A fork updates from its own releases when origin is on GitHub.
function Get-RepoSlug {
    $url = (Get-Native 'git' @('remote', 'get-url', 'origin')).Out
    if ($url -and ("$url".Trim() -match '^(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$')) {
        return $Matches[1]
    }
    'unknowns53/mashu'
}

function Save-Url([string]$Url, [string]$Path) {
    for ($attempt = 1; ; $attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing
            return 200
        } catch {
            $response = $_.Exception.Response
            if ($response) { return [int]$response.StatusCode }
            if ($attempt -gt 3) { Fail "could not download $Url; check the network connection ($($_.Exception.Message))" }
            Write-Host "download failed; retrying in 5 seconds (attempt $attempt of 3, $($_.Exception.Message))"
            Start-Sleep -Seconds 5
        }
    }
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).ProviderPath
$installDir = Join-Path $env:LOCALAPPDATA 'Mashu'
$installedExe = Join-Path $installDir 'mashu-desktop.exe'
# Cleared so the cleanup below never removes a same-named variable of the calling session.
$work = $null

foreach ($tool in 'git', 'uv') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "$tool was not found" }
}

# The app updates the checkout named in its settings. Moving another one would leave the app
# newer than the backend it runs.
$config = Join-Path $env:APPDATA 'dev.mashu.desktop\config.json'
try {
    $configured = (Get-Content -LiteralPath $config -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json).checkout
} catch {
    $configured = $null
}
if ($configured) {
    $resolved = Resolve-Path -LiteralPath $configured -ErrorAction SilentlyContinue
    if (-not $resolved -or $resolved.ProviderPath.TrimEnd('\') -ne $root.TrimEnd('\')) {
        Fail "the app runs the checkout at $configured, not this one ($root); run that checkout's tools\update.ps1, or choose this checkout under the app's Settings > Version"
    }
}

Push-Location -LiteralPath $root
try {
    $branch = (Get-Native 'git' @('branch', '--show-current')).Out
    if ($branch -ne 'main') {
        if (-not $branch) { $branch = '(no branch)' }
        Fail "the checkout at $root is on '$branch'; switch it to main first"
    }
    $dirty = Get-Native 'git' @('status', '--porcelain', '--untracked-files=no')
    if (-not $dirty.Ok) { Fail 'could not check for changes with git' }
    if ($dirty.Out) {
        $dirty.Out | ForEach-Object { Write-Host $_ }
        Fail 'the files above have uncommitted changes; commit or discard them first'
    }

    # Windows PowerShell 5.1 may not offer TLS 1.2, which GitHub requires.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    # Its progress bar slows Invoke-WebRequest several times over.
    $ProgressPreference = 'SilentlyContinue'
    $work = Join-Path ([IO.Path]::GetTempPath()) ('mashu-update-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null

    $slug = Get-RepoSlug
    $jsonPath = Join-Path $work 'release.json'
    if ($Pre) {
        $api = "https://api.github.com/repos/$slug/releases?per_page=50"
        $code = Save-Url $api $jsonPath
        if ($code -ne 200) { Fail "could not download $api (HTTP $code)" }
        # The list comes newest first; the first v release that is not a draft is the one wanted.
        $release = @(Get-Content -LiteralPath $jsonPath -Raw -Encoding UTF8 | ConvertFrom-Json) |
            ForEach-Object { $_ } |
            Where-Object { -not $_.draft -and $_.tag_name -like 'v*' } |
            Select-Object -First 1
        if (-not $release) { Fail "https://github.com/$slug/releases has no release whose tag starts with v yet. Nothing was changed." }
        $kind = 'the latest release including prereleases'
    } else {
        # This API leaves out drafts and prereleases.
        $api = "https://api.github.com/repos/$slug/releases/latest"
        $code = Save-Url $api $jsonPath
        if ($code -eq 404) { Fail "https://github.com/$slug/releases has no stable release yet. Nothing was changed." }
        if ($code -ne 200) { Fail "could not download $api (HTTP $code)" }
        $release = Get-Content -LiteralPath $jsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $kind = 'the latest stable release'
    }
    $tag = $release.tag_name
    if (-not $tag) { Fail "could not read the release tag from $api" }
    # The installer's name carries the version as the app reads it, so it is taken from the release.
    $installer = @($release.assets | Where-Object { $_.name -like 'Mashu_*_x64-setup.exe' }) | Select-Object -First 1
    if (-not $installer) { Fail "release $tag has no Windows installer; check https://github.com/$slug/releases/tag/$tag. Nothing was changed." }
    Write-Host "updating to $tag, $kind"

    # Every refusal comes before the app is closed or anything is changed.
    Invoke-Native 'git' @('fetch', '--quiet', '--tags', 'origin')
    if (-not (Get-Native 'git' @('rev-parse', '--verify', '--quiet', "refs/tags/$tag^{commit}")).Ok) { Fail "origin has no tag $tag" }
    if (-not (Get-Native 'git' @('merge-base', '--is-ancestor', 'HEAD', $tag)).Ok) {
        Fail "main at $root has commits that $tag does not; bring it up to date with git. Nothing was changed."
    }

    $base = "https://github.com/$slug/releases/download/$tag"
    $sumsPath = Join-Path $work 'SHA256SUMS'
    $setupPath = Join-Path $work $installer.name
    # For a few seconds after a release is published its files can still answer 404.
    $deadline = (Get-Date).AddMinutes(1)
    while ($true) {
        $code = Save-Url "$base/SHA256SUMS" $sumsPath
        if ($code -eq 200) { $code = Save-Url "$base/$($installer.name)" $setupPath }
        if ($code -eq 200) { break }
        if ($code -ne 404) { Fail "could not download from $base (HTTP $code)" }
        if ((Get-Date) -ge $deadline) { Fail "release $tag lacks SHA256SUMS or $($installer.name); check https://github.com/$slug/releases/tag/$tag. Nothing was changed." }
        Write-Host "the release's files are not there yet; checking again in 10 seconds (up to 1 min)"
        Start-Sleep -Seconds 10
    }
    $expected = $null
    foreach ($line in Get-Content -LiteralPath $sumsPath) {
        $parts = $line.Trim() -split '\s+', 2
        if ($parts.Count -eq 2 -and $parts[1].TrimStart('*') -eq $installer.name) { $expected = $parts[0].ToLowerInvariant() }
    }
    if (-not $expected) { Fail "SHA256SUMS has no line for $($installer.name). Nothing was changed." }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $setupPath).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { Fail "the checksum of $($installer.name) does not match SHA256SUMS. Nothing was changed." }
    # A Mark of the Web makes Windows ask before the installer runs, which stops /S.
    Unblock-File -LiteralPath $setupPath

    $prefix = $installDir.TrimEnd('\') + '\'
    $running = @(Get-Process -Name 'mashu-desktop' -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($running.Count -gt 0) {
        Write-Host 'closing the running Mashu'
        foreach ($p in $running) {
            if ($p.HasExited) { continue }
            try { $p.Kill() } catch { Write-Host "could not close Mashu (PID $($p.Id)): $($_.Exception.Message)" }
        }
        # A process listed again can still show before it is gone, so the same objects are waited on.
        # One still running is left to the installer, which closes the app itself.
        $left = @($running | Where-Object { -not $_.WaitForExit(10000) })
        if ($left.Count -gt 0) { Write-Host 'Mashu has not closed yet; leaving it to the installer' }
    }

    # From here every step can run again, so after a failure the same command picks up where it stopped.
    Write-Host "moving main to $tag"
    Invoke-Native 'git' @('merge', '--ff-only', '--quiet', $tag)
    Write-Host 'running uv sync --extra mcp'
    Invoke-Native 'uv' @('sync', '--extra', 'mcp') ' An agent that starts .venv\Scripts\mashu.exe holds it while it runs; close that agent''s sessions and run this again, and register its MCP server as .venv\Scripts\python.exe -m mashu serve so later updates need not close them.'
    Write-Host 'running mashu admin migrate'
    Invoke-Native (Join-Path $root '.venv\Scripts\python.exe') @('-m', 'mashu', 'admin', 'migrate')

    Write-Host "reinstalling the app with $($installer.name)"
    $setup = Start-Process -FilePath $setupPath -ArgumentList '/S' -Wait -PassThru
    if ($setup.ExitCode -ne 0) {
        Fail "the installer failed (exit code $($setup.ExitCode)). This happens when Mashu could not be closed; choose Quit from its tray menu and run this again. The checkout is already at $tag."
    }
    if (-not (Test-Path -LiteralPath $installedExe)) {
        Fail "the installer reported success, but $installedExe was not found"
    }

    Write-Host 'starting Mashu'
    Start-Process -FilePath $installedExe
    Write-Host "updated to $tag"
} finally {
    Pop-Location
    if ($work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
}
