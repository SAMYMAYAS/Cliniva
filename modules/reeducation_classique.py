"""Reprise de la grille classique visible sur les photographies de référence."""
from html import escape

ROWS = [
    ('rc_douleur', 'Douleur', ('Neurologique', 'Aiguë', 'Chronique')),
    ('rc_inflammation', 'Inflammation', ('Non', 'Modéré', 'Importante')),
    ('rc_oedeme', 'Œdème-dystrophie', ('Non', 'Modéré', 'Important')),
    ('rc_sensibilite', 'Perte de sensibilité', ('Non', 'Modéré', 'Importante')),
    ('rc_amyotrophie', 'Amyotrophie', ('Non', 'Modérée', 'Importante')),
    ('rc_contracture', 'Contracture', ('Non', 'Modérée', 'Importante')),
    ('rc_instabilite', 'Instabilité articulaire', ('Non', 'Modérée', 'Importante')),
    ('rc_mobilite', 'Restriction de mobilité', ('Non', 'Modérée', 'Importante')),
    ('rc_motricite', 'Perte de motricité', ('Sidération', 'Périphérique', 'Centrale')),
    ('rc_proprioception', 'Déficit de proprioception', ('Non', 'Modéré', 'Importante')),
]
ANALYTICAL_GROUPS = [
    ('Traitement et ATCD', [
        ('rc_traitement', 'Traitement en cours', 'Traitements, posologies et prises en charge en cours…'),
        ('rc_atcd', 'Antécédents (ATCD)', 'Antécédents médicaux, chirurgicaux et autres éléments utiles…'),
    ]),
    ('Examen physique analytique', [
        ('rc_bilan_cutaneo_trophique', 'Bilan cutanéo-trophique', 'Constatations de l’examen cutanéo-trophique…'),
        ('rc_bilan_osteo_articulaire', 'Bilan ostéo-articulaire', 'Constatations, articulations examinées, côté et mesures…'),
        ('rc_bilan_neuromusculaire', 'Bilan neuromusculaire', 'Constatations et résultats du bilan neuromusculaire…'),
        ('rc_bilan_neurologique_specifique', 'Bilan neurologique spécifique', 'Constatations, tests réalisés et résultats…'),
        ('rc_bilan_vesico_sphincterien', 'Bilan vésico-sphinctérien', 'Éléments du bilan vésico-sphinctérien…'),
        ('rc_bilan_cardio_respiratoire', 'Bilan cardio-respiratoire', 'Constatations et résultats de l’examen cardio-respiratoire…'),
        ('rc_capacite_vitale', 'Capacité vitale', 'Valeur, unité, méthode et date de mesure…'),
        ('rc_tolerance_effort', 'Tolérance à l’effort', 'Épreuve ou activité évaluée, tolérance et résultats…'),
    ]),
]
TEXT_FIELDS = {key: label for _, fields in ANALYTICAL_GROUPS for key, label, _ in fields}
FIELDS = {key for key, _, _ in ROWS} | {'rc_zones'} | set(TEXT_FIELDS)
LABELS = {key: label for key, label, _ in ROWS} | {'rc_zones': 'Localisation anatomique', **TEXT_FIELDS}


def validate_values(values):
    if not isinstance(values, dict):
        raise ValueError('Fiche de rééducation classique invalide.')
    result = {}
    for key, label, options in ROWS:
        value = str(values.get(key) or '').strip()
        if value and value not in options:
            raise ValueError(f'{label} : choix invalide.')
        result[key] = value
    zones = str(values.get('rc_zones') or '').strip()
    if len(zones) > 4000:
        raise ValueError('La localisation anatomique est trop longue.')
    result['rc_zones'] = zones
    for key, label in TEXT_FIELDS.items():
        value = str(values.get(key) or '').strip()
        if len(value) > 4000:
            raise ValueError(f'{label} : texte trop long (4 000 caractères maximum).')
        result[key] = value
    return result


def mannequin_svg(view, miniature=False, instance=''):
    from modules.anatomy_assets import anatomical_image_svg
    namespace = ('rc-mini-' + str(instance)) if miniature else 'rc-body'
    silhouette = anatomical_image_svg(view, namespace)
    zones = []
    if not miniature:
        coordinates = [
            ('Cou', 90, 44, 14, 12), ('Thorax / dos',90,80,24,33),
            ('Abdomen / lombaires',90,104,24,24), ('Bassin / hanche',90,128,32,23),
            ('Épaule',63,61,18,19), ('Épaule',117,61,18,19),
            ('Coude',52,100,14,18), ('Coude',128,100,14,18),
            ('Poignet / main',46,137,14,20), ('Poignet / main',134,137,14,20),
            ('Genou',80,183,15,19), ('Genou',100,183,15,19),
            ('Cheville / pied',77,240,15,18), ('Cheville / pied',103,240,15,18),
        ]
        for index,(label,x,y,w,h) in enumerate(coordinates):
            if view == 'Profil' and index in (5,7,9,11,13):
                continue
            region = view + ' - ' + label
            if index >= 4:
                side = ('Droit' if x < 90 else 'Gauche') if view == 'Face' else ('Gauche' if x < 90 else 'Droit')
                region += (' - ' + side) if view != 'Profil' else ' (côté à préciser)'
            if view == 'Profil': x = 94
            zones.append(f'<rect x="{x-w/2}" y="{y-h/2}" width="{w}" height="{h}" class="rc-zone" data-rc-zone="{escape(region)}" tabindex="0" role="button" aria-pressed="false" aria-label="{escape(region)}"><title>{escape(region)}</title></rect>')
    return f'<svg viewBox="28 2 124 254" aria-label="Vue {view}" class="rc-mannequin">{silhouette}{"".join(zones)}</svg>'


def render_classic_html():
    analytical_sections = []
    for title, fields in ANALYTICAL_GROUPS:
        cards = ''.join(
            f'<label class="rc-analysis-field" for="{key}"><span>{escape(label)}</span>'
            f'<textarea id="{key}" data-dme-specialty="reeducation_classique" '
            f'data-dme-specialty-field="{key}" rows="3" maxlength="4000" '
            f'placeholder="{escape(placeholder)}"></textarea></label>'
            for key, label, placeholder in fields
        )
        analytical_sections.append(f'<fieldset class="rc-analysis-group"><legend>{escape(title)}</legend><div class="rc-analysis-grid">{cards}</div></fieldset>')
    rows = []
    for key, label, options in ROWS:
        choices = ''.join(
            f'<label class="rc-choice rc-tone-{index}"><input type="radio" name="{key}" value="{escape(option)}" data-rc-choice="{key}"><span>{escape(option)}</span></label>'
            for index,option in enumerate(options)
        )
        rows.append(f'<fieldset class="rc-row"><legend>{label}</legend><input type="hidden" id="{key}" data-dme-specialty="reeducation_classique" data-dme-specialty-field="{key}"><div class="rc-choices">{choices}</div></fieldset>')
    thumbnails = ''.join('<div class="rc-thumbnail" aria-hidden="true">'+mannequin_svg(view,True,index)+'</div>' for index,view in enumerate(('Face','Dos','Face','Profil','Dos','Face')))
    mannequins = ''.join(mannequin_svg(view) for view in ('Dos','Profil','Face'))
    return '''<div id="dmeSpecialtyPanel-reeducation_classique" data-dme-specialty-panel="reeducation_classique" class="hidden grid grid-cols-1 gap-3 md:col-span-2">
    <style>
    #dmeSpecialtyPanel-reeducation_classique .rc-layout{display:grid;grid-template-columns:minmax(230px,32%) minmax(430px,1fr);gap:24px;padding:14px;background:#f9f8f5;color:#323232;font-family:Arial,sans-serif;min-width:710px}
    #dmeSpecialtyPanel-reeducation_classique .rc-scroll{overflow-x:auto}
    #dmeSpecialtyPanel-reeducation_classique .rc-left{display:flex;flex-direction:column;justify-content:space-between;gap:70px;padding-bottom:10px}
    #dmeSpecialtyPanel-reeducation_classique .rc-experts{background:linear-gradient(135deg,#179caa,#087e96);border:1px solid #138698;border-radius:6px;min-height:175px;padding:28px 20px;color:white;box-shadow:inset 0 0 18px #ffffff12}
    #dmeSpecialtyPanel-reeducation_classique .rc-expert-label{font-size:12px;font-weight:700;margin-bottom:18px}
    #dmeSpecialtyPanel-reeducation_classique .rc-thumbnails{display:flex;justify-content:space-between;gap:10px;margin-top:24px}
    #dmeSpecialtyPanel-reeducation_classique .rc-thumbnail{width:28px;height:40px;background:#eee8dc;border:1px solid #d3d8d2;padding:1px}
    #dmeSpecialtyPanel-reeducation_classique .rc-thumbnail svg{width:100%;height:100%}
    #dmeSpecialtyPanel-reeducation_classique .rc-anatomy{display:flex;justify-content:space-around;align-items:center;height:265px;border:1px solid #a2b5b7;border-radius:5px;padding:8px;background:#fff}
    #dmeSpecialtyPanel-reeducation_classique .rc-mannequin{width:32%;height:100%}
    #dmeSpecialtyPanel-reeducation_classique .rc-zone{fill:transparent;stroke:#428d9d;stroke-width:1;cursor:pointer}
    #dmeSpecialtyPanel-reeducation_classique .rc-zone:hover,#dmeSpecialtyPanel-reeducation_classique .rc-zone:focus{fill:#20b6ba44;stroke:#087e96;stroke-width:1.7;outline:none}
    #dmeSpecialtyPanel-reeducation_classique .rc-zone[aria-pressed="true"]{fill:#e56f7a66;stroke:#a84267;stroke-width:1.7}
    #dmeSpecialtyPanel-reeducation_classique .rc-questionnaire{border:1px solid #9cb8ba;border-radius:6px;padding:8px 10px 9px;background:#faf9f6}
    #dmeSpecialtyPanel-reeducation_classique .rc-row{border:0;padding:0;margin:0 0 12px;min-width:0}
    #dmeSpecialtyPanel-reeducation_classique .rc-row legend{display:block;width:100%;text-align:center;font-size:11px;line-height:15px;font-weight:400;margin:0 0 4px;padding:0}
    #dmeSpecialtyPanel-reeducation_classique .rc-choices{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
    #dmeSpecialtyPanel-reeducation_classique .rc-choice{display:flex;align-items:center;gap:3px;border:1px solid #b3a8a7;border-radius:2px;height:19px;padding:0 4px;color:white;font-size:10px;line-height:1;cursor:pointer;box-shadow:inset 0 1px 1px #ffffff55}
    #dmeSpecialtyPanel-reeducation_classique .rc-tone-0{background:#df7885}
    #dmeSpecialtyPanel-reeducation_classique .rc-tone-1{background:#4bafa9}
    #dmeSpecialtyPanel-reeducation_classique .rc-tone-2{background:#ac78a9}
    #dmeSpecialtyPanel-reeducation_classique .rc-choice input{appearance:auto;accent-color:#fff;width:9px;height:9px;margin:0;flex-shrink:0}
    #dmeSpecialtyPanel-reeducation_classique .rc-choice.rc-selected{outline:2px solid #32666f;outline-offset:1px;filter:saturate(1.15)}
    #dmeSpecialtyPanel-reeducation_classique .rc-footer{display:flex;justify-content:flex-end}
    #dmeSpecialtyPanel-reeducation_classique .rc-confirm{width:22px;height:20px;padding:0;border:1px solid #579da4;border-radius:3px;background:#459eaa;color:#fff;cursor:pointer;font-size:14px}
    #dmeSpecialtyPanel-reeducation_classique .rc-analysis{padding:0 14px 14px;background:#f9f8f5;color:#323232;font-family:Arial,sans-serif}
    #dmeSpecialtyPanel-reeducation_classique .rc-analysis-group{min-width:0;border:1px solid #9cb8ba;border-radius:6px;padding:16px;margin:0 0 16px;background:#faf9f6}
    #dmeSpecialtyPanel-reeducation_classique .rc-analysis-group legend{padding:0 8px;font-size:13px;font-weight:700;color:#087e96}
    #dmeSpecialtyPanel-reeducation_classique .rc-analysis-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
    #dmeSpecialtyPanel-reeducation_classique .rc-analysis-field{display:flex;flex-direction:column;gap:7px;font-size:12px;font-weight:600;min-width:0}
    #dmeSpecialtyPanel-reeducation_classique .rc-analysis-field textarea{width:100%;box-sizing:border-box;min-height:88px;padding:10px;border:1px solid #b7c9c9;border-radius:4px;background:#fff;color:#323232;font:12px/1.5 Arial,sans-serif;resize:vertical}
    #dmeSpecialtyPanel-reeducation_classique .rc-analysis-field textarea:focus{outline:2px solid #4bafa9;outline-offset:1px}
    @media(max-width:640px){#dmeSpecialtyPanel-reeducation_classique .rc-analysis-grid{grid-template-columns:1fr}}
    </style>
    <div class="rc-scroll"><div class="rc-layout"><div class="rc-left"><div class="rc-experts">
    <div class="rc-expert-label">◇ Expert Urogynécologue</div><div class="rc-expert-label">◇ Expert Physiothérapie</div>
    '''+f'<div class="rc-thumbnails">{thumbnails}</div></div><div class="rc-anatomy">{mannequins}</div></div><div class="rc-questionnaire">'+''.join(rows)+'''
    <input type="hidden" id="rc_zones" data-dme-specialty="reeducation_classique" data-dme-specialty-field="rc_zones">
    <div class="rc-footer"><button type="button" class="rc-confirm" title="Enregistrer la consultation" aria-label="Enregistrer la consultation" onclick="saveActiveDmeConsultation()">✓</button></div>
    </div></div></div><div class="rc-analysis">'''+''.join(analytical_sections)+'''</div></div>'''


CLASSIC_SCRIPT = r'''
            function refreshClassicReeducation() {
                document.querySelectorAll('[data-rc-choice]').forEach(radio=>{
                    radio.checked=radio.value===(document.getElementById(radio.dataset.rcChoice)?.value || '');
                    radio.parentElement.classList.toggle('rc-selected',radio.checked);
                });
                const selected=(document.getElementById('rc_zones')?.value || '').split(';').map(value=>value.trim());
                document.querySelectorAll('[data-rc-zone]').forEach(zone=>zone.setAttribute('aria-pressed',String(selected.includes(zone.dataset.rcZone))));
            }
            document.querySelectorAll('[data-rc-choice]').forEach(radio=>radio.addEventListener('change',()=>{
                const hidden=document.getElementById(radio.dataset.rcChoice);
                if(hidden && radio.checked && !radio.disabled) {
                    hidden.value=radio.value;
                    refreshClassicReeducation();
                }
            }));
            function toggleClassicReeducationZone(zone) {
                const tab=window.activeDmeTabs.find(t=>t.key===window.currentActiveDmeKey);
                const input=document.getElementById('rc_zones');
                if(!input || input.disabled || tab?.mode==='lecture')return;
                const selected=input.value.split(';').map(value=>value.trim()).filter(Boolean);
                const value=zone.dataset.rcZone,index=selected.indexOf(value);
                if(index>=0)selected.splice(index,1);else selected.push(value);
                input.value=selected.join('; ');refreshClassicReeducation();
            }
            document.querySelectorAll('[data-rc-zone]').forEach(zone=>{
                zone.addEventListener('click',()=>toggleClassicReeducationZone(zone));
                zone.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleClassicReeducationZone(zone);}});
            });
'''
