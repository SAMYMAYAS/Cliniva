"""Processus isolé pour les transferts C-STORE sortants du PACS Cliniva."""

from __future__ import annotations

import logging
from typing import Any


def run_pacs_transfer(config: dict[str, Any], node_id: int, kind: str,
                     record_id: int, progress_queue: Any) -> None:
    """Exécute un transfert et publie les étapes dans la file du processus parent."""
    try:
        from app.pacs_server import PacsServer

        service = PacsServer(config)

        def report(state: dict[str, Any]) -> None:
            progress_queue.put({"type": "progress", "state": state})

        result = service.send_to_node(node_id, kind, record_id, report)
        progress_queue.put({"type": "complete", "result": result})
    except BaseException as exc:
        logging.getLogger("ClinivaPacsTransferWorker").exception(
            "Échec du processus de transfert DICOM"
        )
        try:
            progress_queue.put({"type": "error", "error": str(exc)})
        except Exception:
            pass
