$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$configPath = Join-Path $PSScriptRoot "config.json"
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "config.json introuvable dans $PSScriptRoot"
}
$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$database = $config.postgres
if (-not $database) { throw "La section postgres est absente de config.json." }

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw "Python est introuvable. Installez Python puis relancez ce script." }
    $python = $pythonCommand.Source
}

$securePassword = Read-Host "Mot de passe PostgreSQL pour $($database.user)@$($database.host)" -AsSecureString
$passwordPointer = [IntPtr]::Zero
try {
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    $env:DB_HOST = [string]$database.host
    $env:DB_PORT = [string]$database.port
    $env:DB_NAME = [string]$database.database
    $env:DB_USER = [string]$database.user
    $env:DB_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    & $python (Join-Path $PSScriptRoot "main.py")
    if ($LASTEXITCODE -ne 0) { throw "Cliniva s’est arrêtée avec le code $LASTEXITCODE." }
} finally {
    Remove-Item Env:DB_HOST, Env:DB_PORT, Env:DB_NAME, Env:DB_USER, Env:DB_PASSWORD -ErrorAction SilentlyContinue
    if ($passwordPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
}
