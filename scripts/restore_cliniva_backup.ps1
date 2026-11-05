param(
    [Parameter(Mandatory = $true)]
    [string]$BackupPath,

    [Parameter(Mandatory = $true)]
    [string]$InstallPath,

    [string]$DbHost = "localhost",
    [int]$DbPort = 5432,
    [string]$DbName = "",
    [string]$DbUser = "postgres",
    [string]$PacsDataPath = (Join-Path $env:LOCALAPPDATA "Cliniva\PACS"),
    [string]$DicomPrintDataPath = (Join-Path $env:USERPROFILE "DicomPrints")
)

$ErrorActionPreference = "Stop"

function Find-PostgresTool([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    $programFiles = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    foreach ($base in $programFiles) {
        $matches = Get-ChildItem -LiteralPath (Join-Path $base "PostgreSQL") -Filter $Name -File -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending
        if ($matches) { return $matches[0].FullName }
    }
    throw "$Name introuvable. Installez PostgreSQL avec les outils en ligne de commande ou ajoutez son dossier bin au PATH."
}

function Invoke-Robocopy([string]$Source, [string]$Target) {
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    & robocopy $Source $Target /E /COPY:DAT /DCOPY:DAT /R:2 /W:2 /XJ /NFL /NDL /NJH /NJS
    if ($LASTEXITCODE -ge 8) {
        throw "Copie interrompue ($Source vers $Target), code Robocopy $LASTEXITCODE."
    }
}

function Assert-EmptyTarget([string]$Path, [string]$Description) {
    if (Test-Path -LiteralPath $Path) {
        $existing = Get-ChildItem -LiteralPath $Path -Force | Select-Object -First 1
        if ($existing) {
            throw "$Description n’est pas vide : $Path. Choisissez un autre chemin pour ne pas mélanger les données."
        }
    }
}

function Set-JsonProperty($Object, [string]$Name, $Value) {
    if ($Object.PSObject.Properties[$Name]) {
        $Object.$Name = $Value
    } else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Assert-TreeInventory([string]$Path, $Expected, [string]$Description) {
    if (-not $Expected) { return }
    $files = @(Get-ChildItem -LiteralPath $Path -Force -Recurse -File).Count
    $directories = @(Get-ChildItem -LiteralPath $Path -Force -Recurse -Directory).Count
    if ($files -ne [int]$Expected.files -or $directories -ne [int]$Expected.directories) {
        throw "Vérification échouée pour $Description : $files fichiers et $directories dossiers restaurés, contre $($Expected.files) fichiers et $($Expected.directories) attendus."
    }
}

$backup = (Resolve-Path -LiteralPath $BackupPath).Path
$manifestPath = Join-Path $backup "MANIFEST.json"
$restoreGuide = Join-Path $backup "RESTORE_GUIDE.txt"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "MANIFEST.json est absent. Le backup ne peut pas être vérifié."
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$backupChain = @()
$cursorPath = $backup
$cursorManifest = $manifest
while ($true) {
    $backupChain = @([pscustomobject]@{ Path = $cursorPath; Manifest = $cursorManifest }) + $backupChain
    if ([string]$cursorManifest.backup_type -ne "incremental") { break }
    $parentName = [string]$cursorManifest.parent_backup
    if (-not $parentName -or [IO.Path]::GetFileName($parentName) -cne $parentName) {
        throw "La chaîne de sauvegardes incrémentielles est invalide : parent absent ou incorrect."
    }
    $cursorPath = Join-Path (Split-Path -Parent $cursorPath) $parentName
    $cursorManifestPath = Join-Path $cursorPath "MANIFEST.json"
    if (-not (Test-Path -LiteralPath $cursorManifestPath -PathType Leaf)) {
        throw "Un élément de la chaîne de sauvegarde est absent : $cursorManifestPath"
    }
    $cursorManifest = Get-Content -LiteralPath $cursorManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$cursorManifest.status -ne "completed") { throw "Un élément de la chaîne n’est pas terminé." }
}
$baseBackup = $backupChain[0].Path
$applicationSource = Join-Path $baseBackup "application"
if (-not (Test-Path -LiteralPath $applicationSource -PathType Container)) {
    throw "Le dossier application du point complet est absent : $applicationSource"
}
$restoreGuide = Join-Path $backup "RESTORE_GUIDE.txt"
if (-not $DbName) { $DbName = [string]$manifest.database.name }
$dumpPath = Join-Path $backup ([string]$manifest.database.dump)
if (-not (Test-Path -LiteralPath $dumpPath -PathType Leaf)) {
    throw "Le dump PostgreSQL est absent ou le backup est incomplet : $dumpPath"
}
if (-not (Test-Path -LiteralPath $restoreGuide -PathType Leaf)) {
    throw "RESTORE_GUIDE.txt est absent. Utilisez un backup créé par la version actuelle de Cliniva."
}

$install = [System.IO.Path]::GetFullPath($InstallPath)
Assert-EmptyTarget $install "Le dossier d’installation"
if (-not (Test-Path -LiteralPath $install)) {
    New-Item -ItemType Directory -Path $install -Force | Out-Null
}
if (Test-Path -LiteralPath (Join-Path $baseBackup "dicom\pacs_data") -PathType Container) {
    Assert-EmptyTarget $PacsDataPath "Le dossier PACS de destination"
}
if (Test-Path -LiteralPath (Join-Path $baseBackup "dicom\dicom_print") -PathType Container) {
    Assert-EmptyTarget $DicomPrintDataPath "Le dossier DICOM Print de destination"
}

if ($DbName -notmatch '^[A-Za-z0-9_ -]+$') {
    throw "Nom de base invalide. Utilisez uniquement lettres, chiffres, espaces, tirets ou soulignements."
}
$pgRestore = Find-PostgresTool "pg_restore.exe"
$psql = Find-PostgresTool "psql.exe"
$createdb = Find-PostgresTool "createdb.exe"
$null = & $pgRestore --list $dumpPath 2>&1
if ($LASTEXITCODE -ne 0) { throw "Le dump PostgreSQL est illisible ou endommagé." }

$securePassword = Read-Host "Mot de passe PostgreSQL pour $DbUser@$DbHost" -AsSecureString
$passwordPointer = [IntPtr]::Zero
try {
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    $dbExists = (& $psql --no-password --host $DbHost --port $DbPort --username $DbUser --dbname postgres --tuples-only --no-align --command "SELECT 1 FROM pg_database WHERE datname = '$($DbName.Replace("'", "''"))';").Trim() -eq "1"
    if ($LASTEXITCODE -ne 0) { throw "Connexion à PostgreSQL impossible. Vérifiez le service, le port, l’utilisateur et le mot de passe." }

    if ($dbExists) {
        $confirmation = Read-Host "La base $DbName existe. Sa restauration remplacera ses objets. Tapez RESTAURER $DbName pour confirmer"
        if ($confirmation -cne "RESTAURER $DbName") { throw "Restauration annulée. La base existante n’a pas été modifiée." }
    }

    Write-Host "Copie des fichiers Cliniva…" -ForegroundColor Cyan
    Invoke-Robocopy $applicationSource $install
    $pacsSource = Join-Path $baseBackup "dicom\pacs_data"
    $printSource = Join-Path $baseBackup "dicom\dicom_print"
    if (Test-Path -LiteralPath $pacsSource -PathType Container) { Invoke-Robocopy $pacsSource $PacsDataPath }
    if (Test-Path -LiteralPath $printSource -PathType Container) { Invoke-Robocopy $printSource $DicomPrintDataPath }

    foreach ($entry in $backupChain | Select-Object -Skip 1) {
        $layer = $entry.Path
        $layerManifest = $entry.Manifest
        foreach ($label in @("application", "pacs_data", "dicom_print")) {
            $targetRoot = switch ($label) {
                "application" { $install }
                "pacs_data" { $PacsDataPath }
                "dicom_print" { $DicomPrintDataPath }
            }
            $deleted = $layerManifest.deleted_files.$label
            foreach ($relative in @($deleted)) {
                $candidate = [IO.Path]::GetFullPath((Join-Path $targetRoot ([string]$relative)))
                $rootPrefix = [IO.Path]::GetFullPath($targetRoot).TrimEnd('\') + '\'
                if (-not $candidate.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "Chemin de suppression invalide dans le manifeste : $relative"
                }
                if (Test-Path -LiteralPath $candidate -PathType Leaf) { Remove-Item -LiteralPath $candidate -Force }
            }
        }
        $applicationDelta = Join-Path $layer "changes\application"
        $pacsDelta = Join-Path $layer "changes\dicom\pacs_data"
        $printDelta = Join-Path $layer "changes\dicom\dicom_print"
        if (Test-Path -LiteralPath $applicationDelta -PathType Container) { Invoke-Robocopy $applicationDelta $install }
        if (Test-Path -LiteralPath $pacsDelta -PathType Container) { Invoke-Robocopy $pacsDelta $PacsDataPath }
        if (Test-Path -LiteralPath $printDelta -PathType Container) { Invoke-Robocopy $printDelta $DicomPrintDataPath }

        foreach ($label in @("application", "pacs_data", "dicom_print")) {
            $targetRoot = switch ($label) {
                "application" { $install }
                "pacs_data" { $PacsDataPath }
                "dicom_print" { $DicomPrintDataPath }
            }
            $desired = @($layerManifest.directory_state.$label)
            $existing = @(Get-ChildItem -LiteralPath $targetRoot -Force -Recurse -Directory -ErrorAction SilentlyContinue |
                Sort-Object { $_.FullName.Length } -Descending)
            foreach ($directory in $existing) {
                $relative = $directory.FullName.Substring($targetRoot.TrimEnd('\').Length).TrimStart('\').Replace('\', '/')
                if ($relative -notin $desired) {
                    if (-not (Get-ChildItem -LiteralPath $directory.FullName -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
                        Remove-Item -LiteralPath $directory.FullName -Force -ErrorAction SilentlyContinue
                    }
                }
            }
            foreach ($relative in $desired) {
                if ($relative) { New-Item -ItemType Directory -Path (Join-Path $targetRoot ([string]$relative)) -Force | Out-Null }
            }
        }
    }
    Assert-TreeInventory $install $manifest.source_inventory.application "l’application"
    $requirementsSource = Join-Path $backup "requirements-installed.txt"
    if (Test-Path -LiteralPath $requirementsSource -PathType Leaf) {
        Copy-Item -LiteralPath $requirementsSource -Destination (Join-Path $install "requirements-installed.txt") -Force
    }
    $launcherSource = Join-Path $install "scripts\start_cliniva.ps1"
    if (Test-Path -LiteralPath $launcherSource -PathType Leaf) {
        Copy-Item -LiteralPath $launcherSource -Destination (Join-Path $install "Lancer_Cliniva.ps1") -Force
    }

    $configPath = Join-Path $install "config.json"
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } else {
        $config = [pscustomobject]@{ server_mode = "local" }
    }
    Set-JsonProperty $config "server_mode" "local"
    if (-not $config.postgres) { Set-JsonProperty $config "postgres" ([pscustomobject]@{}) }
    Set-JsonProperty $config.postgres "host" $DbHost
    Set-JsonProperty $config.postgres "port" $DbPort
    Set-JsonProperty $config.postgres "database" $DbName
    Set-JsonProperty $config.postgres "user" $DbUser
    if (-not $config.pacs_config) { Set-JsonProperty $config "pacs_config" ([pscustomobject]@{}) }
    if (-not $config.dicom_print_config) { Set-JsonProperty $config "dicom_print_config" ([pscustomobject]@{}) }

    if (Test-Path -LiteralPath $pacsSource -PathType Container) {
        Assert-TreeInventory $PacsDataPath $manifest.source_inventory.pacs_data "les données PACS"
    }
    if (Test-Path -LiteralPath $printSource -PathType Container) {
        Assert-TreeInventory $DicomPrintDataPath $manifest.source_inventory.dicom_print "les données DICOM Print"
    }
    Set-JsonProperty $config.pacs_config "data_dir" $PacsDataPath
    Set-JsonProperty $config.dicom_print_config "storage_dir" $DicomPrintDataPath
    $configJson = $config | ConvertTo-Json -Depth 100
    [System.IO.File]::WriteAllText($configPath, $configJson, [System.Text.UTF8Encoding]::new($false))

    if (-not $dbExists) {
        & $createdb --no-password --host $DbHost --port $DbPort --username $DbUser --template template0 $DbName
        if ($LASTEXITCODE -ne 0) { throw "Création de la base $DbName impossible." }
    }
    Write-Host "Restauration de PostgreSQL…" -ForegroundColor Cyan
    & $pgRestore --no-password --exit-on-error --clean --if-exists --no-owner --no-acl --host $DbHost --port $DbPort --username $DbUser --dbname $DbName $dumpPath
    if ($LASTEXITCODE -ne 0) { throw "Échec de pg_restore (code $LASTEXITCODE). Consultez la sortie ci-dessus." }
} finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    if ($passwordPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
}

$guideTarget = Join-Path $install "RESTORE_GUIDE.txt"
Copy-Item -LiteralPath $restoreGuide -Destination $guideTarget -Force

Write-Host "Restauration des fichiers et de la base terminée." -ForegroundColor Green
Write-Host "Dossier Cliniva : $install"
Write-Host "Base restaurée : $DbName sur $DbHost`:$DbPort"
Write-Host "Consultez RESTORE_GUIDE.txt pour installer les dépendances système et démarrer Cliniva."
