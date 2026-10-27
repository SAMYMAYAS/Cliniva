$ErrorActionPreference = "Stop"
$backupPath = $PSScriptRoot
$restoreScript = Join-Path $backupPath "RESTORE_CLINIVA.ps1"
$manifestPath = Join-Path $backupPath "MANIFEST.json"
$installPath = "C:\Clinical_Build_Codex"

if (-not (Test-Path -LiteralPath $restoreScript -PathType Leaf)) {
    throw "RESTORE_CLINIVA.ps1 introuvable à côté de ce lanceur."
}
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "MANIFEST.json introuvable à côté de ce lanceur."
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$dbPort = if ($manifest.database.port) { [int]$manifest.database.port } else { 5432 }
$dbUser = if ($manifest.database.user) { [string]$manifest.database.user } else { "postgres" }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdministrator) {
    $argumentLine = '-NoExit -NoProfile -ExecutionPolicy Bypass -File "{0}" -BackupPath "{1}" -InstallPath "{2}" -DbPort {3} -DbUser "{4}"' -f `
        $restoreScript, $backupPath, $installPath, $dbPort, $dbUser
    $process = Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $argumentLine -Wait -PassThru
    exit $process.ExitCode
}

try {
    & $restoreScript -BackupPath $backupPath -InstallPath $installPath -DbPort $dbPort -DbUser $dbUser
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} catch {
    Write-Host "Échec de la restauration : $_" -ForegroundColor Red
    exit 1
} finally {
    Read-Host "Appuyez sur Entrée pour fermer cette fenêtre"
}
