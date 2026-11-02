# Relevé vidéo — 16 octobre 2026

Source : vidéo fournie par l’utilisateur, 44,3666 secondes, 1918 × 1078, 30 images/s. L’analyse utilise 24 images échantillonnées, puis 11 images sélectionnées pour leurs coupes distinctes et leurs légendes lisibles. Les noms et extrémités des traits sont transcrits manuellement ; REA sert à tester le comportement du visualiseur reconstruit et ne reconnaît pas automatiquement l’anatomie.

| Coupe | Image vidéo | Instant (s) | Repères |
| --- | --- | --- | --- |
| 2 | 04 | 7,394 | 14 |
| 3 | 03 | 5,546 | 16 |
| 5 | 05 | 9,243 | 22 |
| 8 | 06 | 11,092 | 30 |
| 13 | 02 | 3,697 | 59 |
| 14 | 00 | 0,000 | 50 |
| 15 | 01 | 1,849 | 35 |
| 18 | 12 | 22,183 | 20 |
| 19 | 11 | 20,335 | 18 |
| 20 | 08 | 14,789 | 11 |
| 24 | 09 | 16,637 | 4 |

Total : 279 repères. Les timestamps sont ceux demandés lors de l’extraction ; la trame décodée est la première à cet instant ou juste après (précision d’une trame, environ 33 ms).

`evidence/video/registration.json` consigne la recherche de translation sur une zone centrale, en excluant les pixels colorés. Résultat commun : x +70, y +13 ; erreur absolue médiane en niveaux de gris de 1 à 2 sur 255. Cette mesure vérifie la correspondance des coupes, pas la précision de chaque extrémité de trait. Les points sont relevés manuellement, donc approximatifs, et le classement par catégorie est à vérifier.

Les coupes 1, 4, 6, 7, 9, 10, 11, 12, 16, 17, 21, 22 et 23 ne sont pas annotées dans cette livraison. Cela ne prouve pas qu’aucune annotation n’y apparaît dans la vidéo : elles ne font pas partie des images sources sélectionnées. Aucun libellé incomplet n’est reconstitué et aucune annotation n’est propagée d’une coupe à l’autre.

## Vérifications effectuées

- Six tests Node réussis : PNG inchangés, import/export, données invalides, 279 repères sur 11 coupes, conservation de la provenance et coordonnées recalées.
- REA CLI : chargement des repères, recherche « Couronne radiée », ouverture de sa fiche, modification dans un profil temporaire, rechargement et restauration du nom modifié, mode révision et révélation d’un point. Toutes les étapes du scénario corrigé ont réussi.
- Arbre d’accessibilité et capture du résultat conservés dans `evidence/annotations-qa-result.json` et `evidence/annotations-preview.png`.
- Page utilisateur actualisée : 279 repères, 22 sur la coupe 5. Les données de test REA restent dans son profil temporaire.

Une validation anatomique par un professionnel reste nécessaire avant de considérer ces repères comme fiables. Aucune validation clinique n’a été réalisée.
