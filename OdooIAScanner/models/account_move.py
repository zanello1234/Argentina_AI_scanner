# -*- coding: utf-8 -*-
import base64
import io
import json
import logging
import re
import time
from datetime import datetime

from concurrent.futures import ThreadPoolExecutor, as_completed

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thread-safe API call functions (no ORM — safe for ThreadPoolExecutor)
# ---------------------------------------------------------------------------

def _call_anthropic_api_raw(api_key, model_name, image_b64, media_type, prompt):
    """Thread-safe Anthropic API call. Returns (raw_text, tokens)."""
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        raise RuntimeError('La librería "anthropic" no está instalada.')
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model_name,
        max_tokens=4096,
        temperature=0,
        messages=[{
            'role': 'user',
            'content': [
                {
                    'type': 'image',
                    'source': {'type': 'base64', 'media_type': media_type, 'data': image_b64},
                },
                {'type': 'text', 'text': prompt},
            ],
        }],
    )
    raw_text = message.content[0].text if message.content else ''
    tokens = (message.usage.input_tokens or 0) + (message.usage.output_tokens or 0)
    return raw_text, tokens


def _call_gemini_api_raw(api_key, model_name, image_b64, media_type, prompt):
    """Thread-safe Gemini REST API call. Returns (raw_text, tokens)."""
    import requests  # noqa: PLC0415
    url = (
        'https://generativelanguage.googleapis.com/v1beta/models/'
        '%s:generateContent?key=%s' % (model_name, api_key)
    )
    payload = {
        'contents': [{
            'role': 'user',
            'parts': [
                {'inlineData': {'mimeType': media_type, 'data': image_b64}},
                {'text': prompt},
            ],
        }],
        'safetySettings': [
            {'category': 'HARM_CATEGORY_HARASSMENT', 'threshold': 'BLOCK_ONLY_HIGH'},
            {'category': 'HARM_CATEGORY_HATE_SPEECH', 'threshold': 'BLOCK_ONLY_HIGH'},
            {'category': 'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'threshold': 'BLOCK_ONLY_HIGH'},
            {'category': 'HARM_CATEGORY_DANGEROUS_CONTENT', 'threshold': 'BLOCK_ONLY_HIGH'},
        ],
        'generationConfig': {'maxOutputTokens': 4096, 'temperature': 0},
    }
    resp = requests.post(url, json=payload, timeout=120)
    if resp.status_code != 200:
        try:
            err_msg = resp.json().get('error', {}).get('message', resp.text[:500])
        except Exception:
            err_msg = resp.text[:500]
        raise RuntimeError('Gemini API error (HTTP %s): %s' % (resp.status_code, err_msg))
    result = resp.json()
    candidates = result.get('candidates', [])
    if not candidates:
        block_reason = result.get('promptFeedback', {}).get('blockReason', '')
        raise RuntimeError('Gemini sin respuesta (razón: %s)' % (block_reason or 'desconocida'))
    candidate = candidates[0]
    if candidate.get('finishReason') == 'SAFETY':
        raise RuntimeError('Filtro de seguridad de Gemini bloqueó la respuesta')
    parts = candidate.get('content', {}).get('parts', [])
    raw_text = parts[0].get('text', '') if parts else ''
    usage = result.get('usageMetadata', {})
    tokens = (usage.get('promptTokenCount', 0) or 0) + (usage.get('candidatesTokenCount', 0) or 0)
    return raw_text, tokens

AI_SCAN_PROMPT = """Analizá este documento fiscal argentino. Primero determiná el tipo de documento y luego extraé los datos correspondientes.
Respondé SOLO con un objeto JSON compacto en UNA SOLA LÍNEA (sin saltos de línea, sin indentación, sin markdown, sin texto extra).

SIEMPRE incluir el campo "tipo_documento" con uno de estos valores: "FACTURA" (facturas, notas de crédito/débito, recibos comerciales, comprobantes de venta), "SICOSS" (formulario 931, DDJJ SUSS, declaración jurada de cargas sociales), "IVA" (formulario 731, DDJJ IVA, Impuesto al Valor Agregado), "CM" (formulario CM03, CM04, CM05, Convenio Multilateral, declaración jurada de Ingresos Brutos) o "RECIBO" (recibo de sueldo, liquidación de haberes, recibo de haberes).

--- SI tipo_documento = "FACTURA" ---
Campos: tipo_comprobante (ej: FACTURA A, NOTA DE DEBITO B), punto_de_venta (5 dígitos), numero_comprobante (8 dígitos),
fecha_emision (DD/MM/YYYY), cuit_emisor (XX-XXXXXXXX-X), razon_social_emisor,
cuit_receptor (XX-XXXXXXXX-X o "-"), razon_social_receptor (o "-"),
cae (14 dígitos o null), vencimiento_cae (DD/MM/YYYY o null),
neto_gravado_21, neto_gravado_105, neto_gravado_27, neto_no_gravado, exento,
iva_21, iva_105, iva_27, otros_tributos, importe_total,
moneda (ARS/USD/EUR), confianza (0-100), observaciones.
productos: array de objetos con {descripcion, cantidad, precio_unitario, iva_porcentaje} por ítem. Array vacío [] si no hay detalle.

--- SI tipo_documento = "SICOSS" ---
Campos: cuit (XX-XXXXXXXX-X), razon_social, periodo (MM/YYYY), empleados (cantidad en nómina),
remuneracion_1 (Suma de Rem. 1), nro_verificador,
contrib_ss (351-Contribuciones Seg.Social), contrib_os (352-Contribuciones Obra Social),
aporte_ss (301-Aportes Seg.Social), aporte_os (302-Aportes Obra Social),
lrt (312-LRT total a pagar), svo (028-Seguro Colectivo Vida Obligatorio),
renatre (360-Contribuciones RENATRE), sepelio_uatre (935-Seg.Sepelio UATRE),
vales_alimentarios (270-Vales Alimentarios/Cajas), importe_total (suma de todos los montos que se ingresan),
confianza (0-100), observaciones.

--- SI tipo_documento = "IVA" ---
Campos: cuit (XX-XXXXXXXX-X), razon_social, periodo (MM/YYYY), actividad_principal, nro_verificador,
debito_fiscal (Total del débito fiscal del período),
credito_fiscal (Total del crédito fiscal del período),
saldo_tecnico_anterior (Saldo a favor del período anterior),
saldo_tecnico_favor_responsable (Saldo técnico a favor del responsable),
saldo_tecnico_favor_afip (Saldo técnico a favor de AFIP),
retenciones_percepciones (Total de retenciones, percepciones y pagos a cuenta),
saldo_libre_disp_anterior (Saldo a favor de libre disponibilidad del período anterior),
saldo_libre_disp_periodo (Saldo de libre disponibilidad del período),
saldo_favor_afip (Saldo de impuesto a favor de AFIP),
monto_ingresa (Monto que se ingresa),
confianza (0-100), observaciones.

--- SI tipo_documento = "CM" ---
Campos: cuit (XX-XXXXXXXX-X), razon_social, periodo (MM/YYYY), formulario (CM03, CM04). Para CM05 (anual) indicar el año,
provincia (nombre de la provincia principal de la declaración, ej: "Buenos Aires", "Córdoba", "Santa Fe"),
nro_verificador,
impuesto_determinado (Total del impuesto determinado del período),
retenciones_bancarias (Total de retenciones bancarias sufridas),
retenciones_iibb (Total de retenciones de Ingresos Brutos sufridas),
percepciones_iibb (Total de percepciones de Ingresos Brutos sufridas),
saldo_favor_anterior (Saldo a favor del período anterior),
recaudaciones_bancarias (Total de recaudaciones bancarias, 0 si no aparece),
monto_a_pagar (Monto total a pagar / saldo resultante),
confianza (0-100), observaciones.

--- SI tipo_documento = "RECIBO" ---
Campos: cuit_empleador (XX-XXXXXXXX-X), razon_social_empleador, nombre_empleado (nombre completo del trabajador),
legajo (número de legajo del empleado), cuil_empleado (XX-XXXXXXXX-X del empleado, si aparece),
periodo (MM/YYYY del período liquidado), fecha_pago (DD/MM/YYYY si aparece),
categoria (categoría o puesto del empleado, si aparece),
sueldo_basico (sueldo básico bruto), total_haberes (total de haberes/remunerativos),
total_deducciones (total de deducciones/descuentos), neto_a_cobrar (sueldo neto / neto de bolsillo),
confianza (0-100), observaciones.

Montos numéricos, 0 si no aplica."""


def _validate_cuit(cuit_raw):
    """Validate an Argentine CUIT/CUIL using the mod-11 algorithm.

    Accepts formats: XX-XXXXXXXX-X, XXXXXXXXXXX, or XX XXXXXXXX X.
    Returns (is_valid: bool, digits: str).

    AFIP mod-11 rules:
      - remainder 0  → check digit = 0
      - remainder 1  → check digit = 9  (special AFIP rule)
      - otherwise    → check digit = 11 - remainder
    """
    digits = re.sub(r'[^0-9]', '', cuit_raw or '')
    if len(digits) != 11:
        return False, digits
    factors = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(digits[i]) * factors[i] for i in range(10))
    remainder = total % 11
    if remainder == 0:
        check = 0
    elif remainder == 1:
        check = 9
    else:
        check = 11 - remainder
    return str(check) == digits[10], digits


def _parse_amount(value):
    """Parse an amount string or number to float, returning 0.0 on failure."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    clean = re.sub(r'[^\d,\.]', '', str(value))
    # Handle Argentine number format: 1.234,56 -> 1234.56
    if ',' in clean and '.' in clean:
        if clean.rfind(',') > clean.rfind('.'):
            clean = clean.replace('.', '').replace(',', '.')
        else:
            clean = clean.replace(',', '')
    elif ',' in clean:
        clean = clean.replace(',', '.')
    try:
        return float(clean)
    except (ValueError, TypeError):
        return 0.0


class AccountMove(models.Model):
    _inherit = 'account.move'

    # ------------------------------------------------------------------
    # Disable Odoo Enterprise native AI extract (account_invoice_extract)
    # so our custom scanner is used instead when this module is installed.
    # ------------------------------------------------------------------

    def _needs_document_digitization(self):
        """Override to prevent Odoo's native 'Digitize' from triggering."""
        return False

    def _get_edi_creation(self):
        """Override for older Enterprise versions that use this hook."""
        if hasattr(super(), '_get_edi_creation'):
            return super()._get_edi_creation()
        return False

    ai_scan_state = fields.Selection(
        selection=[
            ('draft', 'Sin escanear'),
            ('queued', 'En cola'),
            ('scanning', 'Escaneando...'),
            ('done', 'Completado'),
            ('error', 'Error'),
        ],
        string='Estado de Escaneo IA',
        default='draft',
        copy=False,
        tracking=True,
    )
    ai_scan_confidence = fields.Float(
        string='Confianza IA (%)',
        digits=(5, 2),
        default=0.0,
        copy=False,
        help='Porcentaje de confianza reportado por el modelo de IA en la extracción.',
    )
    ai_scanned_cae = fields.Char(
        string='CAE (IA)',
        size=14,
        copy=False,
        help='Código de Autorización Electrónica extraído por IA.',
    )
    ai_scanned_cuit_emisor = fields.Char(
        string='CUIT Emisor (IA)',
        size=13,
        copy=False,
        help='CUIT del emisor extraído por IA (formato XX-XXXXXXXX-X).',
    )
    ai_scanned_doc_number = fields.Char(
        string='Número de Comprobante (IA)',
        size=20,
        copy=False,
        help='Número de comprobante completo (XXXXX-YYYYYYYY) extraído por IA.',
    )
    ai_scan_raw_result = fields.Text(
        string='Resultado Crudo IA',
        copy=False,
        help='Respuesta JSON completa del modelo de IA.',
    )
    ai_scan_queue_auto_apply = fields.Boolean(
        string='Auto-aplicar (cola)',
        default=False,
        copy=False,
    )
    ai_scan_queue_create_partner = fields.Boolean(
        string='Crear proveedor (cola)',
        default=False,
        copy=False,
    )

    def action_open_scan_wizard(self):
        """Open the invoice scan wizard."""
        self.ensure_one()
        if self.move_type not in ('in_invoice', 'in_refund', 'out_invoice', 'out_refund'):
            raise UserError(_('El escaneo de IA solo está disponible para facturas y notas de crédito/débito.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Escanear Comprobante con IA'),
            'res_model': 'l10n_ar.ai.scan.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_move_id': self.id,
                'active_id': self.id,
                'active_model': 'account.move',
            },
        }

    def action_scan_invoice_ai(self):
        """Main AI scan method: converts attachment to image, calls the configured
        AI Vision API, parses response, populates fields, and creates an audit log entry."""
        self.ensure_one()

        # --- 1. Get provider, API key and model from system parameters ---
        ICP = self.env['ir.config_parameter'].sudo()
        provider = ICP.get_param('l10n_ar_ai_scanner.provider', default='anthropic')
        model_name = ICP.get_param('l10n_ar_ai_scanner.model', default='claude-sonnet-4-5')

        # Validate provider/model consistency
        if provider == 'gemini' and model_name.startswith('claude'):
            raise UserError(_(
                'El proveedor configurado es Google Gemini pero el modelo seleccionado '
                'es "%s" (Anthropic). Cambie el modelo en Configuración > Escaner IA AFIP.'
            ) % model_name)
        if provider == 'anthropic' and model_name.startswith('gemini'):
            raise UserError(_(
                'El proveedor configurado es Anthropic pero el modelo seleccionado '
                'es "%s" (Google). Cambie el modelo en Configuración > Escaner IA AFIP.'
            ) % model_name)

        if provider == 'gemini':
            api_key = ICP.get_param('l10n_ar_ai_scanner.gemini_api_key', default='')
            if not api_key:
                raise UserError(_(
                    'No se ha configurado la clave API de Google Gemini. '
                    'Por favor configure la clave en Contabilidad > Configuración > Ajustes.'
                ))
        else:
            api_key = ICP.get_param('l10n_ar_ai_scanner.api_key', default='')
            if not api_key:
                raise UserError(_(
                    'No se ha configurado la clave API de Anthropic. '
                    'Por favor configure la clave en Contabilidad > Configuración > Ajustes.'
                ))

        # --- 2. Find suitable attachment (PDF or image) ---
        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', 'account.move'),
            ('res_id', '=', self.id),
            ('mimetype', 'in', [
                'application/pdf',
                'image/jpeg',
                'image/jpg',
                'image/png',
                'image/gif',
                'image/webp',
            ]),
        ], order='id desc', limit=1)

        if not attachments:
            raise UserError(_(
                'No se encontró ningún archivo adjunto (PDF o imagen) en este comprobante. '
                'Por favor adjunte la imagen o PDF del comprobante antes de escanear.'
            ))

        attachment = attachments[0]
        self.write({'ai_scan_state': 'scanning'})
        # Flush pending writes so the UI can reflect the "scanning" state
        self.env.cr.flush()

        scan_start = time.time()
        error_message = False
        raw_response_text = ''
        extracted_data = {}
        tokens_used = 0
        success = False
        confidence = 0.0

        try:
            # --- 3. Convert attachment to image bytes ---
            image_bytes, media_type = self._prepare_image_for_scan(attachment)

            # --- 4. Call AI Vision API ---
            if provider == 'gemini':
                raw_response_text, tokens_used = self._call_gemini_api(
                    api_key, model_name, image_bytes, media_type,
                )
            else:
                raw_response_text, tokens_used = self._call_anthropic_api(
                    api_key, model_name, image_bytes, media_type,
                )

            # --- 5. Parse JSON response ---
            extracted_data = self._parse_ai_response(raw_response_text)
            if not extracted_data:
                raise UserError(_(
                    'No se pudo extraer JSON de la respuesta del modelo.\n'
                    'Respuesta cruda (primeros 500 chars):\n%s'
                ) % (raw_response_text[:500] if raw_response_text else '(vacía)'))

            confidence = float(extracted_data.get('confianza', 0) or 0)

            # --- 6. Populate move fields from extracted data ---
            self._apply_extracted_data(extracted_data)
            success = True

        except Exception as exc:
            _logger.exception('Error during AI scan for move %s', self.id)
            error_message = str(exc)
            self.write({'ai_scan_state': 'error'})
        finally:
            scan_duration = time.time() - scan_start

            # --- 7. Create audit log ---
            self.env['l10n_ar.ai.scan.log'].sudo().create({
                'move_id': self.id,
                'ai_model': model_name,
                'tokens_used': tokens_used,
                'scan_duration': scan_duration,
                'success': success,
                'error_message': error_message or False,
                'raw_prompt': AI_SCAN_PROMPT,
                'raw_response': raw_response_text,
                'extracted_data': json.dumps(extracted_data, ensure_ascii=False, indent=2) if extracted_data else '',
                'confidence_score': confidence,
            })

        if success:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Escaneo completado'),
                    'message': _('El comprobante fue escaneado con una confianza del %.1f%%.') % confidence,
                    'type': 'success',
                    'sticky': False,
                },
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Error en el escaneo'),
                'message': _('Ocurrió un error al escanear el comprobante: %s') % (error_message or _('Error desconocido')),
                'type': 'danger',
                'sticky': True,
            },
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _call_anthropic_api(self, api_key, model_name, image_bytes, media_type):
        """Call the Anthropic Claude Vision API.

        Returns:
            tuple(str, int): (raw_response_text, tokens_used)
        """
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:
            raise UserError(_(
                'La librería Python "anthropic" no está instalada. '
                'Ejecute: pip install "anthropic>=0.40.0"'
            )) from exc

        image_b64 = base64.b64encode(image_bytes).decode('utf-8')
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model_name,
            max_tokens=4096,
            temperature=0,
            messages=[
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'image',
                            'source': {
                                'type': 'base64',
                                'media_type': media_type,
                                'data': image_b64,
                            },
                        },
                        {
                            'type': 'text',
                            'text': AI_SCAN_PROMPT,
                        },
                    ],
                }
            ],
        )

        raw_text = message.content[0].text if message.content else ''
        tokens = (message.usage.input_tokens or 0) + (message.usage.output_tokens or 0)
        return raw_text, tokens

    def _call_gemini_api(self, api_key, model_name, image_bytes, media_type):
        """Call the Google Gemini Vision API via REST (no SDK dependency).

        Uses the REST API directly with ``requests`` (bundled with Odoo) to
        avoid issues with the google-generativeai SDK safety filters on the
        free tier.

        Returns:
            tuple(str, int): (raw_response_text, tokens_used)
        """
        import requests  # noqa: PLC0415  (always available in Odoo)

        image_b64 = base64.b64encode(image_bytes).decode('utf-8')

        url = (
            'https://generativelanguage.googleapis.com/v1beta/models/'
            '%s:generateContent?key=%s' % (model_name, api_key)
        )

        payload = {
            'contents': [
                {
                    'role': 'user',
                    'parts': [
                        {
                            'inlineData': {
                                'mimeType': media_type,
                                'data': image_b64,
                            },
                        },
                        {
                            'text': AI_SCAN_PROMPT,
                        },
                    ],
                }
            ],
            'systemInstruction': {
                'parts': [{
                    'text': (
                        'Eres un asistente de contabilidad especializado en '
                        'comprobantes fiscales argentinos (AFIP). Tu tarea es '
                        'extraer datos estructurados de imágenes de facturas, '
                        'notas de débito/crédito y tickets fiscales. '
                        'Responde siempre en formato JSON.'
                    ),
                }],
            },
            'safetySettings': [
                {'category': 'HARM_CATEGORY_HARASSMENT', 'threshold': 'BLOCK_ONLY_HIGH'},
                {'category': 'HARM_CATEGORY_HATE_SPEECH', 'threshold': 'BLOCK_ONLY_HIGH'},
                {'category': 'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'threshold': 'BLOCK_ONLY_HIGH'},
                {'category': 'HARM_CATEGORY_DANGEROUS_CONTENT', 'threshold': 'BLOCK_ONLY_HIGH'},
            ],
            'generationConfig': {
                'maxOutputTokens': 4096,
                'temperature': 0,
            },
        }

        try:
            resp = requests.post(url, json=payload, timeout=120)
        except requests.RequestException as exc:
            raise UserError(_(
                'Error de conexión con la API de Gemini: %s'
            ) % str(exc)) from exc

        if resp.status_code != 200:
            # Try to extract a useful error message
            try:
                err_data = resp.json()
                err_msg = err_data.get('error', {}).get('message', resp.text[:500])
            except Exception:
                err_msg = resp.text[:500]
            raise UserError(_(
                'Error de la API de Gemini (HTTP %s): %s'
            ) % (resp.status_code, err_msg))

        result = resp.json()

        # Check candidates
        candidates = result.get('candidates', [])
        if not candidates:
            # Check if blocked by prompt feedback
            block_reason = result.get('promptFeedback', {}).get('blockReason', '')
            if block_reason:
                raise UserError(_(
                    'Gemini bloqueó la solicitud (razón: %s). '
                    'Intente con otro modelo o proveedor.'
                ) % block_reason)
            raise UserError(_(
                'Gemini no generó respuesta. Verifique que la imagen sea un '
                'comprobante fiscal válido e intente nuevamente.'
            ))

        candidate = candidates[0]
        finish_reason = candidate.get('finishReason', '')

        if finish_reason == 'SAFETY':
            # Extract which safety category triggered the block
            safety_ratings = candidate.get('safetyRatings', [])
            blocked_cats = [
                r.get('category', '?') for r in safety_ratings
                if r.get('blocked')
            ]
            detail = ', '.join(blocked_cats) if blocked_cats else 'categoría no especificada'
            raise UserError(_(
                'El filtro de seguridad de Google bloqueó la respuesta (%s). '
                'Active la facturación en Google AI Studio para desbloquear los filtros, '
                'o use el proveedor Anthropic.'
            ) % detail)

        if finish_reason not in ('STOP', 'MAX_TOKENS', ''):
            raise UserError(_(
                'Gemini no completó el análisis (razón: %s). '
                'Intente con otro modelo o proveedor.'
            ) % finish_reason)

        # Extract response text
        parts = candidate.get('content', {}).get('parts', [])
        raw_text = parts[0].get('text', '') if parts else ''

        # Extract token usage
        usage = result.get('usageMetadata', {})
        tokens = (usage.get('promptTokenCount', 0) or 0) + (usage.get('candidatesTokenCount', 0) or 0)

        return raw_text, tokens

    def _prepare_image_for_scan(self, attachment):
        """Convert an ir.attachment (PDF or image) to raw image bytes suitable
        for AI Vision APIs.  PDFs are converted to images via pdf2image.

        Returns:
            tuple(bytes, str): (image_bytes, media_type)
        """
        raw_data = base64.b64decode(attachment.datas)
        mimetype = (attachment.mimetype or '').lower()

        if mimetype == 'application/pdf':
            raw_data, mimetype = self._pdf_to_image_bytes(raw_data)

        # Resize image to max 1024px width — sufficient for text extraction
        # and significantly reduces upload size / API latency
        raw_data = self._resize_image(raw_data, max_width=1024)

        # Normalise media type (jpeg/png/gif/webp accepted by both APIs)
        if 'jpeg' in mimetype or 'jpg' in mimetype:
            api_media_type = 'image/jpeg'
        elif 'png' in mimetype:
            api_media_type = 'image/png'
        elif 'gif' in mimetype:
            api_media_type = 'image/gif'
        elif 'webp' in mimetype:
            api_media_type = 'image/webp'
        else:
            # Default: treat as JPEG
            api_media_type = 'image/jpeg'

        return raw_data, api_media_type

    def _pdf_to_image_bytes(self, pdf_bytes):
        """Convert the first page of a PDF to a JPEG bytes object.

        Tries in order:
          1. PyMuPDF (fitz) – pure pip, no system deps (recommended)
          2. pdf2image + poppler – requires poppler system package
          3. pypdf + Pillow – limited to PDFs with embedded images

        Returns:
            tuple(bytes, str): (image_bytes, 'image/jpeg')
        """
        # --- Option 1: PyMuPDF (pip install pymupdf) – no system deps ---
        try:
            import fitz  # noqa: PLC0415  (PyMuPDF)
            doc = fitz.open(stream=pdf_bytes, filetype='pdf')
            if not doc.page_count:
                raise UserError(_('El PDF no contiene páginas.'))
            page = doc[0]
            # Render at 120 DPI — good enough for text, smaller image
            pix = page.get_pixmap(dpi=120)
            return pix.tobytes('jpeg'), 'image/jpeg'
        except ImportError:
            pass  # Fall through to pdf2image

        # --- Option 2: pdf2image + poppler ---
        try:
            from pdf2image import convert_from_bytes  # noqa: PLC0415
            images = convert_from_bytes(
                pdf_bytes,
                first_page=1,
                last_page=1,
                dpi=120,
                fmt='jpeg',
            )
            if not images:
                raise UserError(_('No se pudo convertir el PDF a imagen.'))
            buf = io.BytesIO()
            images[0].save(buf, format='JPEG', quality=75)
            return buf.getvalue(), 'image/jpeg'
        except ImportError:
            pass  # Fall through to pypdf fallback
        except Exception:
            _logger.warning('pdf2image failed (poppler missing?), trying pypdf fallback.', exc_info=True)

        # --- Option 3: pypdf + Pillow (limited) ---
        try:
            import pypdf  # noqa: PLC0415
            from PIL import Image  # noqa: PLC0415
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            if not reader.pages:
                raise UserError(_('El PDF no contiene páginas.'))
            page = reader.pages[0]
            page_images = list(page.images)
            if page_images:
                img_data = page_images[0].data
                pil_img = Image.open(io.BytesIO(img_data)).convert('RGB')
                buf = io.BytesIO()
                pil_img.save(buf, format='JPEG', quality=75)
                return buf.getvalue(), 'image/jpeg'
            raise UserError(_(
                'No se pudo extraer imágenes del PDF. '
                'Instale PyMuPDF: pip install pymupdf'
            ))
        except ImportError as exc:
            raise UserError(_(
                'No se pudo convertir el PDF. Instale al menos una de estas librerías:\n'
                '  pip install pymupdf          (recomendado, sin dependencias de sistema)\n'
                '  pip install pdf2image Pillow  (requiere poppler instalado en el sistema)'
            )) from exc

    def _resize_image(self, image_bytes, max_width=1024):
        """Resize image so its width does not exceed max_width pixels.

        Returns the (possibly unchanged) image as JPEG bytes.
        """
        try:
            from PIL import Image  # noqa: PLC0415
            img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
            width, height = img.size
            if width > max_width:
                ratio = max_width / width
                new_size = (max_width, int(height * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=75)
            return buf.getvalue()
        except ImportError:
            _logger.warning('Pillow not installed – skipping image resize.')
            return image_bytes
        except Exception:
            _logger.warning('Could not resize image – using original bytes.', exc_info=True)
            return image_bytes

    def _parse_ai_response(self, raw_text):
        """Extract the JSON object from the AI model's response text.

        The model is instructed to return only JSON, but this method also handles
        cases where it wraps the JSON in markdown code fences.
        """
        if not raw_text:
            return {}
        # Strip markdown code fences if present
        text = raw_text.strip()
        if text.startswith('```'):
            lines = text.splitlines()
            # Remove first line (```json or ```) and last line (```)
            inner = lines[1:] if len(lines) > 1 else lines
            if inner and inner[-1].strip() == '```':
                inner = inner[:-1]
            text = '\n'.join(inner)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            _logger.warning('AI model returned non-dict JSON: %s', type(data))
            return {}
        except json.JSONDecodeError:
            # Try to find a JSON object using a simple brace-matching approach
            start = text.find('{')
            end = text.rfind('}')
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass
            _logger.error('Could not parse AI JSON response: %.500s', raw_text)
            return {}

    def _apply_extracted_data(self, data):
        """Apply extracted data to the account.move record.

        Handles invoice (FACTURA), SICOSS (F931) and IVA (F731) document types.
        Validates CUIT numbers and writes the AI-specific fields. Does NOT
        modify accounting lines – that is handled in the wizard.
        """
        if not data:
            self.write({'ai_scan_state': 'error'})
            return

        vals = {'ai_scan_state': 'done', 'ai_scan_raw_result': json.dumps(data, ensure_ascii=False, indent=2)}

        # Confidence
        confidence = float(data.get('confianza', 0) or 0)
        vals['ai_scan_confidence'] = min(max(confidence, 0.0), 100.0)

        tipo_doc = (data.get('tipo_documento') or 'FACTURA').upper()

        if tipo_doc in ('SICOSS', 'IVA', 'CM', 'RECIBO'):
            # CUIT of the declaring company / employer
            cuit_field_name = 'cuit_empleador' if tipo_doc == 'RECIBO' else 'cuit'
            cuit_raw = str(data.get(cuit_field_name) or '')
            valid, digits = _validate_cuit(cuit_raw)
            if valid and digits:
                vals['ai_scanned_cuit_emisor'] = '{}-{}-{}'.format(digits[:2], digits[2:10], digits[10])
            periodo = str(data.get('periodo') or '').strip()
            if tipo_doc == 'SICOSS' and periodo:
                vals['ai_scanned_doc_number'] = 'F931-{}'.format(periodo)
            elif tipo_doc == 'IVA' and periodo:
                vals['ai_scanned_doc_number'] = 'F731-{}'.format(periodo)
            elif tipo_doc == 'CM' and periodo:
                formulario = str(data.get('formulario') or 'CM03').strip().upper()
                vals['ai_scanned_doc_number'] = '{}-{}'.format(formulario, periodo)
            elif tipo_doc == 'RECIBO':
                legajo = str(data.get('legajo') or '').strip()
                nombre = str(data.get('nombre_empleado') or '').strip()
                vals['ai_scanned_doc_number'] = 'RECIBO-{}-{}'.format(legajo, periodo) if legajo else 'RECIBO-{}'.format(periodo)
        else:
            # CAE
            cae = str(data.get('cae') or '').strip()
            if re.fullmatch(r'\d{14}', cae):
                vals['ai_scanned_cae'] = cae

            # CUIT emisor
            cuit_raw = str(data.get('cuit_emisor') or '')
            valid, digits = _validate_cuit(cuit_raw)
            if valid and digits:
                vals['ai_scanned_cuit_emisor'] = '{}-{}-{}'.format(digits[:2], digits[2:10], digits[10])
            elif cuit_raw and cuit_raw != '-':
                _logger.warning('Invalid CUIT emisor from AI: %s', cuit_raw)

            # Document number (punto_de_venta + numero_comprobante)
            pdv = str(data.get('punto_de_venta') or '').strip().zfill(5)
            num = str(data.get('numero_comprobante') or '').strip().zfill(8)
            if pdv and num and pdv != '00000':
                vals['ai_scanned_doc_number'] = '{}-{}'.format(pdv, num)

        self.write(vals)

    # ------------------------------------------------------------------
    # Background queue processing (cron)
    # ------------------------------------------------------------------

    def _cron_process_queued_scans(self):
        """Cron entry point: process AI scan queue in parallel batches."""
        BATCH_SIZE = 50
        MAX_WORKERS = 5
        MAX_TOTAL_SECONDS = 90  # Stay under Odoo worker timeout
        STUCK_TIMEOUT_MINUTES = 10

        ICP = self.env['ir.config_parameter'].sudo()
        provider = ICP.get_param('l10n_ar_ai_scanner.provider', default='anthropic')
        model_name = ICP.get_param('l10n_ar_ai_scanner.model', default='claude-sonnet-4-5')

        if provider == 'gemini':
            api_key = ICP.get_param('l10n_ar_ai_scanner.gemini_api_key', default='')
        else:
            api_key = ICP.get_param('l10n_ar_ai_scanner.api_key', default='')

        if not api_key:
            _logger.warning('AI scan cron: no API key configured for %s', provider)
            return

        # Recovery: reset moves stuck in 'scanning' for too long
        stuck_cutoff = fields.Datetime.subtract(fields.Datetime.now(), minutes=STUCK_TIMEOUT_MINUTES)
        stuck_moves = self.search([
            ('ai_scan_state', '=', 'scanning'),
            ('write_date', '<', stuck_cutoff),
        ])
        if stuck_moves:
            _logger.warning(
                'AI scan cron: recovering %d stuck moves (scanning > %d min): %s',
                len(stuck_moves), STUCK_TIMEOUT_MINUTES, stuck_moves.ids,
            )
            stuck_moves.write({'ai_scan_state': 'queued'})
            self.env.cr.commit()

        start_time = time.time()
        while (time.time() - start_time) < MAX_TOTAL_SECONDS:
            moves = self.search(
                [('ai_scan_state', '=', 'queued')],
                limit=BATCH_SIZE,
                order='create_date asc',
            )
            if not moves:
                break
            try:
                self._process_scan_batch(moves, provider, api_key, model_name, MAX_WORKERS)
            except Exception:
                _logger.exception(
                    'AI scan cron: batch crashed for moves %s — resetting to queued',
                    moves.ids,
                )
                # Reset any moves still stuck in 'scanning' back to queued
                self.env.cr.rollback()
                still_scanning = self.search([
                    ('id', 'in', moves.ids),
                    ('ai_scan_state', '=', 'scanning'),
                ])
                if still_scanning:
                    still_scanning.write({'ai_scan_state': 'queued'})
                self.env.cr.commit()
                break
            self.env.cr.commit()

    def _process_scan_batch(self, moves, provider, api_key, model_name, max_workers):
        """Process a batch: prepare images, parallel API calls, apply results."""
        _logger.info(
            'AI scan batch: processing %d moves [%s] with %s/%s',
            len(moves), moves.ids, provider, model_name,
        )
        moves.write({'ai_scan_state': 'scanning'})
        self.env.cr.commit()

        # Phase 1: Prepare image data (ORM, main thread)
        tasks = []
        for move in moves:
            try:
                attachment = self.env['ir.attachment'].search([
                    ('res_model', '=', 'account.move'),
                    ('res_id', '=', move.id),
                    ('mimetype', 'in', [
                        'application/pdf', 'image/jpeg', 'image/jpg',
                        'image/png', 'image/gif', 'image/webp',
                    ]),
                ], order='id desc', limit=1)
                if not attachment:
                    move.write({'ai_scan_state': 'error'})
                    self.env['l10n_ar.ai.scan.log'].sudo().create({
                        'move_id': move.id, 'ai_model': model_name,
                        'success': False, 'error_message': 'Sin archivo adjunto',
                    })
                    continue
                image_bytes, media_type = move._prepare_image_for_scan(attachment)
                image_b64 = base64.b64encode(image_bytes).decode('utf-8')
                tasks.append({
                    'move_id': move.id,
                    'image_b64': image_b64,
                    'media_type': media_type,
                })
            except Exception as exc:
                _logger.exception('Queue scan: image prep failed for move %s', move.id)
                move.write({'ai_scan_state': 'error'})
                self.env['l10n_ar.ai.scan.log'].sudo().create({
                    'move_id': move.id, 'ai_model': model_name,
                    'success': False, 'error_message': str(exc)[:500],
                })

        if not tasks:
            _logger.warning('AI scan batch: no tasks prepared (all moves failed image prep)')
            return

        _logger.info('AI scan batch: %d tasks ready, starting parallel API calls (%s)', len(tasks), provider)
        # Phase 2: Parallel API calls (no ORM, thread-safe)
        api_fn = _call_gemini_api_raw if provider == 'gemini' else _call_anthropic_api_raw
        api_results = {}

        def _do_call(task):
            scan_start = time.time()
            raw_text, tokens = api_fn(
                api_key, model_name,
                task['image_b64'], task['media_type'],
                AI_SCAN_PROMPT,
            )
            return task['move_id'], raw_text, tokens, time.time() - scan_start

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(_do_call, t): t['move_id'] for t in tasks
            }
            for future in as_completed(future_map):
                move_id = future_map[future]
                try:
                    _, raw_text, tokens, duration = future.result()
                    api_results[move_id] = {
                        'raw_text': raw_text, 'tokens': tokens,
                        'duration': duration, 'error': None,
                    }
                except Exception as exc:
                    _logger.error('AI scan API call failed for move %s: %s', move_id, exc)
                    api_results[move_id] = {
                        'raw_text': '', 'tokens': 0,
                        'duration': 0, 'error': str(exc)[:500],
                    }

        ok_count = sum(1 for r in api_results.values() if not r['error'])
        err_count = sum(1 for r in api_results.values() if r['error'])
        _logger.info('AI scan batch: API calls done — %d ok, %d errors', ok_count, err_count)

        # Phase 3: Apply results (ORM, main thread)
        for move in moves:
            result = api_results.get(move.id)
            if not result:
                continue

            if result['error']:
                move.write({'ai_scan_state': 'error'})
                self.env['l10n_ar.ai.scan.log'].sudo().create({
                    'move_id': move.id, 'ai_model': model_name,
                    'tokens_used': result['tokens'],
                    'scan_duration': result['duration'],
                    'success': False,
                    'error_message': result['error'],
                    'raw_prompt': AI_SCAN_PROMPT,
                })
                continue

            extracted = self._parse_ai_response(result['raw_text'])
            if not extracted:
                move.write({'ai_scan_state': 'error'})
                self.env['l10n_ar.ai.scan.log'].sudo().create({
                    'move_id': move.id, 'ai_model': model_name,
                    'tokens_used': result['tokens'],
                    'scan_duration': result['duration'],
                    'success': False,
                    'error_message': 'No se pudo parsear JSON de la respuesta',
                    'raw_prompt': AI_SCAN_PROMPT,
                    'raw_response': result['raw_text'],
                })
                continue

            confidence = float(extracted.get('confianza', 0) or 0)

            try:
                move._apply_extracted_data(extracted)

                if move.ai_scan_queue_auto_apply:
                    move._auto_apply_ai_data(
                        create_partner=move.ai_scan_queue_create_partner,
                    )

                self.env['l10n_ar.ai.scan.log'].sudo().create({
                    'move_id': move.id, 'ai_model': model_name,
                    'tokens_used': result['tokens'],
                    'scan_duration': result['duration'],
                    'success': True,
                    'raw_prompt': AI_SCAN_PROMPT,
                    'raw_response': result['raw_text'],
                    'extracted_data': json.dumps(extracted, ensure_ascii=False, indent=2),
                    'confidence_score': confidence,
                })
            except Exception as exc:
                _logger.exception('Queue scan: apply failed for move %s', move.id)
                move.write({'ai_scan_state': 'error'})
                self.env['l10n_ar.ai.scan.log'].sudo().create({
                    'move_id': move.id, 'ai_model': model_name,
                    'tokens_used': result['tokens'],
                    'scan_duration': result['duration'],
                    'success': False,
                    'error_message': str(exc)[:500],
                    'raw_prompt': AI_SCAN_PROMPT,
                    'raw_response': result['raw_text'],
                })

        # Safety net: any moves still stuck in 'scanning' → error
        still_scanning = moves.filtered(lambda m: m.ai_scan_state == 'scanning')
        if still_scanning:
            _logger.error(
                'AI scan batch: %d moves still in scanning state after processing: %s',
                len(still_scanning), still_scanning.ids,
            )
            still_scanning.write({'ai_scan_state': 'error'})

        _logger.info('AI scan batch: completed %d moves', len(moves))

    # ------------------------------------------------------------------
    # Auto-apply AI data (shared by wizard and cron)
    # ------------------------------------------------------------------

    def _auto_apply_ai_data(self, create_partner=True):
        """Apply AI-extracted data to the move: partner, dates, doc type, lines."""
        self.ensure_one()
        raw = self.ai_scan_raw_result
        if not raw:
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        tipo_doc = (data.get('tipo_documento') or 'FACTURA').upper()
        vals = {}

        if tipo_doc == 'SICOSS':
            partner = self._get_or_create_afip_partner('AFIP 931')
            vals['partner_id'] = partner.id
            self.write(vals)
            vals = {}

            periodo = str(data.get('periodo') or '').strip()
            if periodo and '/' in periodo:
                parsed = self._parse_date_str('01/{}'.format(periodo))
                if parsed:
                    vals['invoice_date'] = parsed

            nro_verif = str(data.get('nro_verificador') or '').strip()
            vals['ref'] = 'F931 {} (Verif. {})'.format(periodo, nro_verif) if nro_verif else 'F931 {}'.format(periodo)
            vals['ai_scanned_doc_number'] = 'F931-{}'.format(periodo) if periodo else ''
            if vals:
                self.write(vals)
            if self.state == 'draft':
                self._create_ai_invoice_lines(data)
            return

        if tipo_doc == 'IVA':
            partner = self._get_or_create_afip_partner('AFIP 731')
            vals['partner_id'] = partner.id
            self.write(vals)
            vals = {}

            periodo = str(data.get('periodo') or '').strip()
            if periodo and '/' in periodo:
                parsed = self._parse_date_str('01/{}'.format(periodo))
                if parsed:
                    vals['invoice_date'] = parsed

            nro_verif = str(data.get('nro_verificador') or '').strip()
            vals['ref'] = 'F731 IVA {} (Verif. {})'.format(periodo, nro_verif) if nro_verif else 'F731 IVA {}'.format(periodo)
            vals['ai_scanned_doc_number'] = 'F731-{}'.format(periodo) if periodo else ''
            if vals:
                self.write(vals)
            if self.state == 'draft':
                self._create_ai_invoice_lines(data)
            return

        if tipo_doc == 'CM':
            provincia = str(data.get('provincia') or '').strip()
            partner_name = 'Convenio Multilateral - {}'.format(provincia) if provincia else 'Convenio Multilateral'
            partner = self._get_or_create_afip_partner(partner_name)
            vals['partner_id'] = partner.id
            self.write(vals)
            vals = {}

            periodo = str(data.get('periodo') or '').strip()
            if periodo and '/' in periodo:
                parsed = self._parse_date_str('01/{}'.format(periodo))
                if parsed:
                    vals['invoice_date'] = parsed

            formulario = str(data.get('formulario') or 'CM03').strip().upper()
            nro_verif = str(data.get('nro_verificador') or '').strip()
            vals['ref'] = '{} {} {} (Verif. {})'.format(formulario, provincia, periodo, nro_verif) if nro_verif else '{} {} {}'.format(formulario, provincia, periodo)
            vals['ai_scanned_doc_number'] = '{}-{}'.format(formulario, periodo) if periodo else ''
            if vals:
                self.write(vals)
            if self.state == 'draft':
                self._create_ai_invoice_lines(data)
            return

        if tipo_doc == 'RECIBO':
            nombre_empleado = str(data.get('nombre_empleado') or '').strip()
            legajo = str(data.get('legajo') or '').strip()
            partner_name = nombre_empleado or 'Empleado Leg. {}'.format(legajo)
            partner = self._get_or_create_employee_partner(partner_name, legajo)
            vals['partner_id'] = partner.id
            self.write(vals)
            vals = {}

            periodo = str(data.get('periodo') or '').strip()
            if periodo and '/' in periodo:
                parsed = self._parse_date_str('01/{}'.format(periodo))
                if parsed:
                    vals['invoice_date'] = parsed

            fecha_pago = str(data.get('fecha_pago') or '').strip()
            if fecha_pago:
                parsed = self._parse_date_str(fecha_pago)
                if parsed:
                    vals['invoice_date'] = parsed

            vals['ref'] = 'Recibo sueldo {} Leg.{} {}'.format(nombre_empleado, legajo, periodo)
            vals['ai_scanned_doc_number'] = 'RECIBO-{}-{}'.format(legajo, periodo) if legajo else 'RECIBO-{}'.format(periodo)
            if vals:
                self.write(vals)
            if self.state == 'draft':
                self._create_ai_invoice_lines(data)
            return

        # --- FACTURA ---
        cuit_field = 'cuit_emisor' if self.move_type in ('in_invoice', 'in_refund') else 'cuit_receptor'
        partner_cuit = data.get(cuit_field) or ''
        if partner_cuit and partner_cuit != '-':
            valid, digits = _validate_cuit(partner_cuit)
            if valid and digits:
                ar_vat = 'AR' + digits
                formatted_cuit = '{}-{}-{}'.format(digits[:2], digits[2:10], digits[10])
                partner = self.env['res.partner'].search([
                    '|', '|', '|',
                    ('vat', '=', ar_vat),
                    ('vat', '=', digits),
                    ('vat', '=', formatted_cuit),
                    ('vat', 'ilike', digits),
                ], limit=1)
                if partner:
                    vals['partner_id'] = partner.id
                elif create_partner:
                    name_field = 'razon_social_emisor' if self.move_type in ('in_invoice', 'in_refund') else 'razon_social_receptor'
                    partner_name = data.get(name_field) or formatted_cuit
                    if partner_name == '-':
                        partner_name = formatted_cuit
                    new_partner_vals = {
                        'name': partner_name,
                        'vat': digits,
                        'company_type': 'company',
                        'supplier_rank': 1 if self.move_type in ('in_invoice', 'in_refund') else 0,
                        'customer_rank': 1 if self.move_type in ('out_invoice', 'out_refund') else 0,
                        'country_id': self.env.ref('base.ar', raise_if_not_found=False).id or False,
                    }
                    tipo = (data.get('tipo_comprobante') or '').upper()
                    if hasattr(self.env['res.partner'], 'l10n_ar_afip_responsibility_type_id'):
                        afip_resp = False
                        if 'A' in tipo:
                            afip_resp = self.env.ref('l10n_ar.res_IVARI', raise_if_not_found=False)
                        elif 'B' in tipo:
                            afip_resp = self.env.ref('l10n_ar.res_CF', raise_if_not_found=False)
                        elif 'C' in tipo:
                            afip_resp = self.env.ref('l10n_ar.res_mono', raise_if_not_found=False)
                        if afip_resp:
                            new_partner_vals['l10n_ar_afip_responsibility_type_id'] = afip_resp.id
                    partner = self.env['res.partner'].create(new_partner_vals)
                    vals['partner_id'] = partner.id

        tipo_comprobante = data.get('tipo_comprobante') or ''
        if tipo_comprobante and hasattr(self, 'l10n_latam_document_type_id'):
            doc_type = self._find_document_type_from_ai(tipo_comprobante)
            if doc_type:
                vals['l10n_latam_document_type_id'] = doc_type.id

        if vals:
            self.write(vals)
            vals = {}

        pdv = str(data.get('punto_de_venta') or '').strip().zfill(5)
        num = str(data.get('numero_comprobante') or '').strip().zfill(8)
        if pdv != '00000' and num != '00000000':
            doc_number = '{}-{}'.format(pdv, num)
            vals['ai_scanned_doc_number'] = doc_number
            if hasattr(self, 'l10n_latam_document_number'):
                vals['l10n_latam_document_number'] = doc_number

        fecha = data.get('fecha_emision') or ''
        if fecha:
            parsed = self._parse_date_str(fecha)
            if parsed:
                vals['invoice_date'] = parsed

        ref_parts = []
        if tipo_comprobante:
            ref_parts.append(tipo_comprobante)
        if pdv != '00000':
            ref_parts.append('{}-{}'.format(pdv, num))
        if ref_parts:
            vals['ref'] = ' '.join(ref_parts)

        if vals:
            self.write(vals)

        if self.state == 'draft':
            self._create_ai_invoice_lines(data)

    def _create_ai_invoice_lines(self, data):
        """Create invoice lines from AI-extracted data (SICOSS / IVA / FACTURA)."""
        from odoo.fields import Command

        lines = []
        tipo_doc = (data.get('tipo_documento') or 'FACTURA').upper()

        if tipo_doc == 'SICOSS':
            periodo = data.get('periodo') or ''
            acct_contrib = self._get_mapped_account('account_contribuciones')
            acct_aportes = self._get_mapped_account('account_aportes')

            contrib_ss = _parse_amount(data.get('contrib_ss'))
            contrib_os = _parse_amount(data.get('contrib_os'))
            lrt = _parse_amount(data.get('lrt'))
            svo = _parse_amount(data.get('svo'))
            renatre = _parse_amount(data.get('renatre'))
            sepelio = _parse_amount(data.get('sepelio_uatre'))
            vales = _parse_amount(data.get('vales_alimentarios'))
            total_contribuciones = contrib_ss + contrib_os + lrt + svo + renatre + sepelio + vales

            if total_contribuciones > 0:
                detail_parts = []
                if contrib_ss: detail_parts.append('SS: {:,.2f}'.format(contrib_ss))
                if contrib_os: detail_parts.append('OS: {:,.2f}'.format(contrib_os))
                if lrt: detail_parts.append('LRT: {:,.2f}'.format(lrt))
                if svo: detail_parts.append('SVO: {:,.2f}'.format(svo))
                if renatre: detail_parts.append('RENATRE: {:,.2f}'.format(renatre))
                if sepelio: detail_parts.append('Sepelio: {:,.2f}'.format(sepelio))
                if vales: detail_parts.append('Vales: {:,.2f}'.format(vales))
                detail = ' ({})'.format(', '.join(detail_parts)) if detail_parts else ''
                line_vals = {
                    'name': 'Contribuciones patronales F931 {}{}'.format(periodo, detail),
                    'price_unit': total_contribuciones,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_contrib:
                    line_vals['account_id'] = acct_contrib.id
                lines.append(Command.create(line_vals))

            aporte_ss = _parse_amount(data.get('aporte_ss'))
            aporte_os = _parse_amount(data.get('aporte_os'))
            total_aportes = aporte_ss + aporte_os

            if total_aportes > 0:
                detail_parts = []
                if aporte_ss: detail_parts.append('SS: {:,.2f}'.format(aporte_ss))
                if aporte_os: detail_parts.append('OS: {:,.2f}'.format(aporte_os))
                detail = ' ({})'.format(', '.join(detail_parts)) if detail_parts else ''
                line_vals = {
                    'name': 'Aportes empleados F931 {}{}'.format(periodo, detail),
                    'price_unit': total_aportes,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_aportes:
                    line_vals['account_id'] = acct_aportes.id
                lines.append(Command.create(line_vals))

        elif tipo_doc == 'IVA':
            periodo = data.get('periodo') or ''
            acct_debito = self._get_mapped_account('account_iva_debito')
            acct_credito = self._get_mapped_account('account_iva_credito')
            acct_saldo_libre = self._get_mapped_account('account_iva_saldo_libre')
            acct_retencion = self._get_mapped_account('account_iva_retencion')
            acct_percepcion = self._get_mapped_account('account_iva_percepcion')

            debito = _parse_amount(data.get('debito_fiscal'))
            credito = _parse_amount(data.get('credito_fiscal'))
            saldo_tec_ant = _parse_amount(data.get('saldo_tecnico_anterior'))
            ret_perc = _parse_amount(data.get('retenciones_percepciones'))
            saldo_libre_ant = _parse_amount(data.get('saldo_libre_disp_anterior'))

            if debito > 0:
                line_vals = {
                    'name': 'Débito Fiscal IVA F731 {}'.format(periodo),
                    'price_unit': debito,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_debito:
                    line_vals['account_id'] = acct_debito.id
                lines.append(Command.create(line_vals))

            if credito > 0:
                line_vals = {
                    'name': 'Crédito Fiscal IVA F731 {}'.format(periodo),
                    'price_unit': -credito,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_credito:
                    line_vals['account_id'] = acct_credito.id
                lines.append(Command.create(line_vals))

            if saldo_tec_ant > 0:
                line_vals = {
                    'name': 'Saldo técnico anterior IVA F731 {}'.format(periodo),
                    'price_unit': saldo_tec_ant,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_debito:
                    line_vals['account_id'] = acct_debito.id
                lines.append(Command.create(line_vals))

            if ret_perc > 0:
                line_vals = {
                    'name': 'Retenciones IVA sufridas F731 {}'.format(periodo),
                    'price_unit': -ret_perc,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_retencion:
                    line_vals['account_id'] = acct_retencion.id
                lines.append(Command.create(line_vals))

            if saldo_libre_ant > 0:
                line_vals = {
                    'name': 'Saldo libre disponibilidad anterior IVA F731 {}'.format(periodo),
                    'price_unit': -saldo_libre_ant,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_saldo_libre:
                    line_vals['account_id'] = acct_saldo_libre.id
                lines.append(Command.create(line_vals))

        elif tipo_doc == 'CM':
            periodo = data.get('periodo') or ''
            provincia = str(data.get('provincia') or '').strip()
            formulario = str(data.get('formulario') or 'CM03').strip().upper()
            cm_accts = self._get_cm_jurisdiction_accounts(provincia)
            acct_impuesto = cm_accts.get('account_cm_impuesto')
            acct_ret_bancarias = cm_accts.get('account_cm_ret_bancarias')
            acct_ret_iibb = cm_accts.get('account_cm_ret_iibb')
            acct_perc_iibb = cm_accts.get('account_cm_perc_iibb')
            acct_saldo_favor = cm_accts.get('account_cm_saldo_favor')

            impuesto = _parse_amount(data.get('impuesto_determinado'))
            ret_bancarias = _parse_amount(data.get('retenciones_bancarias'))
            ret_iibb = _parse_amount(data.get('retenciones_iibb'))
            perc_iibb = _parse_amount(data.get('percepciones_iibb'))
            saldo_favor_ant = _parse_amount(data.get('saldo_favor_anterior'))

            label_suffix = '{} {} {}'.format(formulario, provincia, periodo).strip()

            if impuesto > 0:
                line_vals = {
                    'name': 'Impuesto determinado {}'.format(label_suffix),
                    'price_unit': impuesto,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_impuesto:
                    line_vals['account_id'] = acct_impuesto.id
                lines.append(Command.create(line_vals))

            if ret_bancarias > 0:
                line_vals = {
                    'name': 'Retenciones bancarias {}'.format(label_suffix),
                    'price_unit': -ret_bancarias,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_ret_bancarias:
                    line_vals['account_id'] = acct_ret_bancarias.id
                lines.append(Command.create(line_vals))

            if ret_iibb > 0:
                line_vals = {
                    'name': 'Retenciones IIBB {}'.format(label_suffix),
                    'price_unit': -ret_iibb,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_ret_iibb:
                    line_vals['account_id'] = acct_ret_iibb.id
                lines.append(Command.create(line_vals))

            if perc_iibb > 0:
                line_vals = {
                    'name': 'Percepciones IIBB {}'.format(label_suffix),
                    'price_unit': -perc_iibb,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_perc_iibb:
                    line_vals['account_id'] = acct_perc_iibb.id
                lines.append(Command.create(line_vals))

            if saldo_favor_ant > 0:
                line_vals = {
                    'name': 'Saldo a favor anterior {}'.format(label_suffix),
                    'price_unit': -saldo_favor_ant,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_saldo_favor:
                    line_vals['account_id'] = acct_saldo_favor.id
                lines.append(Command.create(line_vals))

        elif tipo_doc == 'RECIBO':
            periodo = data.get('periodo') or ''
            nombre = str(data.get('nombre_empleado') or '').strip()
            legajo = str(data.get('legajo') or '').strip()
            acct_neto = self._get_mapped_account('account_recibo_neto')

            neto = _parse_amount(data.get('neto_a_cobrar'))
            if neto > 0:
                line_vals = {
                    'name': 'Neto de bolsillo {} Leg.{} {}'.format(nombre, legajo, periodo),
                    'price_unit': neto,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_neto:
                    line_vals['account_id'] = acct_neto.id
                lines.append(Command.create(line_vals))

        else:
            # --- Regular invoice ---
            tax_type = 'purchase' if self.move_type in ('in_invoice', 'in_refund') else 'sale'
            tax_21 = self._find_tax_by_amount(21.0, tax_type)
            tax_105 = self._find_tax_by_amount(10.5, tax_type)
            tax_27 = self._find_tax_by_amount(27.0, tax_type)
            tax_map = {
                21: tax_21, 21.0: tax_21,
                10.5: tax_105, 10: tax_105,
                27: tax_27, 27.0: tax_27,
            }

            productos = data.get('productos') or []
            if productos and isinstance(productos, list):
                for prod in productos:
                    if not isinstance(prod, dict):
                        continue
                    desc = prod.get('descripcion') or _('Producto (IA)')
                    qty = float(prod.get('cantidad') or 1) or 1
                    price = _parse_amount(prod.get('precio_unitario'))
                    iva_pct = float(prod.get('iva_porcentaje') or 0)
                    if price <= 0:
                        continue
                    line_vals = {'name': desc, 'price_unit': price, 'quantity': qty}
                    tax = tax_map.get(iva_pct)
                    if tax:
                        line_vals['tax_ids'] = [Command.set([tax.id])]
                    elif iva_pct == 0:
                        line_vals['tax_ids'] = [Command.clear()]
                    lines.append(Command.create(line_vals))

                for field_key, label in [
                    ('neto_no_gravado', _('Neto No Gravado')),
                    ('exento', _('Exento')),
                    ('otros_tributos', _('Otros Tributos / Percepciones')),
                ]:
                    amount = _parse_amount(data.get(field_key))
                    if amount > 0:
                        lines.append(Command.create({
                            'name': label, 'price_unit': amount,
                            'quantity': 1, 'tax_ids': [Command.clear()],
                        }))
            else:
                for field_key, tax_amount, label in [
                    ('neto_gravado_21', 21.0, _('Neto Gravado 21%%')),
                    ('neto_gravado_105', 10.5, _('Neto Gravado 10.5%%')),
                    ('neto_gravado_27', 27.0, _('Neto Gravado 27%%')),
                ]:
                    amount = _parse_amount(data.get(field_key))
                    if amount > 0:
                        tax = tax_map.get(tax_amount)
                        line_vals = {'name': label, 'price_unit': amount, 'quantity': 1}
                        if tax:
                            line_vals['tax_ids'] = [Command.set([tax.id])]
                        lines.append(Command.create(line_vals))

                for field_key, label in [
                    ('neto_no_gravado', _('Neto No Gravado')),
                    ('exento', _('Exento')),
                    ('otros_tributos', _('Otros Tributos / Percepciones')),
                ]:
                    amount = _parse_amount(data.get(field_key))
                    if amount > 0:
                        lines.append(Command.create({
                            'name': label, 'price_unit': amount,
                            'quantity': 1, 'tax_ids': [Command.clear()],
                        }))

        if lines:
            self.write({'invoice_line_ids': lines})

    # ------------------------------------------------------------------
    # Shared helpers (used by auto-apply and wizards)
    # ------------------------------------------------------------------

    def _get_or_create_afip_partner(self, name):
        """Find or create an AFIP partner by name (e.g. 'AFIP 731', 'AFIP 931')."""
        partner = self.env['res.partner'].search([('name', '=', name)], limit=1)
        if not partner:
            new_vals = {
                'name': name,
                'company_type': 'company',
                'supplier_rank': 1,
                'country_id': self.env.ref('base.ar', raise_if_not_found=False).id or False,
            }
            if hasattr(self.env['res.partner'], 'l10n_ar_afip_responsibility_type_id'):
                afip_resp = self.env.ref('l10n_ar.res_IVARI', raise_if_not_found=False)
                if afip_resp:
                    new_vals['l10n_ar_afip_responsibility_type_id'] = afip_resp.id
            partner = self.env['res.partner'].create(new_vals)
        return partner

    def _get_or_create_employee_partner(self, name, legajo=''):
        """Find or create an employee partner by name (for payroll receipts)."""
        partner = self.env['res.partner'].search([('name', '=', name)], limit=1)
        if not partner:
            new_vals = {
                'name': name,
                'company_type': 'person',
                'supplier_rank': 1,
                'country_id': self.env.ref('base.ar', raise_if_not_found=False).id or False,
            }
            if legajo:
                new_vals['ref'] = 'Leg. {}'.format(legajo)
            partner = self.env['res.partner'].create(new_vals)
        return partner

    def _get_mapped_account(self, param_key):
        """Get a mapped account from system parameters. Returns account.account or False."""
        ICP = self.env['ir.config_parameter'].sudo()
        account_id = ICP.get_param('l10n_ar_ai_scanner.{}'.format(param_key), default='')
        if account_id:
            try:
                account = self.env['account.account'].browse(int(account_id))
                if account.exists():
                    return account
            except (ValueError, TypeError):
                pass
        return False

    def _get_cm_jurisdiction_accounts(self, provincia):
        """Get CM account mapping for a given jurisdiction (provincia).

        Searches for an exact match first, then tries case-insensitive / partial match.
        Returns a dict with keys: account_cm_impuesto, account_cm_ret_bancarias,
        account_cm_ret_iibb, account_cm_perc_iibb, account_cm_saldo_favor.
        Values are account.account recordsets or False.
        """
        empty = {
            'account_cm_impuesto': False,
            'account_cm_ret_bancarias': False,
            'account_cm_ret_iibb': False,
            'account_cm_perc_iibb': False,
            'account_cm_saldo_favor': False,
        }
        if not provincia:
            return empty
        company = self.company_id or self.env.company
        CmJur = self.env['l10n_ar.ai.scanner.cm.jurisdiction'].sudo()
        # Exact match
        jur = CmJur.search([
            ('name', '=', provincia),
            ('company_id', '=', company.id),
        ], limit=1)
        if not jur:
            # Case-insensitive / like match
            jur = CmJur.search([
                ('name', '=ilike', provincia),
                ('company_id', '=', company.id),
            ], limit=1)
        if not jur:
            # Partial match (provincia contained in name or vice versa)
            jur = CmJur.search([
                ('name', 'ilike', provincia),
                ('company_id', '=', company.id),
            ], limit=1)
        if not jur:
            return empty
        return {
            'account_cm_impuesto': jur.account_cm_impuesto or False,
            'account_cm_ret_bancarias': jur.account_cm_ret_bancarias or False,
            'account_cm_ret_iibb': jur.account_cm_ret_iibb or False,
            'account_cm_perc_iibb': jur.account_cm_perc_iibb or False,
            'account_cm_saldo_favor': jur.account_cm_saldo_favor or False,
        }

    def _find_tax_by_amount(self, tax_amount, type_tax_use):
        """Search for a tax record by amount and type."""
        company_id = self.company_id.id
        tax = self.env['account.tax'].search([
            ('amount', '=', tax_amount),
            ('type_tax_use', '=', type_tax_use),
            ('company_id', '=', company_id),
        ], limit=1)
        if not tax:
            tax = self.env['account.tax'].search([
                ('amount', '=', tax_amount),
                ('type_tax_use', '=', type_tax_use),
            ], limit=1)
        return tax

    def _find_document_type_from_ai(self, tipo_text):
        """Map AI tipo_comprobante text to l10n_latam.document.type."""
        tipo_upper = (tipo_text or '').strip().upper()
        afip_code_map = {
            'FACTURA A': '1', 'FACTURA B': '6', 'FACTURA C': '11',
            'FACTURA E': '19', 'FACTURA M': '51',
            'NOTA DE DEBITO A': '2', 'NOTA DE DEBITO B': '7',
            'NOTA DE DEBITO C': '12', 'NOTA DE DEBITO M': '52',
            'NOTA DE CREDITO A': '3', 'NOTA DE CREDITO B': '8',
            'NOTA DE CREDITO C': '13', 'NOTA DE CREDITO M': '53',
        }
        code = afip_code_map.get(tipo_upper)
        if code:
            doc = self.env['l10n_latam.document.type'].search([
                ('code', '=', code), ('country_id.code', '=', 'AR'),
            ], limit=1)
            if doc:
                return doc
        if tipo_upper:
            return self.env['l10n_latam.document.type'].search([
                ('name', 'ilike', tipo_upper), ('country_id.code', '=', 'AR'),
            ], limit=1)
        return False

    def _parse_date_str(self, date_str):
        """Parse DD/MM/YYYY to YYYY-MM-DD string."""
        if not date_str:
            return False
        m = re.match(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})', str(date_str).strip())
        if m:
            return '%s-%s-%s' % (m.group(3), m.group(2).zfill(2), m.group(1).zfill(2))
        return False
