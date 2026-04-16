# Reporte Técnico: Módulo Odoo 19 — Escáner de Facturas y Tickets Argentinos con IA

**Fecha:** Marzo 2026
**Versión objetivo:** Odoo 19.0
**Alcance:** OCR + LLM para facturas y tickets fiscales argentinos (AFIP/ARCA)

---

## Índice

1. [Estructura del Módulo (File Tree)](#1-estructura-del-módulo)
2. [Manifest __manifest__.py](#2-manifest)
3. [Modelos Odoo a Usar/Extender](#3-modelos-odoo)
4. [Campos de Facturas Argentinas y Mapeo a account.move](#4-campos-argentinos)
5. [Regulaciones AFIP/ARCA: Tipos de Comprobante y CAE/CAI](#5-regulaciones-afip)
6. [Estructura del Módulo l10n_ar en Odoo 19](#6-l10n_ar)
7. [Manejo de Adjuntos ir.attachment](#7-ir-attachment)
8. [Enfoque AI/OCR Recomendado: Pros y Contras](#8-enfoque-ai-ocr)
9. [Dependencias Python Requeridas](#9-dependencias-python)
10. [Flujo de Procesamiento Recomendado](#10-flujo)

---

## 1. Estructura del Módulo

```
l10n_ar_ai_invoice_scanner/
│
├── __init__.py
├── __manifest__.py
│
├── models/
│   ├── __init__.py
│   ├── account_move.py          # Hereda account.move — campos adicionales AR
│   ├── ir_attachment.py         # Hereda ir.attachment — trigger de escaneo
│   └── ai_invoice_scan_log.py   # Modelo de log/auditoría de escaneos
│
├── wizard/
│   ├── __init__.py
│   └── scan_invoice_wizard.py   # Wizard para escaneo manual desde adjunto
│
├── controllers/
│   ├── __init__.py
│   └── main.py                  # Rutas HTTP para callback async (opcional)
│
├── views/
│   ├── account_move_views.xml       # Hereda vista de factura — botón Escanear
│   ├── scan_invoice_wizard_views.xml
│   └── ai_invoice_scan_log_views.xml
│
├── security/
│   ├── ir.model.access.csv
│   └── security_groups.xml      # Grupo: l10n_ar_ai_scanner.group_scanner_user
│
├── data/
│   └── ir_config_parameter.xml  # Parámetros: clave API, modelo LLM
│
├── static/
│   └── description/
│       ├── icon.png
│       └── index.html
│
├── i18n/
│   ├── es_AR.po
│   └── l10n_ar_ai_invoice_scanner.pot
│
└── tests/
    ├── __init__.py
    ├── test_invoice_extraction.py
    └── sample_invoices/          # Facturas de prueba (no subir a producción)
```

---

## 2. Manifest

```python
# l10n_ar_ai_invoice_scanner/__manifest__.py

{
    'name': 'AR Invoice AI Scanner',
    'version': '19.0.1.0.0',
    'summary': 'Escaneo de facturas y tickets argentinos con IA (OCR + LLM)',
    'description': """
        Módulo para escanear facturas electrónicas y tickets fiscales argentinos
        usando visión por IA (Claude, GPT-4o o Google Vision).
        Extrae automáticamente: CUIT emisor/receptor, CAE, tipo de comprobante,
        número de factura, fecha, importe neto, IVA y total.
        Compatible con facturas tipo A, B, C, E, M y tickets fiscales.
        Requiere Odoo 19 con la localización argentina (l10n_ar).
    """,
    'author': 'Tu Empresa / Nombre',
    'website': 'https://tu-empresa.com.ar',
    'category': 'Accounting/Localizations',
    'license': 'LGPL-3',

    'depends': [
        'account',                          # Módulo base de contabilidad
        'l10n_ar',                          # Localización argentina
        'l10n_latam_invoice_document',      # Documentos LATAM (tipos de comprobante)
        'base_setup',                       # Configuración básica
        'mail',                             # Para chatter/log en facturas
    ],

    'data': [
        # Seguridad — siempre primero
        'security/security_groups.xml',
        'security/ir.model.access.csv',

        # Datos de configuración
        'data/ir_config_parameter.xml',

        # Vistas
        'views/account_move_views.xml',
        'views/scan_invoice_wizard_views.xml',
        'views/ai_invoice_scan_log_views.xml',

        # Wizard
        'wizard/scan_invoice_wizard_views.xml',  # alias si se separa
    ],

    'demo': [],

    'assets': {
        'web.assets_backend': [
            # 'l10n_ar_ai_invoice_scanner/static/src/js/scanner_widget.js',
        ],
    },

    'installable': True,
    'application': False,
    'auto_install': False,

    # Odoo 19: external_dependencies para Python
    'external_dependencies': {
        'python': ['anthropic', 'pdf2image', 'PIL'],
    },
}
```

---

## 3. Modelos Odoo

### 3.1 Herencia de `account.move`

```python
# models/account_move.py
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
import json
import logging

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    # ─── Campos de escaneo AI ────────────────────────────────────────────────
    l10n_ar_scan_state = fields.Selection([
        ('not_scanned', 'No escaneado'),
        ('pending',     'Procesando'),
        ('done',        'Extraído'),
        ('error',       'Error'),
    ], string='Estado de Escaneo', default='not_scanned', copy=False)

    l10n_ar_scan_confidence = fields.Float(
        string='Confianza IA (%)', digits=(5, 2), readonly=True
    )
    l10n_ar_scan_raw_response = fields.Text(
        string='Respuesta RAW IA', readonly=True, copy=False
    )
    l10n_ar_scan_log_ids = fields.One2many(
        'l10n_ar.ai.invoice.scan.log', 'move_id',
        string='Log de Escaneos'
    )

    # ─── Campos AFIP extraídos (redundantes con l10n_ar pero útiles para
    #     almacenar el valor tal como fue leído del documento) ─────────────────
    l10n_ar_afip_cuit_emisor   = fields.Char('CUIT Emisor (escaneado)', size=13)
    l10n_ar_afip_cuit_receptor = fields.Char('CUIT Receptor (escaneado)', size=13)
    l10n_ar_afip_cae_scanned   = fields.Char('CAE (escaneado)', size=14)
    l10n_ar_afip_cae_due_date_scanned = fields.Date('Vto. CAE (escaneado)')
    l10n_ar_afip_doc_number_scanned   = fields.Char('Nro. Comprobante (escaneado)')
    # Ej: "00015-00001234" → punto_venta=00015, nro=00001234

    # ─── Acción: lanzar escaneo ───────────────────────────────────────────────
    def action_scan_with_ai(self):
        """Abre el wizard de escaneo para el adjunto principal."""
        self.ensure_one()
        attachment = self.env['ir.attachment'].search([
            ('res_model', '=', 'account.move'),
            ('res_id', '=', self.id),
        ], limit=1, order='id desc')

        if not attachment:
            raise UserError(_('No hay adjunto para escanear.'))

        return {
            'type': 'ir.actions.act_window',
            'name': _('Escanear Factura con IA'),
            'res_model': 'l10n_ar.scan.invoice.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_move_id': self.id,
                'default_attachment_id': attachment.id,
            },
        }
```

### 3.2 Herencia de `ir.attachment` — trigger automático

```python
# models/ir_attachment.py
from odoo import models, api
import logging

_logger = logging.getLogger(__name__)

INVOICE_MIMETYPES = {'application/pdf', 'image/jpeg', 'image/png', 'image/webp'}

class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model_create_multi
    def create(self, vals_list):
        """Al crear un adjunto en account.move, dispara escaneo automático
        si está habilitado en la configuración del sistema."""
        attachments = super().create(vals_list)
        auto_scan = self.env['ir.config_parameter'].sudo().get_param(
            'l10n_ar_ai_scanner.auto_scan', default='False'
        )
        if auto_scan == 'True':
            for att in attachments:
                if (
                    att.res_model == 'account.move'
                    and att.mimetype in INVOICE_MIMETYPES
                ):
                    move = self.env['account.move'].browse(att.res_id)
                    if move.exists() and move.l10n_ar_scan_state == 'not_scanned':
                        move.sudo().with_delay().action_auto_scan(att.id)
        return attachments
```

### 3.3 Modelo de Log/Auditoría

```python
# models/ai_invoice_scan_log.py
from odoo import models, fields

class AiInvoiceScanLog(models.Model):
    _name = 'l10n_ar.ai.invoice.scan.log'
    _description = 'Log de Escaneo AI de Facturas'
    _order = 'create_date desc'

    move_id        = fields.Many2one('account.move', string='Factura', ondelete='cascade')
    attachment_id  = fields.Many2one('ir.attachment', string='Adjunto')
    provider       = fields.Selection([
        ('claude',  'Anthropic Claude'),
        ('openai',  'OpenAI GPT-4o'),
        ('google',  'Google Vision'),
        ('tesseract','Tesseract (local)'),
    ], string='Proveedor IA')
    model_used     = fields.Char('Modelo utilizado')
    tokens_input   = fields.Integer('Tokens entrada')
    tokens_output  = fields.Integer('Tokens salida')
    cost_usd       = fields.Float('Costo estimado (USD)', digits=(10, 6))
    duration_ms    = fields.Integer('Duración (ms)')
    success        = fields.Boolean('Exitoso')
    error_message  = fields.Text('Mensaje de error')
    extracted_json = fields.Text('JSON extraído')
    create_date    = fields.Datetime('Fecha', readonly=True)
```

### 3.4 Wizard de Escaneo

```python
# wizard/scan_invoice_wizard.py
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
import json
import logging
import time

_logger = logging.getLogger(__name__)


class ScanInvoiceWizard(models.TransientModel):
    _name = 'l10n_ar.scan.invoice.wizard'
    _description = 'Wizard de Escaneo de Factura Argentina con IA'

    move_id       = fields.Many2one('account.move', string='Factura', required=True)
    attachment_id = fields.Many2one('ir.attachment', string='Adjunto', required=True)
    provider      = fields.Selection([
        ('claude',  'Anthropic Claude (recomendado)'),
        ('openai',  'OpenAI GPT-4o'),
        ('google',  'Google Vision API'),
    ], string='Proveedor IA', default='claude', required=True)

    # Resultados pre-visualizables
    result_cuit_emisor     = fields.Char('CUIT Emisor')
    result_cuit_receptor   = fields.Char('CUIT Receptor')
    result_doc_type        = fields.Char('Tipo Comprobante')
    result_doc_number      = fields.Char('Nro. Comprobante')
    result_date            = fields.Date('Fecha')
    result_cae             = fields.Char('CAE')
    result_cae_due         = fields.Date('Vto. CAE')
    result_amount_untaxed  = fields.Float('Neto gravado')
    result_amount_tax      = fields.Float('IVA')
    result_amount_total    = fields.Float('Total')
    result_currency        = fields.Char('Moneda')
    result_raw_json        = fields.Text('JSON completo extraído', readonly=True)
    state = fields.Selection([
        ('draft',     'Listo para escanear'),
        ('scanned',   'Extraído — revisar'),
        ('confirmed', 'Confirmado'),
    ], default='draft')

    def action_scan(self):
        """Llama al proveedor de IA y rellena los campos de resultado."""
        self.ensure_one()
        provider = self.provider
        attachment = self.attachment_id

        image_b64, media_type = self._get_image_base64(attachment)
        config = self._get_provider_config(provider)
        t0 = time.time()

        try:
            if provider == 'claude':
                result = self._call_claude(image_b64, media_type, config)
            elif provider == 'openai':
                result = self._call_openai(image_b64, media_type, config)
            elif provider == 'google':
                result = self._call_google_vision(image_b64, config)
            else:
                raise UserError(_('Proveedor no soportado.'))
        except Exception as e:
            _logger.exception('Error en escaneo IA: %s', e)
            raise UserError(_('Error al escanear: %s') % str(e))

        duration_ms = int((time.time() - t0) * 1000)
        self._populate_results(result, duration_ms, provider)
        return {'type': 'ir.actions.act_window_close'}   # queda abierto en state=scanned

    def action_confirm(self):
        """Aplica los resultados extraídos a la factura en account.move."""
        self.ensure_one()
        move = self.move_id
        vals = {}

        # Campos propios del módulo (escaneados)
        vals.update({
            'l10n_ar_afip_cuit_emisor':          self.result_cuit_emisor,
            'l10n_ar_afip_cuit_receptor':        self.result_cuit_receptor,
            'l10n_ar_afip_cae_scanned':          self.result_cae,
            'l10n_ar_afip_cae_due_date_scanned': self.result_cae_due,
            'l10n_ar_afip_doc_number_scanned':   self.result_doc_number,
            'l10n_ar_scan_state':                'done',
            'l10n_ar_scan_raw_response':         self.result_raw_json,
        })

        # Campos nativos de account.move
        if self.result_date:
            vals['invoice_date'] = self.result_date
        if self.result_amount_total:
            # Nota: no se sobreescribe el total calculado; se usa como referencia
            pass

        # l10n_latam_document_number (número de comprobante LATAM)
        if self.result_doc_number:
            vals['l10n_latam_document_number'] = self.result_doc_number

        move.write(vals)
        move.message_post(
            body=_('Factura escaneada con IA (%s). CAE: %s') % (
                self.provider, self.result_cae or 'N/A'
            )
        )
        return {'type': 'ir.actions.act_window_close'}

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _get_image_base64(self, attachment):
        """Convierte el adjunto a base64. Si es PDF, convierte la primera página."""
        if attachment.mimetype == 'application/pdf':
            try:
                from pdf2image import convert_from_bytes
                from io import BytesIO
                pdf_bytes = base64.b64decode(attachment.datas)
                pages = convert_from_bytes(pdf_bytes, dpi=200, first_page=1, last_page=1)
                buf = BytesIO()
                pages[0].save(buf, format='JPEG', quality=95)
                b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                return b64, 'image/jpeg'
            except ImportError:
                raise UserError(_(
                    'pdf2image no está instalado. '
                    'Instale con: pip install pdf2image'
                ))
        else:
            return attachment.datas.decode('utf-8'), attachment.mimetype

    def _get_provider_config(self, provider):
        params = self.env['ir.config_parameter'].sudo()
        return {
            'api_key': params.get_param(f'l10n_ar_ai_scanner.{provider}_api_key'),
            'model':   params.get_param(
                f'l10n_ar_ai_scanner.{provider}_model',
                default={
                    'claude': 'claude-opus-4-6',
                    'openai': 'gpt-4o',
                    'google': 'vision-v1p3beta1',
                }.get(provider, '')
            ),
        }

    def _build_prompt(self):
        return """Eres un extractor especializado de facturas y tickets fiscales ARGENTINOS.
Analiza la imagen y extrae los siguientes campos en formato JSON estricto.
Si un campo no está presente, usa null.

Responde ÚNICAMENTE con el JSON, sin texto adicional:
{
  "tipo_comprobante": "A|B|C|E|M|Ticket",
  "punto_venta": "XXXXX (5 dígitos)",
  "numero_comprobante": "YYYYYYYY (8 dígitos)",
  "numero_completo": "XXXXX-YYYYYYYY",
  "fecha_emision": "YYYY-MM-DD",
  "cuit_emisor": "XX-XXXXXXXX-X",
  "razon_social_emisor": "string",
  "condicion_iva_emisor": "Responsable Inscripto|Monotributista|Exento|...",
  "cuit_receptor": "XX-XXXXXXXX-X o null",
  "razon_social_receptor": "string o null",
  "condicion_iva_receptor": "string o null",
  "neto_gravado": 0.00,
  "neto_no_gravado": 0.00,
  "iva_21": 0.00,
  "iva_10_5": 0.00,
  "iva_27": 0.00,
  "otros_impuestos": 0.00,
  "importe_total": 0.00,
  "moneda": "ARS|USD|EUR",
  "cae": "XXXXXXXXXXXXXX (14 dígitos) o null",
  "vencimiento_cae": "YYYY-MM-DD o null",
  "cai": "string o null",
  "codigo_qr_presente": true|false,
  "es_ticket_fiscal": true|false,
  "confianza": 0.0-1.0
}"""

    def _call_claude(self, image_b64, media_type, config):
        """Llama a Anthropic Claude Vision API."""
        try:
            import anthropic
        except ImportError:
            raise UserError(_('anthropic no está instalado. pip install anthropic'))

        client = anthropic.Anthropic(api_key=config['api_key'])
        message = client.messages.create(
            model=config['model'],
            max_tokens=1024,
            messages=[{
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
                    {'type': 'text', 'text': self._build_prompt()},
                ],
            }],
        )
        return json.loads(message.content[0].text)

    def _call_openai(self, image_b64, media_type, config):
        """Llama a OpenAI GPT-4o Vision API."""
        try:
            from openai import OpenAI
        except ImportError:
            raise UserError(_('openai no está instalado. pip install openai'))

        client = OpenAI(api_key=config['api_key'])
        data_url = f'data:{media_type};base64,{image_b64}'
        response = client.chat.completions.create(
            model=config['model'],
            max_tokens=1024,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image_url', 'image_url': {'url': data_url}},
                    {'type': 'text', 'text': self._build_prompt()},
                ],
            }],
        )
        return json.loads(response.choices[0].message.content)

    def _call_google_vision(self, image_b64, config):
        """Llama a Google Cloud Vision API (document_text_detection + parsing LLM)."""
        try:
            from google.cloud import vision
        except ImportError:
            raise UserError(_('google-cloud-vision no está instalado.'))
        # Implementación simplificada — Google Vision extrae texto,
        # luego se necesita un segundo paso con un LLM para estructurar.
        raise UserError(_('Google Vision requiere implementación en dos pasos. '
                          'Use Claude o OpenAI para una solución end-to-end.'))

    def _populate_results(self, result, duration_ms, provider):
        """Llena los campos del wizard con el resultado JSON."""
        self.write({
            'result_cuit_emisor':    result.get('cuit_emisor'),
            'result_cuit_receptor':  result.get('cuit_receptor'),
            'result_doc_type':       result.get('tipo_comprobante'),
            'result_doc_number':     result.get('numero_completo'),
            'result_date':           result.get('fecha_emision'),
            'result_cae':            result.get('cae'),
            'result_cae_due':        result.get('vencimiento_cae'),
            'result_amount_untaxed': result.get('neto_gravado', 0.0),
            'result_amount_tax': (
                (result.get('iva_21') or 0)
                + (result.get('iva_10_5') or 0)
                + (result.get('iva_27') or 0)
            ),
            'result_amount_total': result.get('importe_total', 0.0),
            'result_currency':     result.get('moneda', 'ARS'),
            'result_raw_json':     json.dumps(result, ensure_ascii=False, indent=2),
            'state': 'scanned',
            'l10n_ar_scan_confidence': (result.get('confianza') or 0) * 100,
        })
        # Log de auditoría
        self.env['l10n_ar.ai.invoice.scan.log'].create({
            'move_id':       self.move_id.id,
            'attachment_id': self.attachment_id.id,
            'provider':      provider,
            'model_used':    self._get_provider_config(provider).get('model'),
            'duration_ms':   duration_ms,
            'success':       True,
            'extracted_json': self.result_raw_json,
        })
```

---

## 4. Campos de Facturas Argentinas y Mapeo a account.move

### 4.1 Campos Obligatorios por AFIP/ARCA

| Campo Factura AR          | Descripción                                 | Campo Odoo / l10n_ar                         | Campo módulo scanner                  |
|---------------------------|---------------------------------------------|----------------------------------------------|---------------------------------------|
| Tipo de comprobante       | A, B, C, E, M                               | `l10n_latam_document_type_id`                | `result_doc_type`                     |
| Número de comprobante     | XXXXX-YYYYYYYY (PV-NroComp)                 | `l10n_latam_document_number`                 | `result_doc_number` / `l10n_ar_afip_doc_number_scanned` |
| Fecha de emisión          | DD/MM/AAAA                                  | `invoice_date`                               | `result_date`                         |
| CUIT del emisor           | XX-XXXXXXXX-X (11 dígitos sin guiones)      | `partner_id.vat` (emisor)                    | `l10n_ar_afip_cuit_emisor`            |
| Razón social emisor       | Nombre empresa/persona                      | `partner_id.name`                            | `result_raw_json`                     |
| Condición IVA emisor      | Resp. Inscripto, Monotributista, Exento…    | `partner_id.l10n_ar_afip_responsibility_type_id` | —                                |
| CUIT del receptor         | XX-XXXXXXXX-X                               | `commercial_partner_id.vat`                  | `l10n_ar_afip_cuit_receptor`          |
| Condición IVA receptor    | —                                           | `fiscal_position_id`                         | —                                     |
| Neto gravado              | Base imponible IVA                          | `amount_untaxed`                             | `result_amount_untaxed`               |
| IVA 21%                   | Alícuota estándar                           | `amount_tax` (línea impuesto 21%)            | `result_iva_21` (en JSON)             |
| IVA 10.5%                 | Alícuota reducida                           | `amount_tax` (línea impuesto 10.5%)          | `result_iva_10_5` (en JSON)           |
| IVA 27%                   | Servicios públicos                          | `amount_tax` (línea impuesto 27%)            | `result_iva_27` (en JSON)             |
| Importe total             | Total a pagar                               | `amount_total`                               | `result_amount_total`                 |
| CAE                       | Código Autorización Electrónica (14 dígitos)| `l10n_ar_afip_auth_code` (l10n_ar_edi)      | `l10n_ar_afip_cae_scanned`            |
| Vencimiento CAE           | 10 días desde emisión (para facturas A/B)   | `l10n_ar_afip_auth_code_due` (l10n_ar_edi)  | `l10n_ar_afip_cae_due_date_scanned`   |
| CAI                       | Código Autorización Impresión (comprobantes físicos) | —                                    | campo en JSON                         |
| Moneda                    | ARS, USD, EUR                               | `currency_id`                               | `result_currency`                     |
| Código QR                 | URL con datos comprobante (desde 2021)      | — (para verificación externa)               | `codigo_qr_presente` en JSON          |

### 4.2 Formato CUIT

- **Estructura:** XX-XXXXXXXX-X (11 dígitos totales)
- **Persona física:** 20-XXXXXXXX-X (hombre) / 27-XXXXXXXX-X (mujer)
- **Persona jurídica:** 30-XXXXXXXX-X
- **Prefijos especiales:** 33- (empleadores), 34- (sociedades extranjeras)
- **Validación:** dígito verificador con módulo 11

```python
def validate_cuit(cuit: str) -> bool:
    """Valida el dígito verificador de un CUIT argentino."""
    cuit_clean = cuit.replace('-', '').replace(' ', '')
    if len(cuit_clean) != 11 or not cuit_clean.isdigit():
        return False
    weights = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(cuit_clean[i]) * weights[i] for i in range(10))
    remainder = total % 11
    check = 11 - remainder if remainder not in (0, 1) else (0 if remainder == 0 else 9)
    return check == int(cuit_clean[10])
```

### 4.3 Formato Número de Comprobante

```
XXXXX-YYYYYYYY
  ↑       ↑
  Punto   Número correlativo
  de      (8 dígitos, desde
  Venta   00000001)
  (5 díg.)
```

Ejemplo: `00015-00001234` → PV 00015, Nro. 00001234

### 4.4 Datos del QR Code (desde 2021 obligatorio)

El código QR en facturas electrónicas contiene una URL hacia ARCA con estos datos:
```json
{
  "ver": 1,
  "fecha": "2024-01-15",
  "cuit": 30123456789,
  "ptoVta": 15,
  "tipoCmp": 1,
  "nroCmp": 1234,
  "importe": 121.00,
  "moneda": "PES",
  "ctz": 1,
  "tipoDocRec": 80,
  "nroDocRec": 20987654321,
  "tipoCodAut": "E",
  "codAut": 70417054367476
}
```

---

## 5. Regulaciones AFIP/ARCA

### 5.1 Tipos de Comprobante (Letras)

| Letra | Transacción                              | Emisor                              | Receptor                               | Requiere |
|-------|------------------------------------------|-------------------------------------|----------------------------------------|----------|
| **A** | B2B (empresa a empresa)                  | Resp. Inscripto IVA                 | Resp. Inscripto IVA                    | CAE + CUIT receptor |
| **B** | B2C (empresa a consumidor final)         | Resp. Inscripto IVA                 | Consumidor Final / Monotributista / Exento | CAE |
| **C** | Monotributistas / no inscritos IVA       | Monotributista / Exento IVA         | Cualquiera                             | CAE/CAI |
| **E** | Exportación                              | Cualquier tipo                      | Clientes extranjeros                   | CAE + datos aduaneros |
| **M** | B2B (pymes con régimen especial)         | Resp. Inscripto (condiciones FCE)   | Resp. Inscripto IVA                    | CAE + datos adicionales |

### 5.2 CAE (Código de Autorización Electrónica)

- **14 dígitos** numéricos
- Emitido por ARCA (ex-AFIP) vía WebService WSFE
- **Vencimiento:** 10 días calendario desde la fecha de emisión
- Una vez vencido, el comprobante sigue siendo válido pero no puede recibir CAE nuevo
- Para facturas tipo A: un CAE por comprobante
- Para facturas tipo B (≤$1.000 en 2019): puede ser un CAE para un lote completo

### 5.3 CAI (Código de Autorización de Impresión)

- Para **comprobantes impresos** (no electrónicos)
- Principalmente usado por monotributistas para talonarios físicos
- Formato: distinto del CAE (string alfanumérico de longitud variable según la versión)
- Se obtiene ante un impresor autorizado por ARCA

### 5.4 Ticket Fiscal

Los tickets fiscales son emitidos por **controladores fiscales homologados**. Campos obligatorios:
- Número de controlador fiscal (código)
- Número de ticket (secuencial por controlador)
- CUIT del emisor
- Fecha y hora
- Descripción items
- IVA desglosado
- Total
- **NO requieren CAE** (son generados y firmados por el hardware fiscal)
- El "Z Report" diario consolida los totales

---

## 6. Estructura del Módulo l10n_ar en Odoo 19

### 6.1 Módulos Involucrados

```
l10n_latam_invoice_document   ← Base LATAM: document types, l10n_latam_document_number
       ↓
l10n_ar                       ← Localización AR: CUIT, responsabilidad AFIP,
                                  impuestos AR (21%, 10.5%, 27%, percepciones, retenciones)
       ↓ (Enterprise)
l10n_ar_edi                   ← EDI Electrónico: WebService AFIP, CAE, firma digital,
                                  l10n_ar_afip_auth_code, l10n_ar_afip_auth_code_due
       ↓ (opcional)
l10n_ar_reports               ← Reportes: Libro IVA, IIBB, VAT Summary
```

### 6.2 Campos Clave en account.move (l10n_latam + l10n_ar + l10n_ar_edi)

```python
# Heredados de l10n_latam_invoice_document
l10n_latam_document_type_id       # Many2one → l10n_latam.document.type
l10n_latam_document_number        # Char: "00015-00001234"
l10n_latam_use_documents          # Boolean (del journal)
l10n_latam_manual_document_number # Boolean
l10n_latam_amount_untaxed         # Monetary
l10n_latam_available_document_type_ids  # Many2many

# Heredados de l10n_ar_edi (Enterprise)
l10n_ar_afip_auth_mode            # Selection: CAE, CAEA, CAI
l10n_ar_afip_auth_code            # Char(14): código CAE/CAI
l10n_ar_afip_auth_code_due        # Date: vencimiento
l10n_ar_is_einvoice               # Boolean: ¿es factura electrónica?
```

### 6.3 Campos en res.partner (l10n_ar)

```python
l10n_ar_afip_responsibility_type_id   # Many2one: Resp. Inscripto, Monotrib., etc.
vat                                   # CUIT: almacenado como "30123456789" (sin guiones)
l10n_latam_identification_type_id     # DNI, CUIT, CUIL, Pasaporte, etc.
```

### 6.4 Tabla l10n_latam.document.type (tipos de comprobante)

```
Código  | Nombre                              | Letra
--------|-------------------------------------|-------
1       | Factura A                           | A
6       | Factura B                           | B
11      | Factura C                           | C
19      | Factura E                           | E
51      | Factura M                           | M
2       | Nota de Débito A                    | A
7       | Nota de Débito B                    | B
3       | Nota de Crédito A                   | A
8       | Nota de Crédito B                   | B
83      | Liquidación A                       | A
```

---

## 7. Manejo de Adjuntos ir.attachment

### 7.1 Modelo ir.attachment — Campos Relevantes

```python
# Campos nativos de ir.attachment
name        # Char: nombre del archivo
datas       # Binary: contenido en base64
mimetype    # Char: 'application/pdf', 'image/jpeg', etc.
res_model   # Char: 'account.move'
res_id      # Integer: ID del registro vinculado
type        # Selection: 'binary' | 'url'
store_fname # Char: ruta en filestore (si usa almacenamiento externo)
file_size   # Integer: tamaño en bytes
index_content # Text: texto extraído para búsqueda (se puede aprovechar!)
```

### 7.2 Leer el Contenido de un Adjunto

```python
# Desde Python en un modelo Odoo
attachment = self.env['ir.attachment'].browse(attachment_id)

# Método 1: desde el campo binary (base64)
pdf_bytes = base64.b64decode(attachment.datas)

# Método 2: desde el filestore (más eficiente para archivos grandes)
if attachment.store_fname:
    filepath = self.env['ir.attachment']._full_path(attachment.store_fname)
    with open(filepath, 'rb') as f:
        pdf_bytes = f.read()
```

### 7.3 Registrar un Adjunto en una Factura

```python
attachment = self.env['ir.attachment'].create({
    'name': 'factura_escaneada.jpg',
    'type': 'binary',
    'datas': base64.b64encode(image_bytes).decode('utf-8'),
    'mimetype': 'image/jpeg',
    'res_model': 'account.move',
    'res_id': move.id,
})
# Marcar como adjunto principal (muestra en preview del chatter)
attachment.register_as_main_attachment()
```

### 7.4 Hook Automático en Creación de Adjuntos

El módulo puede interceptar la creación de adjuntos en `ir.attachment` usando `@api.model_create_multi` para disparo automático cuando se sube un PDF a una factura. Ver sección 3.2.

### 7.5 Limitaciones de Tamaño

- Odoo por defecto limita uploads a `10MB` (configurable en `ir.config_parameter`: `web.max_file_upload_size`)
- Para PDFs multi-página, convertir sólo la **primera página** es suficiente para la mayoría de facturas AR
- Si la factura tiene múltiples páginas (ej. factura con muchas líneas), considerar enviar las primeras 2 páginas al LLM

---

## 8. Enfoque AI/OCR Recomendado

### 8.1 Comparativa de Opciones

| Proveedor              | Exactitud (PDF escaneado) | Exactitud (digital) | Costo/1000 facturas | Latencia   | Privacidad | Complejidad impl. |
|------------------------|---------------------------|---------------------|---------------------|------------|------------|-------------------|
| **Claude Sonnet 4.5**  | ~90%                      | ~97%                | ~$4.80              | 2-5 seg    | Alta (no training) | Baja |
| **GPT-4o**             | ~91%                      | ~98%                | ~$16.10             | 2-6 seg    | Media      | Baja |
| **Gemini 2.5 Pro**     | ~94%                      | ~96%                | ~$7-12              | 1-3 seg    | Media      | Baja |
| **Google Vision API**  | ~98% (texto puro)         | ~98%                | ~$1.50 (sólo OCR)   | <1 seg     | Media      | Alta (2 pasos) |
| **Tesseract (local)**  | ~65-80%                   | ~89-94%             | $0                  | <0.5 seg   | Máxima     | Alta |
| **PaddleOCR (local)**  | ~85-92%                   | ~92-95%             | $0                  | 0.5-2 seg  | Máxima     | Media-Alta |

### 8.2 Recomendación Principal: Claude Vision (Anthropic)

**Por qué Claude para facturas argentinas:**

**Ventajas:**
- Mejor consistencia de formato JSON (100% JSON válido según benchmarks)
- Excelente manejo de estructuras de datos complejas (tablas de IVA, múltiples alícuotas)
- Entiende el contexto de facturas en español sin instrucciones especiales
- Anthropic no usa datos para entrenamiento por defecto → cumple con privacidad fiscal
- API simple y robusta (`anthropic` Python SDK)
- Maneja PDF como imagen sin convertir (con pdf_data source)
- Modelo `claude-sonnet-4-6` balancea costo/calidad para uso masivo
- `claude-opus-4-6` para máxima precisión en casos difíciles (tickets degradados)

**Desventajas:**
- Requiere conexión a internet (no local)
- Costo por uso (≈$3-15/1000 facturas según modelo)
- Posible hallucination en números difíciles de leer

### 8.3 Recomendación Secundaria: Tesseract + Post-procesamiento

Para entornos sin acceso a internet o con alta sensibilidad de datos:

```python
# Flujo: pdf2image → pytesseract → regex para campos AR → validación
import pytesseract
from PIL import Image
import re

def extract_cae_tesseract(image_path):
    text = pytesseract.image_to_string(Image.open(image_path), lang='spa')
    cae_match = re.search(r'CAE[:\s]+(\d{14})', text, re.IGNORECASE)
    cuit_match = re.search(r'CUIT[:\s]+(\d{2}-\d{8}-\d)', text)
    # ... más regex para otros campos
```

**Limitaciones Tesseract:**
- 65-80% precisión en tickets físicos escaneados o degradados
- Requiere reglas regex frágiles para cada layout de factura
- No maneja rotación o fondos complejos bien
- Configuración compleja para español argentino

### 8.4 Estrategia Híbrida Recomendada (Producción)

```
Adjunto subido
      ↓
¿PDF digital (no escaneado)?
      ├─ SÍ → Extraer texto con pdfminer/pypdf → Claude como "estructurador" (barato)
      └─ NO → pdf2image → Claude Vision (imagen completa)
                              ↓
                    Validación campos AR:
                    - CUIT check dígito verificador
                    - CAE 14 dígitos
                    - Número comprobante XXXXX-YYYYYYYY
                    - Fecha coherente (± 30 días)
                              ↓
                    Confianza > 85%?
                    ├─ SÍ → Pre-rellenar factura (requiere confirmación usuario)
                    └─ NO → Marcar para revisión manual
```

### 8.5 Configuración del Modelo en Odoo

Los parámetros de configuración se almacenan en `ir.config_parameter`:

```
l10n_ar_ai_scanner.claude_api_key      → clave API Anthropic
l10n_ar_ai_scanner.claude_model        → claude-sonnet-4-6 (default)
l10n_ar_ai_scanner.openai_api_key      → clave API OpenAI
l10n_ar_ai_scanner.openai_model        → gpt-4o (default)
l10n_ar_ai_scanner.auto_scan           → True/False (escaneo automático al subir)
l10n_ar_ai_scanner.confidence_threshold → 0.85 (umbral de confianza)
```

---

## 9. Dependencias Python Requeridas

### 9.1 `requirements.txt` del módulo

```
# IA / LLM
anthropic>=0.40.0          # Claude Vision API (recomendado)
openai>=1.50.0             # GPT-4o Vision API (alternativa)

# PDF a Imagen
pdf2image>=1.17.0          # Convierte páginas PDF a imágenes PIL
Pillow>=10.0.0             # Manipulación de imágenes (PIL)
pypdf>=4.0.0               # Extracción de texto de PDFs digitales (sin escaneo)

# OCR Local (opcional, para modo offline)
pytesseract>=0.3.10        # Wrapper Python para Tesseract OCR
# Nota: requiere Tesseract instalado en el sistema:
# Ubuntu: apt-get install tesseract-ocr tesseract-ocr-spa
# macOS: brew install tesseract tesseract-lang

# Validaciones AR
python-stdnum>=1.20        # Validación CUIT/CUIL con stdnum.ar.cuit
# O implementar validación propia (ver sección 4.2)

# Extras opcionales
google-cloud-vision>=3.7.0 # Google Cloud Vision API (alternativa)
paddleocr>=2.7.0           # PaddleOCR local de alta precisión
pydantic>=2.5.0            # Validación de datos extraídos con modelos

# Dependencias del sistema (no pip)
# poppler-utils (para pdf2image):
# Ubuntu: apt-get install poppler-utils
# macOS: brew install poppler
```

### 9.2 Instalación en Servidor Odoo

```bash
# En el entorno virtual de Odoo
pip install anthropic pdf2image Pillow pypdf python-stdnum

# En el sistema operativo
sudo apt-get install -y poppler-utils tesseract-ocr tesseract-ocr-spa
```

### 9.3 Declaración en `__manifest__.py`

```python
'external_dependencies': {
    'python': ['anthropic', 'pdf2image', 'PIL', 'pypdf'],
    # 'bin': ['tesseract', 'pdftoppm'],  # si se usa modo offline
},
```

---

## 10. Flujo de Procesamiento Recomendado

### 10.1 Diagrama de Flujo

```
Usuario sube PDF/imagen de factura AR
              ↓
    ir.attachment.create()
              ↓
    ¿auto_scan activado?
    ├─ SÍ → trigger background job (queue_job / ir.cron)
    └─ NO → usuario hace clic en "Escanear con IA"
              ↓
    Abrir ScanInvoiceWizard
              ↓
    _get_image_base64()
    ├─ PDF → pdf2image → JPEG
    └─ IMG → base64 directo
              ↓
    Selección proveedor (Claude / OpenAI / Google)
              ↓
    _build_prompt() → prompt especializado AR
              ↓
    Llamada API Vision
              ↓
    JSON parseado → validaciones:
    ├─ validate_cuit(cuit_emisor)
    ├─ len(cae) == 14
    ├─ numero_comprobante formato XXXXX-YYYYYYYY
    └─ importe_total coherente con neto+iva
              ↓
    Mostrar resultados en wizard para revisión
              ↓
    Usuario confirma → action_confirm()
              ↓
    account.move.write({
        l10n_latam_document_number,
        invoice_date,
        l10n_ar_afip_cae_scanned,
        l10n_ar_afip_cuit_emisor,
        ...
    })
              ↓
    message_post() en chatter
              ↓
    Log en l10n_ar.ai.invoice.scan.log
```

### 10.2 Seguridad y Acceso

```csv
# security/ir.model.access.csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_scan_wizard_user,scan_wizard_user,model_l10n_ar_scan_invoice_wizard,account.group_account_user,1,1,1,0
access_scan_log_user,scan_log_user,model_l10n_ar_ai_invoice_scan_log,account.group_account_user,1,0,0,0
access_scan_log_manager,scan_log_manager,model_l10n_ar_ai_invoice_scan_log,account.group_account_manager,1,1,1,1
```

### 10.3 Parámetros de Configuración (data/ir_config_parameter.xml)

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <record id="param_claude_model" model="ir.config_parameter">
            <field name="key">l10n_ar_ai_scanner.claude_model</field>
            <field name="value">claude-sonnet-4-6</field>
        </record>
        <record id="param_auto_scan" model="ir.config_parameter">
            <field name="key">l10n_ar_ai_scanner.auto_scan</field>
            <field name="value">False</field>
        </record>
        <record id="param_confidence" model="ir.config_parameter">
            <field name="key">l10n_ar_ai_scanner.confidence_threshold</field>
            <field name="value">0.85</field>
        </record>
    </data>
</odoo>
```

---

## Notas Finales

### Consideraciones Importantes

1. **Privacidad Fiscal:** Las facturas contienen CUITs y datos sensibles. Asegurarse de usar proveedores que no entrenen con los datos (Anthropic por defecto no lo hace).

2. **l10n_ar_edi (Enterprise):** Si el cliente tiene Odoo Enterprise, el módulo `l10n_ar_edi` ya tiene los campos `l10n_ar_afip_auth_code` y `l10n_ar_afip_auth_code_due`. El scanner debería escribir sobre esos campos en lugar de los custom, en ese caso.

3. **Tickets Fiscales vs Facturas Electrónicas:** Los tickets de controladores fiscales no tienen CAE pero sí tienen un número de copia/Z-report. El prompt del LLM debe manejar ambos casos.

4. **Validación CUIT:** Siempre validar el dígito verificador antes de mapear al partner. Un CUIT inválido indica error de extracción.

5. **Facturas Multimoneda:** Argentina usa ARS y USD (tipo de cambio dólar). El campo `moneda` en el JSON y `currency_id` en Odoo deben sincronizarse.

6. **Rate Limiting:** Implementar cola con `queue_job` (OCA) para procesar lotes grandes sin bloquear la UI.

7. **Odoo 19 Python 3.11+:** El módulo debe ser compatible con Python 3.11. Todas las dependencias listadas lo son.

---

## Referencias y Fuentes

- [Odoo 19 Module Manifests — Documentación oficial](https://www.odoo.com/documentation/19.0/developer/reference/backend/module.html)
- [Odoo 19 Building a Module — Tutorial](https://www.odoo.com/documentation/19.0/developer/tutorials/backend.html)
- [Odoo 18/19 Argentina Localization](https://www.odoo.com/documentation/18.0/applications/finance/fiscal_localizations/argentina.html)
- [Odoo 19 Electronic Invoicing Argentina](https://www.odoo.com/documentation/19.0/applications/finance/accounting/customer_invoices/electronic_invoicing/argentina.html)
- [AFIP — Factura Electrónica WebServices](https://www.afip.gob.ar/ws/documentacion/ws-factura-electronica.asp)
- [AFIP — Código QR en Comprobantes](https://www.afip.gob.ar/fe/qr/)
- [AFIP — Constatación de CAE](https://servicioscf.afip.gob.ar/publico/comprobantes/cae.aspx)
- [Anthropic Claude Vision API](https://platform.claude.com/docs/en/build-with-claude/vision)
- [Koncile — Claude vs GPT vs Gemini for Invoice Extraction](https://www.koncile.ai/en/ressources/claude-gpt-or-gemini-which-is-the-best-llm-for-invoice-extraction)
- [Odoo Extract API](https://www.odoo.com/documentation/19.0/developer/reference/extract_api.html)
- [OCA l10n-argentina](https://github.com/OCA/l10n-argentina)
- [l10n_latam_invoice_document account_move.py](https://github.com/odoo/odoo/blob/14.0/addons/l10n_latam_invoice_document/models/account_move.py)
- [Punto de Venta 5 dígitos — AFIP 2018](http://blog.delrincon.com.ar/2018/08/punto-de-venta-5-digitos.html)
