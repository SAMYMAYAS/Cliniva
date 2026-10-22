"""Module de gestion de la persistance PostgreSQL pour Cliniva.

Fournit un pool de connexions thread-safe et l'initialisation des schémas.
Conforme PEP 8 et sécurisé.
"""

import logging
import os
from contextlib import contextmanager
from typing import Generator
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger("ClinivaDB")

# Dossier pour les documents et médias téléversés
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(DOCS_DIR, exist_ok=True)

# Vers le haut de database.py, définissez le dossier des avatars :
AVATARS_DIR = os.path.join(BASE_DIR, "uploads", "avatars")
os.makedirs(AVATARS_DIR, exist_ok=True)

CR_TEMPLATES_DIR = os.path.join(BASE_DIR, "uploads", "cr_templates")
MODEL_TEMPLATE_DIR = os.path.join(BASE_DIR, "uploads", "model_templates")

os.makedirs(CR_TEMPLATES_DIR, exist_ok=True)
os.makedirs(MODEL_TEMPLATE_DIR, exist_ok=True)

CR_PATIENTS_DIR = os.path.join(BASE_DIR, "uploads", "cr_patients")
ECHOS_DIR = os.path.join(BASE_DIR, "uploads", "echographies")
LIVRETS_PDF_DIR = os.path.join(BASE_DIR, "uploads", "livrets_pdf")

for _d in [CR_PATIENTS_DIR, ECHOS_DIR, LIVRETS_PDF_DIR]:
    os.makedirs(_d, exist_ok=True)

PATIENT_CR_DIR = os.path.join(BASE_DIR, "uploads", "patient_cr")
os.makedirs(PATIENT_CR_DIR, exist_ok=True)

# Configuration de la base de données PostgreSQL
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "database": os.getenv("DB_NAME", "cliniva_db"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "postgres"),
}

# Initialisation du pool de connexions
try:
    connection_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=20,
        **DB_CONFIG
    )
    logger.info("Pool de connexions PostgreSQL initialisé avec succès.")
except Exception as error:
    logger.critical("Échec de connexion à PostgreSQL : %s", error)
    connection_pool = None


@contextmanager
def get_db_cursor() -> Generator[psycopg2.extensions.cursor, None, None]:
    """Gestionnaire de contexte fournissant un curseur transactionnel avec RealDictCursor.

    Yields:
        psycopg2.extensions.cursor: Curseur retournant les lignes sous forme de dictionnaires.
    """
    if not connection_pool:
        raise RuntimeError("Le pool de connexions PostgreSQL n'est pas initialisé.")

    conn = connection_pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            yield cursor
            conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.error("Erreur de transaction SQL, rollback effectué : %s", exc)
        raise exc
    finally:
        connection_pool.putconn(conn)





TOUTES_LES_SPECIALITES = [
    ("MED-GEN", "Médecine Générale", "Consultations omnipraticien et bilans", 2000, 3000),
]


def init_database() -> None:
    """Initialise l'ensemble des tables et insère le catalogue complet des spécialités."""
    query_schema = """
    CREATE TABLE IF NOT EXISTS clinic_info (
        id INT PRIMARY KEY DEFAULT 1,
        nom_raison_sociale TEXT DEFAULT '',
        telephone TEXT DEFAULT '',
        email TEXT DEFAULT '',
        adresse TEXT DEFAULT '',
        nif TEXT DEFAULT '',
        nis TEXT DEFAULT '',
        rc TEXT DEFAULT '',
        article_imposition TEXT DEFAULT '',
        entete_ticket TEXT DEFAULT '',
        logo TEXT DEFAULT '',
        entete_pdf TEXT DEFAULT '',
        pied_page_pdf TEXT DEFAULT '',
        horaires_tarification JSONB DEFAULT '{}'::jsonb
    );

    CREATE TABLE IF NOT EXISTS clinic_services (
        id SERIAL PRIMARY KEY,
        nom_service TEXT NOT NULL,
        responsable_nom TEXT DEFAULT '',
        responsable_tel TEXT DEFAULT '',
        entrepot_nom TEXT DEFAULT '',
        responsable_pharma_nom TEXT DEFAULT '',
        responsable_pharma_tel TEXT DEFAULT '',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS ref_specialites (
        id SERIAL PRIMARY KEY,
        code TEXT DEFAULT '',
        nom TEXT UNIQUE NOT NULL,
        description TEXT DEFAULT '',
        tarif_base NUMERIC DEFAULT 0,
        tarif_nuit NUMERIC DEFAULT 0,
        actif BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS ref_categories (
        id SERIAL PRIMARY KEY,
        nom TEXT UNIQUE NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ref_modalites (
        id SERIAL PRIMARY KEY,
        nom TEXT UNIQUE NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ref_types_consultation (
        id SERIAL PRIMARY KEY,
        nom TEXT UNIQUE NOT NULL
    );

    CREATE TABLE IF NOT EXISTS rooms (
        id SERIAL PRIMARY KEY,
        designation TEXT NOT NULL,
        etage TEXT DEFAULT 'Rez-de-chaussée',
        telephone TEXT DEFAULT '',
        types JSONB DEFAULT '[]'::jsonb,
        specialite TEXT DEFAULT 'Polyvalente / Toutes Spécialités',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS room_types (
        id SERIAL PRIMARY KEY,
        nom TEXT NOT NULL UNIQUE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_room_types_nom_ci ON room_types (LOWER(nom));
    INSERT INTO room_types (nom) VALUES
        ('Consultation'), ('Salle De Soin'), ('Bloc Opératoire'), ('Salle De Réveil'),
        ('Salle De Déchocage'), ('Salle De Prélévemnt'), ('Salle radiologique')
    ON CONFLICT DO NOTHING;

    CREATE TABLE IF NOT EXISTS consultation_types (
        id SERIAL PRIMARY KEY,
        designation TEXT NOT NULL,
        categorie TEXT DEFAULT '',
        type_consultation TEXT DEFAULT 'Consultation',
        transmettre_hl7 BOOLEAN DEFAULT FALSE,
        duree TEXT DEFAULT '',
        modalite TEXT DEFAULT '',
        tarif NUMERIC DEFAULT 0.0,
        tarif_nuit NUMERIC DEFAULT 0.0,
        specialite TEXT DEFAULT '',
        salles_dediees JSONB DEFAULT '[]'::jsonb,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );

    ALTER TABLE consultation_types ADD COLUMN IF NOT EXISTS type_consultation TEXT DEFAULT 'Consultation';
    ALTER TABLE consultation_types ADD COLUMN IF NOT EXISTS transmettre_hl7 BOOLEAN DEFAULT FALSE;


    -- Insertion par défaut de la clinique
    INSERT INTO clinic_info (id, nom_raison_sociale)
    VALUES (1, 'Cliniva Medical Center')
    ON CONFLICT (id) DO NOTHING;

    -- Catégories et Modalités initiales
    INSERT INTO ref_categories (nom) VALUES ('Consultation'), ('Acte Technique'), ('Imagerie'), ('Chirurgie Ambulatoire')
    ON CONFLICT (nom) DO NOTHING;

    INSERT INTO ref_modalites (nom) VALUES ('Présentiel'), ('Urgence'), ('Contrôle'), ('Téléconsultation')
    ON CONFLICT (nom) DO NOTHING;

    INSERT INTO ref_types_consultation (nom) VALUES ('Consultation'), ('Control')
    ON CONFLICT (nom) DO NOTHING;
    
    -- Table pour le personnel et corps médical
    CREATE TABLE IF NOT EXISTS clinic_staff (
        id SERIAL PRIMARY KEY,
        nom TEXT NOT NULL,
        prenom TEXT NOT NULL,
        type_personnel TEXT NOT NULL,
        service_nom TEXT DEFAULT '',
        specialite TEXT DEFAULT '',
        telephone TEXT DEFAULT '',
        email TEXT DEFAULT '',
        avatar TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        actif BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    
    -- Table du répertoire des médicaments
    CREATE TABLE IF NOT EXISTS medications (
        id SERIAL PRIMARY KEY,
        nom_commercial TEXT NOT NULL,
        dci TEXT NOT NULL,
        forme TEXT DEFAULT '',
        dosage TEXT DEFAULT '',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
        
        
        -- Table des dossiers patients
    CREATE TABLE IF NOT EXISTS patients (
        id SERIAL PRIMARY KEY,
        nom TEXT NOT NULL,
        prenom TEXT NOT NULL,
        date_naissance DATE NOT NULL,
        genre TEXT DEFAULT 'Homme',
        telephone_1 TEXT NOT NULL,
        telephone_2 TEXT DEFAULT '',
        email TEXT DEFAULT '',
        wilaya TEXT NOT NULL,
        commune TEXT NOT NULL,
        groupe_sanguin TEXT DEFAULT 'O+',
        type_piece_identite TEXT DEFAULT 'Carte Chifa',
        doc_identite TEXT DEFAULT '',
        avatar TEXT DEFAULT '',
        photo_face TEXT DEFAULT '',
        photo_profil_droit TEXT DEFAULT '',
        photo_profil_gauche TEXT DEFAULT '',
        empreinte_index TEXT DEFAULT '',
        pieces_scannees JSONB DEFAULT '[]'::jsonb,
        date_creation DATE DEFAULT CURRENT_DATE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
        
        
-- Table des médecins traitants avec colonne commune
    CREATE TABLE IF NOT EXISTS treating_doctors (
        id SERIAL PRIMARY KEY,
        grade TEXT NOT NULL DEFAULT 'Dr',
        nom TEXT NOT NULL,
        prenom TEXT NOT NULL,
        telephone TEXT DEFAULT '',
        email TEXT DEFAULT '',
        adresse TEXT DEFAULT '',
        wilaya TEXT DEFAULT '',
        commune TEXT DEFAULT '',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );

    ALTER TABLE treating_doctors ADD COLUMN IF NOT EXISTS commune TEXT DEFAULT '';
    ALTER TABLE treating_doctors ADD COLUMN IF NOT EXISTS specialite TEXT DEFAULT '';
        
        -- Table des utilisateurs et matrice des permissions
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        prenom TEXT NOT NULL,
        nom TEXT NOT NULL,
        telephone TEXT DEFAULT '',
        role TEXT NOT NULL DEFAULT 'Médecin',
        login TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Actif',
        avatar TEXT DEFAULT '',
        permissions JSONB DEFAULT '{}'::jsonb,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );

    -- Création d'un administrateur par défaut (login: admin / mdp: admin123) si aucun utilisateur n'existe
    INSERT INTO users (prenom, nom, telephone, role, login, password_hash, status, permissions)
    VALUES (
        'Admin', 
        'SYSTEM', 
        '0550000000', 
        'Administrateur', 
        'admin', 
        '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9', 
        'Actif', 
        '{"dash_view": true, "dash_financial": true, "reception_view": true, "reception_create": true, "reception_edit": true, "cash_collect": true, "cash_collect_rest": true, "cash_refund": true, "cash_remise": true, "cash_reports": true, "pat_view": true, "pat_create": true, "pat_edit": true, "pat_delete": true, "dme_view": true, "dme_consult": true, "dme_prescribe": true, "dme_demandes": true, "act_view": true, "act_manage": true, "ctype_view": true, "ctype_manage": true, "cr_view": true, "cr_manage": true, "med_view": true, "med_manage": true, "med_import_export": true, "doc_manage": true, "room_manage": true, "clinic_settings": true, "admin_users": true, "admin_perms": true}'::jsonb
    )
    ON CONFLICT (login) DO NOTHING;

ALTER TABLE users ADD COLUMN IF NOT EXISTS specialites_autorisees JSONB DEFAULT '[]'::jsonb;
ALTER TABLE users ADD COLUMN IF NOT EXISTS plannings_rdv_autorises JSONB;
ALTER TABLE users ADD COLUMN IF NOT EXISTS default_livret_cover TEXT DEFAULT '';
        
        -- 1. Table des sessions actives
    CREATE TABLE IF NOT EXISTS user_sessions (
        session_token TEXT PRIMARY KEY,
        user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL
    );

    -- 2. Table d'audit et traçabilité de toutes les actions
    CREATE TABLE IF NOT EXISTS audit_logs (
        id SERIAL PRIMARY KEY,
        user_id INT REFERENCES users(id) ON DELETE SET NULL,
        user_fullname TEXT NOT NULL,
        user_role TEXT NOT NULL,
        action_type TEXT NOT NULL,
        description TEXT NOT NULL,
        ip_address TEXT DEFAULT '',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
        
    -- Table des modèles de comptes rendus médicaux et radiologiques
    CREATE TABLE IF NOT EXISTS cr_templates (
        id SERIAL PRIMARY KEY,
        designation TEXT NOT NULL,
        type_modele TEXT NOT NULL DEFAULT 'Word',
        modalite TEXT NOT NULL DEFAULT 'IRM',
        service TEXT NOT NULL DEFAULT '',
        utilisateur TEXT DEFAULT 'admin',
        chemin TEXT DEFAULT '',
        contenu_html TEXT DEFAULT '',
        date_heure_ajout TEXT DEFAULT '',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );  
        
         CREATE TABLE IF NOT EXISTS patient_passages (
        id SERIAL PRIMARY KEY,
        id_patient INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        date_passage DATE NOT NULL DEFAULT CURRENT_DATE,
        heure_passage TEXT NOT NULL DEFAULT '',
        utilisateur TEXT DEFAULT 'admin',
        type_passage TEXT DEFAULT 'Consultation',
        actes_json JSONB DEFAULT '[]'::jsonb,
        montant_total NUMERIC DEFAULT 0.0,
        remise NUMERIC DEFAULT 0.0,
        remise_type TEXT DEFAULT 'DA',
        remise_valeur NUMERIC DEFAULT 0.0,
        accordeur_remise TEXT DEFAULT '',
        versement NUMERIC DEFAULT 0.0,
        reste NUMERIC DEFAULT 0.0,
        mode_paiement TEXT DEFAULT 'Espèces',
        statut_paiement TEXT DEFAULT 'Non payé',
        questionnaire_json JSONB DEFAULT '{}'::jsonb,
        documents_scannes JSONB DEFAULT '[]'::jsonb,
        statut TEXT DEFAULT 'En attente',
        medecin_traitant_id INT REFERENCES treating_doctors(id) ON DELETE SET NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS mwl_worklist_items (
        id SERIAL PRIMARY KEY,
        patient_id INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        passage_id INT NOT NULL REFERENCES patient_passages(id) ON DELETE CASCADE,
        act_index INT NOT NULL,
        accession_number TEXT NOT NULL DEFAULT '',
        requested_procedure_id TEXT NOT NULL DEFAULT '',
        procedure_description TEXT NOT NULL DEFAULT '',
        modality TEXT NOT NULL DEFAULT '',
        scheduled_date DATE NOT NULL DEFAULT CURRENT_DATE,
        scheduled_time TEXT NOT NULL DEFAULT '',
        station_ae_title TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'SCHEDULED',
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (passage_id, act_index)
    );
    CREATE INDEX IF NOT EXISTS idx_mwl_worklist_date_modality
        ON mwl_worklist_items (scheduled_date, modality, enabled);

    CREATE TABLE IF NOT EXISTS mwl_station_titles (
        id SERIAL PRIMARY KEY,
        ae_title TEXT NOT NULL UNIQUE,
        designation TEXT NOT NULL DEFAULT '',
        modality TEXT NOT NULL DEFAULT '',
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );

    -- Planning des rendez-vous, lié aux référentiels et au passage créé à l'accueil.
    CREATE TABLE IF NOT EXISTS appointments (
        id SERIAL PRIMARY KEY,
        id_patient INT REFERENCES patients(id) ON DELETE SET NULL,
        patient_nom_saisi TEXT NOT NULL DEFAULT '',
        id_praticien INT REFERENCES clinic_staff(id) ON DELETE SET NULL,
        specialite TEXT NOT NULL,
        type_examen_id INT REFERENCES consultation_types(id) ON DELETE SET NULL,
        type_examen TEXT NOT NULL DEFAULT '',
        salle_id INT REFERENCES rooms(id) ON DELETE SET NULL,
        date_rdv DATE NOT NULL,
        heure_debut TIME NOT NULL,
        heure_fin TIME NOT NULL,
        motif TEXT NOT NULL DEFAULT '',
        statut TEXT NOT NULL DEFAULT 'En attente'
            CHECK (statut IN ('Confirmé', 'En attente', 'En salle d''attente', 'Honoré', 'Annulé')),
        id_passage INT UNIQUE REFERENCES patient_passages(id) ON DELETE SET NULL,
        created_by TEXT DEFAULT '',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        CHECK (heure_fin > heure_debut)
    );
    ALTER TABLE appointments ADD COLUMN IF NOT EXISTS type_examen_id INT REFERENCES consultation_types(id) ON DELETE SET NULL;
    ALTER TABLE appointments ADD COLUMN IF NOT EXISTS type_examen TEXT NOT NULL DEFAULT '';
    ALTER TABLE appointments ALTER COLUMN id_patient DROP NOT NULL;
    ALTER TABLE appointments ADD COLUMN IF NOT EXISTS patient_nom_saisi TEXT NOT NULL DEFAULT '';
    CREATE INDEX IF NOT EXISTS idx_appointments_date_time
        ON appointments (date_rdv, heure_debut);
    CREATE INDEX IF NOT EXISTS idx_appointments_practitioner_date
        ON appointments (id_praticien, date_rdv, heure_debut);
    CREATE INDEX IF NOT EXISTS idx_appointments_room_date
        ON appointments (salle_id, date_rdv, heure_debut);

    -- Plannings hebdomadaires par spécialité : plusieurs segments par journée sont possibles.
    CREATE TABLE IF NOT EXISTS appointment_specialty_schedules (
        id SERIAL PRIMARY KEY,
        specialite TEXT NOT NULL,
        jour_semaine SMALLINT NOT NULL CHECK (jour_semaine BETWEEN 0 AND 6),
        jours_semaine SMALLINT[] NOT NULL DEFAULT ARRAY[0]::SMALLINT[],
        heure_debut TIME NOT NULL,
        heure_fin TIME NOT NULL,
        intervalle_minutes SMALLINT NOT NULL CHECK (intervalle_minutes BETWEEN 5 AND 240),
        capacite_patients SMALLINT NOT NULL DEFAULT 1 CHECK (capacite_patients BETWEEN 1 AND 500),
        actif BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        CHECK (heure_fin > heure_debut),
        UNIQUE (specialite, jour_semaine, heure_debut, heure_fin)
    );
    ALTER TABLE appointment_specialty_schedules
        ADD COLUMN IF NOT EXISTS jours_semaine SMALLINT[];
    UPDATE appointment_specialty_schedules
        SET jours_semaine=ARRAY[jour_semaine]::SMALLINT[]
        WHERE jours_semaine IS NULL;
    ALTER TABLE appointment_specialty_schedules
        ALTER COLUMN jours_semaine SET DEFAULT ARRAY[0]::SMALLINT[];
    ALTER TABLE appointment_specialty_schedules
        ALTER COLUMN jours_semaine SET NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_appointment_specialty_schedules_lookup
        ON appointment_specialty_schedules (specialite, jour_semaine, actif);
    CREATE INDEX IF NOT EXISTS idx_appointment_specialty_schedules_days_gin
        ON appointment_specialty_schedules USING GIN (jours_semaine);
        

-- Table d'archivage des consultations cliniques et du DME
    CREATE TABLE IF NOT EXISTS patient_dme_records (
        id SERIAL PRIMARY KEY,
        id_patient INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        id_passage INT NOT NULL REFERENCES patient_passages(id) ON DELETE CASCADE,
        praticien TEXT DEFAULT 'Dr',
        constantes_json JSONB DEFAULT '{}'::jsonb,
        motif TEXT DEFAULT '',
        examen_clinique TEXT DEFAULT '',
        diagnostic TEXT DEFAULT '',
        ordonnance_text TEXT DEFAULT '',
        remarques TEXT DEFAULT '',
        verrouille BOOLEAN NOT NULL DEFAULT TRUE,
        date_consultation TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS verrouille BOOLEAN NOT NULL DEFAULT TRUE;
    ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS allergies_intolerances TEXT DEFAULT '';
    ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS traitement_en_cours TEXT DEFAULT '';
    ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS antecedents_medicaux TEXT DEFAULT '';
    ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS antecedents_chirurgicaux TEXT DEFAULT '';
    ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS notes_habitudes TEXT DEFAULT '';
    ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS conduite_a_tenir TEXT DEFAULT '';
    

    
      CREATE TABLE IF NOT EXISTS patient_prescriptions (
        id SERIAL PRIMARY KEY,
        id_patient INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        id_passage INT NOT NULL REFERENCES patient_passages(id) ON DELETE CASCADE,
        id_consultation INT REFERENCES patient_dme_records(id) ON DELETE CASCADE,
        medecin_nom TEXT NOT NULL DEFAULT '',
        medecin_specialite TEXT DEFAULT '',
        lignes_json JSONB DEFAULT '[]'::jsonb,
        statut TEXT DEFAULT 'Valide',
        supprime_par TEXT DEFAULT '',
        date_suppression TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
    ALTER TABLE patient_prescriptions ADD COLUMN IF NOT EXISTS id_consultation INT REFERENCES patient_dme_records(id) ON DELETE CASCADE;
    CREATE INDEX IF NOT EXISTS idx_patient_prescriptions_scope ON patient_prescriptions (id_patient, id_passage, id_consultation);

    -- Consommations réelles de médicaments et dispositifs médicaux, liées à la consultation.
    CREATE TABLE IF NOT EXISTS patient_consumptions (
        id SERIAL PRIMARY KEY,
        id_patient INT NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        id_passage INT NOT NULL REFERENCES patient_passages(id) ON DELETE CASCADE,
        id_consultation INT REFERENCES patient_dme_records(id) ON DELETE SET NULL,
        type_produit TEXT NOT NULL CHECK (type_produit IN ('Médicament', 'Dispositif médical')),
        nom_produit TEXT NOT NULL,
        quantite NUMERIC(12,3) NOT NULL CHECK (quantite > 0),
        unite TEXT NOT NULL,
        salle_id INT REFERENCES rooms(id) ON DELETE SET NULL,
        service TEXT NOT NULL,
        utilisateur_id INT REFERENCES users(id) ON DELETE SET NULL,
        utilisateur TEXT NOT NULL DEFAULT '',
        lot TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        consomme_le TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_patient_consumptions_scope ON patient_consumptions (id_patient, id_passage, id_consultation);
    CREATE INDEX IF NOT EXISTS idx_patient_consumptions_reporting ON patient_consumptions (consomme_le, utilisateur, service, salle_id);

    -- Répertoire des entreprises et conventions de la filiale Kacel Job.
    CREATE TABLE IF NOT EXISTS kacel_job_companies (
        id SERIAL PRIMARY KEY,
        designation TEXT NOT NULL UNIQUE,
        adresse TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        telephone TEXT NOT NULL DEFAULT '',
        contact TEXT NOT NULL DEFAULT '',
        secteur_activite TEXT NOT NULL DEFAULT '',
        observation TEXT NOT NULL DEFAULT '',
        actif BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS kacel_job_conventions (
        id SERIAL PRIMARY KEY,
        entreprise_id INT NOT NULL REFERENCES kacel_job_companies(id) ON DELETE CASCADE,
        reference TEXT NOT NULL DEFAULT '',
        designation TEXT NOT NULL,
        date_debut DATE,
        date_fin DATE,
        statut TEXT NOT NULL DEFAULT 'Brouillon',
        conditions TEXT NOT NULL DEFAULT '',
        observation TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_kacel_job_conventions_company ON kacel_job_conventions (entreprise_id, date_fin);
    CREATE INDEX IF NOT EXISTS idx_kacel_job_conventions_status ON kacel_job_conventions (statut, date_fin);
    CREATE TABLE IF NOT EXISTS kacel_job_employees (
        id SERIAL PRIMARY KEY,
        entreprise_id INT NOT NULL REFERENCES kacel_job_companies(id) ON DELETE CASCADE,
        nom TEXT NOT NULL,
        prenom TEXT NOT NULL,
        date_naissance DATE,
        date_embauche DATE,
        poste TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_kacel_job_employees_company
        ON kacel_job_employees (entreprise_id, nom, prenom);
    ALTER TABLE kacel_job_companies ADD COLUMN IF NOT EXISTS representant TEXT NOT NULL DEFAULT '';
    ALTER TABLE kacel_job_companies ADD COLUMN IF NOT EXISTS rc TEXT NOT NULL DEFAULT '';
    ALTER TABLE kacel_job_companies ADD COLUMN IF NOT EXISTS nif TEXT NOT NULL DEFAULT '';
    ALTER TABLE kacel_job_companies ADD COLUMN IF NOT EXISTS art TEXT NOT NULL DEFAULT '';
    ALTER TABLE kacel_job_conventions ADD COLUMN IF NOT EXISTS effectif_contractuel INT NOT NULL DEFAULT 0;
    ALTER TABLE kacel_job_conventions ADD COLUMN IF NOT EXISTS taille_entreprise TEXT NOT NULL DEFAULT '';
    CREATE TABLE IF NOT EXISTS kacel_job_convention_tariffs (
        id SERIAL PRIMARY KEY,
        convention_id INT NOT NULL REFERENCES kacel_job_conventions(id) ON DELETE CASCADE,
        code TEXT NOT NULL,
        categorie TEXT NOT NULL DEFAULT '',
        designation TEXT NOT NULL,
        mode_tarif TEXT NOT NULL DEFAULT 'fixed',
        configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
        description TEXT NOT NULL DEFAULT '',
        ordre INT NOT NULL DEFAULT 0,
        personnalisee BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (convention_id, code)
    );
    CREATE INDEX IF NOT EXISTS idx_kacel_job_tariffs_convention
        ON kacel_job_convention_tariffs (convention_id, ordre, id);
    CREATE TABLE IF NOT EXISTS kacel_job_convention_categories (
        id SERIAL PRIMARY KEY,
        convention_id INT NOT NULL REFERENCES kacel_job_conventions(id) ON DELETE CASCADE,
        nom TEXT NOT NULL,
        ordre INT NOT NULL DEFAULT 0,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (convention_id, nom)
    );
    CREATE INDEX IF NOT EXISTS idx_kacel_job_categories_convention
        ON kacel_job_convention_categories (convention_id, ordre, id);

    """
    from modules.exam_requests_core import SCHEMA as EXAM_REQUESTS_SCHEMA
    query_schema += EXAM_REQUESTS_SCHEMA
    try:
        with get_db_cursor() as cur:
            cur.execute(query_schema)

            # Insertion en masse des spécialités médicales (sans écraser les existantes)
            query_insert_spe = """
                INSERT INTO ref_specialites (code, nom, description, tarif_base, tarif_nuit)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (nom) DO NOTHING;
            """
            cur.executemany(query_insert_spe, TOUTES_LES_SPECIALITES)

        logger.info("Base PostgreSQL synchronisée avec le catalogue complet des spécialités.")
    except Exception as err:
        logger.error("Erreur lors de l'initialisation du schéma : %s", err)



def record_audit(
    user_id: Optional[int],
    user_fullname: str,
    user_role: str,
    action_type: str,
    description: str,
    ip_address: str = "127.0.0.1"
) -> None:
    """Enregistre un événement dans la table d'audit PostgreSQL.

    Args:
        user_id (Optional[int]): ID de l'utilisateur (ou None si anonyme).
        user_fullname (str): Prénom et Nom de l'utilisateur.
        user_role (str): Rôle/Poste de l'utilisateur.
        action_type (str): Type d'événement (ex: CONNEXION, DECONNEXION, CREATION, MODIFICATION).
        description (str): Description détaillée de l'action réalisée.
        ip_address (str): Adresse IP source.
    """
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                INSERT INTO audit_logs 
                (user_id, user_fullname, user_role, action_type, description, ip_address)
                VALUES (%s, %s, %s, %s, %s, %s);
            """, (user_id, user_fullname, user_role, action_type, description, ip_address))
    except Exception as err:
        logger.error("Impossible d'écrire dans audit_logs : %s", err)
