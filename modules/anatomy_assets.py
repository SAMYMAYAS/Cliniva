"""Image anatomique fournie par l'utilisateur, commune aux consultations et PDF.

Le JPEG est conservé intact ; seuls les cadres d'affichage isolent chaque vue.
Les coordonnées restent dans le repère SVG historique 180 x 260.
"""
from pathlib import Path

IMAGE_PATH = Path(__file__).resolve().parent.parent / 'static' / 'images' / 'anatomy' / 'muscles-reference.jpg'
IMAGE_URL = '/static/images/anatomy/muscles-reference.jpg'
SCALE = .34
IMAGE_WIDTH, IMAGE_HEIGHT = 340, 253.3
VIEWS = {
    'Face': {'offset': 15.54, 'left': 38, 'width': 104},
    'Dos': {'offset': -89.52, 'left': 38, 'width': 104},
    'Profil': {'offset': -177.3, 'left': 65, 'width': 60},
}


def anatomical_image_svg(view, namespace):
    """Affiche le JPEG original dans un cadre, sans modifier ses pixels."""
    spec = VIEWS[view]
    clip_id = f'{namespace}-{view.lower()}'
    return (
        f'<defs><clipPath id="{clip_id}"><rect x="{spec["left"]}" y="0" '
        f'width="{spec["width"]}" height="{IMAGE_HEIGHT}"/></clipPath></defs>'
        f'<image href="{IMAGE_URL}" x="{spec["offset"]}" y="0" '
        f'width="{IMAGE_WIDTH}" height="{IMAGE_HEIGHT}" '
        f'preserveAspectRatio="none" clip-path="url(#{clip_id})"/>'
    )


def body_map_pdf():
    from reportlab.platypus import Flowable
    from reportlab.lib.utils import ImageReader

    class AnatomicalViews(Flowable):
        def __init__(self):
            super().__init__()
            self.width = 490
            self.height = 180

        def draw(self):
            canvas = self.canv
            image = ImageReader(str(IMAGE_PATH))
            for index, view in enumerate(('Face', 'Dos', 'Profil')):
                center = 85 + index * 160
                canvas.setFont('Helvetica-Bold', 9)
                canvas.setFillColorRGB(.067, .369, .349)
                canvas.drawCentredString(center, 171, view)
                spec = VIEWS[view]
                base_x, base_y, scale = center - 54, 162, .60
                canvas.saveState()
                clip = canvas.beginPath()
                clip.rect(base_x + spec['left'] * scale, base_y - IMAGE_HEIGHT * scale,
                          spec['width'] * scale, IMAGE_HEIGHT * scale)
                canvas.clipPath(clip, stroke=0, fill=0)
                canvas.drawImage(image, base_x + spec['offset'] * scale,
                                 base_y - IMAGE_HEIGHT * scale,
                                 width=IMAGE_WIDTH * scale, height=IMAGE_HEIGHT * scale)
                canvas.restoreState()
            canvas.setFont('Helvetica', 8)
            canvas.setFillColorRGB(.39, .45, .55)
            canvas.drawCentredString(245, 0, 'Entourer les zones concernées et préciser le côté du patient.')

    return AnatomicalViews()
