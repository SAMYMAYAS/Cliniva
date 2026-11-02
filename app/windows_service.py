"""Hôte Windows Service du backend Cliniva, sans interface WebView."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
os.chdir(BASE_DIR)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError as exc:  # pragma: no cover - Windows service runtime only
    raise SystemExit("pywin32 est requis pour exécuter Cliniva comme service Windows.") from exc


class ClinivaBackendService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ClinivaBackend"
    _svc_display_name_ = "Cliniva Backend"
    _svc_description_ = "Backend Cliniva, Worklist DICOM et services configurés, sans interface graphique locale."

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server = None
        self.bridge = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        if self.server is not None:
            self.server.should_exit = True

    def SvcDoRun(self):
        servicemanager.LogInfoMsg("Démarrage du backend Cliniva sans interface locale.")
        try:
            self._run_backend()
        except Exception:
            servicemanager.LogErrorMsg("Échec du backend Cliniva :\n" + traceback.format_exc())
            raise

    def _run_backend(self):
        import uvicorn
        import main as cliniva

        cliniva.init_database()
        config = cliniva.load_app_configuration()
        if config.get("server_mode", "local") != "local":
            raise RuntimeError("Le service Windows Cliniva doit être installé sur un poste en mode serveur local.")

        self.bridge = cliniva.ClientDicomServicesBridge(config)
        if config.get("dicom_print_enabled"):
            result = self.bridge.dicom_print_start(config.get("dicom_print_config"))
            if not result.get("ok"):
                servicemanager.LogErrorMsg("DICOM Print non démarré : " + str(result.get("error")))
        if config.get("dicom_print_a3_enabled"):
            result = self.bridge.dicom_print_a3_start()
            if not result.get("ok"):
                servicemanager.LogErrorMsg("DICOM Print A3 non démarré : " + str(result.get("error")))
        if config.get("pacs_auto_start", config.get("pacs_enabled", False)):
            result = self.bridge.pacs_start(config.get("pacs_config"))
            if not result.get("ok"):
                servicemanager.LogErrorMsg("PACS non démarré : " + str(result.get("error")))

        uvicorn_config = uvicorn.Config(
            app=cliniva.app,
            host=str(config.get("local_host") or "0.0.0.0"),
            port=int(config.get("local_port") or 8000),
            log_level="info",
            access_log=False,
        )
        self.server = uvicorn.Server(uvicorn_config)
        try:
            self.server.run()
        finally:
            if self.bridge:
                self.bridge.dicom_services_shutdown()
                self.bridge = None
            self.server = None


if __name__ == "__main__":  # pragma: no cover - Windows service runtime only
    win32serviceutil.HandleCommandLine(ClinivaBackendService)
