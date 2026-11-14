<#
.SYNOPSIS
Build the experimental ADAPT Windows wireless receiver (dexdesk.exe) locally from source.

.DESCRIPTION
Runs `cargo build --release --locked` in native\windows-receiver, then prints the
SHA-256 of the produced executable. Optional steps: -Test runs the offline unit tests,
-Stamp embeds the manifest, icon, version information and license texts with the
project's resource_stamp tool.

This script does not download anything itself. cargo (and rustup, which installs the
toolchain pinned in rust-toolchain.toml) may download the locked crates and that
toolchain on a first build; use -Offline to forbid network access. It does not start
the receiver, publish anything, change PATH permanently or touch any phone.

-DryRun (or the standard -WhatIf) prints what would run and runs nothing: no cargo, no
build, no test. It only looks for cargo and reads two source files.

Exit codes: 0 done (or dry run complete), 1 a build, test or stamp step failed,
2 a prerequisite is missing (source tree, cargo, or the pinned toolchain with -Offline).

.PARAMETER Test
Also run `cargo test --locked --offline` (the locked crates must already be in the cargo cache).

.PARAMETER Stamp
After building, run the project's resource_stamp tool on the executable.

.PARAMETER Offline
Pass --offline to cargo build/run so it cannot use the network. Fails early if the pinned
toolchain is not installed, because rustup would have to download it.

.PARAMETER DryRun
Show the plan and run nothing. Equivalent to -WhatIf for the build steps.

.EXAMPLE
powershell -NoProfile -File scripts\build_receiver.ps1 -DryRun

.EXAMPLE
powershell -NoProfile -File scripts\build_receiver.ps1 -Test
#>
#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Test,
    [switch]$Stamp,
    [switch]$Offline,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Stop-Build([string]$Message, [int]$Code) {
    [Console]::Error.WriteLine("build_receiver: $Message")
    exit $Code
}

# A step runs only when this returns true. -DryRun and -WhatIf both make it false.
function Confirm-Step([string]$Target, [string]$Action) {
    if ($DryRun) {
        Write-Host "[dry run] would run: $Action"
        return $false
    }
    return $PSCmdlet.ShouldProcess($Target, $Action)
}

# Run a native program with its output shown and return its exit code. Errors the program
# writes to stderr are shown as text and never turned into PowerShell exceptions.
function Invoke-Native([string]$File, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $File @Arguments | Out-Host
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Find-Cargo {
    $found = Get-Command cargo -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    $roots = @()
    if ($env:CARGO_HOME) { $roots += (Join-Path $env:CARGO_HOME 'bin') }
    if ($env:USERPROFILE) { $roots += (Join-Path $env:USERPROFILE '.cargo\bin') }
    foreach ($root in $roots) {
        $candidate = Join-Path $root 'cargo.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$receiver = Join-Path $repoRoot 'native\windows-receiver'
foreach ($name in 'Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'app.manifest') {
    if (-not (Test-Path -LiteralPath (Join-Path $receiver $name) -PathType Leaf)) {
        Stop-Build "native\windows-receiver\$name is missing. Run this script from an ADAPT source checkout; the receiver is not part of the Python wheel." 2
    }
}

$cargo = Find-Cargo
if (-not $cargo) {
    Stop-Build ("cargo was not found on PATH or in %CARGO_HOME%\bin or %USERPROFILE%\.cargo\bin. " +
        "Install the Rust toolchain with rustup (https://rustup.rs); this script does not install it.") 2
}
$cargoDirectory = Split-Path -Parent $cargo
if (($env:PATH -split ';') -notcontains $cargoDirectory) {
    # This process only. Nothing is written to the user or machine PATH.
    $env:PATH = "$cargoDirectory;$env:PATH"
}

$channel = $null
$pin = Get-Content -LiteralPath (Join-Path $receiver 'rust-toolchain.toml') -Raw
if ($pin -match 'channel\s*=\s*"([^"]+)"') { $channel = $Matches[1] }
$toolchainState = 'unknown'
$rustup = Join-Path $cargoDirectory 'rustup.exe'
if ($channel -and (Test-Path -LiteralPath $rustup -PathType Leaf)) {
    $installed = @(& $rustup toolchain list | ForEach-Object { "$_" })
    $toolchainState = if (@($installed | Where-Object { $_ -like "$channel*" }).Count -gt 0) { 'installed' } else { 'missing' }
}

$targetRoot = if ($env:CARGO_TARGET_DIR) {
    [IO.Path]::GetFullPath((Join-Path $receiver $env:CARGO_TARGET_DIR))
} else {
    Join-Path $receiver 'target'
}
$exe = Join-Path $targetRoot 'release\dexdesk.exe'
$offlineArgument = if ($Offline) { @('--offline') } else { @() }

Write-Host 'ADAPT Windows receiver build (experimental source; nothing here starts or publishes it)'
Write-Host "  source:    $receiver"
Write-Host "  cargo:     $cargo"
Write-Host "  toolchain: pinned $channel, $toolchainState"
Write-Host "  output:    $exe"
if ($toolchainState -eq 'missing') {
    if ($Offline) {
        Stop-Build "the pinned toolchain $channel is not installed and -Offline forbids downloading it. Run: rustup toolchain install $channel" 2
    }
    Write-Warning "The pinned toolchain $channel is not installed; rustup will download it during the first cargo command."
}
$free = $null
try { $free = ([IO.DriveInfo]::new([IO.Path]::GetPathRoot($targetRoot))).AvailableFreeSpace } catch { $free = $null }
if ($null -ne $free -and $free -lt 3GB) {
    Write-Warning ("Only {0:N1} GB is free where the build output goes; a release build with its dependencies can need more than that." -f ($free / 1GB))
}

Push-Location -LiteralPath $receiver
try {
    $buildArguments = @('build', '--release', '--locked') + $offlineArgument
    if (Confirm-Step $exe ("cargo " + ($buildArguments -join ' '))) {
        if ((Invoke-Native $cargo $buildArguments) -ne 0) { Stop-Build 'cargo build failed.' 1 }
    }
    if ($Test) {
        $testArguments = @('test', '--locked', '--offline')
        if (Confirm-Step $receiver ("cargo " + ($testArguments -join ' '))) {
            if ((Invoke-Native $cargo $testArguments) -ne 0) {
                Stop-Build 'cargo test failed (the tests are offline: the locked crates must already be in the cargo cache).' 1
            }
        }
    }
    if ($Stamp) {
        $stampArguments = @('run', '--release', '--locked') + $offlineArgument + @('--bin', 'resource_stamp', '--', $exe, 'app.manifest')
        if (Confirm-Step $exe ("cargo " + ($stampArguments -join ' '))) {
            if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { Stop-Build "cannot stamp: $exe does not exist." 1 }
            if ((Invoke-Native $cargo $stampArguments) -ne 0) { Stop-Build 'resource_stamp failed.' 1 }
        }
    }
}
finally {
    Pop-Location
}

if ($DryRun -or $WhatIfPreference) {
    Write-Host 'Dry run complete: nothing was built, tested or stamped, and no hash was computed.'
    exit 0
}
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    Stop-Build "the build reported success but $exe does not exist; a cargo configuration may place its output elsewhere." 1
}
$hash = Get-FileHash -LiteralPath $exe -Algorithm SHA256
Write-Host ''
Write-Host "Built:   $exe"
Write-Host ("Size:    {0:N0} bytes" -f (Get-Item -LiteralPath $exe).Length)
Write-Host "SHA-256: $($hash.Hash.ToLowerInvariant())"
Write-Host 'This hash identifies this local build only; builds are not claimed to be reproducible, and building or testing is not a certification.'
Write-Host 'The receiver was not started and nothing was published. To use it:'
Write-Host "  adapt dex receiver-start --exe `"$exe`" --yes"
exit 0
