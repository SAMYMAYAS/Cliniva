"""Serveur de vérification : données de démonstration en mémoire, aucune base réelle."""
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
import json
from modules.exam_requests_core import clean_catalog,clean_request
from modules.exam_request_print import render_request_print
catalog=[]
requests=[]
class Handler(SimpleHTTPRequestHandler):
    def output(self,value,status=200,html=False):
        data=value.encode('utf-8') if html else json.dumps(value,ensure_ascii=False).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type','text/html; charset=utf-8' if html else 'application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        route=urlparse(self.path)
        if route.path=='/api/dme/exam-requests/catalog':
            kind=parse_qs(route.query)['kind'][0]
            return self.output({'items':[item for item in catalog if item['kind']==kind],'can_manage':True})
        if route.path.startswith('/api/dme/exam-requests/scope/'):
            return self.output({'items':requests,'locked':False,'can_manage':True})
        if route.path.startswith('/api/dme/exam-requests/print/'):
            item=next(item for item in requests if item['id']==int(route.path.split('/')[-1]))
            return self.output(render_request_print({**item,'nom':'PATIENT','prenom':'DÉMONSTRATION','birth_date':'01/01/1980','request_date':'15/10/2026','prescriber_role':'Médecin'}),html=True)
        return super().do_GET()
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        try:
            if self.path.endswith('/catalog/save'):
                cleaned=clean_catalog(payload)
                item={'id':payload.get('id') or len(catalog)+1,**cleaned,'items_json':cleaned['items'],'active':True}
                catalog[:]=[row for row in catalog if row['id']!=item['id']]+[item]
                return self.output({'success':True,'id':item['id']})
            if '/catalog/' in self.path and self.path.endswith('/toggle'):
                item=next(row for row in catalog if row['id']==int(self.path.split('/')[-2]));item['active']=payload['active']
                return self.output({'success':True})
            if self.path.endswith('/save'):
                cleaned=clean_request(payload)
                item={'id':len(requests)+1,**cleaned,'panels_json':cleaned['panels'],'prescriber':'Praticien de démonstration','created':'15/10/2026 10:00','status':'Valide'}
                requests.append(item)
                return self.output({'success':True,'id':item['id']})
            if self.path.endswith('/cancel'):
                item=next(row for row in requests if row['id']==int(self.path.split('/')[-2]));item['status']='Annulée'
                return self.output({'success':True})
            self.output({'detail':'Route inconnue'},404)
        except ValueError as error:self.output({'detail':str(error)},400)
ThreadingHTTPServer(('127.0.0.1',8766),Handler).serve_forever()
