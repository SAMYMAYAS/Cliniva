# Examen du modèle et reconstruction indépendante

Examen effectué le 16 octobre 2026 avec REA 6.5.0, commande `capture-browser-scenario`, navigateur Chrome existant en mode headless et profil temporaire. Aucun moteur natif nécessaire. La session Codex n’exposait pas les outils MCP REA ; le CLI installé a été utilisé.

## Observations

La capture du DOM et de l’arbre d’accessibilité de la page IMAIOS a montré : une série axiale de 24 coupes, des vues de référence, un choix de pondération T1, des catégories de structures anatomiques, des fonctions d’entraînement, d’épingles et de légendage ciblé, des transformations, un contrôle d’opacité, un champ numérique de coupe et un bouton plein écran.

REA a rempli le champ numérique `input[type=number]` avec 12, puis pressé Entrée. Toutes les étapes déclarées ont été exécutées sans erreur. Cette observation établit le fonctionnement du contrôle de navigation ; elle ne constitue pas une validation complète de chaque fonction ni de la précision anatomique.

Les fichiers `evidence/reference-result.json` et `evidence/reference-navigation-result.json` conservent les résultats REA et leurs identifiants de preuve, les empreintes, les scénarios et les limites déclarées. Le navigateur temporaire a été fermé par REA. Aucun code du modèle n’a été intégré à la reconstruction et aucune série d’images supplémentaire n’a été téléchargée.

## Reconstruction

Interface originale en HTML/CSS/JavaScript. Données : les 24 PNG fournis par l’utilisateur, copiés sans transformation, plus un format JSON pour les repères personnels. La navigation, le zoom, la lecture automatique, la recherche et la révision ont été réimplémentés. Les références multiplanaires, les autres séquences et les légendes d’origine ne sont pas reconstruites à partir des captures.

## Vérification

- Tests Node : identité SHA-256 et dimensions des 24 images ; validation d’annotations et rejet des imports malformés. Trois tests réussis.
- Scénario REA local : passage à la coupe 12, zoom à 120 %, placement d’un repère, enregistrement, activation du mode révision et révélation du nom. L’arbre final montre la coupe 12, 120 %, le repère et le mode révision actif.
- Scénario REA étendu : rechargement, restauration du repère sauvegardé, passage à la coupe 24, lancement/arrêt de lecture et retour à la coupe 12. Toutes les étapes ont été exécutées sans erreur. La lecture a été exercée, sa cadence exacte n’a pas été mesurée.
- Scénario mobile : viewport 390 × 844, sélection de la coupe 12, capture d’écran et arbre d’accessibilité pour vérification visuelle.

Les captures de test utilisent un profil temporaire : le repère « Repère de test » n’est pas ajouté à la session utilisateur. La validation du format d’import/export est automatisée ; la boîte de téléchargement et le sélecteur de fichier n’ont pas été exercés par REA. Aucune validation clinique ou homologation n’est revendiquée.
