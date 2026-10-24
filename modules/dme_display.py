"""Catalogue commun des préférences d'affichage du dossier médical."""

DME_TABS = {
    'consultation': 'Consultation clinique & constantes',
    'consommations': 'Produits consommés',
    'ordonnance': 'Rédiger ordonnance',
    'bilans': 'Demandes de bilans biologiques et radiologiques',
    'compterendu': 'Compte rendu & livret',
    'echographie': 'Dossier échographie',
    'historique': 'Historique & archives',
    'croissance': 'Courbes de croissance',
}
DME_GROUPS = {
    'observation': ('Observation médicale & renseignements cliniques', '#dme-panel-consultation > div:has(> #dme_passage_observation)'),
    'constantes': ('Constantes vitales & biométrie', '#dme-panel-consultation > div:has(#dme_ta)'),
    'allergies': ('Allergies & intolérances', 'div:has(> #dme_allergies)'),
    'traitement': ('Traitement en cours', 'div:has(> #dme_traitement)'),
    'ant_med': ('Antécédents médicaux', 'div:has(> #dme_ant_med)'),
    'ant_chir': ('Antécédents chirurgicaux & hospitalisations', 'div:has(> #dme_ant_chir)'),
    'habitudes': ('Notes particulières & habitudes de vie', 'div:has(> #dme_habitudes)'),
    'motif': ('Motif de consultation', 'div:has(> #dme_motif)'),
    'diagnostic': ('Diagnostic & hypothèse', 'div:has(> #dme_diagnostic)'),
    'conduite': ('Conduite à tenir / surveillance', 'div:has(> #dme_conduite)'),
    'specialites': ('Examen clinique & sections spécialisées', '#dme-panel-consultation > section:has([data-dme-specialty-panel])'),
    'physiotherapie': ('Bilan de rééducation / physiothérapie', '#dmeSpecialtyPanel-physiotherapie'),
    'reeducation_classique': ('Rééducation classique', '#dmeSpecialtyPanel-reeducation_classique'),
    'actes': ('Actes médicaux & prestations / facturation', '#dme-panel-consultation > div:has(#dmeActsTableBody)'),
}


def display_permissions(user):
    permissions = (user or {}).get('permissions') or {}
    if isinstance(permissions, str):
        import json
        permissions = json.loads(permissions)
    return permissions if isinstance(permissions, dict) else {}


def display_settings_html():
    def checkbox(key, label):
        return f'<label class="flex items-center gap-2 cursor-pointer"><input type="checkbox" checked id="perm_dme_display_{key}" class="user-dme-display-check rounded text-emerald-600"><span>{label}</span></label>'
    tabs = ''.join(checkbox('tab_' + key, label) for key, label in DME_TABS.items())
    groups = ''.join(checkbox('group_' + key, label) for key, (label, _) in DME_GROUPS.items())
    return f'''<div class="space-y-3 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
        <h3 class="font-bold">Affichage du DME par utilisateur</h3>
        <p>Cocher pour afficher, décocher pour masquer. Les données sont conservées. Les droits d’accès restent définis séparément.</p>
        <div class="flex gap-3"><button type="button" onclick="document.querySelectorAll('.user-dme-display-check').forEach(c=>c.checked=true)">Tout afficher</button><button type="button" onclick="document.querySelectorAll('.user-dme-display-check').forEach(c=>c.checked=false)">Tout masquer</button></div>
        <h4 class="font-bold">Onglets</h4>{tabs}<h4 class="font-bold">Groupes de consultation</h4>{groups}</div>'''
