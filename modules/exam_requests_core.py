"""Schéma et validation des catalogues et demandes de bilans."""
KINDS = {'biologique': 'Bilan biologique', 'radiologique': 'Bilan radiologique'}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS exam_request_catalog (
    id SERIAL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('biologique','radiologique')),
    name TEXT NOT NULL,
    items_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by INT REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_exam_catalog_name ON exam_request_catalog (kind, LOWER(name));
CREATE TABLE IF NOT EXISTS patient_exam_requests (
    id SERIAL PRIMARY KEY,
    id_patient INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    id_passage INT NOT NULL REFERENCES patient_passages(id) ON DELETE CASCADE,
    id_consultation INT NOT NULL REFERENCES patient_dme_records(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('biologique','radiologique')),
    panels_json JSONB NOT NULL,
    indication TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    prescriber TEXT NOT NULL,
    prescriber_role TEXT NOT NULL DEFAULT '',
    created_by INT REFERENCES users(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'Valide' CHECK (status IN ('Valide','Annulée')),
    cancelled_by INT REFERENCES users(id) ON DELETE SET NULL,
    cancelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_exam_requests_scope ON patient_exam_requests (id_patient,id_passage,id_consultation,kind);
'''


def text_value(value, label, maximum, required=False, multiline=False):
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ValueError(f'{label} : texte attendu.')
    value = value.strip() if multiline else ' '.join(value.split())
    if required and not value:
        raise ValueError(f'{label} obligatoire.')
    if len(value) > maximum:
        raise ValueError(f'{label} : {maximum} caractères maximum.')
    return value


def kind_value(kind):
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError('Choisir un bilan biologique ou radiologique.')
    return kind


def positive_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('Identifiant invalide.')
    return value


def clean_items(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 60:
        raise ValueError('Un bilan doit contenir de 1 à 60 paramètres ou examens.')
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Paramètre ou examen invalide.')
        cleaned.append({'label': text_value(item.get('label'), 'Paramètre / examen', 200, True),
                        'details': text_value(item.get('details'), 'Précisions', 500)})
    return cleaned


def clean_catalog(payload):
    if not isinstance(payload, dict):
        raise ValueError('Bilan invalide.')
    return {'kind': kind_value(payload.get('kind')),
            'name': text_value(payload.get('name'), 'Nom du bilan', 160, True),
            'items': clean_items(payload.get('items'))}


def clean_request(payload):
    if not isinstance(payload, dict):
        raise ValueError('Demande invalide.')
    panels = payload.get('panels')
    if not isinstance(panels, list) or not 1 <= len(panels) <= 30:
        raise ValueError('Sélectionner de 1 à 30 bilans.')
    cleaned = []
    for panel in panels:
        if not isinstance(panel, dict):
            raise ValueError('Bilan invalide.')
        catalog_id = panel.get('catalog_id')
        cleaned.append({'catalog_id': positive_id(catalog_id) if catalog_id is not None else None,
                        'name': text_value(panel.get('name'), 'Nom du bilan', 160, True),
                        'items': clean_items(panel.get('items'))})
    if sum(len(panel['items']) for panel in cleaned) > 300:
        raise ValueError('Une demande peut contenir au maximum 300 paramètres ou examens.')
    return {'kind': kind_value(payload.get('kind')), 'panels': cleaned,
            'indication': text_value(payload.get('indication'), 'Renseignements cliniques', 4000, multiline=True),
            'notes': text_value(payload.get('notes'), 'Consignes', 4000, multiline=True)}
