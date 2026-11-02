# Service Windows Cliniva

Le service lance le backend/API sur le serveur sans ouvrir WebView. Les postes clients continuent à se connecter à l’adresse réseau habituelle du serveur.

## Installer et activer le démarrage automatique

1. Vérifie que Cliniva fonctionne normalement et que `config.json` est configuré en `server_mode: "local"`.
2. Ouvre PowerShell **en tant qu’administrateur** dans le dossier Cliniva.
3. Lance :

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\ClinivaService.ps1 -Action Install
```

Le script installe `ClinivaBackend`, configure son démarrage automatique et le démarre. Le choix est enregistré dans `config.json` sous `windows_service_enabled`.

## Accéder au bureau Cliniva sur le serveur

Pour afficher la fenêtre locale, dans PowerShell administrateur :

```powershell
.\scripts\ClinivaService.ps1 -Action Stop
python .\main.py
```

Après fermeture de Cliniva, le redémarrage du service peut être fait par :

```powershell
.\scripts\ClinivaService.ps1 -Action Start
```

Un simple arrêt ne désactive pas le démarrage automatique au prochain redémarrage Windows. Pour désactiver ce dernier :

```powershell
.\scripts\ClinivaService.ps1 -Action Disable
```

Pour le réactiver :

```powershell
.\scripts\ClinivaService.ps1 -Action Enable
```

`Status` affiche l’état du service et `Remove` le désinstalle. L’interface reste accessible depuis les postes clients quand le service tourne.

## Compte Windows du service

Le service s’exécute par défaut sous **LocalSystem**. Les variables PostgreSQL définies seulement dans une fenêtre PowerShell ou dans le profil de l’utilisateur ne seront donc pas visibles. Définis `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` et surtout `DB_PASSWORD` dans les variables d’environnement **système** de Windows, puis redémarre le service. Le mot de passe ne doit pas rester celui par défaut si PostgreSQL utilise un autre mot de passe.

Le compte LocalSystem doit aussi pouvoir lire le projet et accéder aux dossiers de stockage configurés. Les lecteurs réseau mappés dans la session d’un utilisateur ne sont généralement pas visibles par LocalSystem : pour un NAS, utilise un chemin UNC et configure le service avec un compte Windows ayant les permissions nécessaires.

Le service n’ouvre pas l’interface graphique. Pour une session locale, arrête-le d’abord et lance `main.py` depuis la session Windows.
