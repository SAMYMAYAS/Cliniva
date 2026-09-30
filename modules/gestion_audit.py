"""Module de traçabilité et de consultation du journal d'audit.

Permet de suivre qui a fait quoi, quand et depuis quelle adresse IP.
Connecté à PostgreSQL, conforme PEP 8.
"""

from typing import Dict, Any, List
from fastapi import APIRouter
from database import get_db_cursor

audit_router = APIRouter(prefix="/api/audit", tags=["Journal d'Audit"])


class AuditManager:
    """Gestionnaire de consultation des traces d'audit."""

    @staticmethod
    def get_logs(limit: int = 150) -> List[Dict[str, Any]]:
        with get_db_cursor() as cursor:
            cursor.execute("""
                           SELECT id,
                                  user_fullname,
                                  user_role,
                                  action_type,
                                  description,
                                  ip_address,
                                  TO_CHAR(created_at, 'DD/MM/YYYY à HH24:MI:SS') AS date_formatee
                           FROM audit_logs
                           ORDER BY id DESC
                               LIMIT %s;
                           """, (limit,))
            return [dict(r) for r in cursor.fetchall()]


audit_manager = AuditManager()


@audit_router.get("")
async def api_get_audit_logs():
    return {"logs": audit_manager.get_logs()}


def render_html() -> str:
    """Génère l'interface complète du journal d'audit."""
    return """
    <div id="view-audit" class="space-y-6">

        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Traçabilité & Journal d'Audit Système</h1>
                <p class="text-xs text-slate-300">Historique chronologique et légal de toutes les actions réalisées sur l'application.</p>
            </div>
            <button onclick="loadAuditModule()" class="flex items-center gap-2 bg-brand-neon text-black px-4 py-2.5 rounded-xl font-bold text-xs shadow-md shadow-brand-neon/20 hover:opacity-90 transition">
                <i class="ph-bold ph-arrows-clockwise text-base"></i>
                <span>Actualiser les Traces</span>
            </button>
        </div>

        <!-- TABLEAU D'AUDIT -->
        <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col transition-colors overflow-x-auto">
            <table class="w-full text-left text-xs">
                <thead>
                    <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                        <th class="pb-3 pl-2">Date & Heure</th>
                        <th class="pb-3">Utilisateur</th>
                        <th class="pb-3">Rôle</th>
                        <th class="pb-3">Type d'Action</th>
                        <th class="pb-3">Description Détaillée</th>
                        <th class="pb-3 pr-2 text-right">Adresse IP</th>
                    </tr>
                </thead>
                <tbody id="audit-table-body" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300 font-medium"></tbody>
            </table>
        </div>

    </div>

    <script>
        (function() {
            window.loadAuditModule = async function() {
                try {
                    const res = await fetch('/api/audit');
                    const data = await res.json();
                    const tbody = document.getElementById('audit-table-body');
                    tbody.innerHTML = '';

                    if (!data.logs || data.logs.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="6" class="text-center py-6 text-slate-400">Aucune action enregistrée.</td></tr>';
                        return;
                    }

                    const actionStyles = {
                        'CONNEXION': 'bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300',
                        'DECONNEXION': 'bg-slate-100 text-slate-700 border-slate-200 dark:bg-black/30 dark:text-slate-300',
                        'CREATION': 'bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950/40 dark:text-blue-300',
                        'MODIFICATION': 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-300',
                        'SUPPRESSION': 'bg-rose-50 text-rose-700 border-rose-200 dark:bg-rose-950/40 dark:text-rose-300',
                        'EXPORTATION': 'bg-purple-50 text-purple-700 border-purple-200 dark:bg-purple-950/40 dark:text-purple-300',
                    };

                    data.logs.forEach(l => {
                        const tr = document.createElement('tr');
                        tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";

                        let badgeClass = actionStyles[l.action_type] || 'bg-slate-100 dark:bg-black/30 text-slate-700';

                        tr.innerHTML = `
                            <td class="py-3 pl-2 font-mono text-[11px] text-slate-400 font-bold">${l.date_formatee}</td>
                            <td class="py-3 font-bold text-slate-900 dark:text-white">${l.user_fullname}</td>
                            <td class="py-3"><span class="px-2 py-0.5 rounded-lg text-[10px] font-bold border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30">${l.user_role}</span></td>
                            <td class="py-3"><span class="px-2.5 py-1 rounded-xl text-[10px] font-bold border ${badgeClass}">${l.action_type}</span></td>
                            <td class="py-3 text-slate-600 dark:text-slate-300">${l.description}</td>
                            <td class="py-3 pr-2 text-right font-mono text-[11px] text-slate-400">${l.ip_address}</td>
                        `;
                        tbody.appendChild(tr);
                    });
                } catch(e) {
                    console.error("Erreur audit:", e);
                }
            };

            loadAuditModule();
        })();
    </script>
    """