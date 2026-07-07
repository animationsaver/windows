# setup.ps1 - provision the everyday Chrome environment on each session.
# NOTE: To actually persist across sessions, D: must be cloud-synced
# (pull at start / push at end). Within a single session it works standalone.

$ErrorActionPreference = "Stop"
Write-Host "== Provisioning Chrome environment =="

# 1) Install Chrome if it is not already present
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) {
  Write-Host "Installing Google Chrome..."
  winget install --id Google.Chrome -e --silent --accept-source-agreements --accept-package-agreements
}

# 2) Persistent profile on the cloud-synced D: drive.
#    Bookmarks / extensions / settings / history live here and survive across
#    sessions via the D: sync. (Saved passwords & cookies do NOT survive a copy,
#    because Chrome encrypts them with DPAPI - use Chrome Sync for those.)
$profileDir = "D:\chrome-profile"
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

# 3) Force-install extensions with NO sign-in required (Chrome policy).
#    Add the extension IDs you want (found in the Chrome Web Store URL).
#    Example: uBlock Origin = cjpalhdlnbpafiamejdnhcphjbkeiagm
$extList = @(
  # "cjpalhdlnbpafiamejdnhcphjbkeiagm",  # uBlock Origin
  # "eimadpbcbfnmbkopoojfekhnkhdbieeh"   # Dark Reader
)
$polRoot = "HKLM:\SOFTWARE\Policies\Google\Chrome"
$polKey  = "$polRoot\ExtensionInstallForcelist"
New-Item -Path $polRoot -Force | Out-Null
New-Item -Path $polKey  -Force | Out-Null
$i = 1
foreach ($id in $extList) {
  Set-ItemProperty -Path $polKey -Name "$i" -Value "$id;https://clients2.google.com/service/update2/crx"
  $i++
}
Write-Host "Force-install extensions configured: $($extList.Count)"

# 4) Desktop shortcut that always launches Chrome with the persistent profile
$desktop = "C:\Users\runneradmin\Desktop"
$lnkPath = Join-Path $desktop "Chrome (persistent).lnk"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath = $chrome
$sc.Arguments  = "--user-data-dir=`"$profileDir`""
$sc.IconLocation = $chrome
$sc.Save()

Write-Host "== Done =="
Write-Host "Use the 'Chrome (persistent)' desktop shortcut."
Write-Host "Sign in to Chrome once and turn ON Sync to restore saved passwords."
