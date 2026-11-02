"""DICOM Print SCP local au poste Cliniva Desktop.

Le service reçoit les commandes DICOM Print Management et conserve les
images reçues sur le poste qui exécute Cliniva. Il n'est jamais démarré par
le backend distant.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("ClinivaDicomPrint")


def _safe_component(value: Any, fallback: str = "Unknown") -> str:
    text = str(value or "").strip()
    if not text or text.upper() in {"N/A", "UNKNOWN"}:
        return fallback
    text = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", "_", text).strip("._ ")
    return text[:100] or fallback


def _safe_folder_label(value: Any, fallback: str = "Patient inconnu") -> str:
    text = str(value or "").strip()
    text = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(". ")
    text = text[:90].rstrip(". ")
    if not text or text in {".", ".."}:
        return fallback
    # Windows also reserves device names, even when they have an extension.
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", text):
        text = f"_{text}"
    return text


def _normalize_exam_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return datetime.now().strftime("%Y-%m-%d")
    for fmt in ("%Y%m%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d %Y", "%B %d %Y",
                "%b %d, %Y", "%B %d, %Y", "%b%d%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return _safe_folder_label(text, datetime.now().strftime("%Y-%m-%d"))


def _value(dataset, tag, default=None):
    element = dataset.get(tag) if dataset is not None else None
    return element.value if hasattr(element, "value") else (element if element is not None else default)


def _consensus_print_total(manifest: dict[str, Any], incoming_ocr: dict[str, Any]) -> int | None:
    """Écarte les faux totaux OCR en retenant la valeur la plus répétée du job."""
    totals: list[int] = []
    for entry in manifest.get("images", []):
        if not isinstance(entry, dict):
            continue
        data = entry.get("ocr") if isinstance(entry.get("ocr"), dict) else {}
        value = data.get("total_images") or entry.get("total_images")
        if str(value or "").isdigit() and 0 < int(value) <= 999:
            totals.append(int(value))
    value = str(incoming_ocr.get("total_images") or "").strip()
    if value.isdigit() and 0 < int(value) <= 999:
        totals.append(int(value))
    if not totals:
        return None
    counts = {value: totals.count(value) for value in set(totals)}
    # En cas d’égalité, retenir le plus petit total plausible : les faux
    # positifs OCR tendent à ajouter un chiffre venant des annotations voisines.
    return min(counts, key=lambda number: (-counts[number], number))


def _extract_print_image_numbers(frame) -> tuple[str, str]:
    """Lit le compteur centré sous le bandeau (ex. 1 / 5) comme dans pri.py."""
    import cv2
    import pytesseract

    height, width = frame.shape[:2]
    # Ne lit que le compteur situé au centre sous le bandeau. Les anciens
    # cadrages larges incluaient aussi les chiffres des annotations médicales
    # et pouvaient transformer « 4 / 4 » en « 4 / 7 ».
    crop = frame[int(height * .057):int(height * .088),
                 int(width * .43):int(width * .57)]
    if crop.size == 0:
        return "", ""
    resized = cv2.resize(crop, None, fx=8, fy=8, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY) if len(resized.shape) == 3 else resized
    normalized = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    # Le bandeau de cette station est clair avec des caractères noirs.
    # Garder le texte noir sur fond blanc (l’inversion effaçait le compteur).
    _, fixed = cv2.threshold(normalized, 200, 255, cv2.THRESH_BINARY)
    _, otsu = cv2.threshold(normalized, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    for processed in (fixed, otsu):
        padded = cv2.copyMakeBorder(processed, 30, 30, 40, 40,
                                    cv2.BORDER_CONSTANT, value=255)
        for psm in (7, 8):
            raw = pytesseract.image_to_string(
                padded, lang="eng",
                config=f"--oem 1 --psm {psm} -c tessedit_char_whitelist=0123456789/|-lISsO"
            ).strip()
            clean = (raw.replace("l", "1").replace("I", "1").replace("|", "/")
                     .replace("\\", "/").replace("S", "5").replace("s", "5")
                     .replace("O", "0").replace("o", "0"))
            found = re.search(r"(?<!\d)(\d{1,3})\s*[/\-]\s*(\d{1,3})(?!\d)", clean)
            if not found:
                found = re.search(r"(?<!\d)(\d{1,3})\s+(\d{1,3})(?!\d)", clean)
            if found:
                current, total = map(int, found.groups())
                if 0 < current <= total <= 999:
                    return str(current), str(total)
    return "", ""


def _ocr_print_page(image) -> dict[str, str]:
    """Utilise les zones et prétraitements OCR validés dans l’ancien pri.py."""
    values = {key: "" for key in (
        "patient_name", "patient_id", "center", "order_number", "passage_number",
        "sex", "date_of_birth", "exam_date", "current_image", "total_images",
    )}
    try:
        import cv2
        import numpy as np
        import pytesseract

        local_app_data = os.environ.get("LOCALAPPDATA", "")
        candidates = (shutil.which("tesseract"),
                      os.path.join(local_app_data, "Programs", "Tesseract-OCR", "tesseract.exe")
                      if local_app_data else None,
                      r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                      r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")
        tesseract_path = next((item for item in candidates if item and os.path.isfile(item)), None)
        if tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
        frame = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
        height, width = frame.shape[:2]

        # pri.py montre que le bandeau utile s'arrête à 6,5 % de l'image.
        left = frame[:max(1, int(height * .065)), :int(width * .55)]
        right = frame[:max(1, int(height * .065)), int(width * .45):]
        left = cv2.resize(left, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        right = cv2.resize(right, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        left_gray = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
        right_gray = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)
        _, left_binary = cv2.threshold(left_gray, 120, 255, cv2.THRESH_BINARY_INV)
        _, right_binary = cv2.threshold(right_gray, 120, 255, cv2.THRESH_BINARY_INV)
        left_text = pytesseract.image_to_string(left_binary, lang="eng", config="--psm 6")
        right_text = pytesseract.image_to_string(right_binary, lang="eng", config="--psm 6")

        left_lines = [line.strip() for line in left_text.splitlines() if line.strip()]
        if left_lines:
            name = re.sub(r"[^A-Za-z\s_-]", "", left_lines[0]).strip()
            values["patient_name"] = (name or left_lines[0]).strip()[:100]
        passage = re.search(r"\b(Pas[-\s]?\d+)\b", left_text, re.IGNORECASE)
        if passage:
            values["passage_number"] = re.sub(r"\s+", "", passage.group(1))
            values["patient_id"] = values["passage_number"]
        order = re.search(r"Num\.?\s*Ordre\s*[:;.]?\s*(\d+)", right_text, re.IGNORECASE)
        if order:
            values["order_number"] = order.group(1)
        exam_date = re.search(
            r"Date\s*[:;.]?\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
            right_text, re.IGNORECASE)
        if not exam_date:
            exam_date = re.search(r"\b([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b",
                                  right_text, re.IGNORECASE)
        if exam_date:
            values["exam_date"] = exam_date.group(1).strip()
        sex = re.search(r"Sex[e]?\s*[:;.\s]*([MF])\b", left_text, re.IGNORECASE)
        if sex:
            values["sex"] = sex.group(1).upper()
        dob = re.search(r"(?:naissance|DOB|Birth)\s*[:;.\s]*([A-Za-z]{3}\s*\d{1,2}\s*\d{4})",
                        left_text, re.IGNORECASE)
        if dob:
            values["date_of_birth"] = dob.group(1).strip()

        # Le nom du centre n'a pas de libellé sur cette station : première
        # ligne de la zone droite (KACEL MEDICAL GROUPE dans l'image fournie).
        right_lines = [line.strip() for line in right_text.splitlines() if line.strip()]
        if right_lines:
            center = re.sub(r"[^A-Za-z0-9&.' -]", "", right_lines[0]).strip()
            if center and not re.search(r"(?i)num|ordre|date", center):
                values["center"] = center[:100]

        values["current_image"], values["total_images"] = _extract_print_image_numbers(frame)
        return values
    except Exception as exc:
        logger.warning("OCR DICOM Print indisponible ou échoué : %s", exc)
        return values


class DicomPrintServer:
    """Contrôleur thread-safe du récepteur d'impression DICOM local."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._server = None
        self._config = {"ae_title": "MY_PRINT_SCP", "host": "0.0.0.0", "port": 104,
                        "storage_dir": str(Path.home() / "DicomPrints"),
                        "auto_print_enabled": False, "auto_print_printer": "", "auto_print_paper": "A3"}
        self._pending_print_paths: dict[int, list[Path]] = {}
        self._last_error = ""

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self._server is not None, "config": dict(self._config),
                    "last_error": self._last_error}

    def start(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._server is not None:
                return self.status()
            config = dict(self._config)
            if settings:
                config.update(settings)
            ae_title = str(config.get("ae_title", "MY_PRINT_SCP")).strip().upper()
            host = str(config.get("host", "0.0.0.0")).strip() or "0.0.0.0"
            try:
                port = int(config.get("port", 104))
            except (TypeError, ValueError) as exc:
                raise ValueError("Le port DICOM doit être un nombre entre 1 et 65535.") from exc
            if not re.fullmatch(r"[A-Z0-9_ -]{1,16}", ae_title):
                raise ValueError("L’AE Title doit contenir de 1 à 16 caractères (A-Z, 0-9, espace, _ ou -).")
            if not 1 <= port <= 65535:
                raise ValueError("Le port DICOM doit être compris entre 1 et 65535.")
            storage_dir = Path(str(config.get("storage_dir") or "")).expanduser()
            if not storage_dir.is_absolute():
                raise ValueError("Le dossier de réception doit être un chemin absolu sur ce poste client.")
            storage_dir.mkdir(parents=True, exist_ok=True)

            try:
                from pynetdicom import AE, evt
                from pynetdicom.sop_class import (
                    BasicColorImageBox, BasicColorPrintManagementMeta, BasicFilmBox,
                    BasicFilmSession, BasicGrayscaleImageBox,
                    BasicGrayscalePrintManagementMeta, Printer, Verification,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "Dépendances DICOM Print manquantes sur ce poste client. "
                    "Installez-les avec : py -m pip install pynetdicom pydicom numpy Pillow"
                ) from exc

            from pydicom.dataset import Dataset
            from pydicom.sequence import Sequence

            def echo(_event):
                logger.info("C-ECHO reçu")
                return 0x0000

            def printer_status(_event):
                response = Dataset()
                response.PrinterStatus = "NORMAL"
                response.PrinterStatusInfo = "NORMAL"
                response.PrinterName = ae_title
                return 0x0000, response

            def create(event):
                request = event.request
                attributes = event.attribute_list or Dataset()
                response = Dataset()
                for element in attributes:
                    response.add(element)
                instance_uid = getattr(request, "AffectedSOPInstanceUID", None) or str(_value(attributes, (0x0008, 0x0018), "") or "")
                if not instance_uid:
                    from pydicom.uid import generate_uid
                    instance_uid = generate_uid()
                response.SOPInstanceUID = instance_uid
                response.AffectedSOPInstanceUID = instance_uid
                if request.AffectedSOPClassUID == BasicFilmBox:
                    color = event.context.abstract_syntax == BasicColorPrintManagementMeta
                    image_box_class = BasicColorImageBox if color else BasicGrayscaleImageBox
                    reference = Dataset()
                    reference.ReferencedSOPClassUID = image_box_class
                    from pydicom.uid import generate_uid
                    reference.ReferencedSOPInstanceUID = generate_uid()
                    response.ReferencedImageBoxSequence = Sequence([reference])
                return 0x0000, response

            def set_image(event):
                attributes = event.attribute_list or Dataset()
                sequence = attributes.get((0x2020, 0x0111)) or attributes.get((0x2020, 0x0110))
                if not sequence or not sequence.value:
                    logger.warning("N-SET reçu sans séquence d’image exploitable")
                    return 0x0110, Dataset()
                pixels = sequence.value[0]
                try:
                    saved_path = self._save_print_image(pixels, attributes, storage_dir)
                    with self._lock:
                        self._pending_print_paths.setdefault(id(event.assoc), []).append(saved_path)
                    logger.info("Image DICOM Print reçue : %s", saved_path)
                    return 0x0000, Dataset()
                except Exception:
                    logger.exception("Échec de sauvegarde de l’image reçue par DICOM Print")
                    # Ne pas acquitter une image qui n'a pas été conservée.
                    return 0x0110, Dataset()

            def action(event):
                logger.info("N-ACTION d’impression reçue")
                with self._lock:
                    paths = self._pending_print_paths.pop(id(event.assoc), [])
                    enabled = bool(self._config.get("auto_print_enabled"))
                    printer_name = str(self._config.get("auto_print_printer") or "").strip()
                if enabled and paths:
                    if not printer_name:
                        logger.error("Impression automatique demandée, mais aucune imprimante A3 n’est configurée.")
                    else:
                        threading.Thread(target=self._print_paths_a3, args=(paths, printer_name),
                                         name="ClinivaDprintAutoPrint", daemon=True).start()
                return 0x0000, Dataset()

            def delete(_event):
                logger.info("N-DELETE reçu : ressources du film libérées")
                return 0x0000

            def association_ended(event):
                with self._lock:
                    self._pending_print_paths.pop(id(event.assoc), None)

            ae = AE(ae_title=ae_title.encode("ascii"))
            for context in (Verification, BasicGrayscalePrintManagementMeta,
                            BasicColorPrintManagementMeta, BasicFilmSession,
                            BasicFilmBox, BasicGrayscaleImageBox, BasicColorImageBox, Printer):
                ae.add_supported_context(context)
            handlers = [(evt.EVT_C_ECHO, echo), (evt.EVT_N_GET, printer_status),
                        (evt.EVT_N_CREATE, create), (evt.EVT_N_SET, set_image),
                        (evt.EVT_N_ACTION, action), (evt.EVT_N_DELETE, delete)]
            for event_name in ("EVT_RELEASED", "EVT_ABORTED"):
                event_code = getattr(evt, event_name, None)
                if event_code is not None:
                    handlers.append((event_code, association_ended))
            try:
                self._server = ae.start_server((host, port), block=False, evt_handlers=handlers)
            except Exception as exc:
                self._last_error = str(exc)
                raise RuntimeError(f"Impossible de démarrer DICOM Print sur {host}:{port} : {exc}") from exc
            self._config = {**config, "ae_title": ae_title, "host": host, "port": port,
                            "storage_dir": str(storage_dir), "auto_print_paper": "A3"}
            self._last_error = ""
            return self.status()

    @staticmethod
    def available_printers() -> list[dict[str, Any]]:
        """Liste les imprimantes installées sur le poste qui héberge ce récepteur."""
        if os.name != "nt":
            return []
        try:
            import win32print
        except ImportError as exc:
            raise RuntimeError("pywin32 est nécessaire pour détecter les imprimantes Windows.") from exc
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        printers = win32print.EnumPrinters(flags, None, 2)
        default_name = win32print.GetDefaultPrinter() if printers else ""
        result = []
        for printer in printers:
            name = str(printer.get("pPrinterName") or "").strip()
            if name:
                papers = None
                try:
                    import win32con
                    paper_ids = win32print.DeviceCapabilities(
                        name, str(printer.get("pPortName") or ""), win32con.DC_PAPERS)
                    papers = [int(item) for item in paper_ids] if paper_ids else []
                except Exception:
                    pass
                result.append({"name": name, "default": name == default_name,
                               "supports_a3": None if papers is None else 8 in papers})
        return sorted(result, key=lambda item: (not item["default"], item["name"].casefold()))

    def configure_auto_print(self, enabled: bool, printer_name: str = "") -> dict[str, Any]:
        printer_name = str(printer_name or "").strip()
        if enabled:
            available = {item["name"] for item in self.available_printers()}
            if not printer_name or printer_name not in available:
                raise ValueError("Choisissez une imprimante actuellement installée sur ce poste.")
            details = next(item for item in self.available_printers() if item["name"] == printer_name)
            if details.get("supports_a3") is False:
                raise ValueError("L’imprimante sélectionnée ne déclare pas le format A3 dans son pilote Windows.")
        with self._lock:
            self._config["auto_print_enabled"] = bool(enabled)
            self._config["auto_print_printer"] = printer_name if enabled else ""
            self._config["auto_print_paper"] = "A3"
            return self.status()

    @staticmethod
    def _print_paths_a3(paths: list[Path], printer_name: str) -> None:
        """Imprime les images du N-ACTION dans un seul travail Windows au format A3."""
        if os.name != "nt":
            logger.error("Impression DICOM automatique disponible uniquement sous Windows.")
            return
        dc = None
        try:
            import win32con
            import win32print
            import win32ui
            from PIL import Image, ImageWin

            printer_info = next((item for item in DicomPrintServer.available_printers()
                                 if item["name"] == printer_name), None)
            if not printer_info:
                raise RuntimeError(f"L’imprimante configurée « {printer_name} » n’est plus disponible.")
            handle = win32print.OpenPrinter(printer_name)
            try:
                devmode = win32print.GetPrinter(handle, 2).get("pDevMode")
                if devmode is None:
                    raise RuntimeError("Windows n’a pas fourni les réglages du pilote d’imprimante.")
                devmode.PaperSize = win32con.DMPAPER_A3
                devmode.Orientation = win32con.DMORIENT_PORTRAIT
                devmode.Fields |= win32con.DM_PAPERSIZE | win32con.DM_ORIENTATION
                dc = win32ui.CreateDC()
                dc.CreateDC("WINSPOOL", printer_name, None, devmode)
            finally:
                win32print.ClosePrinter(handle)
            width = dc.GetDeviceCaps(win32con.HORZRES)
            height = dc.GetDeviceCaps(win32con.VERTRES)
            if width < 1 or height < 1:
                raise RuntimeError("Le pilote de l’imprimante ne fournit pas de zone A3 imprimable.")
            dc.StartDoc(f"Cliniva DICOM Print - {len(paths)} image(s)")
            for path in sorted(paths, key=lambda item: item.stat().st_mtime_ns):
                with Image.open(path) as source:
                    image = source.convert("RGB")
                    image.thumbnail((width, height), Image.Resampling.LANCZOS)
                    left, top = (width - image.width) // 2, (height - image.height) // 2
                    dib = ImageWin.Dib(image)
                    dc.StartPage()
                    dib.draw(dc.GetHandleOutput(), (left, top, left + image.width, top + image.height))
                    dc.EndPage()
            dc.EndDoc()
            logger.info("Impression automatique terminée : %d image(s) sur %s (A3)", len(paths), printer_name)
        except Exception:
            logger.exception("Échec de l’impression automatique DICOM vers %s", printer_name)
            if dc is not None:
                try:
                    dc.AbortDoc()
                except Exception:
                    pass
        finally:
            if dc is not None:
                try:
                    dc.DeleteDC()
                except Exception:
                    pass

    @staticmethod
    def _save_print_image(pixel_dataset, attributes, storage_dir: Path) -> Path:
        import numpy as np
        from PIL import Image

        rows = int(_value(pixel_dataset, (0x0028, 0x0010), _value(attributes, (0x0028, 0x0010), 0)))
        columns = int(_value(pixel_dataset, (0x0028, 0x0011), _value(attributes, (0x0028, 0x0011), 0)))
        pixel_bytes = _value(pixel_dataset, (0x7FE0, 0x0010))
        if pixel_bytes is None or rows < 1 or columns < 1 or rows * columns > 150_000_000:
            raise ValueError("Dimensions ou données Pixel Data invalides.")
        samples = int(_value(pixel_dataset, (0x0028, 0x0002), _value(attributes, (0x0028, 0x0002), 1)))
        bits = int(_value(pixel_dataset, (0x0028, 0x0100), _value(attributes, (0x0028, 0x0100), 8)))
        planar = int(_value(pixel_dataset, (0x0028, 0x0006), _value(attributes, (0x0028, 0x0006), 0)))
        photometric = str(_value(pixel_dataset, (0x0028, 0x0004), _value(attributes, (0x0028, 0x0004), "RGB"))).upper()
        if bits not in (8, 16) or samples not in (1, 3):
            raise ValueError(f"Format Pixel Data non pris en charge ({bits} bits, {samples} composantes).")
        dtype = np.dtype("<u2" if bits == 16 else "u1")
        array = np.frombuffer(pixel_bytes, dtype=dtype)
        expected = rows * columns * samples
        if expected > 150_000_000:
            raise ValueError("Image reçue trop volumineuse pour être décodée en mémoire.")
        if array.size < expected:
            raise ValueError("Pixel Data tronqué.")
        array = array[:expected]
        if samples == 3:
            if planar == 1:
                array = array.reshape((3, rows, columns)).transpose((1, 2, 0))
            else:
                array = array.reshape((rows, columns, 3))
            if bits == 16:
                array = (array >> 8).astype(np.uint8)
            if "YBR" in photometric:
                image = Image.fromarray(array.astype(np.uint8), mode="YCbCr").convert("RGB")
            else:
                image = Image.fromarray(array.astype(np.uint8), mode="RGB")
        else:
            array = array.reshape((rows, columns))
            if bits == 16:
                low, high = int(array.min()), int(array.max())
                array = ((array.astype(np.float32) - low) * (255 / max(1, high - low))).astype(np.uint8)
            if photometric == "MONOCHROME1":
                array = 255 - array.astype(np.uint8)
            image = Image.fromarray(array.astype(np.uint8), mode="L")

        aspect_ratio = _value(pixel_dataset, (0x0028, 0x0034), _value(attributes, (0x0028, 0x0034)))
        if aspect_ratio and len(aspect_ratio) >= 2:
            try:
                vertical, horizontal = int(aspect_ratio[0]), int(aspect_ratio[1])
                if vertical > 0 and horizontal > 0 and vertical != horizontal:
                    image = image.resize((columns, max(1, int(round(rows * vertical / horizontal)))), Image.Resampling.LANCZOS)
            except (TypeError, ValueError):
                logger.debug("Rapport d’aspect DICOM Print ignoré : %s", aspect_ratio)

        ocr = _ocr_print_page(image)
        now = datetime.now()
        dicom_name = str(_value(attributes, (0x0010, 0x0010), "") or "").replace("^", " ").strip()
        patient_name = (ocr.get("patient_name") or dicom_name or "Patient inconnu").strip()
        patient_id = str(ocr.get("patient_id") or _value(attributes, (0x0010, 0x0020), "") or "").strip()
        accession_number = str(ocr.get("order_number") or _value(attributes, (0x0008, 0x0050), "") or "").strip()
        center = str(ocr.get("center") or _value(attributes, (0x0008, 0x0080), "") or "").strip()
        passage_number = str(ocr.get("passage_number")
                             or _value(attributes, (0x0040, 0x1001), "") or "").strip()
        exam_date_raw = (ocr.get("exam_date")
                         or _value(attributes, (0x0008, 0x0020), "")
                         or _value(attributes, (0x0008, 0x0023), "")
                         or now.strftime("%Y%m%d"))
        exam_date = _normalize_exam_date(exam_date_raw)
        date_folder = storage_dir / _safe_folder_label(exam_date, now.strftime("%Y-%m-%d"))
        date_folder.mkdir(parents=True, exist_ok=True)
        identity_value = accession_number or patient_id or patient_name
        group_key = "|".join((exam_date, identity_value.casefold(), patient_id.casefold()))
        total_text = str(ocr.get("total_images") or "").strip()
        total_count = int(total_text) if total_text.isdigit() and int(total_text) > 0 else None
        current_text = str(ocr.get("current_image") or "").strip()
        current_index = int(current_text) if current_text.isdigit() else None

        # Un patient peut avoir plusieurs travaux d'impression le même jour,
        # avec le même numéro d'ordre. Ne jamais reprendre un dossier qui a
        # déjà reçu son quota, ni un ancien dossier lorsqu'un nouveau travail
        # recommence à la page 1.
        candidates: list[tuple[datetime, int, int, Path, dict[str, Any]]] = []
        for candidate in date_folder.iterdir():
            manifest_path = candidate / "cliniva_print.json"
            if not candidate.is_dir() or not manifest_path.is_file():
                continue
            try:
                candidate_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            same_patient = (
                (patient_id and str(candidate_manifest.get("patient_id") or "").casefold() == patient_id.casefold())
                or (patient_name.casefold() != "patient inconnu"
                    and str(candidate_manifest.get("patient_name") or "").casefold() == patient_name.casefold())
            )
            same_order_or_missing = (
                str(candidate_manifest.get("order_number") or "").casefold() == accession_number.casefold()
                or not accession_number or not candidate_manifest.get("order_number")
            )
            if not (candidate_manifest.get("group_key") == group_key or (same_patient and same_order_or_missing)):
                continue

            actual_count = len(list(candidate.glob("Image_*.png")))
            candidate_total = candidate_manifest.get("total_images")
            try:
                candidate_total = int(candidate_total) if candidate_total else None
            except (TypeError, ValueError):
                candidate_total = None
            if candidate_total and actual_count >= candidate_total:
                continue

            image_numbers = [
                int(match.group(1))
                for path in candidate.glob("Image_*.png")
                if (match := re.match(r"Image_(\d+)_", path.name))
            ]
            highest_page = max(image_numbers, default=0)
            # Le compteur OCR constitue une frontière de travail : une nouvelle
            # page 1 ne doit pas être ajoutée à un dossier déjà commencé.
            if current_index == 1 and actual_count:
                continue
            # Pour les pages suivantes, rattacher au dossier dont la dernière
            # page précède le mieux la page reçue. Cela évite de revenir sur un
            # ancien dossier incomplet du même patient.
            if current_index and current_index > 1 and highest_page >= current_index:
                continue
            try:
                updated = datetime.strptime(
                    str(candidate_manifest.get("updated_at") or ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                updated = datetime.fromtimestamp(candidate.stat().st_mtime)
            candidates.append((updated, highest_page, candidate.stat().st_mtime_ns,
                               candidate, candidate_manifest))

        folder = None
        manifest: dict[str, Any] = {}
        if candidates:
            # Continuer le travail le plus récent, puis celui dont la séquence
            # de pages est la plus avancée.
            _, _, _, folder, manifest = max(
                candidates, key=lambda item: (item[0], item[1], item[2]))

        if folder is None:
            count_label = str(total_count) if total_count else "nombre inconnu"
            label = _safe_folder_label(f"{patient_name} - {count_label} images")
            folder = date_folder / label
            if folder.exists():
                label = _safe_folder_label(f"{label} - {_safe_folder_label(accession_number, 'Examen')}")
                folder = date_folder / label
            duplicate_number = 2
            while folder.exists():
                folder = date_folder / _safe_folder_label(f"{label} ({duplicate_number})")
                duplicate_number += 1
            folder.mkdir(parents=True, exist_ok=True)
            manifest = {"group_key": group_key, "patient_name": patient_name,
                        "patient_id": patient_id, "exam_date": exam_date,
                        "center": center, "order_number": accession_number,
                        "passage_number": passage_number, "total_images": total_count,
                        "images": []}
        manifest["patient_name"] = patient_name or manifest.get("patient_name") or "Patient inconnu"
        manifest["patient_id"] = patient_id or manifest.get("patient_id", "")
        manifest["exam_date"] = exam_date or manifest.get("exam_date", "")
        manifest["order_number"] = accession_number or manifest.get("order_number", "")
        manifest["center"] = center or manifest.get("center", "")
        manifest["passage_number"] = passage_number or manifest.get("passage_number", "")
        # Le total OCR d’une page peut être confondu avec un chiffre voisin
        # (par ex. 4/4 lu 4/7). Le total du dossier est donc décidé par vote
        # entre les compteurs lus sur toutes ses pages, pas par la dernière page.
        consensus_total = _consensus_print_total(manifest, ocr)
        manifest["total_images"] = consensus_total or manifest.get("total_images")
        if any(str(ocr.get(key) or "").strip() for key in (
                "patient_name", "center", "order_number", "passage_number", "total_images")):
            manifest["ocr_version"] = 3
        else:
            manifest.setdefault("ocr_version", 0)
        existing_images = sorted(folder.glob("Image_*.png"))
        known_indices = [int(match.group(1)) for path in existing_images
                         if (match := re.match(r"Image_(\d+)_", path.name))]
        if current_index is None:
            current_index = max(known_indices, default=0) + 1
        total_for_name = manifest.get("total_images")
        total_part = f"sur_{int(total_for_name):03d}" if total_for_name else "sur_inconnu"
        base = f"Image_{current_index:03d}_{total_part}"
        target = folder / f"{base}.png"
        suffix = 1
        while target.exists():
            target = folder / f"{base}_{suffix}.png"
            suffix += 1
        image.save(target, format="PNG")

        image_entry = {"name": target.name, "current_image": current_index,
                       "total_images": total_count or manifest.get("total_images"),
                       "received_at": now.strftime("%Y-%m-%d %H:%M:%S"), "ocr": ocr}
        manifest.setdefault("images", []).append(image_entry)
        consensus_total = _consensus_print_total(manifest, {})
        if consensus_total:
            manifest["total_images"] = consensus_total
            for entry in manifest["images"]:
                if isinstance(entry, dict):
                    entry["total_images"] = consensus_total
            corrected_folder = folder.parent / _safe_folder_label(
                f"{manifest['patient_name']} - {consensus_total} images")
            if corrected_folder != folder and not corrected_folder.exists():
                folder.rename(corrected_folder)
                folder = corrected_folder
                target = folder / target.name
        manifest["received_images"] = len(list(folder.glob("Image_*.png")))
        manifest["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        (folder / "cliniva_print.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["=== INFORMATIONS DE L’EXAMEN (OCR / DICOM) ===",
                 f"Patient : {manifest['patient_name']}",
                 f"Centre : {manifest.get('center') or 'Non reconnu'}",
                 f"Identifiant patient : {manifest.get('patient_id') or 'Non reconnu'}",
                 f"Date de l’examen : {manifest.get('exam_date') or 'Non reconnue'}",
                 f"Numéro d’ordre / Accession Number : {manifest.get('order_number') or 'Non reconnu'}",
                 f"Passage : {manifest.get('passage_number') or 'Non reconnu'}",
                 f"Images reçues : {manifest['received_images']}/{manifest.get('total_images') or '?'}",
                 f"Dernière page reconnue : {current_index}/{manifest.get('total_images') or '?'}",
                 f"Dernière réception : {manifest['updated_at']}"]
        (folder / "Informations_examen.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target

    def stop(self) -> dict[str, Any]:
        with self._lock:
            server = self._server
            if server is not None:
                server.shutdown()
                self._server = None
            return self.status()

    def _resolve_group(self, group_id: str) -> tuple[Path, Path]:
        with self._lock:
            root = Path(self._config["storage_dir"]).expanduser().resolve()
        folder = (root / str(group_id)).resolve()
        try:
            folder.relative_to(root)
        except ValueError as exc:
            raise ValueError("Dossier d’impression invalide.") from exc
        if not folder.is_dir():
            raise ValueError("Dossier d’impression introuvable.")
        return root, folder

    def delete_group(self, group_id: str) -> dict[str, Any]:
        """Supprime un seul dossier d’examen situé sous le répertoire DICOM Print."""
        import shutil

        root, folder = self._resolve_group(group_id)
        if folder == root:
            raise ValueError("La suppression du répertoire racine DICOM Print est interdite.")
        if not folder.is_dir() or folder.is_symlink():
            raise ValueError("Le dossier d’examen n’est pas valide.")
        if not any(folder.glob("*.png")):
            raise ValueError("Le dossier sélectionné ne contient pas d’images d’examen DICOM Print.")
        shutil.rmtree(folder)
        logger.info("Dossier DICOM Print supprimé : %s", folder)
        return {"ok": True, "deleted": str(group_id)}

    def merge_groups(self, target_group_id: str, source_group_ids: list[str]) -> dict[str, Any]:
        """Fusionne des travaux du même examen dans le dossier cible, par ordre de réception."""
        if not source_group_ids or len(source_group_ids) > 30:
            raise ValueError("Sélectionnez entre 1 et 30 dossiers à fusionner.")
        target_root, target_folder = self._resolve_group(target_group_id)
        requested = list(dict.fromkeys(str(item) for item in source_group_ids))
        if len(requested) != len(source_group_ids) or target_group_id in requested:
            raise ValueError("La sélection de dossiers contient un doublon ou le dossier cible.")

        def read_manifest(folder: Path) -> dict[str, Any]:
            path = folder / "cliniva_print.json"
            try:
                return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            except (OSError, ValueError):
                return {}

        target_manifest = read_manifest(target_folder)
        target_patient_id = str(target_manifest.get("patient_id") or "").strip().casefold()
        target_patient_name = str(target_manifest.get("patient_name") or "").strip().casefold()
        required = {
            "order_number": str(target_manifest.get("order_number") or "").strip().casefold(),
            "passage_number": str(target_manifest.get("passage_number") or "").strip().casefold(),
            "exam_date": _normalize_exam_date(target_manifest.get("exam_date") or "").casefold()
            if target_manifest.get("exam_date") else "",
        }
        if not all(required.values()) or not (target_patient_id or target_patient_name):
            raise ValueError("Le dossier cible n’a pas toutes les informations nécessaires pour vérifier l’examen.")

        source_folders: list[tuple[Path, dict[str, Any]]] = []
        for source_id in requested:
            source_root, source_folder = self._resolve_group(source_id)
            if source_root != target_root:
                raise ValueError("Les dossiers à fusionner doivent appartenir au même récepteur DICOM Print.")
            source_manifest = read_manifest(source_folder)
            source_patient_id = str(source_manifest.get("patient_id") or "").strip().casefold()
            source_patient_name = str(source_manifest.get("patient_name") or "").strip().casefold()
            same_patient = (
                target_patient_id == source_patient_id if target_patient_id and source_patient_id
                else bool(target_patient_name and source_patient_name
                          and target_patient_name == source_patient_name)
            )
            source_values = {
                "order_number": str(source_manifest.get("order_number") or "").strip().casefold(),
                "passage_number": str(source_manifest.get("passage_number") or "").strip().casefold(),
                "exam_date": _normalize_exam_date(source_manifest.get("exam_date") or "").casefold()
                if source_manifest.get("exam_date") else "",
            }
            if not same_patient or source_values != required:
                raise ValueError("Fusion refusée : patient, numéro d’ordre, passage ou date différents.")
            source_folders.append((source_folder, source_manifest))

        # Préparer les images de tous les dossiers et les trier par date réelle
        # de modification du fichier (précision sous-seconde), donc par réception.
        all_items: list[tuple[int, Path, dict[str, Any], bool]] = []
        target_entries = {
            item.get("name"): item for item in target_manifest.get("images", [])
            if isinstance(item, dict) and item.get("name")
        }
        for path in target_folder.glob("Image_*.png"):
            all_items.append((path.stat().st_mtime_ns, path, dict(target_entries.get(path.name, {})), False))
        for source_folder, source_manifest in source_folders:
            entries = {
                item.get("name"): item for item in source_manifest.get("images", [])
                if isinstance(item, dict) and item.get("name")
            }
            for path in source_folder.glob("Image_*.png"):
                all_items.append((path.stat().st_mtime_ns, path, dict(entries.get(path.name, {})), True))
        if not all_items:
            raise ValueError("Aucune image à fusionner.")
        all_items.sort(key=lambda item: item[0])

        copied_paths: list[Path] = []
        merged_entries: list[dict[str, Any]] = []
        total = len(all_items)
        try:
            for position, (_received_ns, path, old_entry, is_source) in enumerate(all_items, start=1):
                if is_source:
                    filename = f"Image_fusion_{position:04d}_{_safe_component(path.stem, 'image')}.png"
                    destination = target_folder / filename
                    suffix = 2
                    while destination.exists():
                        destination = target_folder / f"Image_fusion_{position:04d}_{suffix}.png"
                        suffix += 1
                    shutil.copy2(path, destination)
                    copied_paths.append(destination)
                else:
                    destination = path
                entry = dict(old_entry)
                entry.update({"name": destination.name, "current_image": position,
                              "total_images": total, "display_order": position})
                merged_entries.append(entry)

            target_manifest["images"] = merged_entries
            target_manifest["received_images"] = total
            target_manifest["total_images"] = total
            target_manifest["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            target_manifest["merged_from"] = requested
            manifest_path = target_folder / "cliniva_print.json"
            temporary_manifest = target_folder / "cliniva_print.json.tmp"
            temporary_manifest.write_text(json.dumps(target_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary_manifest, manifest_path)
            info_lines = [
                "=== INFORMATIONS DE L’EXAMEN (OCR / DICOM) ===",
                f"Patient : {target_manifest.get('patient_name') or 'Non reconnu'}",
                f"Centre : {target_manifest.get('center') or 'Non reconnu'}",
                f"Identifiant patient : {target_manifest.get('patient_id') or 'Non reconnu'}",
                f"Date de l’examen : {target_manifest.get('exam_date') or 'Non reconnue'}",
                f"Numéro d’ordre / Accession Number : {target_manifest.get('order_number') or 'Non reconnu'}",
                f"Passage : {target_manifest.get('passage_number') or 'Non reconnu'}",
                f"Images reçues : {total}/{total}",
            ]
            (target_folder / "Informations_examen.txt").write_text(
                "\n".join(info_lines) + "\n", encoding="utf-8")
        except Exception:
            for copied in copied_paths:
                copied.unlink(missing_ok=True)
            raise

        for source_folder, _source_manifest in source_folders:
            shutil.rmtree(source_folder)
        logger.info("Dossiers DICOM Print fusionnés dans %s : %s", target_folder, requested)
        return {"ok": True, "target": target_group_id, "removed": requested, "images": total}

    def recent_images(self, limit: int = 100) -> list[dict[str, Any]]:
        """Retourne une ligne par dossier/examen, y compris les anciens dossiers."""
        with self._lock:
            root = Path(self._config["storage_dir"])
        if not root.exists():
            return []
        group_folders = {path.parent for path in root.rglob("*.png") if path.is_file()}
        results: list[dict[str, Any]] = []
        for folder in group_folders:
            images = sorted(folder.glob("*.png"), key=lambda item: item.name.lower())
            if not images:
                continue
            manifest: dict[str, Any] = {}
            manifest_path = folder / "cliniva_print.json"
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    logger.warning("Manifeste DICOM Print illisible : %s", manifest_path)
            manifest_total_before = manifest.get("total_images")
            voted_total = _consensus_print_total(manifest, {})
            if voted_total:
                manifest["total_images"] = voted_total
                for entry in manifest.get("images", []):
                    if isinstance(entry, dict):
                        entry["total_images"] = voted_total
            last_total_attempt = str(manifest.get("total_ocr_attempted_at") or "")
            try:
                retry_total_ocr = (not manifest.get("total_images") and
                                   (not last_total_attempt or
                                    (datetime.now() - datetime.strptime(
                                        last_total_attempt, "%Y-%m-%d %H:%M:%S")).total_seconds() > 300))
            except ValueError:
                retry_total_ocr = not manifest.get("total_images")
            if int(manifest.get("ocr_version") or 0) < 3 or retry_total_ocr:
                # Migre aussi les images reçues avant l’amélioration OCR afin
                # que le tableau affiche les métadonnées corrigées sans renvoi.
                try:
                    from PIL import Image
                    image_entries = manifest.setdefault("images", [])
                    ocr_succeeded = False
                    for image_path in images[:3]:
                        with Image.open(image_path) as saved_image:
                            recognized = _ocr_print_page(saved_image)
                        for key in ("patient_name", "patient_id", "center", "order_number",
                                    "passage_number", "exam_date", "total_images"):
                            value = str(recognized.get(key) or "").strip()
                            if value:
                                manifest[key] = value
                                if key in {"patient_name", "center", "order_number", "passage_number", "total_images"}:
                                    ocr_succeeded = True
                        entry = next((item for item in image_entries
                                      if isinstance(item, dict) and item.get("name") == image_path.name), None)
                        if entry is None:
                            entry = {"name": image_path.name}
                            image_entries.append(entry)
                        for key in ("current_image", "total_images"):
                            if recognized.get(key):
                                entry[key] = recognized[key]
                        current = str(recognized.get("current_image") or "").strip()
                        total = str(recognized.get("total_images") or "").strip()
                        if current.isdigit() and total.isdigit() and int(total) > 0:
                            corrected_image = folder / f"Image_{int(current):03d}_sur_{int(total):03d}.png"
                            if corrected_image != image_path and not corrected_image.exists():
                                image_path.rename(corrected_image)
                                entry["name"] = corrected_image.name
                    manifest["total_ocr_attempted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    voted_total = _consensus_print_total(manifest, {})
                    if voted_total:
                        manifest["total_images"] = voted_total
                        for entry in image_entries:
                            if isinstance(entry, dict):
                                entry["total_images"] = voted_total
                    if manifest.get("exam_date"):
                        manifest["exam_date"] = _normalize_exam_date(manifest["exam_date"])
                    if ocr_succeeded:
                        manifest["ocr_version"] = 3
                    manifest["received_images"] = len(images)
                    patient_label = _safe_folder_label(manifest.get("patient_name"))
                    image_total = str(manifest.get("total_images") or "nombre inconnu")
                    corrected_name = _safe_folder_label(f"{patient_label} - {image_total} images")
                    corrected_folder = folder.parent / corrected_name
                    if ocr_succeeded and patient_label != "Patient inconnu" and corrected_folder != folder and not corrected_folder.exists():
                        folder.rename(corrected_folder)
                        folder = corrected_folder
                    images = sorted(folder.glob("*.png"), key=lambda item: item.name.lower())
                    manifest["received_images"] = len(images)
                    manifest_path = folder / "cliniva_print.json"
                    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
                    info_lines = [
                        "=== INFORMATIONS DE L’EXAMEN (OCR / DICOM) ===",
                        f"Patient : {manifest.get('patient_name') or 'Non reconnu'}",
                        f"Centre : {manifest.get('center') or 'Non reconnu'}",
                        f"Identifiant patient : {manifest.get('patient_id') or 'Non reconnu'}",
                        f"Date de l’examen : {manifest.get('exam_date') or 'Non reconnue'}",
                        f"Numéro d’ordre / Accession Number : {manifest.get('order_number') or 'Non reconnu'}",
                        f"Passage : {manifest.get('passage_number') or 'Non reconnu'}",
                        f"Images reçues : {manifest['received_images']}/{manifest.get('total_images') or '?'}",
                    ]
                    (folder / "Informations_examen.txt").write_text(
                        "\n".join(info_lines) + "\n", encoding="utf-8")
                except Exception:
                    logger.exception("Impossible de retraiter l’OCR du groupe DICOM Print %s", folder)
            elif manifest.get("total_images") != manifest_total_before:
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
            modified = max(path.stat().st_mtime for path in images)
            try:
                group_id = folder.relative_to(root).as_posix()
            except ValueError:
                continue
            results.append({
                "id": group_id,
                "name": folder.name,
                "folder": str(folder),
                "patient_name": manifest.get("patient_name") or folder.name,
                "patient_id": manifest.get("patient_id") or "",
                "center": manifest.get("center") or "",
                "exam_date": manifest.get("exam_date") or folder.parent.name,
                "order_number": manifest.get("order_number") or "",
                "passage_number": manifest.get("passage_number") or "",
                "total_images": manifest.get("total_images"),
                "received_images": len(images),
                "modified": datetime.fromtimestamp(modified).strftime("%d/%m/%Y %H:%M:%S"),
                "_modified_timestamp": modified,
            })
        results.sort(key=lambda item: item["_modified_timestamp"], reverse=True)
        for item in results:
            item.pop("_modified_timestamp", None)
        return results[:max(1, min(int(limit), 300))]

    def group_images(self, group_id: str) -> list[dict[str, Any]]:
        _root, folder = self._resolve_group(group_id)
        manifest_path = folder / "cliniva_print.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        except (OSError, ValueError):
            manifest = {}
        entries_by_name = {entry.get("name"): entry for entry in manifest.get("images", [])
                           if isinstance(entry, dict) and entry.get("name")}
        images = []
        for path in folder.glob("*.png"):
            entry = entries_by_name.get(path.name, {})
            try:
                current = int(entry.get("current_image") or re.search(r"Image_(\d+)_", path.name).group(1))
            except (AttributeError, TypeError, ValueError):
                current = 0
            images.append({"name": path.name, "current_image": current,
                           "total_images": entry.get("total_images") or manifest.get("total_images"),
                           "received_at": entry.get("received_at") or "",
                           "display_order": entry.get("display_order")})
        if any(item.get("display_order") is not None for item in images):
            images.sort(key=lambda item: (int(item["display_order"]) if item.get("display_order") is not None else 1_000_000,
                                          item["current_image"] or 999999, item["name"].lower()))
        else:
            images.sort(key=lambda item: (item["current_image"] or 999999, item["name"].lower()))
        return images

    def reorder_group_images(self, group_id: str, image_names: list[str]) -> dict[str, Any]:
        """Persistit l'ordre manuel des images sans renommer les fichiers reçus."""
        _root, folder = self._resolve_group(group_id)
        current = [item["name"] for item in self.group_images(group_id)]
        requested = [str(name) for name in image_names]
        if len(requested) != len(current) or set(requested) != set(current):
            raise ValueError("La liste ne correspond pas aux images du dossier sélectionné.")
        manifest_path = folder / "cliniva_print.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        except (OSError, ValueError):
            manifest = {}
        entries = manifest.setdefault("images", [])
        if not isinstance(entries, list):
            entries = manifest["images"] = []
        by_name = {entry.get("name"): entry for entry in entries if isinstance(entry, dict)}
        for position, name in enumerate(requested, start=1):
            entry = by_name.get(name)
            if entry is None:
                entry = {"name": name}
                entries.append(entry)
                by_name[name] = entry
            entry["display_order"] = position
        manifest["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "count": len(requested)}

    def import_group_images(self, target_group_id: str, source_group_id: str,
                            image_names: list[str]) -> dict[str, Any]:
        """Copie des images d'un autre examen dans le dossier cible, sans altérer les originaux."""
        if target_group_id == source_group_id:
            raise ValueError("Choisissez un examen source différent du dossier cible.")
        if not image_names or len(image_names) > 500:
            raise ValueError("Sélectionnez entre 1 et 500 images à importer.")
        _source_root, source_folder = self._resolve_group(source_group_id)
        _target_root, target_folder = self._resolve_group(target_group_id)
        source_images = {item["name"]: item for item in self.group_images(source_group_id)}
        names = list(dict.fromkeys(str(name) for name in image_names))
        if len(names) != len(image_names) or any(name not in source_images for name in names):
            raise ValueError("Une ou plusieurs images sélectionnées ne font pas partie de l'examen source.")
        source_manifest_path = source_folder / "cliniva_print.json"
        target_manifest_path = target_folder / "cliniva_print.json"
        try:
            source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8")) if source_manifest_path.is_file() else {}
        except (OSError, ValueError):
            source_manifest = {}
        try:
            target_manifest = json.loads(target_manifest_path.read_text(encoding="utf-8")) if target_manifest_path.is_file() else {}
        except (OSError, ValueError):
            target_manifest = {}
        source_entries = {entry.get("name"): entry for entry in source_manifest.get("images", [])
                          if isinstance(entry, dict) and entry.get("name")}
        target_entries = target_manifest.setdefault("images", [])
        if not isinstance(target_entries, list):
            target_entries = target_manifest["images"] = []
        target_ordered_images = self.group_images(target_group_id)
        target_by_name = {entry.get("name"): entry for entry in target_entries
                          if isinstance(entry, dict) and entry.get("name")}
        for position, image in enumerate(target_ordered_images, start=1):
            entry = target_by_name.get(image["name"])
            if entry is None:
                entry = {"name": image["name"]}
                target_entries.append(entry)
                target_by_name[image["name"]] = entry
            entry["display_order"] = position
        target_count = len(list(target_folder.glob("*.png")))
        display_order = len(target_ordered_images)
        imported = []
        for offset, name in enumerate(names, start=1):
            source_path = (source_folder / name).resolve()
            try:
                source_path.relative_to(source_folder.resolve())
            except ValueError as exc:
                raise ValueError("Chemin d'image source invalide.") from exc
            if not source_path.is_file() or source_path.suffix.lower() != ".png":
                raise ValueError("Une image source n'est plus disponible.")
            sequence = target_count + offset
            new_name = f"Image_importee_{sequence:04d}_{_safe_component(Path(name).stem, 'image')}.png"
            while (target_folder / new_name).exists():
                sequence += 1
                new_name = f"Image_importee_{sequence:04d}_{_safe_component(Path(name).stem, 'image')}.png"
            shutil.copy2(source_path, target_folder / new_name)
            entry = dict(source_entries.get(name, {}))
            entry.update({"name": new_name, "source_exam": source_group_id,
                          "source_name": name, "display_order": display_order + offset})
            target_entries.append(entry)
            imported.append(new_name)
        target_manifest["received_images"] = len(list(target_folder.glob("*.png")))
        target_manifest["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        target_manifest_path.write_text(json.dumps(target_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "count": len(imported), "names": imported}

    def group_image(self, group_id: str, index: int) -> dict[str, Any]:
        images = self.group_images(group_id)
        if not 0 <= int(index) < len(images):
            raise IndexError("Image d’impression introuvable.")
        _root, folder = self._resolve_group(group_id)
        image = images[int(index)]
        path = folder / image["name"]
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {**image, "data_url": f"data:image/png;base64,{encoded}",
                "position": int(index) + 1, "count": len(images)}

    def group_image_thumbnail(self, group_id: str, index: int) -> dict[str, Any]:
        """Retourne une vignette légère pour le gestionnaire d'images."""
        import io
        from PIL import Image

        images = self.group_images(group_id)
        if not 0 <= int(index) < len(images):
            raise IndexError("Image d’impression introuvable.")
        _root, folder = self._resolve_group(group_id)
        image = images[int(index)]
        with Image.open(folder / image["name"]) as source:
            thumb = source.convert("RGB")
            thumb.thumbnail((360, 260), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            thumb.save(output, format="JPEG", quality=78, optimize=True)
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return {"name": image["name"], "data_url": f"data:image/jpeg;base64,{encoded}"}
