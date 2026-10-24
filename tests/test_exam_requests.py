"""Validation, isolation et autorisations des demandes, sans base patient réelle."""
import ast
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import unittest
from modules.exam_requests_core import clean_catalog, clean_request, SCHEMA
from modules.exam_request_print import render_request_print
from modules.dme_display import display_permissions


class HttpError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class Cursor:
    def __init__(self):
        self.passage = {'id_patient':11,'statut':'En cours'}
        self.consultation = {'id':33,'verrouille':False}
        self.latest_id = 33
        self.catalog_ids = [1]
        self.calls = []
        self.query = ''

    def execute(self, query, params=()):
        self.query = query
        self.calls.append((query,params))

    def fetchone(self):
        if 'SELECT id_patient' in self.query:
            return self.passage
        if 'SELECT id,verrouille' in self.query:
            return self.consultation
        if 'SELECT MAX' in self.query:
            return {'id':self.latest_id}
        return {'id':99}

    def fetchall(self):
        return [{'id':value} for value in self.catalog_ids]


class ExamRequestTests(unittest.TestCase):
    def setUp(self):
        self.cursor = Cursor()
        self.user = {'id':7,'role':'Médecin','prenom':'Test','nom':'Praticien','permissions':{'dme_view':True,'dme_demandes':True}}
        @contextmanager
        def cursor_context():
            yield self.cursor
        self.env = {'json':json,'Request':object,'HTTPException':HttpError,
                    'display_permissions':display_permissions,'get_current_user_optional':lambda request:self.user,
                    'get_db_cursor':cursor_context,'track_action':lambda *args:None,
                    'SCHEMA':SCHEMA,'clean_catalog':clean_catalog,'clean_request':clean_request}
        from modules.exam_requests_core import kind_value,positive_id
        self.env.update(kind_value=kind_value,positive_id=positive_id)
        source=Path(__file__).resolve().parents[1]/'modules/gestion_demandes_bilans.py'
        tree=ast.parse(source.read_text(encoding='utf-8'))
        functions=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))]
        for node in functions:
            node.decorator_list=[]
        exec(compile(ast.Module(body=functions,type_ignores=[]),str(source),'exec'),self.env)
        self.payload={'id_patient':11,'id_passage':22,'id_consultation':33,'kind':'biologique',
                      'panels':[{'catalog_id':1,'name':'Bilan personnalisé','items':[{'label':'Analyse A','details':'Précision A'}]}],
                      'indication':'Renseignement de démonstration','notes':'Consigne'}

    def test_biology_and_radiology_accept_custom_components(self):
        for kind in ('biologique','radiologique'):
            payload={**self.payload,'kind':kind}
            self.assertEqual(clean_request(payload)['panels'][0]['items'][0]['details'],'Précision A')

    def test_invalid_kinds_empty_selection_and_identifiers_are_rejected(self):
        for payload in ({**self.payload,'kind':'autre'},{**self.payload,'panels':[]},
                        {**self.payload,'panels':[{'catalog_id':True,'name':'A','items':[{'label':'B'}]}]}):
            with self.assertRaises(ValueError):clean_request(payload)

    def test_catalog_limits_and_request_limits(self):
        with self.assertRaises(ValueError):clean_catalog({'kind':'biologique','name':'X','items':[{'label':'X'}]*61})
        with self.assertRaises(ValueError):clean_request({**self.payload,'notes':'X'*4001})
        with self.assertRaises(ValueError):clean_request({**self.payload,'panels':[{'name':'X','items':[{'label':'X'}]*60}]*6})

    def test_read_only_permission_cannot_write_catalog_or_request(self):
        self.user['permissions']={'dme_view':True}
        for name,payload in [('save_request',self.payload),('save_catalog',{'kind':'biologique','name':'X','items':[{'label':'X'}]})]:
            with self.assertRaises(HttpError) as error:asyncio.run(self.env[name](payload,object()))
            self.assertEqual(error.exception.status_code,403)
        self.assertEqual(self.cursor.calls,[])

    def test_session_and_inactive_user_rejected(self):
        self.user=None
        with self.assertRaises(HttpError) as error:self.env['authorized_user'](object())
        self.assertEqual(error.exception.status_code,401)
        self.user={'role':'Administrateur','status':'Inactif'}
        with self.assertRaises(HttpError) as error:self.env['authorized_user'](object())
        self.assertEqual(error.exception.status_code,403)

    def test_wrong_patient_locked_and_stale_consultation_rejected(self):
        for scenario in ('patient','locked','stale','finished'):
            self.cursor=Cursor()
            if scenario=='patient':self.cursor.passage['id_patient']=12
            if scenario=='locked':self.cursor.consultation['verrouille']=True
            if scenario=='stale':self.cursor.latest_id=34
            if scenario=='finished':self.cursor.passage['statut']='Terminé'
            with self.assertRaises(HttpError):asyncio.run(self.env['save_request'](self.payload,object()))
            self.assertFalse(any('INSERT INTO patient_exam_requests' in query for query,_ in self.cursor.calls))

    def test_inactive_or_other_kind_catalog_is_rejected(self):
        self.cursor.catalog_ids=[]
        with self.assertRaises(HttpError) as error:asyncio.run(self.env['save_request'](self.payload,object()))
        self.assertEqual(error.exception.status_code,409)

    def test_saved_snapshot_is_patient_scoped_and_independent_of_catalog(self):
        payload=deepcopy(self.payload)
        result=asyncio.run(self.env['save_request'](payload,object()))
        self.assertEqual(result['id'],99)
        query,params=next((q,p) for q,p in self.cursor.calls if 'INSERT INTO patient_exam_requests' in q)
        self.assertEqual(params[:4],(11,22,33,'biologique'))
        payload['panels'][0]['items'][0]['label']='Catalogue modifié'
        self.assertEqual(json.loads(params[4])[0]['items'][0]['label'],'Analyse A')

    def test_print_escapes_untrusted_text_and_keeps_paginated_layout(self):
        record={'id':1,'kind':'radiologique','nom':'<script>alert(1)</script>','prescriber':'A & B',
                'panels_json':[{'name':'X < Y','items':[{'label':'<img src=x>','details':'A & B'}]}],'status':'Annulée'}
        html=render_request_print(record)
        self.assertNotIn('<script>alert(1)</script>',html)
        self.assertIn('&lt;img src=x&gt;',html)
        self.assertIn('ANNULÉE',html)
        self.assertIn('top:18.2%',html)
        self.assertIn('function paginate()',html)


if __name__=='__main__':unittest.main()
