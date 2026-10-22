# Contexte projet — Cliniva

Dernière analyse : 2026-09-29

## But de ce document

Ce fichier sert de carte de référence pour les prochains changements. Il décrit le code et les relations observables dans le dépôt. Il ne reprend aucune donnée patient présente sous `uploads/` et aucune valeur de secret de configuration.

## Vue d’ensemble

Cliniva est une application de gestion de clinique, conçue comme application de bureau Windows avec un serveur web embarqué. Le backend est **FastAPI**, exécuté par Uvicorn dans un thread, et l’interface native est une fenêtre `pywebview` qui affiche le serveur local ou un serveur distant. La persistance métier est dans **PostgreSQL** avec `psycopg2` et un pool de connexions partagé. Les ressources générées/téléversées (avatars, pièces, images échographiques, modèles DOCX, comptes rendus et PDF) restent sur le système de fichiers dans `uploads/`; la base conserve généralement leur chemin.

Le tableau de bord est une SPA simple pilotée par le serveur : `templates/dashboard.html` affiche une navigation permanente, puis demande le HTML d’un module Python via `/api/modules/{module_name}` et l’injecte dans la page. Les modules métiers fabriquent eux-mêmes leur écran HTML dans `render_html()` et incluent souvent leur JavaScript dans la chaîne retournée. Les API utilisées par ces écrans sont définies dans les mêmes fichiers Python que leurs gestionnaires SQL.

## Arborescence utile

- `main.py` : configuration, construction FastAPI, attachement des routeurs, login/logout, page dashboard, chargement dynamique des modules et lancement pywebview/Uvicorn.
- `database.py` : chemins `uploads`, configuration PostgreSQL (variables `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`), pool `ThreadedConnectionPool`, contexte transactionnel, création/mise à niveau initiale des tables et écriture d’audit.
- `app/auth.py` : empreinte des mots de passe, sessions persistées, lecture de l’utilisateur via cookie, audit des actions.
- `app/services.py`, `app/models.py` : données et modèles Pydantic du tableau de bord. Les métriques présentes sont actuellement des valeurs de démonstration statiques, sans lecture de la base.
- `templates/login.html` : écran de connexion.
- `templates/dashboard.html` : shell de navigation, thème, chargement de module par fetch et insertion des scripts du module.
- `modules/*.py` : domaines métier, chacun réunissant souvent gestion SQL, endpoints, HTML et JavaScript.
- `config.json` : configuration du lancement local et paramètres PostgreSQL. Ne pas recopier ni publier les valeurs sensibles de ce fichier.
- `client/config.json` : configuration de poste client pointant vers un serveur distant.
- `pip.txt` : actuellement une commande `pip install ...`, pas un format de dépendances réutilisable tel que `requirements.txt`.
- `uploads/` : documents et images, dont certains éléments sont des données médicales identifiantes. Ne pas indexer leur contenu dans le contexte, les logs ou des exemples.

## Démarrage et navigation

1. `main()` appelle `init_database()` puis lit `config.json`.
2. En mode local, Uvicorn démarre sur `local_host:local_port` (configuration actuelle : toutes interfaces, port 8000), le processus attend le port puis ouvre `http://127.0.0.1:<port>` dans pywebview. En mode distant, pywebview va sur `remote_server_url`; `client/config.json` donne un exemple de poste client.
3. `/login` sert `login.html`. `POST /api/auth/login` vérifie `login` et `password_hash`, crée une ligne `user_sessions`, pose le cookie HTTP-only `cliniva_session` pour 24 h et écrit l’audit de connexion.
4. `/` vérifie le cookie, récupère les métriques via `DashboardService` et rend `dashboard.html`.
5. `loadModule(name)` appelle `/api/modules/name`. Le serveur importe `modules.name`, appelle `render_html()` et retourne un fragment. Le navigateur remplace le contenu de la zone principale, puis recrée les balises script incluses dans le fragment pour initialiser le module.
6. Les scripts appellent les API des routeurs métiers; ces routes lisent/modifient PostgreSQL et parfois les fichiers sous `uploads/`.

Le thème/style utilise des classes Tailwind et des ressources front-end chargées par les templates ou les fragments. Les écrans module ne sont pas des templates Jinja séparés, ce qui rend les gros fichiers de gestion difficiles à parcourir et à tester isolément.

## Modules et liens fonctionnels

| Entrée/écran | Module Python | Préfixe API | Rôle et connexions principales |
|---|---|---|---|
| Tableau de bord `operations` | aucun module dédié; métriques dans `app/services.py` | — | Vue d’accueil. Données actuellement statiques/démonstratives. |
| Accueil / passages | `gestion_passages.py` | `/api/reception` | Recherche patients, création/édition/suppression de passages, actes, médecin traitant, documents, ticket et demande d’examen. Crée/consomme les références patient, type de consultation, médecin traitant, tarifs et clinique. |
| Dossier médical (DME) | `gestion_dme.py` | `/api/dme` | Consultation par patient et passage; constantes, antécédents, diagnostic, prescriptions, comptes rendus, échographies, livret PDF. Connexions vers passages, patients, personnel, médicaments/types d’actes et modèles de CR. |
| Caisse | `gestion_caisse.py` | `/api/finance` | Encaissements, règlements/annulations, statistiques et PV de clôture. Travaille principalement sur `patient_passages` et `caisse_mouvements`; joint les patients, et valide certains actes avec login/mot de passe. |
| Patients | `gestion_patients.py` | `/api/patients-module` | CRUD patient, données d’identité et pièces, acquisition scanner/empreinte. Patient parent des passages et données DME. |
| Médecins traitants | `gestion_medecins_traitants.py` | `/api/treating-doctors` | Répertoire référencé par les passages, suppression référentielle avec `ON DELETE SET NULL`. |
| Médicaments | `gestion_medicaments.py` | `/api/medications` | Répertoire, import/export. Catalogue consommé par les prescriptions du DME. |
| Modèles de CR | `gestion_modeles_cr.py` | `/api/cr-templates` | Métadonnées de modèles Word/HTML, service/modalité, téléchargement/téléversement et ouverture DOCX. Consommé par le DME lors de la génération des CR. |
| Paramètres clinique | `clinique_param.py` | `/api/clinic` | Identité, coordonnées, entête et services; utilisé par staff, modèles, reçus et documents. |
| Spécialités / actes | `gestion_specialites.py` | `/api/specialites-module` | Spécialités, types de consultation/actes, catégories, modalités, tarifs de jour/nuit et salles dédiées; utilisé dans passages, caisse, DME, salles et périmètre utilisateur. |
| Salles de soins | `gestion_salles_soin.py` | `/api/rooms` | Référentiel de salles, spécialités/types; relié aux types de consultation via listes JSON. |
| Personnel | `gestion_staff.py` | `/api/staff` | Personnel et rattachement service/spécialité; fournit des praticiens dans DME et valeurs de service à d’autres écrans. |
| Utilisateurs | `gestion_utilisateur.py` | `/api/users` | Comptes, rôles, activation, permissions JSON et spécialités autorisées. La connexion et plusieurs opérations de caisse consultent `users`. |
| Audit | `gestion_audit.py` | `/api/audit` | Consultation de `audit_logs`, alimentée par login/logout et `track_action` lorsque appelé par les modules. |

**Écart de navigation relevé :** le menu appelle `loadModule('gestion_actes_medicaux')`, mais aucun `modules/gestion_actes_medicaux.py` n’existe. L’écran de gestion actes/spécialités semble être `gestion_specialites.py`; son API est enregistrée sous `/api/specialites-module`. Il faut confirmer si le menu doit viser ce module ou si un module distinct était prévu.

## Modèle relationnel observé

La création initiale est concentrée dans `database.py`; certaines tables/colonnes DME et caisse sont aussi créées/ajustées au démarrage des gestionnaires dans leurs fichiers de module. Il s’agit donc d’un schéma évolutif par `CREATE TABLE IF NOT EXISTS` et `ALTER TABLE`, sans système de migrations versionnées identifié.

- `clinic_info` (singleton id 1) : identité clinique et configuration tarifaire/horaire.
- `clinic_services` : services; consommés comme libellés par personnel et modèles CR.
- `ref_specialites` : catalogue spécialités; référencé par libellé texte dans salles, types de consultation et personnel.
- `ref_categories`, `ref_modalites` : listes de référence pour les types d’actes.
- `rooms` : salles, spécialité en texte et `types` JSONB.
- `consultation_types` : actes/types, catégorie, modalité, tarifs, spécialité en texte, salles dédiées JSONB.
- `clinic_staff` : personnel, service et spécialité sous forme de texte.
- `medications` : catalogue médicament (nom, DCI, forme, dosage).
- `patients` : identité, coordonnées, photos, identité scannée/empreinte; `pieces_scannees` en JSONB.
- `treating_doctors` : répertoire médecins traitants.
- `users` : comptes, hash, état, permissions JSONB, spécialités autorisées JSONB.
- `user_sessions` : jeton de session et expiration, FK vers utilisateur avec cascade.
- `audit_logs` : journal, FK utilisateur nullable avec `SET NULL`.
- `patient_passages` : FK patient avec cascade; médecin traitant nullable; actes/questionnaire/documents en JSONB, informations de paiement et statuts.
- `patient_dme_records` : FK patient et passage avec cascade; constantes JSONB et champs cliniques (antécédents, allergies, traitement, conduite…).
- `patient_prescriptions` : FK patient et passage avec cascade; lignes d’ordonnance JSONB, statut et suppression logique/traçabilité.
- `patient_cr_records` : table créée par `gestion_dme.py`, associée aux CR produits; métadonnées et chemins de fichiers sont ajustés progressivement par ALTER.
- `patient_echographies` : table créée par `gestion_dme.py`; référence les fichiers image d’échographie liés au patient/passage.
- `caisse_mouvements` : table créée par `gestion_caisse.py`; mouvements financiers rattachés au passage.
- Relation centrale : `patients` 1→N `patient_passages`; passage 1→0/1 (logique applicative) données DME et 1→N prescriptions, CR, échographies et mouvements caisse. Les contraintes physiques exactes des tables DME/caisse doivent être vérifiées dans les DDL de ces modules avant une migration.

Plusieurs associations sont des textes libres (`specialite`, `service_nom`) et JSONB plutôt que des FK relationnelles. Renommer un libellé de référence peut donc laisser des valeurs historiques ou des données non synchronisées.

## Flux métier bout en bout

1. Paramétrer clinique, services, spécialités, catégories/modalités, salles et types d’actes/tarifs.
2. Enregistrer un patient dans le registre patient, ou le créer depuis la réception.
3. Ouvrir un passage pour ce patient : sélectionner date/heure, type et actes, médecin traitant, état et documents/questionnaire. Les tarifs et salle/spécialité proviennent des référentiels.
4. Calculer total/remise/versement/reste et encaisser depuis la réception ou la caisse. Les montants courants résident sur le passage; la caisse enregistre des mouvements pour les opérations de règlement/annulation.
5. Depuis le passage, documenter la consultation dans le DME, créer une prescription à partir du catalogue médicaments, générer un CR depuis un modèle et attacher les échographies; produire les documents patients correspondants.
6. Les administrateurs paramètrent comptes et permissions; le journal audit retrace les événements appelés par les routes métier.

## Constats techniques à traiter avec prudence

1. **Contrôle d’accès API à vérifier en priorité.** Le chargeur de fragment vérifie une session, mais plusieurs routes métiers ne semblent pas appeler une dépendance commune d’authentification. Les décorateurs n’ont pas de `dependencies` visibles. Quelques routes vérifient explicitement un utilisateur (`reception`, DME, certaines actions caisse), mais cela paraît inégal. Les permissions stockées en JSONB sont manipulées dans l’interface utilisateur; leur application effective côté serveur n’est pas visible de façon transversale. Avant exposition réseau, il faut inventorier et protéger toutes les routes et les fichiers servis.
2. **Hash de mot de passe faible.** `app/auth.py` utilise SHA-256 simple, sans sel ni facteur de coût. Le schéma initialise en plus un administrateur par défaut. À remplacer par un hash spécialisé (Argon2/bcrypt/PBKDF2 avec paramètres sûrs) et stratégie d’installation initiale contrôlée.
3. **Configuration sensible.** `config.json` contient des paramètres PostgreSQL avec identifiants. Les valeurs sont volontairement omises de ce document. Vérifier le suivi Git, changer les secrets déjà exposés si nécessaire, puis préférer variables d’environnement ou stockage protégé.
4. **Serveur d’écoute.** `local_host` est réglé sur `0.0.0.0`; le serveur est donc joignable sur les interfaces réseau, alors que le commentaire du lanceur insiste sur l’accès WebView local. L’URL WebView locale en `127.0.0.1` ne restreint pas l’écoute. Cookie sans `secure` (HTTP local) et pas de TLS configuré.
5. **Chargement dynamique.** Le nom de module est filtré contre les séparateurs de chemin, mais le chargeur tente aussi l’import à la racine après `modules.*`. Une liste explicite de modules chargeables réduirait la surface. Toute nouvelle route de module doit vérifier session et permission côté serveur.
6. **Initialisation DB avale les erreurs.** `init_database()` intercepte les erreurs et ne les relance pas; `main()` peut donc continuer malgré schéma absent. Les gestionnaires caisse/DME font eux aussi des DDL à l’exécution, ce qui couple le lancement des écrans aux changements de schéma.
7. **Tableau de bord non réel.** `DashboardService.get_dashboard_metrics()` renvoie des compteurs/noms statiques de démonstration en anglais, sans données de la clinique.
8. **Gestion des documents.** Les chemins et conversions DOCX/PDF dépendent de Windows/Word ou composants disponibles; inspecter les validations d’upload et le contrôle de propriété avant de modifier les routes de téléchargement. `uploads/` contient des fichiers sensibles, à exclure des commits/tests partagés.
9. **Dépendances et reproductibilité.** `pip.txt` n’est pas un lock ou une liste standard; les imports montrent notamment FastAPI, Uvicorn, Jinja2, Pydantic, psycopg2, pywebview et des bibliothèques document/PDF/tableur à relever précisément. Aucun environnement/test automatisé n’est recensé dans l’arborescence inspectée.
10. **Architecture compacte.** Les fichiers `gestion_dme.py`, `gestion_passages.py`, `gestion_patients.py`, `gestion_caisse.py` combinent beaucoup de responsabilités et de HTML/JS inline. Des évolutions futures devraient préserver les contrats API et être faites par parcours métier, car un changement de libellé/format de données peut toucher plusieurs modules.

## Repères pour poursuivre le développement

- Avant une évolution fonctionnelle, suivre le flux depuis l’événement HTML/JS, vers la route FastAPI, le manager/SQL, puis la table et les autres consommateurs.
- Pour les données de référence et tarifs, vérifier la compatibilité réception → caisse → DME/CR.
- Pour les champs persistés dans `actes_json`, autres colonnes JSONB ou chemins d’upload, rechercher tous les consommateurs et les anciens formats avant de changer leur structure.
- Pour les nouveaux endpoints, établir une authentification commune et contrôler chaque permission côté serveur; ne pas considérer un bouton masqué comme une protection.
- Les routes des modules se trouvent en fin ou près de la fin des fichiers; `render_html()` contient l’interface. `main.py` est le registre central des routeurs et `dashboard.html` le registre de navigation.
- Lors de l’analyse initiale, aucune modification fonctionnelle ni migration n’avait été effectuée. Les fichiers médicaux d’upload n’ont pas été ouverts ni copiés dans ce contexte.

## Détails de configuration à ne pas manquer

`main.py` lit `config.json` relativement au répertoire courant pour choisir le mode serveur et la fenêtre. À l’inverse, `database.py` configure PostgreSQL uniquement depuis les variables `DB_*`, avec des valeurs par défaut codées en dur; la section `postgres` présente dans `config.json` n’est pas lue par le code observé. En conséquence, changer les paramètres PostgreSQL dans ce JSON ne change pas la connexion effective, sauf si un autre lanceur externe exporte les variables correspondantes. `client/config.json` n’est pas référencé par `main.py` et semble être un fichier de configuration de déploiement manuel.

## État du dépôt

À la racine analysée, aucun répertoire `.git` n’est présent; `git status` ne peut donc pas fournir d’état de modifications ni d’historique. Le contexte doit être mis à jour avec les prochaines évolutions importantes.

## Vérification de la base réelle (2026-09-29)

Connexion établie à `cliniva_db` en transaction `READ ONLY`; les requêtes de vérification ont été terminées par `ROLLBACK`. Seules les métadonnées système (catalogue, clés/index) et des `COUNT(*)` agrégés ont été consultés; aucune ligne nominative, clinique, de paiement, de compte ou de document n’a été lue. La base a 27 tables applicatives et les tables cliniques centrales sont peuplées.

### Écarts importants entre le code observé et le schéma réel

- La base contient six tables DME supplémentaires absentes des DDL du code courant : `dme_antecedents`, `dme_consultations`, `dme_ordonnances`, `dme_comptes_rendus`, `dme_echographies` et `patient_cr_archives`. Elles forment un modèle DME alternatif plus normalisé : consultations, antécédents du patient, ordonnances, CR et échographies, parfois reliés par `id_consultation` en plus de patient/passage. Les sources actuelles de `gestion_dme.py` lisent et écrivent surtout `patient_dme_records`, `patient_prescriptions`, `patient_cr_records` et `patient_echographies`; les six tables additionnelles ne sont pas référencées dans le code courant recherché. Elles ressemblent à un schéma d’une autre version ou à des tables conservées, mais leur provenance et leur usage historique ne sont pas établis par une inspection de schéma seule. Ne pas les supprimer ni migrer avant d’identifier leur producteur et leur contenu avec une procédure approuvée.
- La table réelle `rooms` comporte `type_salle`, absent du DDL actuel de `database.py` et non repéré dans les modules Python. `patient_passages` contient aussi les champs d’annulation (`raison_annulation`, `date_annulation`, `utilisateur_annulation`, `montant_annule`) ajoutés par `gestion_caisse.py` au démarrage du module.
- Les colonnes de `patient_passages` et `patient_dme_records` incluent les champs attendus par la version courante. Le compte PostgreSQL `postgres` a permis une lecture des catalogues seulement; la tentative antérieure avec `MAYAS` échouait car ce rôle n’existe pas dans cette instance.

### Relations et contraintes vérifiées dans PostgreSQL

- Les FK présentes confirment `patients → patient_passages → caisse_mouvements`, avec suppression en cascade pour passages et mouvements; le médecin traitant d’un passage est nullable avec `SET NULL`.
- Les tables DME normalisées relient leurs objets au patient en cascade et les références optionnelles au passage/consultation avec `SET NULL`; les objets du modèle courant (DME, ordonnances, CR, échographies) sont principalement rattachés au patient et au passage avec cascade.
- `dme_antecedents.id_patient` a une contrainte UNIQUE : un seul dossier d’antécédents par patient. À l’inverse, `patient_dme_records.id_passage` n’a pas de contrainte unique dans la base, donc « au plus un DME par passage » n’est pas garanti au niveau SQL même si l’interface semble supposer un enregistrement unique.
- Les contraintes uniques métier observées sont les noms de catégories/modalités/spécialités et le login utilisateur. Les index sont essentiellement les clés primaires, ces contraintes uniques et l’index unique de `dme_antecedents.id_patient`; aucune indexation dédiée sur les clés étrangères n’a été observée. Pour de gros volumes, examiner les requêtes de filtres/tri et ajouter des index via migration contrôlée.
- Plusieurs liens restent des champs texte/JSONB et non des FK (service, spécialité, salle/type associé, actes d’un passage). Les agrégats seuls ne valident pas l’intégrité métier des valeurs stockées.

### Conséquence pour la suite

Le schéma PostgreSQL courant reflète des évolutions qui ne sont pas entièrement documentées dans `init_database()`. Avant toute migration ou suppression, établir une matrice **table ↔ code actif ↔ écrans/rapports ↔ chemins de fichiers**. Le modèle DME double est la priorité d’analyse : déterminer quelle génération alimente les dossiers utilisés, vérifier les écritures par version et planifier seulement ensuite une éventuelle consolidation. Maintenir les requêtes exploratoires en lecture seule et continuer à éviter les valeurs médicales identifiantes dans le contexte du projet.

## Documentation de reprise reçue

L’utilisateur a fourni un résumé texte et le PDF Cliniva_Architecture_Et_Specifications_Completes.pdf, qui décrivent l’architecture, la base, les modules et suggèrent la prise de rendez-vous comme prochaine évolution. Ce contenu est une spécification de référence; ses descriptions ne remplacent pas la vérification du code et du PostgreSQL.

Éléments recoupés ou corrigés :

- L’architecture de bureau Windows, PyWebView, FastAPI et l’injection SPA sont confirmées par les sources.
- Le document PDF parle de « 18 tables » mais énumère 20 tables; la base inspectée en contient 27, dont six tables DME d’une autre génération.
- secrets.token_hex(32) produit 64 caractères hexadécimaux à partir de 32 octets aléatoires; le document confond longueur du jeton hexadécimal et octets aléatoires.
- Le lanceur écoute sur local_host, configuré actuellement à 0.0.0.0, puis WebView ouvre 127.0.0.1; le serveur n’écoute donc pas uniquement sur loopback.
- Le code ne trace pas automatiquement chaque requête HTTP. Il écrit des événements aux endroits qui appellent track_action/record_audit.
- Le trigger de caisse est bien présent dans la base inspectée (trg_sync_passage_caisse, après insertion d’un passage) et est créé par CaisseManager.ensure_caisse_schema() au chargement de données de caisse. L’initialisation générique de database.py ne le crée pas.
- L’inventaire API du document est une sélection, pas la liste complète des routes (plusieurs endpoints CRUD/suppression/téléchargement/export ne sont pas détaillés).
- appointments n’existait pas dans PostgreSQL au moment de l’inspection; le schéma ci-dessous est une évolution locale en code, pas une modification déjà appliquée à la base.

## Évolution en cours — agenda des rendez-vous

Le périmètre proposé a été relié au modèle existant :

- Le praticien est une ligne de clinic_staff (fonctions médecin/professeur/docteur); la salle vient de rooms, le patient de patients, et la spécialité est contrôlée par les références et la spécialité du praticien.
- Une nouvelle table appointments est déclarée dans database.py, avec références FK, statuts contrôlés, heures validées, lien unique vers un passage, horodatage de modification et index de calendrier. Elle sera créée lors du prochain lancement qui exécute init_database(); aucun DDL n’a été lancé sur la base live pendant l’édition.
- modules/gestion_rendezvous.py apporte les vues jour/semaine/mois, filtres, recherche patient, création/modification/annulation, détection de chevauchement par praticien, salle ou patient et conversion transactionnelle en passage. Les contrôles de chevauchement sérialisent les réservations concurrentes par verrous advisory PostgreSQL.
- Les routes sont enregistrées dans main.py, le menu dans templates/dashboard.html; la matrice utilisateurs expose rdv_view et rdv_manage. Les API contrôlent la session, la permission et le périmètre specialites_autorisees.
- La conversion crée un passage initial sans acte facturable (montant nul) et le marque comme rattaché au rendez-vous. Le rendez-vous ne fournit ni actes ni médecin traitant, alors que le flux normal d’accueil les demande; le comportement évite d’inventer un tarif ou de confondre praticien et médecin traitant. Confirmer le parcours métier avant d’ajouter une prestation automatique.
- L’application, le parcours navigateur, l’initialisation du nouveau schéma et les conflits en conditions réelles restent à vérifier. La table n’est pas encore présente dans PostgreSQL live; un redémarrage de l’application exécutant l’initialisation est nécessaire pour la créer.

## Évolution — accès DME, verrouillage de consultation et statut de passage

- À l’ouverture de `openPatientDme(patientId, passageId)`, l’écran demande désormais le mode **Lecture seule** ou **Exécuter la consultation**. Si une ligne existe déjà dans `patient_dme_records` pour le passage, seul le mode lecture seule est proposé.
- Le démarrage en mode exécution appelle `POST /api/dme/consultation/start`, valide l’association patient/passage et place `patient_passages.statut` à `En cours`. La liste des passages est rechargée lorsqu’elle est disponible.
- Le premier enregistrement clinique exige un passage `En cours`, insère le DME et synchronise les actes et montants. Il marque ensuite le passage `Terminé`. La réponse indique le praticien et l’état final.
- Toute ligne DME déjà présente est considérée comme verrouillée. `save_consultation()` refuse désormais de mettre à jour cette ligne. À la lecture, un DME préexistant avec un passage qui n’est pas encore `Terminé` met ce passage à `Terminé` pour réconcilier les anciennes données.
- Le front-end désactive les contrôles de modification, création et suppression pour les onglets lecture seule et les DME enregistrés; la consultation des historiques et l’ouverture/impression des documents restent disponibles. Les routes de mutation des ordonnances, comptes rendus, livrets et échographies contrôlent aussi le verrou côté serveur. L’ouverture Word est traitée comme une action de modification; les routes de visualisation PDF restent en lecture seule.
- Le verrou repose sur la présence d’une ligne dans `patient_dme_records`; il n’ajoute pas de colonne ni contrainte SQL. La base live n’a pas été modifiée directement dans cette évolution. L’interface et les routes n’ont pas encore été parcourues dans l’application, et aucun test automatisé n’a été lancé.

## Évolution — détail des archives DME

- L’onglet `Historique & Archives` fournit désormais une action `Voir l’archive` par passage.
- `GET /api/dme/archive/{patient_id}/{passage_id}` charge une seule archive en vérifiant l’association patient/passage. Le résultat inclut le dossier clinique, constantes, actes et montants, ordonnances avec lignes de médicaments et métadonnées de comptes rendus/livrets; chemins de fichiers et contenu HTML ne sont pas renvoyés.
- La fenêtre d’archive affiche les champs cliniques en lecture seule. Les ordonnances ouvrent leur vue PDF; les comptes rendus HTML utilisent l’aperçu A4, et les documents Word/livrets utilisent la visionneuse PDF existante. L’absence d’une fiche clinique DME est indiquée sans masquer les autres archives disponibles.
- La table `patient_dme_records` n’ayant pas de contrainte unique par passage, les deux lectures d’historique (liste et détail) prennent le dernier enregistrement DME du passage pour éviter les doublons d’affichage.
- Cette évolution modifie seulement le code Python/HTML/JavaScript; aucun DDL ni accès aux données réelles n’a été effectué. Aucun test n’a été lancé.

## Évolution — ajout rapide d’un médicament depuis une ordonnance DME

- Le formulaire d’ajout rapide dans l’onglet ordonnance reprend les champs de `gestion_medicaments` : nom commercial, DCI, forme et dosage; il écrit via `POST /api/medications/save`.
- Après enregistrement, le nouveau médicament est ajouté au cache de recherche et sélectionné dans le champ de l’ordonnance, sans effacer posologie, durée ou quantité déjà saisis.
- L’ajout n’est proposé qu’en mode exécution. En lecture seule, les règles d’accès DME continuent à désactiver ce contrôle et la fonction vérifie aussi le mode avant l’écriture.
- Le script JavaScript DME rendu depuis la chaîne Python a été vérifié avec `node --check`; la syntaxe Python a été compilée. Le parcours visuel et l’écriture effective en base restent à vérifier dans l’application.

## Évolution — cloisonnement des ordonnances par consultation

- `patient_prescriptions` possède désormais `id_consultation` en plus de `id_patient` et `id_passage`. L’historique de l’ordonnance, les archives, la suppression et l’impression utilisent les trois identifiants; les lectures et écritures API valident leur concordance côté serveur.
- Le démarrage d’une consultation DME crée une fiche clinique brouillon non verrouillée afin d’obtenir son identifiant avant de générer une ordonnance. L’enregistrement final met à jour cette même fiche puis la verrouille; les anciens DME restent verrouillés via une migration de colonne `verrouille` avec valeur par défaut vraie.
- La migration DME ajoute le lien, rattache les ordonnances historiques au dernier DME du même patient et passage, puis indexe les trois champs. Des ordonnances anciennes sans DME peuvent garder `id_consultation` NULL et restent lisibles en étant scellées par patient + passage + NULL.
- Les scripts Python et le JavaScript réellement rendu par `render_html()` passent les vérifications de syntaxe. La migration réelle et les parcours de création, affichage PDF, suppression et historique restent à vérifier sur la base et l’application.

## Évolutions récentes — interface, caisse et passages (2026-09-30)

- Le logo Cliniva est dessiné en SVG inline, aux couleurs vert/noir de l’application, dans `templates/dashboard.html` (navigation latérale) et `templates/login.html` (connexion). Il ne dépend pas d’un fichier image externe.
- `modules/gestion_caisse.py` calcule maintenant le KPI `remboursements_total` comme la somme des mouvements négatifs de type `ANNULATION` sur la période filtrée. Le PV de clôture affiche aussi une colonne « Nature des prestations », issue des `actes_json` du passage.
- La cellule « Règlement » du tableau de passages affiche le badge « Remboursé », le montant remboursé et `raison_annulation` pour un passage dont `statut_paiement` vaut `Annulé`. `ReceptionManager.get_all()` sélectionne ces colonnes et les coordonnées du patient nécessaires à la modification.
- Une permission indépendante `cash_refund` est ajoutée à l’éditeur de droits utilisateur. L’API de remboursement (`POST /api/finance/annuler-paiement`), l’action du journal et l’accès au module caisse la vérifient. Elle est distincte de `cash_remise`. Pour les comptes existants sans clé `cash_refund`, l’interface reprend provisoirement l’ancien état `cash_remise` à l’ouverture de la fiche; une sauvegarde du compte persiste ensuite le choix dédié. Les presets Caissier, Direction et Administrateur autorisent le remboursement. `database.py` ne fait qu’ajouter ces clés à la matrice de l’administrateur par défaut lors d’une nouvelle installation; aucune modification de la base live n’a été appliquée.
- Depuis un passage non encaissé, non commencé en DME et encore `En attente`, les comptes autorisés par `pat_edit` ouvrent le formulaire habituel « Nouveau Passage », prérempli avec le dossier existant. Les champs personnels (nom, prénom, date de naissance, genre, groupe sanguin, téléphones, e-mail, wilaya, commune) sont visibles et modifiables directement dans ce formulaire; le formulaire patient habituel reste accessible pour modifier les photos. Le formulaire passage édite médecin, date/heure, prestations, observation/questionnaire médical et documents d’admission. `GET /api/reception/patient/edit-data/{passage_id}` charge les données associées seulement au clic; `POST /api/reception/patient/update-from-passage` revalide côté serveur les droits, l’association patient/passage, l’absence d’historique caisse/avance, l’absence de DME et le statut `En attente`, met à jour atomiquement patient et passage, et journalise. Les valeurs financières saisies sont verrouillées; le serveur recalcule total et reste depuis les actes en conservant la remise, puisqu’il n’existe aucun encaissement. Après sauvegarde, la liste est rafraîchie et la demande d’examen est régénérée depuis les informations mises à jour.
- Dernière vérification statique du 2026-10-01 : compilation Python de `modules/gestion_passages.py` avec `SyntaxWarning` élevé en erreur et `git diff --check` réussis après réutilisation du formulaire passage. Aucun test fonctionnel navigateur, test automatisé ou changement direct de données PostgreSQL n’a été effectué dans cette évolution.

## Reprise dans un nouveau chat

Le présent document est le résumé persistant du projet et doit être lu au début d’un nouveau chat Cliniva. Il conserve la structure, les relations et les évolutions importantes, mais pas le verbatim de chaque échange ni le journal complet des modifications fichier par fichier. L’historique intégral demeure dans l’ancien chat; le nouveau chat ne doit pas supposer qu’il en a automatiquement le contenu. Pour reprendre proprement, ouvrir le dossier `C:\Clinical_Build_Codex`, lire `PROJECT_CONTEXT.md`, puis examiner les fichiers concernés et demander à l’utilisateur les messages d’erreur/captures manquants. Le dépôt Git est initialisé; consulter `git status --short` et `git log` pour l’état courant. Ne pas committer sans instruction explicite.

## Évolution — courbes de croissance pédiatriques DME (2026-10-02)

- `modules/gestion_dme.py` ajoute un onglet Courbes de croissance visible lorsque l’âge au passage est inférieur ou égal à 19 ans. Les courbes OMS en scores Z superposent les mesures des consultations DME antérieures avec le point de la consultation courante.
- Indicateurs : taille/âge 0–19 ans (OMS 0–5 + référence 5–19), poids/âge 0–10 ans, IMC/âge 0–19 ans, périmètre crânien/âge 0–5 ans, périmètre brachial/âge 3 mois–5 ans. L’affichage est sexospécifique et n’extrapole pas en dehors des plages de référence.
- Les nouvelles mesures poids/taille/PC/PB se rangent dans `patient_dme_records.constantes_json`; le schéma SQL reste inchangé. Un endpoint DME sert `resources/who_growth/growth_reference.json`, reconstruit depuis les tableaux Excel OMS avec `resources/who_growth/build_reference.py`. Les sources officielles et plages sont documentées dans le README du dossier.
- Validations statiques : `py_compile` avec avertissements de syntaxe traités en erreur, `node --check` du script inline extrait du rendu et `git diff --check` réussis. Pas de test navigateur ni d’écriture de consultation en base.

## Évolution — formulaire patient partagé depuis l’accueil (2026-10-02)

Depuis les modules Accueil & Passages et Attente Radiologie, « Nouveau Patient » ouvre le composant et le JavaScript de `gestion_patients.py`, avec ses champs complets (identité, contact, wilaya/commune, groupe sanguin, photos/profils, empreinte, pièce d’identité et documents numérisés). Le composant Patients est inclus masqué dans le rendu Passage afin de réutiliser le même formulaire et les mêmes fonctions, sans dupliquer les champs. La création utilise l’API `/api/patients-module/save`; un callback recharge les passages puis sélectionne la nouvelle fiche. En mode correction d’un dossier déjà ouvert, le formulaire de correction préconsultation reste utilisé. Contrôles statiques réussis : compilation Python, syntaxe JavaScript du rendu Patients/Passages et `git diff --check`.
# Sauvegarde complète Cliniva (2026-10-10)

- Nouvelle option administrateur `gestion_backup` dans la sidebar, avec API protégée sous `/api/backup`. Le chemin de destination absolu est mémorisé dans `%LOCALAPPDATA%/Cliniva/backup_settings.json` (ou `~/.cliniva/Cliniva/backup_settings.json`) et doit être accessible depuis le serveur Cliniva.
- La sauvegarde est créée dans un dossier daté et contient une copie de l’application (incluant `uploads`), un dump PostgreSQL au format custom via `pg_dump`, ainsi que les répertoires PACS et DICOM Print externes configurés dans `config.json`. `MANIFEST.json` décrit le contenu et le dump se restaure avec `pg_restore`.
- L’opération s’exécute en arrière-plan avec suivi de progression. Seuls les caches Python sont exclus; les sources ne sont pas modifiées. Les identifiants PostgreSQL ne sont pas inclus dans le manifeste. Nécessite `pg_dump` installé et un emplacement de destination hors des données source; les sauvegardes contiennent des données médicales sensibles.
- `main.py` garde `gestion_backup` chargé sans hot-reload pour conserver l’état de progression du job. Aucune exécution réelle de sauvegarde ni restauration n’a été effectuée; vérifier l’installation de `pg_dump`, l’espace du disque cible et le parcours de restauration avant de s’appuyer sur une première sauvegarde.
