"""Impression A4 des demandes, sur le même fond et aux mêmes positions que l'ordonnance."""
import base64
from html import escape
import json
from pathlib import Path
import sys
import textwrap


def render_request_print(record):
    title = {'biologique': 'Demande d’analyse médicale',
             'radiologique': 'Demande de radio'}[record['kind']]
    panels = record.get('panels_json') or []
    if isinstance(panels, str):
        panels = json.loads(panels)
    units = []
    def note(label, value):
        if value:
            units.append('<div class="print-unit section">'+escape(label)+'</div>')
            for line in str(value).splitlines():
                for piece in textwrap.wrap(line, 160):
                    units.append('<div class="print-unit note">'+escape(piece)+'</div>')
    note('Renseignements cliniques', record.get('indication'))
    for index,panel in enumerate(panels,1):
        units.append(f'<div class="print-unit section">{index}. {escape(panel["name"])}</div>')
        for item in panel['items']:
            detail = ('<span class="detail"> — '+escape(item['details'])+'</span>') if item.get('details') else ''
            units.append('<div class="print-unit exam">• '+escape(item['label'])+detail+'</div>')
    note('Consignes / précisions', record.get('notes'))
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    model = root / 'uploads' / 'model_templates' / 'ORD.png'
    background = ''
    if model.is_file():
        background = '<img class="bg-template" alt="" src="data:image/png;base64,'+base64.b64encode(model.read_bytes()).decode('ascii')+'">'
    cancelled = '<div class="watermark">ANNULÉE</div>' if record.get('status') == 'Annulée' else ''
    patient = escape(' '.join(str(record.get(key) or '') for key in ('nom','prenom')).upper())
    header = f'''<div class="patient"><div class="identity"><span>Patient : <strong>{patient}</strong></span><b class="reference">#BIL - {int(record['id']):04d}</b></div>
        <div class="identity small"><span>Date de naissance : <strong>{escape(str(record.get('birth_date') or '-'))}</strong></span><span>Date : <strong>{escape(str(record.get('request_date') or ''))}</strong></span></div></div>'''
    footer = f'''<div class="signature"><div>Le Médecin Prescripteur</div><strong>{escape(str(record.get('prescriber') or 'Praticien'))} ({escape(str(record.get('prescriber_role') or 'Médecine'))})</strong><small>(Signature &amp; Griffe)</small></div><span class="page-number"></span>'''
    html = '''<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>__TITLE__</title><style>
    @page{size:A4 portrait;margin:0}*{box-sizing:border-box}body{margin:0;background:#525659;font-family:'Plus Jakarta Sans',Arial,sans-serif}
    .page{position:relative;width:210mm;height:297mm;margin:0 auto 14px;background:white;overflow:hidden;break-after:page;page-break-after:always}.page:last-child{break-after:auto;page-break-after:auto}
    .bg-template{position:absolute;inset:0;width:100%;height:100%;z-index:1;pointer-events:none}
    .request-title-mask,.request-heading{position:absolute;top:11.6%;left:12.5%;width:82%;height:5.7%;z-index:4}
    .request-heading{z-index:5;display:flex;align-items:center;justify-content:center;text-align:center;color:#24264e;font-family:Arial,sans-serif;font-size:34px;font-weight:900;white-space:nowrap}
    .patient{position:absolute;top:18.2%;left:12.5%;right:5.5%;z-index:10;color:#334155;font-size:14.5px;border-bottom:2px solid #0284c7;padding-bottom:10px}
    .identity{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.identity strong{color:#0f172a}.small{margin-top:6px;font-size:14px}.reference{color:#0284c7;font:800 14.5px monospace}
    .content{position:absolute;top:25.8%;left:12.5%;right:5.5%;height:52%;z-index:10;overflow:hidden;overflow-wrap:anywhere}
    .document-title{font-weight:900;font-size:15px;color:#0c1a10;margin-bottom:15px}.section{font-size:15.5px;font-weight:800;color:#0c1a10;margin:12px 0 7px}
    .exam,.note{font-size:14px;line-height:1.55;color:#1e293b;margin:0 0 6px;padding-left:14px}.detail{color:#475569}
    .signature{position:absolute;bottom:12.5%;left:12.5%;right:5.5%;z-index:10;text-align:right;border-top:1px dotted #cbd5e1;padding-top:12px;color:#64748b;font-size:11.5px;font-weight:700}
    .signature strong{display:block;color:#059669;font-size:13.5px;font-weight:900;margin-top:3px}.signature small{display:block;color:#94a3b8;font-size:10.5px;font-style:italic;margin-top:4px}
    .page-number{position:absolute;right:5.5%;bottom:9%;font-size:10px;color:#64748b;z-index:10}.watermark{position:absolute;top:48%;left:20%;transform:rotate(-32deg);font-size:70px;color:#dc262660;border:6px solid #dc262660;z-index:20;padding:15px;pointer-events:none}
    .no-print{position:fixed;top:15px;right:15px;z-index:9999;display:flex;gap:10px}.btn{border:0;padding:10px 18px;border-radius:10px;font-weight:bold;cursor:pointer;font-size:13px;box-shadow:0 4px 10px #0004;background:#121c15;color:#8be140}.close{background:#dc2626;color:white}
    @media print{body{background:white}.no-print{display:none!important}.page{margin:0}}
    </style></head><body><div class="no-print"><button class="btn" onclick="window.print()">Imprimer / PDF</button><button class="btn close" onclick="window.parent?.closePrescriptionModal ? window.parent.closePrescriptionModal() : window.close()">Quitter</button></div>
    <template id="page-template"><div class="page">__BACKGROUND__<svg class="request-title-mask" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect width="100%" height="100%" fill="white"/></svg><div class="request-heading">__TITLE__</div>__CANCELLED____HEADER__<div class="content"></div>__FOOTER__</div></template>
    <div id="units" hidden>__UNITS__</div><div id="pages"></div>
    <script>
    function paginate(){
        const units=document.getElementById('units'),pages=document.getElementById('pages');
        if(!units.children.length)return;
        function page(){const node=document.getElementById('page-template').content.firstElementChild.cloneNode(true);pages.append(node);return node.querySelector('.content');}
        let content=page();
        [...units.children].forEach(unit=>{
            content.append(unit);
            if(content.scrollHeight>content.clientHeight+1 && content.children.length>1){
                const heading=unit.previousElementSibling?.classList.contains('section')?unit.previousElementSibling:null;
                unit.remove();if(heading)heading.remove();content=page();if(heading)content.append(heading);content.append(unit);
            }
        });
        const all=[...pages.children];all.forEach((node,index)=>node.querySelector('.page-number').textContent=(index+1)+' / '+all.length);
        document.body.dataset.paginationReady='true';
    }
    paginate();
    </script></body></html>'''
    replacements = {'__TITLE__':escape(title),'__BACKGROUND__':background,'__CANCELLED__':cancelled,'__HEADER__':header,'__FOOTER__':footer,'__UNITS__':''.join(units)}
    # Une substitution unique évite de réinterpréter un jeton présent dans un texte saisi.
    import re
    return re.sub('|'.join(re.escape(key) for key in replacements),lambda match:replacements[match.group()],html)
