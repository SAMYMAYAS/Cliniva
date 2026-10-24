"""Réinitialisation limitée à la consultation active, avec instantané transactionnel."""
from fastapi import HTTPException
import json
from modules.dme_display import display_permissions

CLINICAL_FIELDS = (
    'allergies_intolerances', 'traitement_en_cours', 'antecedents_medicaux',
    'antecedents_chirurgicaux', 'notes_habitudes', 'motif', 'examen_clinique',
    'diagnostic', 'conduite_a_tenir', 'remarques', 'ordonnance_text',
)


def user_can_reset_dme(user):
    return bool(user and user.get('status', 'Actif') == 'Actif' and (user.get('role') == 'Administrateur' or
                         display_permissions(user).get('dme_reset') is True))


def reset_active_consultation(cursor, user, patient_id, passage_id, consultation_id):
    if not user:
        raise HTTPException(status_code=401, detail='Session expirée.')
    if not user_can_reset_dme(user):
        raise HTTPException(status_code=403, detail='Vous ne pouvez pas réinitialiser les consultations DME.')
    identifiers = (patient_id, passage_id, consultation_id)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in identifiers):
        raise HTTPException(status_code=400, detail='Patient, passage et consultation obligatoires.')
    # Même ordre de verrouillage que l'enregistrement de la consultation.
    cursor.execute('SELECT id_patient, statut FROM patient_passages WHERE id=%s FOR UPDATE;', (passage_id,))
    passage = cursor.fetchone()
    if not passage or int(passage['id_patient']) != patient_id:
        raise HTTPException(status_code=404, detail='Passage introuvable pour ce patient.')
    if passage.get('statut') != 'En cours':
        raise HTTPException(status_code=409, detail='Seule une consultation en cours peut être réinitialisée.')
    cursor.execute('''
        SELECT id, verrouille FROM patient_dme_records
        WHERE id_patient=%s AND id_passage=%s
        ORDER BY id DESC LIMIT 1 FOR UPDATE;
    ''', (patient_id, passage_id))
    record = cursor.fetchone()
    if not record or int(record['id']) != consultation_id:
        raise HTTPException(status_code=409, detail='Cette consultation ne correspond plus à la consultation active.')
    if record.get('verrouille', True):
        raise HTTPException(status_code=409, detail='Une consultation enregistrée en lecture seule ne peut pas être réinitialisée.')
    # Les documents sans id_consultation sont limités à ceux créés depuis le début
    # de cette consultation. Les anciens passages et consultations sont conservés.
    since_start = 'created_at >= (SELECT date_consultation FROM patient_dme_records WHERE id=%s)'
    scoped_tables = (
        ('patient_prescriptions', '(id_consultation=%s OR (id_consultation IS NULL AND ' + since_start + '))', (consultation_id, consultation_id)),
        ('patient_consumptions', 'id_consultation=%s', (consultation_id,)),
        ('patient_exam_requests', 'id_consultation=%s', (consultation_id,)),
        ('patient_cr_records', '(id_consultation=%s OR (id_consultation IS NULL AND ' + since_start + '))', (consultation_id, consultation_id)),
        ('patient_echographies', since_start, (consultation_id,)),
        ('patient_livret_drafts', since_start, (consultation_id,)),
    )
    related = {}
    for table, scope, scope_params in scoped_tables:
        # DELETE RETURNING sauvegarde exactement les lignes retirées, même en cas
        # d'écriture concurrente. Les noms de tables proviennent de cette liste fixe.
        cursor.execute(f'''
            WITH removed AS (
                DELETE FROM {table} WHERE id_patient=%s AND id_passage=%s AND {scope}
                RETURNING *
            ) SELECT COALESCE(jsonb_agg(to_jsonb(removed)), '[]'::jsonb) AS rows FROM removed;
        ''', (patient_id, passage_id, *scope_params))
        rows = cursor.fetchone()['rows']
        related[table] = json.loads(rows) if isinstance(rows, str) else rows
    # L'instantané et le reset sont atomiques. Les fichiers physiques sont gardés.
    cursor.execute('''
        UPDATE patient_dme_records AS d SET
            reset_history_json = COALESCE(d.reset_history_json, '[]'::jsonb) ||
                jsonb_build_array(jsonb_build_object(
                    'date', CURRENT_TIMESTAMP, 'user_id', %s,
                    'snapshot', to_jsonb(d) - 'reset_history_json', 'related', %s::jsonb)),
            constantes_json='{}'::jsonb, specialite_consultation_json='{}'::jsonb,
            allergies_intolerances='', traitement_en_cours='', antecedents_medicaux='',
            antecedents_chirurgicaux='', notes_habitudes='', motif='', examen_clinique='',
            diagnostic='', conduite_a_tenir='', remarques='', ordonnance_text=''
        WHERE d.id=%s AND d.id_patient=%s AND d.id_passage=%s AND d.verrouille=FALSE;
    ''', (user['id'], json.dumps(related, ensure_ascii=False), consultation_id, patient_id, passage_id))
    if cursor.rowcount != 1:
        raise HTTPException(status_code=409, detail='La consultation a changé. Rechargez le dossier.')
    return {'id': consultation_id, 'verrouille': False, 'constantes': {}, 'constantes_json': {},
            'donnees_specialisees': {}, 'specialite_consultation_json': {},
            **{field: '' for field in CLINICAL_FIELDS}}
