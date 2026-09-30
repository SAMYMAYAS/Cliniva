"""Module de gestion des données et logique métier du tableau de bord."""

import logging
from typing import Optional
from app.models import DashboardData, MetricSummary, OperationRecord

logger = logging.getLogger(__name__)


class DashboardService:
    """Fournit les métriques cliniques pour l'application."""

    @staticmethod
    def get_dashboard_metrics() -> Optional[DashboardData]:
        """Retourne les données consolidées sans les modules retirés."""
        try:
            return DashboardData(
                new_patients=MetricSummary(
                    title="New Patients",
                    value=129,
                    sparkline=[20, 35, 25, 45, 60, 50, 80, 100],
                    theme="green",
                ),
                appointments=MetricSummary(
                    title="Appointments",
                    value=650,
                    sparkline=[15, 30, 45, 40, 55, 70, 85, 95],
                    theme="purple",
                ),
                earning=MetricSummary(
                    title="Earning",
                    value=201,
                    sparkline=[25, 40, 35, 50, 45, 75, 65, 90],
                    theme="cyan",
                ),
                operations=MetricSummary(
                    title="Operations",
                    value=54,
                    sparkline=[10, 20, 40, 30, 60, 55, 70, 85],
                    theme="orange",
                ),
                operation_records=[
                    OperationRecord(
                        patient_name="John Deo",
                        avatar_url="https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=100&h=100&fit=crop&crop=faces",
                        date_of_operation="12-08-2019",
                        duration="3 hours",
                        follow_up_date="12-09-2019",
                    ),
                    OperationRecord(
                        patient_name="Jens Brincker",
                        avatar_url="https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=100&h=100&fit=crop&crop=faces",
                        date_of_operation="14-08-2019",
                        duration="2 hours",
                        follow_up_date="14-09-2019",
                    ),
                ],
            )
        except Exception as error:
            logger.error("Erreur lors de la récupération des données : %s", error, exc_info=True)
            return None