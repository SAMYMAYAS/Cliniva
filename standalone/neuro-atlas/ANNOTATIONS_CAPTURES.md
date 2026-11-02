# Transcription des 48 captures — 16 octobre 2026

Livraison : 647 repères détaillés et 81 régionaux sur 24 coupes. Douze noms restent tronqués et associés à une demande de précision.

Chaque repère conserve l’empreinte SHA-256 de sa capture, son numéro, ses coordonnées source et la translation [70, 13]. Le recalage des 24 coupes détaillées présente une erreur médiane nulle sur les zones échantillonnées. Les erreurs aux croisements de traits et les faux points sur le fond IRM ont été corrigés. Les corrections explicites figurent dans tools/build_screenshot_annotations.py ; notamment sinus transverse coupe 16 et culmen coupe 15. Les noms OCR ont été normalisés et les variantes supérieur/inférieur contrôlées.

Les SVG de dist/regions/ sont extraits des zones chromatiques centrales. Les traits fins sont retirés par morphologie ; les contours sont simplifiés et restent approximatifs. Les teintes distinguent notamment insula, lobes cérébraux, mésencéphale, pont et lobes cérébelleux. Les coupes 23 et 24 ont un SVG vide, conformément aux sources.

evidence/screenshots-ocr.json, screenshots-geometry.json et screenshots-transcription-review.json conservent les étapes intermédiaires. dist/annotations-completes.json est le résultat final importable. ANNOTATIONS_VIDEO.md décrit l’étape historique précédente ; ses limites de couverture ne s’appliquent plus à cette livraison.

Validation technique : 11 tests Node réussis ; 58 actions REA réussies, dont modification persistante après rechargement et restauration d’une suppression. Les essais utilisent un profil temporaire. La page utilisateur a été actualisée avec 728 repères.

Ce contrôle de transcription et de fonctionnement ne constitue pas une validation anatomique ou clinique.
