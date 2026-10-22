"""Vue dédiée à la salle d'attente de radiologie."""
from modules.gestion_passages import render_html as render_passages_html


def render_html() -> str:
    """Réutilise le tableau et le formulaire de l'accueil en mode radiologie."""
    return render_passages_html(mode="radiologie")
