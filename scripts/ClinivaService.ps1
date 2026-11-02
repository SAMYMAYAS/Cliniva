param(
    [ValidateSet('Install', 'Enable', 'Disable', 'Start', 'Stop', 'Status', 'Remove')]
    [string]$Action = 'Status'
)

$ErrorActionPreference = 'Stop'
$serviceName = 'ClinivaBackend'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$serviceScript = Join-Path $projectRoot 'app\windows_service.py'
$configPath = Join-Path $projectRoot 'config.json'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Ouvre PowerShell en tant qu’administrateur pour gérer le service Cliniva."
}

$python = (Get-Command python.exe -ErrorAction Stop).Source
if (-not (Test-Path -LiteralPath $serviceScript)) { throw "Module service introuvable : $serviceScript" }

function Set-ClinivaServicePreference([bool]$Enabled) {
    $config = @{}
    if (Test-Path -LiteralPath $configPath) {
        $loaded = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
        if ($loaded) { $config = $loaded }
    }
    $config['windows_service_enabled'] = $Enabled
    $temporary = "$configPath.tmp"
    $json = $config | ConvertTo-Json -Depth 30
    [System.IO.File]::WriteAllText($temporary, $json, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $configPath -Force
}

switch ($Action) {
    'Install' {
        if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
            Write-Host 'Le service Cliniva est déjà installé.' -ForegroundColor Yellow
        } else {
            Push-Location $projectRoot
            try { & $python $serviceScript --startup auto install }
            finally { Pop-Location }
            if ($LASTEXITCODE -ne 0) { throw 'Installation du service échouée. Vérifie pywin32 et son installation post-installation.' }
        }
        Set-ClinivaServicePreference $true
        Set-Service -Name $serviceName -StartupType Automatic
        Start-Service -Name $serviceName
        Get-Service -Name $serviceName | Format-Table Name, Status, StartType -AutoSize
    }
    'Enable' {
        if (-not (Get-Service -Name $serviceName -ErrorAction SilentlyContinue)) { throw "Installe d’abord le service avec -Action Install." }
        Set-ClinivaServicePreference $true
        Set-Service -Name $serviceName -StartupType Automatic
        Start-Service -Name $serviceName
        Get-Service -Name $serviceName | Format-Table Name, Status, StartType -AutoSize
    }
    'Disable' {
        if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
            if ((Get-Service -Name $serviceName).Status -ne 'Stopped') { Stop-Service -Name $serviceName -Force }
            Set-Service -Name $serviceName -StartupType Manual
        }
        Set-ClinivaServicePreference $false
        Write-Host 'Démarrage automatique désactivé. Le lancement manuel de main.py reste disponible.' -ForegroundColor Green
    }
    'Start' { Start-Service -Name $serviceName; Get-Service -Name $serviceName | Format-Table Name, Status, StartType -AutoSize }
    'Stop' { Stop-Service -Name $serviceName -Force; Get-Service -Name $serviceName | Format-Table Name, Status, StartType -AutoSize }
    'Status' {
        $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        if ($service) { $service | Format-Table Name, Status, StartType -AutoSize }
        else { Write-Host 'Service Cliniva non installé.' -ForegroundColor Yellow }
        if (Test-Path -LiteralPath $configPath) {
            $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
            Write-Host "windows_service_enabled = $($config.windows_service_enabled)"
        }
    }
    'Remove' {
        $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        if ($service) {
            if ($service.Status -ne 'Stopped') { Stop-Service -Name $serviceName -Force }
            Push-Location $projectRoot
            try { & $python $serviceScript remove }
            finally { Pop-Location }
            if ($LASTEXITCODE -ne 0) { throw 'Suppression du service échouée.' }
        }
        Set-ClinivaServicePreference $false
        Write-Host 'Service supprimé; config.json est réglé en mode manuel.' -ForegroundColor Green
    }
}
