"""Tests sans base réelle des limites d'autorisation et de consultation du reset."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


class HttpError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


spec = importlib.util.spec_from_file_location(
    'reset_under_test', Path(__file__).resolve().parents[1] / 'modules' / 'dme_reset.py')
reset_module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'fastapi': types.SimpleNamespace(HTTPException=HttpError)}):
    spec.loader.exec_module(reset_module)


class Cursor:
    def __init__(self, passage=None, record=None, affected=1):
        self.rows = [passage, record]
        self.queries = []
        self.rowcount = affected

    def execute(self, query, params):
        self.queries.append((query, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else {'rows': [{'id': 101}]}


class DmeResetTests(unittest.TestCase):
    def setUp(self):
        self.user = {'id': 7, 'role': 'Médecin', 'permissions': {'dme_reset': True}}
        self.passage = {'id_patient': 11, 'statut': 'En cours'}
        self.record = {'id': 33, 'verrouille': False}

    def reset(self, cursor, user=None, identifiers=(11, 22, 33)):
        return reset_module.reset_active_consultation(cursor, user or self.user, *identifiers)

    def assert_denied(self, cursor, code, **kwargs):
        with self.assertRaises(HttpError) as error:
            self.reset(cursor, **kwargs)
        self.assertEqual(error.exception.status_code, code)
        self.assertFalse(any(query.lstrip().startswith('UPDATE') or 'DELETE FROM' in query for query, _ in cursor.queries))

    def test_permission_absent_or_false_rejects_without_queries(self):
        for permissions in ({}, {'dme_reset': False}):
            cursor = Cursor()
            self.assert_denied(cursor, 403, user={**self.user, 'permissions': permissions})
            self.assertEqual(cursor.queries, [])

    def test_missing_session_is_rejected(self):
        cursor = Cursor()
        with self.assertRaises(HttpError) as error:
            reset_module.reset_active_consultation(cursor, None, 11, 22, 33)
        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(cursor.queries, [])

    def test_admin_and_json_permissions(self):
        self.assertTrue(reset_module.user_can_reset_dme({'role': 'Administrateur'}))
        self.assertTrue(reset_module.user_can_reset_dme({**self.user, 'permissions': '{"dme_reset":true}'}))
        self.assertFalse(reset_module.user_can_reset_dme({**self.user, 'status': 'Inactif'}))

    def test_wrong_patient_is_rejected(self):
        self.assert_denied(Cursor({'id_patient': 99, 'statut': 'En cours'}, self.record), 404)

    def test_finished_passage_is_rejected(self):
        self.assert_denied(Cursor({**self.passage, 'statut': 'Terminé'}, self.record), 409)

    def test_locked_consultation_is_rejected(self):
        self.assert_denied(Cursor(self.passage, {**self.record, 'verrouille': True}), 409)

    def test_stale_consultation_is_rejected(self):
        self.assert_denied(Cursor(self.passage, {'id': 34, 'verrouille': False}), 409)

    def test_invalid_identifiers_are_rejected_before_sql(self):
        for identifiers in ((True, 22, 33), (11, 0, 33), (11, 22, None), ('11', 22, 33)):
            cursor = Cursor()
            self.assert_denied(cursor, 400, identifiers=identifiers)
            self.assertEqual(cursor.queries, [])

    def test_full_reset_is_scoped_and_all_removed_records_are_snapshotted(self):
        cursor = Cursor(self.passage, self.record)
        result = self.reset(cursor)
        self.assertEqual(result['id'], 33)
        self.assertFalse(result['verrouille'])
        self.assertEqual(result['constantes'], {})
        self.assertEqual(result['donnees_specialisees'], {})
        self.assertTrue(all(result[key] == '' for key in reset_module.CLINICAL_FIELDS))
        writes = [(query, params) for query, params in cursor.queries if query.lstrip().startswith('UPDATE')]
        self.assertEqual(len(writes), 1)
        query, params = writes[0]
        self.assertIn('UPDATE patient_dme_records', query)
        self.assertIn("to_jsonb(d) - 'reset_history_json'", query)
        self.assertIn('d.verrouille=FALSE', query)
        self.assertEqual(params[0], 7)
        self.assertEqual(params[2:], (33, 11, 22))
        import json
        related = json.loads(params[1])
        self.assertEqual(set(related), {'patient_prescriptions', 'patient_consumptions', 'patient_exam_requests', 'patient_cr_records', 'patient_echographies', 'patient_livret_drafts'})
        self.assertTrue(all(rows == [{'id': 101}] for rows in related.values()))
        deletes = [(query, params) for query, params in cursor.queries if 'DELETE FROM' in query]
        self.assertEqual(len(deletes), 6)
        for query, params in deletes:
            self.assertIn('id_patient=%s AND id_passage=%s', query)
            self.assertIn('RETURNING *', query)
            self.assertEqual(params[:2], (11, 22))
        image_query = next(query for query, _ in deletes if 'DELETE FROM patient_echographies' in query)
        self.assertIn('created_at >= (SELECT date_consultation', image_query)

    def test_concurrent_change_returns_conflict(self):
        with self.assertRaises(HttpError) as error:
            self.reset(Cursor(self.passage, self.record, affected=0))
        self.assertEqual(error.exception.status_code, 409)


if __name__ == '__main__':
    unittest.main()
