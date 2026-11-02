from pathlib import Path
root=Path(__file__).resolve().parent.parent
p=root/'dist/index.html';s=p.read_text(encoding='utf-8')
for key,label in [('pan','Déplacer'),('annotate','Annoter'),('labels','Repères')]:s=s.replace(f'id="{key}" class=',f'id="{key}" aria-label="{label}" class=')
s=s.replace('Utilisez « Lobes et régions » pour changer de couche.', 'Activez séparément « Repères » et « Lobes et régions » pour les afficher seuls ou ensemble. Les longues colonnes de légendes défilent indépendamment ; l’image reste centrée dans l’écran.')
p.write_text(s,encoding='utf-8')
p=root/'README.md';s=p.read_text(encoding='utf-8').replace('Bouton « Lobes et régions » pour basculer entre les deux couches', 'Boutons indépendants « Repères » et « Lobes et régions » pour afficher les couches séparément ou ensemble')
s+='\n## Affichage centré et superposition\n\nLe visualiseur tient dans la hauteur de l’écran, indépendamment du nombre de légendes. Seules les colonnes de légendes et la bibliothèque défilent ; la molette sur l’image continue de parcourir les coupes. Les boutons Repères et Lobes et régions sont indépendants. Sur écran étroit, les légendes sont sous l’image et le bouton ☰ ouvre la bibliothèque. Les cinq états de couche ont été vérifiés à 1440 × 900 et 390 × 844, puis le défilement des légendes a été contrôlé dans la page utilisateur.\n'
p.write_text(s,encoding='utf-8')
