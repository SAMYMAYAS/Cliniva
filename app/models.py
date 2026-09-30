"""Module définissant les modèles de données Pydantic pour Cliniva.

Structures légères et typées pour l'injection dans le dashboard.
Conforme strictement à PEP 8.
"""

from typing import List
from pydantic import BaseModel, Field


class MetricSummary(BaseModel):
    """Représente les cartes métriques du haut du tableau de bord."""

    title: str = Field(..., description="Titre de la métrique")
    value: int = Field(..., ge=0, description="Valeur numérique")
    sparkline: List[int] = Field(..., description="Données du mini-graphique")
    theme: str = Field(default="green", description="Couleur d'accent")


class OperationRecord(BaseModel):
    """Enregistrement d'une opération chirurgicale pour le tableau inférieur."""

    patient_name: str
    avatar_url: str
    date_of_operation: str
    duration: str
    follow_up_date: str


class DashboardData(BaseModel):
    """Structure racine regroupant les données du tableau de bord."""

    new_patients: MetricSummary
    appointments: MetricSummary
    earning: MetricSummary
    operations: MetricSummary
    operation_records: List[OperationRecord]