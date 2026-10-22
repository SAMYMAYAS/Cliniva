# Références OMS de croissance

`growth_reference.json` contient des lignes de scores Z (-3 à +3) utilisées pour dessiner hors ligne les courbes de taille-pour-âge, poids-pour-âge, IMC-pour-âge, périmètre crânien-pour-âge et périmètre brachial-pour-âge. Les données sources Excel sont les tables étendues officielles OMS conservées dans ce dossier. `build_reference.py` les convertit en JSON compact pour l’API DME.

La sélection est sexospécifique. Les limites suivent les références publiées : taille 0–19 ans (normes 0–5 ans et référence 5–19 ans), poids 0–10 ans, IMC 0–19 ans, périmètre crânien 0–5 ans et périmètre brachial 3 mois–5 ans. L’OMS publie des références distinctes selon l’âge; aucune extrapolation n’est faite en dehors de ces limites. Le logiciel n’affiche pas de courbe tant que le sexe n’est pas renseigné.

Sources OMS :

- [Normes de croissance de l’enfant (0–5 ans)](https://www.who.int/tools/child-growth-standards/standards)
- [Taille pour l’âge (5–19 ans)](https://www.who.int/tools/growth-reference-data-for-5to19-years/indicators/height-for-age)
- [IMC pour l’âge (5–19 ans)](https://www.who.int/tools/growth-reference-data-for-5to19-years/indicators/bmi-for-age)
- [Poids pour l’âge (0–5 ans)](https://www.who.int/tools/child-growth-standards/standards/weight-for-age)
- [Périmètre crânien pour l’âge (0–5 ans)](https://www.who.int/tools/child-growth-standards/standards/head-circumference-for-age)
- [Périmètre brachial pour l’âge (3 mois–5 ans)](https://www.who.int/tools/child-growth-standards/standards/arm-circumference-for-age)
- [IMC pour l’âge (0–5 ans)](https://www.who.int/toolkits/child-growth-standards/standards/body-mass-index-for-age-bmi-for-age)
- [Poids pour l’âge (5–10 ans)](https://www.who.int/tools/growth-reference-data-for-5to19-years/indicators/weight-for-age-5to10-years)

Les références graphiques sont des aides de suivi clinique. L’interprétation appartient au professionnel de santé.
