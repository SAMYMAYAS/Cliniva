from pathlib import Path
p=Path(__file__).resolve().parent.parent/'dist/index.html'
s=p.read_text(encoding='utf-8').replace('<link rel="stylesheet" href="style.css">','<link rel="stylesheet" href="style.css"><link rel="stylesheet" href="annotations.css">')
s=s.replace('Les autres séquences, les vues sagittales/coronales et les annotations IMAIOS ne sont pas disponibles. Aucun repère médical automatique n’est généré.','279 repères ont été transcrits de la vidéo sur 11 coupes, avec leur provenance. Les autres coupes et les libellés tronqués ne sont pas complétés automatiquement. Les positions, catégories et noms restent à valider.')
p.write_text(s,encoding='utf-8')
