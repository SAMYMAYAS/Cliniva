"""Bilan structuré de physiothérapie, partagé entre le DME et le PDF."""
from html import escape
from io import BytesIO

SEVERITY = ('Absent', 'Léger', 'Modéré', 'Important', 'Non évalué')
GROUPS = [
    ('contexte', '1. Contexte et bilan', [
        ('date_bilan', 'Date du bilan', 'date', ()),
        ('type_bilan', 'Étape du bilan', 'select', ('Initial', 'Intermédiaire', 'Final')),
        ('evaluateur', 'Professionnel évaluateur', 'text', ()),
        ('indication', 'Motif, diagnostic médical et prescription', 'textarea', ()),
        ('antecedents', 'Antécédents, interventions et examens utiles', 'textarea', ()),
        ('precautions', 'Précautions, contre-indications et points à vérifier', 'textarea', ()),
    ]),
    ('localisation', '2. Localisation anatomique', [
        ('zones', 'Zones concernées (schéma ou saisie libre)', 'textarea', ()),
        ('cote', 'Côté du patient', 'select', ('Droit', 'Gauche', 'Bilatéral', 'Médian', 'Non applicable')),
        ('localisation_details', 'Localisation précise, irradiation et contexte', 'textarea', ()),
    ]),
    ('douleur', '3. Douleur', [
        ('douleur_presence', 'Douleur présente', 'select', ('Oui', 'Non', 'Non évaluée')),
        ('douleur_type', 'Caractéristiques / mécanisme évoqué par le professionnel', 'select', ('Neuropathique suspectée', 'Nociceptive suspectée', 'Mixte suspectée', 'Autre / à préciser', 'Indéterminé')),
        ('douleur_duree', 'Évolution de la douleur', 'select', ('Aiguë', 'Subaiguë', 'Chronique', 'À préciser')),
        ('douleur_debut', 'Date de début ou durée rapportée', 'text', ()),
        ('douleur_repos', 'Douleur au repos (EN 0 à 10)', 'score', ()),
        ('douleur_effort', 'Douleur au mouvement (EN 0 à 10)', 'score', ()),
        ('douleur_nuit', 'Douleur nocturne (EN 0 à 10)', 'score', ()),
        ('douleur_details', 'Facteurs aggravants / soulageants et retentissement', 'textarea', ()),
    ]),
    ('deficits', '4. Signes et déficiences', [
        ('inflammation', 'Inflammation', 'select', SEVERITY),
        ('oedeme', 'Œdème', 'select', SEVERITY),
        ('dystrophie', 'Troubles trophiques / dystrophie', 'select', SEVERITY),
        ('sensibilite', 'Perte de sensibilité', 'select', SEVERITY),
        ('amyotrophie', 'Amyotrophie', 'select', SEVERITY),
        ('contracture', 'Contracture', 'select', SEVERITY),
        ('instabilite', 'Instabilité articulaire', 'select', SEVERITY),
        ('mobilite', 'Restriction de mobilité', 'select', SEVERITY),
        ('motricite', 'Perte de motricité : origine évoquée', 'select', ('Absente', 'Sidération / inhibition', 'Périphérique suspectée', 'Centrale suspectée', 'Autre / indéterminée', 'Non évaluée')),
        ('proprioception', 'Déficit de proprioception', 'select', SEVERITY),
        ('deficits_details', 'Précisions par zone, côté, tests et constatations', 'textarea', ()),
    ]),
    ('mesures', '5. Mesures objectives', [
        ('amplitudes', 'Mobilité : articulation, mouvement, actif/passif, degrés, côté', 'textarea', ()),
        ('force', 'Force : muscle / mouvement, côté, méthode et résultat', 'textarea', ()),
        ('mesures_neuro', 'Sensibilité, proprioception, œdème : méthode et résultat', 'textarea', ()),
    ]),
    ('fonction', '6. Retentissement fonctionnel', [
        ('activites', 'Activités limitées et priorités du patient', 'textarea', ()),
        ('marche', 'Marche, équilibre, transferts et aides techniques', 'textarea', ()),
        ('autonomie', 'Autonomie, travail, sport et sommeil', 'textarea', ()),
        ('tests', 'Tests / échelles : nom, date, résultat et unité', 'textarea', ()),
    ]),
    ('plan', '7. Synthèse et projet de rééducation', [
        ('synthese', 'Synthèse clinique du professionnel', 'textarea', ()),
        ('objectifs', 'Objectifs mesurables et échéance', 'textarea', ()),
        ('programme', 'Techniques, exercices, éducation et consignes à domicile', 'textarea', ()),
        ('rythme', 'Rythme, durée et nombre de séances prévus', 'text', ()),
        ('reevaluation', 'Date de réévaluation prévue', 'date', ()),
    ]),
    ('suivi', '8. Suivi et validation', [
        ('evolution', 'Évolution : douleur, mobilité, force et activités', 'textarea', ()),
        ('adaptation', 'Tolérance, événements et adaptations du programme', 'textarea', ()),
        ('validation', 'Nom et visa / signature du professionnel', 'text', ()),
    ]),
]
FIELDS = {field[0] for _, _, fields in GROUPS for field in fields}
LABELS = {key: label for _, _, fields in GROUPS for key, label, _, _ in fields}


def validate_values(values):
    """Valide les choix sans transformer une absence de réponse en résultat normal."""
    from datetime import date
    if not isinstance(values, dict):
        raise ValueError('Fiche de physiothérapie invalide.')
    cleaned = {}
    for _, _, fields in GROUPS:
        for key, label, kind, options in fields:
            raw = values.get(key)
            value = '' if raw is None else str(raw).strip()
            if len(value) > (4000 if kind == 'textarea' else 200):
                raise ValueError(f'{label} : texte trop long.')
            if value and kind == 'score' and (not value.isdigit() or not 0 <= int(value) <= 10):
                raise ValueError(f'{label} : saisir un entier de 0 à 10.')
            if value and kind == 'select' and value not in options:
                raise ValueError(f'{label} : choix invalide.')
            if value and kind == 'date':
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise ValueError(f'{label} : date invalide.') from None
            cleaned[key] = value
    return cleaned


from modules.anatomy_assets import anatomical_image_svg, body_map_pdf


def body_map_html():
    zones = [('Tête',90,24,12,17),('Cou',90,45,8,6),('Épaule',63,59,10,10),('Épaule',117,59,10,10),('Bras',57,81,7,15),('Bras',123,81,7,15),('Coude',52,100,7,8),('Coude',128,100,7,8),('Poignet / main',46,139,9,16),('Poignet / main',134,139,9,16),('Thorax / dos',90,76,17,19),('Abdomen / lombaires',90,104,17,14),('Bassin / hanche',90,128,22,13),('Cuisse',79,157,9,18),('Cuisse',101,157,9,18),('Genou',80,183,8,8),('Genou',100,183,8,8),('Cheville / pied',77,240,9,9),('Cheville / pied',103,240,9,9)]
    views = []
    for view in ('Face', 'Dos', 'Profil'):
        shapes = ''
        for index, (label,x,y,rx,ry) in enumerate(zones):
            region = f'{view} - {label}'
            if label in ('Épaule','Bras','Coude','Poignet / main','Cuisse','Genou','Cheville / pied'):
                side = ('Droit' if x < 90 else 'Gauche') if view == 'Face' else ('Gauche' if x < 90 else 'Droit')
                region += ' - ' + side if view != 'Profil' else ' (côté à préciser)'
            if view == 'Profil' and index in (3,5,7,9,14,16,18):
                continue
            if view == 'Profil':
                x = 93; rx = min(rx,9)
            shapes += f'<ellipse class="physio-zone" tabindex="0" role="button" aria-pressed="false" aria-label="{escape(region)}" data-physio-zone="{escape(region)}" cx="{x}" cy="{y}" rx="{rx}" ry="{ry}"><title>{escape(region)}</title></ellipse>'
        silhouette = anatomical_image_svg(view, 'physio-body')
        orientation = '' if view == 'Profil' else f'<text x="36" y="52">{"D" if view == "Face" else "G"}</text><text x="139" y="52">{"G" if view == "Face" else "D"}</text>'
        views.append(f'<div class="physio-body-view"><div class="physio-body-title">{view}</div><svg viewBox="28 2 124 254" style="width:100%;height:330px" aria-label="Schéma corporel {view}">{silhouette}<g fill="#64748b" font-size="7" font-family="sans-serif">{orientation}</g>{shapes}</svg></div>')
    return '<div class="physio-body-grid">'+''.join(views)+'</div><p style="font-size:11px;color:#64748b;margin:8px 0">D / G : côté droit / gauche du patient. Cliquez sur une zone pour la sélectionner.</p>'


def render_bilan_html():
    sections = []
    for group, title, fields in GROUPS:
        content = body_map_html() if group == 'localisation' else ''
        for key, label, kind, options in fields:
            attrs = f'data-dme-specialty="physiotherapie" data-dme-specialty-field="{key}" id="physio-{key}"'
            if kind == 'select':
                control = f'<select {attrs}><option value="">Non renseigné</option>'+''.join(f'<option>{escape(option)}</option>' for option in options)+'</select>'
            elif kind == 'textarea':
                control = f'<textarea {attrs} rows="2" maxlength="4000"></textarea>'
            else:
                extra = 'min="0" max="10" step="1"' if kind == 'score' else 'maxlength="200"'
                control = f'<input {attrs} type="{"number" if kind == "score" else kind}" {extra}>'
            content += f'<label class="dme-specialty-field">{escape(label)}{control}</label>'
        sections.append(f'<fieldset style="min-width:0;border:1px solid #cbd5e1;border-radius:16px;padding:16px" class="space-y-3"><legend style="font-weight:800;padding:0 8px">{escape(title)}</legend>{content}</fieldset>')
    return '''<div id="dmeSpecialtyPanel-physiotherapie" data-dme-specialty-panel="physiotherapie" class="hidden grid grid-cols-1 gap-4 md:col-span-2">
        <div class="flex flex-wrap items-center justify-between gap-3"><h3 class="font-extrabold text-sm">Bilan de rééducation / physiothérapie</h3>
        <button type="button" data-dme-allow-readonly onclick="printPhysiotherapyBilan()" class="rounded-xl bg-indigo-600 px-4 py-2.5 text-xs font-bold text-white">Aperçu PDF / Imprimer</button></div>
        <p class="text-xs text-slate-500">Choisir une zone ou saisir sa localisation. Les cases vides signifient « non renseigné ». Schéma indicatif ; les constatations et décisions sont saisies par le professionnel.</p>
        <style>.physio-body-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.physio-body-view{border:1px solid #dce5e6;border-radius:18px;background:#fff;padding:14px 8px 8px}.physio-body-title{text-align:center;font-size:12px;font-weight:800;color:#115e59;letter-spacing:.04em}.physio-zone{fill:#14b8a6;fill-opacity:0;stroke:transparent;stroke-width:1;cursor:pointer;transition:fill-opacity .15s}.physio-zone:hover,.physio-zone:focus{fill-opacity:.28;stroke:#0f766e;outline:none}.physio-zone[aria-pressed="true"]{fill:#e11d48;fill-opacity:.48;stroke:#9f1239}.dark .physio-body-view{background:#fff;border-color:#334b41}</style>
        '''+''.join(sections)+'</div>'


def build_bilan_pdf(values=None, patient=None):
    """Version imprimable vierge ou renseignée ; texte long paginé sans troncature."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    from xml.sax.saxutils import escape as xml_escape
    values, patient = values or {}, patient or {}
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=17*mm, rightMargin=17*mm, topMargin=19*mm, bottomMargin=17*mm, title='Bilan de rééducation / physiothérapie', author='Cliniva')
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle('PhysioTitle', fontName='Helvetica-Bold', fontSize=18, leading=22, textColor=colors.HexColor('#115e59'), spaceAfter=8))
    styles.add(ParagraphStyle('PhysioHead', fontName='Helvetica-Bold', fontSize=11, leading=14, textColor=colors.HexColor('#115e59'), spaceBefore=10, spaceAfter=6, keepWithNext=True))
    styles.add(ParagraphStyle('PhysioBody', fontName='Helvetica', fontSize=8.5, leading=12, spaceAfter=4))
    def para(text, style='PhysioBody'):
        return Paragraph(xml_escape(str(text)).replace('\n','<br/>'), styles[style])
    story=[para('BILAN DE RÉÉDUCATION', 'PhysioTitle'),para('Physiothérapie - évaluation clinique et suivi fonctionnel')]
    identity = 'Patient : '+str(patient.get('nom') or '................................')+' '+str(patient.get('prenom') or '')
    story += [para(identity),para('Naissance : '+str(patient.get('date_naissance') or '........................')+'     Passage : '+str(patient.get('passage_id') or '........................')),Spacer(1,5*mm)]
    for index,(group,title,fields) in enumerate(GROUPS):
        if group in ('douleur','mesures'):story.append(PageBreak())
        story.append(para(title,'PhysioHead'))
        if group == 'localisation':
            story.append(body_map_pdf())
            story.append(Spacer(1,3*mm))
        for key,label,kind,options in fields:
            value=str(values.get(key) if values.get(key) is not None else '')
            if value:
                story += [para(label+' :'),para(value)]
            elif kind=='select':
                # Cases imprimées et option explicite pour ne pas confondre absence et non-évaluation.
                text='   '.join('[ ] '+option for option in options)
                story += [para(label+' :'),para(text)]
            else:
                story.append(para(label+' : '+('_'*18 if kind in ('date','score','text') else '')))
                if kind=='textarea':
                    story.append(para('_'*95))
            story.append(Spacer(1,1.3*mm))
    story += [Spacer(1,4*mm),para('EN : 0 = aucune douleur ; 10 = douleur maximale imaginable. Un champ vide n’indique pas un résultat normal. Aucun score global ni diagnostic automatique n’est calculé.')]
    def footer(canvas,document):
        canvas.saveState();canvas.setStrokeColor(colors.HexColor('#cbd5e1'));canvas.line(17*mm,14*mm,193*mm,14*mm)
        canvas.setFont('Helvetica',8);canvas.setFillColor(colors.HexColor('#64748b'));canvas.drawString(17*mm,10*mm,'CLINIVA | Bilan de physiothérapie');canvas.drawRightString(193*mm,10*mm,str(document.page));canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    return buffer.getvalue()
