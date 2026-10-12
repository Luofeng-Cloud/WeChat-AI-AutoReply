<#
  Create a Desktop and Start Menu shortcut for the packaged app.

  Version 1.x shelled out to a generated .vbs file run by cscript, wrote it to %TEMP%
  with GBK encoding, and silently reported success when cscript failed. This uses the
  shell COM object directly, verifies the .lnk exists afterwards, and resolves the
  Desktop through the shell so a OneDrive-redirected Desktop still works.
#>
param(
  [Parameter(Mandatory = $true)][string]$TargetPath,
  [string]$IconPath = "",
  [string]$ShortcutName = "微信AI自动回复",
  [switch]$SkipDesktop,
  [switch]$SkipStartMenu
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $TargetPath)) {
  Write-Error "Target executable not found: $TargetPath"
  exit 1
}

$target = (Resolve-Path -LiteralPath $TargetPath).Path
$workingDirectory = Split-Path -Parent $target
$shell = New-Object -ComObject WScript.Shell
$created = @()

function New-Shortcut([string]$Directory, [string]$Name) {
  if (-not (Test-Path -LiteralPath $Directory)) { return }
  $linkPath = Join-Path $Directory "$Name.lnk"
  $shortcut = $shell.CreateShortcut($linkPath)
  $shortcut.TargetPath = $target
  $shortcut.WorkingDirectory = $workingDirectory
  $shortcut.Description = "微信AI自动回复 - Electron 控制中心"
  if ($IconPath -and (Test-Path -LiteralPath $IconPath)) {
    $shortcut.IconLocation = $IconPath
  } else {
    $shortcut.IconLocation = "$target,0"
  }
  $shortcut.Save()
  if (Test-Path -LiteralPath $linkPath) {
    $script:created += $linkPath
    Write-Output "created: $linkPath"
  } else {
    Write-Error "shortcut was not written: $linkPath"
  }
}

if (-not $SkipDesktop) {
  # Resolve through the shell so a relocated or OneDrive Desktop is honoured.
  $desktop = [Environment]::GetFolderPath("Desktop")
  New-Shortcut -Directory $desktop -Name $ShortcutName
}

if (-not $SkipStartMenu) {
  $programs = Join-Path ([Environment]::GetFolderPath("ApplicationData")) "Microsoft\Windows\Start Menu\Programs"
  New-Shortcut -Directory $programs -Name $ShortcutName
}

if ($created.Count -eq 0) {
  Write-Error "no shortcuts were created"
  exit 2
}
Write-Output "done: $($created.Count) shortcut(s)"
