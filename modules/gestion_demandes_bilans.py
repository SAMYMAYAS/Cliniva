"""Catalogues partagés et demandes de bilans liées à la consultation DME."""
import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from app.auth import get_current_user_optional, track_action
from database import get_db_cursor
from modules.dme_display import display_permissions
from modules.exam_requests_core import SCHEMA, clean_catalog, clean_request, kind_value, positive_id

exam_requests_router = APIRouter(prefix='/api/dme/exam-requests', tags=['Demandes de bilans'])


def authorized_user(request, write=False):
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(401, 'Session expirée.')
    permissions = display_permissions(user)
    allowed = permissions.get('dme_demandes') if write else any(permissions.get(key) for key in ('dme_view','dme_consult','dme_demandes'))
    if user.get('status', 'Actif') != 'Actif' or (user.get('role') != 'Administrateur' and not allowed):
        raise HTTPException(403, 'Autorisation de demandes de bilans manquante.')
    return user


def validate(function, *args):
    try:
        return function(*args)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def ensure_schema():
    with get_db_cursor() as cursor:
        cursor.execute(SCHEMA)


def check_scope(cursor, patient_id, passage_id, consultation_id, write=False):
    for value in (patient_id, passage_id, consultation_id):
        validate(positive_id, value)
    cursor.execute('SELECT id_patient,statut FROM patient_passages WHERE id=%s' + (' FOR UPDATE;' if write else ';'), (passage_id,))
    passage = cursor.fetchone()
    if not passage or int(passage['id_patient']) != patient_id:
        raise HTTPException(404, 'Passage introuvable pour ce patient.')
    cursor.execute('''SELECT id,verrouille FROM patient_dme_records
                      WHERE id=%s AND id_patient=%s AND id_passage=%s''' + (' FOR UPDATE;' if write else ';'),
                   (consultation_id, patient_id, passage_id))
    consultation = cursor.fetchone()
    if not consultation:
        raise HTTPException(404, 'Consultation introuvable.')
    if write:
        if consultation.get('verrouille', True) or passage.get('statut') != 'En cours':
            raise HTTPException(409, 'La consultation doit être en cours et modifiable.')
        cursor.execute('SELECT MAX(id) AS id FROM patient_dme_records WHERE id_patient=%s AND id_passage=%s;', (patient_id,passage_id))
        if cursor.fetchone()['id'] != consultation_id:
            raise HTTPException(409, 'Cette consultation n’est plus la consultation active.')
    return consultation


@exam_requests_router.get('/catalog')
async def list_catalog(request: Request, kind: str):
    user = authorized_user(request)
    validate(kind_value, kind)
    ensure_schema()
    with get_db_cursor() as cursor:
        cursor.execute('SELECT id,kind,name,items_json,active FROM exam_request_catalog WHERE kind=%s ORDER BY LOWER(name),id;', (kind,))
        items = [dict(row) for row in cursor.fetchall()]
    can_manage = user.get('role') == 'Administrateur' or display_permissions(user).get('dme_demandes') is True
    return {'items': items, 'can_manage': can_manage}


@exam_requests_router.post('/catalog/save')
async def save_catalog(payload: dict, request: Request):
    user = authorized_user(request, True)
    cleaned = validate(clean_catalog, payload)
    catalog_id = payload.get('id')
    if catalog_id is not None:
        validate(positive_id, catalog_id)
    ensure_schema()
    try:
        with get_db_cursor() as cursor:
            if catalog_id is None:
                cursor.execute('''INSERT INTO exam_request_catalog(kind,name,items_json,created_by)
                                  VALUES (%s,%s,%s::jsonb,%s) RETURNING id;''',
                               (cleaned['kind'],cleaned['name'],json.dumps(cleaned['items'],ensure_ascii=False),user['id']))
            else:
                cursor.execute('''UPDATE exam_request_catalog SET name=%s,items_json=%s::jsonb,updated_at=CURRENT_TIMESTAMP
                                  WHERE id=%s AND kind=%s RETURNING id;''',
                               (cleaned['name'],json.dumps(cleaned['items'],ensure_ascii=False),catalog_id,cleaned['kind']))
            result = cursor.fetchone()
            if not result:
                raise HTTPException(404, 'Bilan introuvable dans cette catégorie.')
            catalog_id = result['id']
    except Exception as exc:
        if getattr(exc, 'pgcode', None) == '23505':
            raise HTTPException(409, 'Un bilan de ce nom existe déjà dans cette catégorie.') from exc
        raise
    track_action(request, 'CATALOGUE_BILANS', f"Bilan {cleaned['kind']} #{catalog_id} enregistré")
    return {'success': True, 'id': catalog_id}


@exam_requests_router.post('/catalog/{catalog_id}/toggle')
async def toggle_catalog(catalog_id: int, payload: dict, request: Request):
    authorized_user(request, True)
    active = payload.get('active')
    if not isinstance(active, bool):
        raise HTTPException(400, 'État du bilan invalide.')
    ensure_schema()
    with get_db_cursor() as cursor:
        cursor.execute('UPDATE exam_request_catalog SET active=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING id;', (active,catalog_id))
        if not cursor.fetchone():
            raise HTTPException(404, 'Bilan introuvable.')
    track_action(request, 'CATALOGUE_BILANS', f'Bilan #{catalog_id} : actif={active}')
    return {'success': True}


@exam_requests_router.get('/scope/{patient_id}/{passage_id}/{consultation_id}')
async def list_requests(patient_id: int, passage_id: int, consultation_id: int, request: Request):
    user = authorized_user(request)
    ensure_schema()
    with get_db_cursor() as cursor:
        consultation = check_scope(cursor,patient_id,passage_id,consultation_id)
        cursor.execute('''SELECT id,kind,panels_json,indication,notes,prescriber,status,
                          TO_CHAR(created_at,'DD/MM/YYYY HH24:MI') AS created
                          FROM patient_exam_requests WHERE id_patient=%s AND id_passage=%s AND id_consultation=%s
                          ORDER BY created_at DESC,id DESC;''', (patient_id,passage_id,consultation_id))
        items = [dict(row) for row in cursor.fetchall()]
    return {'items': items, 'locked': consultation.get('verrouille',True),
            'can_manage': user.get('role') == 'Administrateur' or display_permissions(user).get('dme_demandes') is True}


@exam_requests_router.post('/save')
async def save_request(payload: dict, request: Request):
    user = authorized_user(request, True)
    cleaned = validate(clean_request, payload)
    scope = tuple(payload.get(key) for key in ('id_patient','id_passage','id_consultation'))
    ensure_schema()
    with get_db_cursor() as cursor:
        check_scope(cursor,*scope,write=True)
        ids = list({panel['catalog_id'] for panel in cleaned['panels'] if panel['catalog_id'] is not None})
        if ids:
            cursor.execute('SELECT id FROM exam_request_catalog WHERE id=ANY(%s) AND kind=%s AND active=TRUE FOR SHARE;', (ids,cleaned['kind']))
            if {row['id'] for row in cursor.fetchall()} != set(ids):
                raise HTTPException(409, 'Un bilan du catalogue est désactivé ou ne correspond pas à cette catégorie. Rechargez la liste.')
        prescriber = ' '.join(str(user.get(key) or '') for key in ('prenom','nom')).strip() or user.get('login','Praticien')
        cursor.execute('''INSERT INTO patient_exam_requests(id_patient,id_passage,id_consultation,kind,panels_json,indication,notes,prescriber,prescriber_role,created_by)
                          VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s) RETURNING id;''',
                       (*scope,cleaned['kind'],json.dumps(cleaned['panels'],ensure_ascii=False),cleaned['indication'],cleaned['notes'],prescriber,user.get('role',''),user['id']))
        request_id = cursor.fetchone()['id']
    track_action(request, 'DEMANDE_BILAN', f"Demande {cleaned['kind']} #{request_id}, consultation #{scope[2]}")
    return {'success': True, 'id': request_id}


@exam_requests_router.post('/{request_id}/cancel')
async def cancel_request(request_id: int, payload: dict, request: Request):
    user = authorized_user(request, True)
    scope = tuple(payload.get(key) for key in ('id_patient','id_passage','id_consultation'))
    ensure_schema()
    with get_db_cursor() as cursor:
        check_scope(cursor,*scope,write=True)
        cursor.execute('''UPDATE patient_exam_requests SET status='Annulée',cancelled_by=%s,cancelled_at=CURRENT_TIMESTAMP
                          WHERE id=%s AND id_patient=%s AND id_passage=%s AND id_consultation=%s AND status='Valide' RETURNING id;''',
                       (user['id'],request_id,*scope))
        if not cursor.fetchone():
            raise HTTPException(404, 'Demande introuvable ou déjà annulée.')
    track_action(request, 'ANNULATION_BILAN', f'Demande de bilan #{request_id} annulée')
    return {'success': True}


@exam_requests_router.get('/print/{patient_id}/{passage_id}/{consultation_id}/{request_id}', response_class=HTMLResponse)
async def print_request(patient_id: int, passage_id: int, consultation_id: int, request_id: int, request: Request):
    authorized_user(request)
    ensure_schema()
    with get_db_cursor() as cursor:
        check_scope(cursor,patient_id,passage_id,consultation_id)
        cursor.execute('''SELECT r.*,p.nom,p.prenom,TO_CHAR(p.date_naissance,'DD/MM/YYYY') AS birth_date,
                          TO_CHAR(r.created_at,'DD/MM/YYYY') AS request_date
                          FROM patient_exam_requests r JOIN patients p ON p.id=r.id_patient
                          WHERE r.id=%s AND r.id_patient=%s AND r.id_passage=%s AND r.id_consultation=%s;''',
                       (request_id,patient_id,passage_id,consultation_id))
        record = cursor.fetchone()
    if not record:
        raise HTTPException(404, 'Demande introuvable.')
    from modules.exam_request_print import render_request_print
    return HTMLResponse(render_request_print(dict(record)), headers={'Cache-Control':'no-store'})
