# Neuro Atlas local

Application indépendante de CLINIVA, sans dépendance d’exécution autre que Node.js.

Double-cliquer sur Lancer-Neuro-Atlas.cmd, puis ouvrir http://127.0.0.1:4177. Garder le serveur ouvert. Autre méthode : npm start depuis ce dossier.

## Série et repères

- 24 captures IRM axiales originales, conservées dans dist/assets/.
- 647 repères détaillés et 81 repères de lobes et régions, répartis sur les 24 coupes : 728 au total.
- Boutons indépendants « Repères » et « Lobes et régions » pour afficher les couches séparément ou ensemble ; opacité réglable de 10 à 80 %.
- Contours SVG sur les coupes 1 à 22. Les captures 23 et 24 n’ont pas de région colorée : le repère « Moelle spinale » reste disponible.
- Légendes extérieures et flèches, recherche, catégories, zoom, déplacement, luminosité, contraste, lecture automatique et révision.
- Modification, déplacement, suppression et restauration de la dernière suppression.
- Sauvegarde locale et export/import JSON, incluant les réponses aux demandes de précision.

Sources : fichiers Capture d’écran (28) à (51) pour les structures détaillées et (53) à (76) pour les régions. Recalage vers les PNG originaux : +70 pixels en x et +13 en y, mesuré sur chacune des 24 coupes détaillées. Les libellés sont transcrits par OCR et corrigés ; les points sont relevés à partir des traits et disques, avec corrections des cas ambigus. Les contours sont des approximations vectorielles des zones fournies ; leurs couleurs sont harmonisées.

Les 279 identifiants du relevé vidéo sont conservés. Au chargement, les anciens repères non modifiés sont remplacés par les transcriptions des captures ; les modifications personnelles et suppressions restent conservées.

## Douze libellés à préciser

Les captures tronquent encore 12 libellés sur les coupes 18 à 23 : citernes cérébellomédullaires et tonsilles cérébelleuses / paraflocculus. Ils apparaissent avec une ellipse et dans « À préciser avec vous ». Chaque demande ouvre la capture source. Les réponses restent locales et exportables ; elles ne sont pas envoyées automatiquement à un agent.

Cliquer sur une légende permet de consulter sa provenance et de corriger son nom ou sa position. Le mode révision masque les noms et les révèle au clic.

## Données et limites

Images IMAIOS e-Anatomy fournies par l’utilisateur ; attribution : Antoine Micheau et Denis Hoa, société IMAIOS. Livraison locale, non publiée. Les PNG sont des captures, pas des DICOM : aucune mesure millimétrique, autre pondération ou reconstruction 3D n’est déduite. Les fichiers PNG originaux ne sont pas modifiés.

La transcription et les contours n’ont pas reçu de validation clinique. L’application ne transmet aucune image ni annotation. La sauvegarde dépend du navigateur et de l’origine http://127.0.0.1:4177 : exporter le JSON avant de vider les données du navigateur ou changer de poste.

## Vérifications et intégration

npm test : 11 tests, dont couverture des deux couches sur 24 coupes, préservation des identifiants, coordonnées recalées, validation et aller-retour JSON, contours et disposition des légendes. Les anciens fichiers source (4) à (27) ne sont plus disponibles : leur comparaison externe est facultative ; la résolution des 24 PNG livrés est vérifiée.

Les scénarios REA sont dans evidence/. screenshots-qa.json vérifie suppression/restauration, modification puis rechargement, demandes de précision, régions, opacité et révision dans un profil temporaire ; ses 58 actions réussissent.

dist/ est autonome en HTML/CSS/JS et peut être servi par une future route CLINIVA. CLINIVA n’est pas modifié. dist/annotations-completes.json contient les 728 repères et leur provenance ; le relevé vidéo historique reste séparé.

## Affichage centré et superposition

Le visualiseur tient dans la hauteur de l’écran, indépendamment du nombre de légendes. Seules les colonnes de légendes et la bibliothèque défilent ; la molette sur l’image continue de parcourir les coupes. Les boutons Repères et Lobes et régions sont indépendants. Sur écran étroit, les légendes sont sous l’image et le bouton ☰ ouvre la bibliothèque. Les cinq états de couche ont été vérifiés à 1440 × 900 et 390 × 844, puis le défilement des légendes a été contrôlé dans la page utilisateur.
