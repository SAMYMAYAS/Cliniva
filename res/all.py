"""Générateur de documentation technique officielle en PDF pour le projet Cliniva.

Compile l'architecture, la stack technique, le schéma complet PostgreSQL (18 tables),
la logique des modules et la liste des routes API dans un PDF A4 prêt à l'analyse.
Conforme strictement aux normes PEP 8.
"""

from datetime import datetime
import os
from pathlib import Path
import platform
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


class NumberedCanvas(canvas.Canvas):
    """Générateur de canvas personnalisé pour numéroter les pages sous la forme Page X / Y."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748b"))

        # Pied de page
        self.setStrokeColor(colors.HexColor("#e2e8f0"))
        self.setLineWidth(0.5)
        self.line(36, 32, A4[0] - 36, 32)

        date_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
        self.drawString(36, 22, f"Cliniva Medical OS — Spécifications d'Architecture — Généré le {date_str}")
        self.drawRightString(A4[0] - 36, 22, f"Page {self._pageNumber} sur {page_count}")
        self.restoreState()


def build_pdf_documentation(output_filepath: str):
    """Assemble l'intégralité du contenu technique dans le document PDF."""
    doc = SimpleDocTemplate(
        output_filepath,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=45,
    )

    styles = getSampleStyleSheet()

    # Définition des styles personnalisés
    color_primary = colors.HexColor("#0c1a10")
    color_accent = colors.HexColor("#15803d")
    color_dark = colors.HexColor("#0f172a")

    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=color_primary,
        spaceAfter=4,
    )

    subtitle_style = ParagraphStyle(
        "DocSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#475569"),
        spaceAfter=15,
    )

    h1_style = ParagraphStyle(
        "SectionH1",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=color_accent,
        spaceBefore=14,
        spaceAfter=6,
        keepWithNext=True,
    )

    h2_style = ParagraphStyle(
        "SectionH2",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=10.5,
        leading=13,
        textColor=color_dark,
        spaceBefore=8,
        spaceAfter=4,
        keepWithNext=True,
    )

    body_style = ParagraphStyle(
        "BodyTextCustom",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor("#1e293b"),
        spaceAfter=4,
    )

    code_style = ParagraphStyle(
        "CodeBlock",
        parent=styles["Code"],
        fontName="Courier",
        fontSize=7,
        leading=8.5,
        textColor=colors.HexColor("#0f172a"),
    )

    th_style = ParagraphStyle(
        "TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9,
        textColor=colors.white,
    )

    td_style = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7,
        leading=8.5,
        textColor=colors.HexColor("#1e293b"),
    )

    elements = []

    # =========================================================================
    # EN-TÊTE DU DOCUMENT
    # =========================================================================
    elements.append(Paragraph("CLINIVA MEDICAL OS — DOSSIER D'ARCHITECTURE & SPÉCIFICATIONS", title_style))
    elements.append(Paragraph("Synthèse technique exhaustive pour analyse, reprise de contexte et audit de code", subtitle_style))
    elements.append(HRFlowable(width="100%", thickness=1.5, color=color_primary, spaceAfter=12))

    # =========================================================================
    # 1. PRÉSENTATION & ARCHITECTURE GLOBALE
    # =========================================================================
    elements.append(Paragraph("1. ARCHITECTURE GLOBALE & PHILOSOPHIE LOGICIELLE", h1_style))
    elements.append(Paragraph(
        "• <b>Type d'application</b> : Logiciel médical de bureau Windows natif piloté par <b>Microsoft Edge WebView2</b> "
        "(via <code>pywebview</code>), avec serveur backend <b>FastAPI</b> asynchrone embarqué dans un thread dédié "
        "(<code>LocalServerThread</code> sur 127.0.0.1:8000).<br/>"
        "• <b>Évolution Client-Serveur</b> : Le fichier <code>config.json</code> permet d'exécuter l'application soit en mode "
        "<code>local</code> (serveur + poste médical autonome), soit en mode <code>remote</code> (client léger se connectant à l'adresse IP du serveur central).<br/>"
        "• <b>Patron Server-Driven SPA</b> : La page principale (<code>templates/dashboard.html</code>) contient une barre latérale fixe "
        "et un conteneur central (<code>#dynamic-content-area</code>). Les modules métiers sont des fragments HTML/JS injectés à la volée via "
        "<code>loadModule(name)</code> avec ré-exécution automatique du code JavaScript par le moteur de rendu.<br/>"
        "• <b>Sécurité & Sessions</b> : Authentification par jeton de session cryptographique (64 octets hexadécimaux) conservé dans le cookie "
        "HTTP-Only <code>cliniva_session</code>. Les mots de passe sont hachés en SHA-256. Chaque requête dispose d'une traçabilité enregistrée "
        "dans la table <code>audit_logs</code> (Utilisateur, Rôle, Action, Date/Heure, IP).",
        body_style
    ))

    # =========================================================================
    # 2. TECHNOLOGIES UTILISÉES
    # =========================================================================
    elements.append(Paragraph("2. STACK TECHNIQUE & BIBLIOTHÈQUES", h1_style))
    elements.append(Paragraph(
        "• <b>Backend</b> : Python 3.11+, FastAPI, Uvicorn, Pydantic v2, Jinja2.<br/>"
        "• <b>Base de données</b> : PostgreSQL avec pool de connexions multithread <code>ThreadedConnectionPool</code> (psycopg2-binary).<br/>"
        "• <b>Frontend</b> : Tailwind CSS (CDN avec mode sombre/clair <code>darkMode: 'class'</code>), Phosphor Icons, TinyMCE 6 Pro (format A4).<br/>"
        "• <b>Traitement de documents</b> : ReportLab (PDF vectoriels), <code>pypdf</code> (fusion exacte de pages PDF sans perte), "
        "Pillow (images), <code>pywin32</code> (conversion native Microsoft Word vers PDF via COM Windows), <code>qrcode</code>.",
        body_style
    ))

    # =========================================================================
    # 3. SCHEMA COMPLET POSTGRESQL (18 TABLES)
    # =========================================================================
    elements.append(Spacer(1, 4))
    elements.append(Paragraph("3. SCHÉMA COMPLET DE LA BASE DE DONNÉES POSTGRESQL (18 TABLES & TRIGGERS)", h1_style))
    elements.append(Paragraph(
        "La base utilise un schéma relationnel avec colonnes <b>JSONB</b> pour les structures dynamiques et les listes configurables :",
        body_style
    ))

    sql_schema_text = """-- 1. clinic_info : Identité, fiscalité (NIF, NIS, RC, Art), bannières base64, horaires 7j JSONB
-- 2. clinic_services : Services cliniques, entrepôts pharmaceutiques, responsables
-- 3. ref_specialites : Nomenclature des spécialités (code, nom, tarifs jour/nuit)
-- 4. ref_categories : Catégories d'actes (Consultation, Acte Technique, Imagerie, etc.)
-- 5. ref_modalites : Modalités d'actes (Présentiel, Urgence, Contrôle, Téléconsultation)
-- 6. rooms : Salles de soin/examen (désignation, étage, types JSONB, spécialité)
-- 7. consultation_types : Actes & consultations (tarifs jour/nuit, salles_dediees JSONB)
-- 8. clinic_staff : Personnel et praticiens (14 rôles/grades, spécialités, avatars)
-- 9. medications : Répertoire pharmaceutique DCI (nom_commercial, dci, forme, dosage)
-- 10. patients : Dossiers civils (58 Wilayas + communes), biométrie 3 profils, empreinte, pièces JSONB
-- 11. treating_doctors : Médecins correspondants/adresseurs (grade, nom, wilaya, commune)
-- 12. users : Comptes d'accès, rôle, login, password_hash, permissions JSONB, specialites_autorisees JSONB
-- 13. user_sessions : Tokens de session sécurisés (session_token PK, user_id, expires_at 24h)
-- 14. audit_logs : Journal d'audit légal (date_heure, utilisateur, rôle, action_type, description, ip)
-- 15. patient_passages : Admissions, actes JSONB, remises (DA/%), acomptes, restes, questionnaire bilingue JSONB
-- 16. caisse_mouvements : Flux réels de caisse (ENCAISSEMENT +, REGULARISATION +, ANNULATION -)
-- 17. patient_dme_records : DME (constantes avec IMC automatique, 8 rubriques médicales en 2 colonnes)
-- 18. patient_prescriptions : Ordonnances médicales, lignes JSONB, statut Valide/Supprimée, calage sur ORD.png
-- 19. patient_cr_records : Comptes rendus Word/HTML et Livrets archivés (cloisonnés patient et passage)
-- 20. patient_echographies : Clichés échographiques haute résolution PNG/JPG rattachés au passage

-- TRIGGER AUTOMATIQUE DE CAISSE :
-- Tout passage créé avec versement > 0 déclenche automatiquement une entrée dans caisse_mouvements."""

    sql_table = Table(
        [[Paragraph(sql_schema_text.replace("\n", "<br/>").replace(" ", "&nbsp;"), code_style)]],
        colWidths=[523],
    )
    sql_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(sql_table)

    # =========================================================================
    # 4. MODULES MÉTIERS & LOGIQUES CRITIQUES
    # =========================================================================
    elements.append(Spacer(1, 6))
    elements.append(Paragraph("4. MODULES MÉTIERS & LOGIQUES CRITIQUES DÉVELOPPÉES", h1_style))

    modules_desc = [
        ("Dossier Médical Électronique (DME)",
         "Système multi-onglets (1 onglet par patient/passage ouvert). 5 sous-onglets : "
         "1) <b>Consultation & Constantes</b> : Calcul automatique de l'IMC (Poids/Taille²), 8 rubriques médicales disposées en "
         "2 colonnes de 50% chacune, panier d'actes en cours de consultation synchronisé avec la caisse. "
         "2) <b>Rédiger Ordonnance</b> : Visionneur intégré sans quitter l'app, calé sur gabarit officiel ORD.png sans cadre bleu parasite. "
         "3) <b>Compte Rendu & Livret</b> : Remplacement garanti des 8 macros Word (${Date}, ${Nom}, ${Date_nais}, ${med}, ${sec}, "
         "${id_av}, ${cor}, ${signe}), bouton 'Ouvrir Word' et bouton 'Vision Pdf' (conversion Word vers PDF à la volée). "
         "Option <b>Création Livret A4 multipage</b> : Page 1 (1.png avec cadre noir renseigné), Page 2 (2.png), Page 3+ "
         "(le compte rendu original exact fusionné via <code>pypdf</code>), Pages clichés (images agrandies en grille sur A4), "
         "avant-dernière page (99.png), dernière page (100.png). "
         "4) <b>Dossier Échographie</b> : Import PNG/JPG et carrousel interactif plein écran avec commandes clavier. "
         "5) <b>Historique</b> : Archives des consultations."),

        ("Accueil & Passages",
         "Enregistrement des admissions et passages des patients. Filtre de sélection des disciplines restreint "
         "<b>exclusivement aux spécialités autorisées</b> du profil utilisateur connecté (users.specialites_autorisees). "
         "Modales d'ajout complet au 1er plan (z-[100]) du Patient (biométrie 3 profils) et du Médecin Traitant avec sélection automatique. "
         "Verrouillage dynamique du bouton d'enregistrement (requis : patient, médecin, 1 acte, 1 observation). "
         "Génération automatique de la <b>Demande d'Examen A4 (DEM_MR_Valid.png)</b> avec QR Code Pas-XXXXXXX, grands 'X' sur les questionnaires "
         "et positionnement du médecin traitant sous 'Insuffisance Rénale'."),

        ("Caisse & Clôture Financière",
         "Comptabilité en flux réels : 1) Encaissements du jour entrent aujourd'hui ; 2) Restes régularisés aujourd'hui "
         "entrent dans la caisse du jour (même si le passage est antérieur) ; 3) Remboursements/annulations du jour sortent de la caisse du jour. "
         "5 onglets : Journal des flux réels, Patients non encaissés, Impayés & Restes, Remises accordées (avec accordeur), Annulations sécurisées avec motif. "
         "Capsules KPIs centrées et impression du Procès-Verbal de Clôture A4 dans la visionneuse intégrée."),

        ("Gestion des Spécialités & Actes",
         "Nomenclature des disciplines, actes médicaux, cotations jour/nuit et salles dédiées. Exportation directe "
         "dans le dossier Téléchargements de Windows et ouverture automatique du fichier Excel/PDF."),

        ("Gestion du Personnel (Staff) & Utilisateurs",
         "14 fonctions du personnel clinique liées aux services et spécialités. Matrice granulaire de permissions "
         "sur les 12 modules du système et attribution des spécialités médicales autorisées par compte."),
    ]

    for mod_title, mod_body in modules_desc:
        elements.append(Paragraph(f"• <b>{mod_title}</b>", h2_style))
        elements.append(Paragraph(mod_body, body_style))

    # =========================================================================
    # 5. INVENTAIRE DES ENDPOINTS API (TABLEAU SYNTHÉTIQUE)
    # =========================================================================
    elements.append(Spacer(1, 6))
    elements.append(Paragraph("5. INVENTAIRE COMPLET DES ROUTES API (FASTAPI)", h1_style))

    api_routes_data = [
        [Paragraph("Méthode", th_style), Paragraph("Route API", th_style), Paragraph("Module", th_style), Paragraph("Description", th_style)],
        [Paragraph("GET", td_style), Paragraph("/login", td_style), Paragraph("main.py", td_style), Paragraph("Page d'authentification", td_style)],
        [Paragraph("POST", td_style), Paragraph("/api/auth/login", td_style), Paragraph("main.py", td_style), Paragraph("Connexion, cookie de session & audit", td_style)],
        [Paragraph("GET", td_style), Paragraph("/api/auth/logout", td_style), Paragraph("main.py", td_style), Paragraph("Déconnexion et destruction de session", td_style)],
        [Paragraph("GET", td_style), Paragraph("/api/modules/{name}", td_style), Paragraph("main.py", td_style), Paragraph("Chargeur dynamique SPA avec reload", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/clinic/info", td_style), Paragraph("clinique_param.py", td_style), Paragraph("Identité clinique et matrice tarifaire 7j", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/clinic/services", td_style), Paragraph("clinique_param.py", td_style), Paragraph("Gestion des départements et entrepôts", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/specialites-module/*", td_style), Paragraph("gestion_specialites.py", td_style), Paragraph("Spécialités, cotations actes et export direct", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/staff/*", td_style), Paragraph("gestion_staff.py", td_style), Paragraph("Gestion RH, 14 fonctions et spécialités", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/medications/*", td_style), Paragraph("gestion_medicaments.py", td_style), Paragraph("Base DCI et import/export CSV/Excel", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/patients-module/*", td_style), Paragraph("gestion_patients.py", td_style), Paragraph("Dossiers patients, 58 Wilayas, WIA USB", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/treating-doctors/*", td_style), Paragraph("gestion_medecins_traitants.py", td_style), Paragraph("Médecins correspondants et communes", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/users/*", td_style), Paragraph("gestion_utilisateur.py", td_style), Paragraph("Droits d'accès et spécialités autorisées", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/reception/*", td_style), Paragraph("gestion_passages.py", td_style), Paragraph("Admissions, ticket 80mm, Demande A4", td_style)],
        [Paragraph("GET", td_style), Paragraph("/api/finance/stats", td_style), Paragraph("gestion_caisse.py", td_style), Paragraph("KPIs et flux de caisse en temps réel", td_style)],
        [Paragraph("POST", td_style), Paragraph("/api/finance/regulariser", td_style), Paragraph("gestion_caisse.py", td_style), Paragraph("Encaissement régularisation reste dû", td_style)],
        [Paragraph("POST", td_style), Paragraph("/api/finance/annuler-paiement", td_style), Paragraph("gestion_caisse.py", td_style), Paragraph("Remboursement / sortie de caisse du jour", td_style)],
        [Paragraph("GET", td_style), Paragraph("/api/finance/cloture-pv-html", td_style), Paragraph("gestion_caisse.py", td_style), Paragraph("PV officiel de clôture A4 dans visionneur", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/dme/record/*", td_style), Paragraph("gestion_dme.py", td_style), Paragraph("Dossier patient, constantes IMC, 8 rubriques", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/dme/prescriptions/*", td_style), Paragraph("gestion_dme.py", td_style), Paragraph("Ordonnances avec gabarit officiel ORD.png", td_style)],
        [Paragraph("POST/GET", td_style), Paragraph("/api/dme/cr/*", td_style), Paragraph("gestion_dme.py", td_style), Paragraph("Génération Word, Vision Pdf interne", td_style)],
        [Paragraph("POST", td_style), Paragraph("/api/dme/livret/create", td_style), Paragraph("gestion_dme.py", td_style), Paragraph("Assemblage vectoriel Livret A4 multipage", td_style)],
        [Paragraph("GET/POST", td_style), Paragraph("/api/dme/echographies/*", td_style), Paragraph("gestion_dme.py", td_style), Paragraph("Import clichés PNG et visionneur Carrousel", td_style)],
        [Paragraph("GET", td_style), Paragraph("/api/audit", td_style), Paragraph("gestion_audit.py", td_style), Paragraph("Piste d'audit légale et traçabilité", td_style)],
    ]

    routes_table = Table(api_routes_data, colWidths=[45, 160, 115, 203])
    routes_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), color_primary),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(routes_table)

    # =========================================================================
    # 6. PROCHAINE ÉTAPE EXACTE DU PROJET
    # =========================================================================
    elements.append(Spacer(1, 10))
    elements.append(KeepTogether([
        Paragraph("6. PROCHAINE ÉTAPE EXACTE DU PROJET : MODULE RENDEZ-VOUS (AGENDA)", h1_style),
        Paragraph(
            "Le prochain module à implémenter est <b>Prise de Rendez-vous & Planning des Praticiens</b> "
            "(<code>modules/gestion_rendezvous.py</code>) :<br/>"
            "1. <b>Table PostgreSQL <code>appointments</code></b> : liaison patient, praticien, spécialité, salle, créneau horaire, motif et statut "
            "(<i>Confirmé, En attente, En salle d'attente, Honoré, Annulé</i>).<br/>"
            "2. <b>Interface Calendrier Interactif</b> : vues commutables (Jour / Semaine / Mois), filtres par médecin et spécialité, "
            "détection automatique des conflits de réservation (double créneau).<br/>"
            "3. <b>Passerelle vers Admission</b> : bouton permettant de convertir un rendez-vous en admission/passage "
            "dans <code>patient_passages</code> en un clic.",
            body_style
        )
    ]))

    # Génération du document avec numérotation de pages
    doc.build(elements, canvasmaker=NumberedCanvas)
    print(f"Documentation technique générée avec succès : {output_filepath}")


if __name__ == "__main__":
    current_dir = Path(__file__).resolve().parent
    output_pdf = current_dir / "Cliniva_Architecture_Et_Specifications_Completes.pdf"
    build_pdf_documentation(str(output_pdf))

    # Ouverture automatique sous Windows
    if platform.system() == "Windows" and output_pdf.exists():
        try:
            os.startfile(str(output_pdf))
        except Exception as e:
            print(f"Ouverture automatique impossible : {e}")