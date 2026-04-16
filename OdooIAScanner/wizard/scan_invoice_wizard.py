# -*- coding: utf-8 -*-
import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


def _parse_amount(value):
    """Parse an amount string or number to float, returning 0.0 on failure."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    clean = re.sub(r'[^\d,\.]', '', str(value))
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


def _validate_cuit(cuit_raw):
    """Validate an Argentine CUIT using mod-11. Returns (is_valid, digits_str).

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


class L10nArAiScanWizard(models.TransientModel):
    """Wizard to review and confirm AI-extracted invoice data before applying it
    to the account.move record."""
    _name = 'l10n_ar.ai.scan.wizard'
    _description = 'Asistente de Escaneo de Comprobantes con IA'

    # ---- Source move ------------------------------------------------------
    move_id = fields.Many2one(
        comodel_name='account.move',
        string='Comprobante',
        required=True,
        readonly=True,
    )

    # ---- State & progress ------------------------------------------------
    state = fields.Selection(
        selection=[
            ('ready', 'Listo para escanear'),
            ('scanned', 'Escaneado – revisar datos'),
            ('error', 'Error'),
        ],
        string='Estado',
        default='ready',
        readonly=True,
    )
    error_message = fields.Text(string='Mensaje de Error', readonly=True)

    # ---- Extracted fields (editable by user before applying) --------------
    tipo_comprobante = fields.Char(string='Tipo de Comprobante')
    punto_de_venta = fields.Char(string='Punto de Venta')
    numero_comprobante = fields.Char(string='Número de Comprobante')
    fecha_emision = fields.Char(string='Fecha de Emisión')

    cuit_emisor = fields.Char(string='CUIT Emisor')
    razon_social_emisor = fields.Char(string='Razón Social Emisor')
    cuit_receptor = fields.Char(string='CUIT Receptor')
    razon_social_receptor = fields.Char(string='Razón Social Receptor')

    cae = fields.Char(string='CAE')
    vencimiento_cae = fields.Char(string='Vencimiento CAE')

    neto_gravado_21 = fields.Float(string='Neto Gravado 21%', digits=(16, 2))
    neto_gravado_105 = fields.Float(string='Neto Gravado 10.5%', digits=(16, 2))
    neto_gravado_27 = fields.Float(string='Neto Gravado 27%', digits=(16, 2))
    neto_no_gravado = fields.Float(string='Neto No Gravado', digits=(16, 2))
    exento = fields.Float(string='Exento', digits=(16, 2))

    iva_21 = fields.Float(string='IVA 21%', digits=(16, 2))
    iva_105 = fields.Float(string='IVA 10.5%', digits=(16, 2))
    iva_27 = fields.Float(string='IVA 27%', digits=(16, 2))
    otros_tributos = fields.Float(string='Otros Tributos', digits=(16, 2))
    importe_total = fields.Float(string='Importe Total', digits=(16, 2))

    moneda = fields.Char(string='Moneda')
    confianza = fields.Float(string='Confianza IA (%)', digits=(5, 2), readonly=True)
    observaciones = fields.Text(string='Observaciones')

    # ---- Field-level accept toggles ---------------------------------------
    apply_tipo_comprobante = fields.Boolean(string='Aplicar', default=True)
    apply_numero = fields.Boolean(string='Aplicar número', default=True)
    apply_fecha = fields.Boolean(string='Aplicar fecha', default=True)
    apply_cuit_emisor = fields.Boolean(string='Aplicar CUIT emisor', default=True)
    create_partner_if_missing = fields.Boolean(
        string='Crear proveedor si no existe',
        default=True,
        help='Si el CUIT no se encuentra en contactos, crear automáticamente el proveedor con los datos del comprobante.',
    )
    apply_cae = fields.Boolean(string='Aplicar CAE', default=True)
    apply_amounts = fields.Boolean(string='Aplicar montos', default=True)

    # ---- Raw JSON (for advanced users / debugging) -----------------------
    raw_json = fields.Text(string='JSON Extraído', readonly=True)

    # ------------------------------------------------------------------
    # Onchange helpers
    # ------------------------------------------------------------------

    @api.model
    def default_get(self, fields_list):
        """Override default_get to pre-populate wizard from existing scan data."""
        res = super().default_get(fields_list)
        move_id = res.get('move_id') or self.env.context.get('default_move_id')
        if move_id:
            move = self.env['account.move'].browse(move_id)
            if move.exists() and move.ai_scan_raw_result:
                try:
                    data = json.loads(move.ai_scan_raw_result)
                    res.update(self._defaults_from_data(data))
                    res['state'] = 'scanned'
                except (json.JSONDecodeError, Exception):
                    pass
        return res

    @api.model
    def _defaults_from_data(self, data):
        """Return a dict of default values from parsed AI JSON data."""
        tipo_doc = (data.get('tipo_documento') or 'FACTURA').upper()

        if tipo_doc == 'SICOSS':
            # For SICOSS F931, map fields to the wizard's existing fields
            contrib_ss = _parse_amount(data.get('contrib_ss'))
            contrib_os = _parse_amount(data.get('contrib_os'))
            aporte_ss = _parse_amount(data.get('aporte_ss'))
            aporte_os = _parse_amount(data.get('aporte_os'))
            lrt = _parse_amount(data.get('lrt'))
            svo = _parse_amount(data.get('svo'))
            renatre = _parse_amount(data.get('renatre'))
            sepelio = _parse_amount(data.get('sepelio_uatre'))
            vales = _parse_amount(data.get('vales_alimentarios'))

            total_contribuciones = contrib_ss + contrib_os + lrt + svo + renatre + sepelio + vales
            total_aportes = aporte_ss + aporte_os

            periodo = str(data.get('periodo') or '').strip()
            # Convert MM/YYYY to a date (last day of the month)
            fecha = ''
            if periodo and '/' in periodo:
                fecha = '01/{}'.format(periodo)

            return {
                'tipo_comprobante': 'F931 SICOSS',
                'punto_de_venta': '',
                'numero_comprobante': str(data.get('nro_verificador') or ''),
                'fecha_emision': fecha,
                'cuit_emisor': data.get('cuit') or '',
                'razon_social_emisor': data.get('razon_social') or '',
                'cuit_receptor': '-',
                'razon_social_receptor': '-',
                'cae': '',
                'vencimiento_cae': '',
                'neto_gravado_21': total_contribuciones,
                'neto_gravado_105': total_aportes,
                'neto_gravado_27': 0,
                'neto_no_gravado': 0,
                'exento': 0,
                'iva_21': 0,
                'iva_105': 0,
                'iva_27': 0,
                'otros_tributos': 0,
                'importe_total': _parse_amount(data.get('importe_total')),
                'moneda': 'ARS',
                'confianza': float(data.get('confianza') or 0),
                'observaciones': data.get('observaciones') or (
                    'SICOSS F931 - Período: {} - Empleados: {} - Rem.1: {}'.format(
                        periodo,
                        data.get('empleados', ''),
                        data.get('remuneracion_1', ''),
                    )
                ),
                'raw_json': json.dumps(data, ensure_ascii=False, indent=2),
            }

        if tipo_doc == 'IVA':
            # For IVA F731, map fields to the wizard's existing fields
            debito = _parse_amount(data.get('debito_fiscal'))
            credito = _parse_amount(data.get('credito_fiscal'))
            saldo_tec_ant = _parse_amount(data.get('saldo_tecnico_anterior'))
            ret_perc = _parse_amount(data.get('retenciones_percepciones'))
            saldo_libre_ant = _parse_amount(data.get('saldo_libre_disp_anterior'))
            monto_ingresa = _parse_amount(data.get('monto_ingresa'))

            periodo = str(data.get('periodo') or '').strip()
            fecha = ''
            if periodo and '/' in periodo:
                fecha = '01/{}'.format(periodo)

            return {
                'tipo_comprobante': 'F731 IVA',
                'punto_de_venta': '',
                'numero_comprobante': str(data.get('nro_verificador') or ''),
                'fecha_emision': fecha,
                'cuit_emisor': data.get('cuit') or '',
                'razon_social_emisor': data.get('razon_social') or '',
                'cuit_receptor': '-',
                'razon_social_receptor': '-',
                'cae': '',
                'vencimiento_cae': '',
                'neto_gravado_21': debito,
                'neto_gravado_105': credito,
                'neto_gravado_27': saldo_tec_ant,
                'neto_no_gravado': ret_perc,
                'exento': saldo_libre_ant,
                'iva_21': 0,
                'iva_105': 0,
                'iva_27': 0,
                'otros_tributos': 0,
                'importe_total': monto_ingresa,
                'moneda': 'ARS',
                'confianza': float(data.get('confianza') or 0),
                'observaciones': data.get('observaciones') or (
                    'IVA F731 - Período: {}'.format(periodo)
                ),
                'raw_json': json.dumps(data, ensure_ascii=False, indent=2),
            }

        if tipo_doc == 'CM':
            impuesto = _parse_amount(data.get('impuesto_determinado'))
            ret_bancarias = _parse_amount(data.get('retenciones_bancarias'))
            ret_iibb = _parse_amount(data.get('retenciones_iibb'))
            perc_iibb = _parse_amount(data.get('percepciones_iibb'))
            saldo_favor_ant = _parse_amount(data.get('saldo_favor_anterior'))
            monto_a_pagar = _parse_amount(data.get('monto_a_pagar'))

            periodo = str(data.get('periodo') or '').strip()
            provincia = str(data.get('provincia') or '').strip()
            formulario = str(data.get('formulario') or 'CM03').strip().upper()
            fecha = ''
            if periodo and '/' in periodo:
                fecha = '01/{}'.format(periodo)

            return {
                'tipo_comprobante': '{} {}'.format(formulario, provincia),
                'punto_de_venta': '',
                'numero_comprobante': str(data.get('nro_verificador') or ''),
                'fecha_emision': fecha,
                'cuit_emisor': data.get('cuit') or '',
                'razon_social_emisor': data.get('razon_social') or '',
                'cuit_receptor': '-',
                'razon_social_receptor': '-',
                'cae': '',
                'vencimiento_cae': '',
                'neto_gravado_21': impuesto,
                'neto_gravado_105': ret_bancarias,
                'neto_gravado_27': ret_iibb,
                'neto_no_gravado': perc_iibb,
                'exento': saldo_favor_ant,
                'iva_21': 0,
                'iva_105': 0,
                'iva_27': 0,
                'otros_tributos': 0,
                'importe_total': monto_a_pagar,
                'moneda': 'ARS',
                'confianza': float(data.get('confianza') or 0),
                'observaciones': data.get('observaciones') or (
                    '{} - Provincia: {} - Período: {}'.format(formulario, provincia, periodo)
                ),
                'raw_json': json.dumps(data, ensure_ascii=False, indent=2),
            }

        if tipo_doc == 'RECIBO':
            neto = _parse_amount(data.get('neto_a_cobrar'))
            total_haberes = _parse_amount(data.get('total_haberes'))
            total_deducciones = _parse_amount(data.get('total_deducciones'))

            periodo = str(data.get('periodo') or '').strip()
            nombre = str(data.get('nombre_empleado') or '').strip()
            legajo = str(data.get('legajo') or '').strip()
            fecha = data.get('fecha_pago') or ''
            if not fecha and periodo and '/' in periodo:
                fecha = '01/{}'.format(periodo)

            return {
                'tipo_comprobante': 'Recibo de Sueldo',
                'punto_de_venta': '',
                'numero_comprobante': legajo,
                'fecha_emision': fecha,
                'cuit_emisor': data.get('cuit_empleador') or '',
                'razon_social_emisor': nombre,
                'cuit_receptor': '-',
                'razon_social_receptor': data.get('razon_social_empleador') or '-',
                'cae': '',
                'vencimiento_cae': '',
                'neto_gravado_21': total_haberes,
                'neto_gravado_105': total_deducciones,
                'neto_gravado_27': 0,
                'neto_no_gravado': 0,
                'exento': 0,
                'iva_21': 0,
                'iva_105': 0,
                'iva_27': 0,
                'otros_tributos': 0,
                'importe_total': neto,
                'moneda': 'ARS',
                'confianza': float(data.get('confianza') or 0),
                'observaciones': data.get('observaciones') or (
                    'Recibo sueldo {} - Leg.{} - Período: {}'.format(nombre, legajo, periodo)
                ),
                'raw_json': json.dumps(data, ensure_ascii=False, indent=2),
            }

        return {
            'tipo_comprobante': data.get('tipo_comprobante') or '',
            'punto_de_venta': str(data.get('punto_de_venta') or ''),
            'numero_comprobante': str(data.get('numero_comprobante') or ''),
            'fecha_emision': data.get('fecha_emision') or '',
            'cuit_emisor': data.get('cuit_emisor') or '',
            'razon_social_emisor': data.get('razon_social_emisor') or '',
            'cuit_receptor': data.get('cuit_receptor') or '',
            'razon_social_receptor': data.get('razon_social_receptor') or '',
            'cae': str(data.get('cae') or '') if data.get('cae') else '',
            'vencimiento_cae': data.get('vencimiento_cae') or '',
            'neto_gravado_21': _parse_amount(data.get('neto_gravado_21')),
            'neto_gravado_105': _parse_amount(data.get('neto_gravado_105')),
            'neto_gravado_27': _parse_amount(data.get('neto_gravado_27')),
            'neto_no_gravado': _parse_amount(data.get('neto_no_gravado')),
            'exento': _parse_amount(data.get('exento')),
            'iva_21': _parse_amount(data.get('iva_21')),
            'iva_105': _parse_amount(data.get('iva_105')),
            'iva_27': _parse_amount(data.get('iva_27')),
            'otros_tributos': _parse_amount(data.get('otros_tributos')),
            'importe_total': _parse_amount(data.get('importe_total')),
            'moneda': data.get('moneda') or 'ARS',
            'confianza': float(data.get('confianza') or 0),
            'observaciones': data.get('observaciones') or '',
            'raw_json': json.dumps(data, ensure_ascii=False, indent=2),
        }

    @api.onchange('move_id')
    def _onchange_move_id(self):
        """Pre-populate wizard if the move already has a raw scan result."""
        if self.move_id and self.move_id.ai_scan_raw_result:
            try:
                data = json.loads(self.move_id.ai_scan_raw_result)
                self._populate_from_data(data)
                self.state = 'scanned'
            except (json.JSONDecodeError, Exception):
                pass

    # ------------------------------------------------------------------
    # Wizard actions
    # ------------------------------------------------------------------

    def action_scan(self):
        """Trigger the AI scan on the linked move and populate wizard fields."""
        self.ensure_one()
        if not self.move_id:
            raise UserError(_('No hay un comprobante vinculado al asistente.'))

        scan_error = False
        # Use a savepoint so that if the scan raises an error we can still
        # write back to the wizard (the failed savepoint is rolled back,
        # but the outer transaction stays valid).
        try:
            with self.env.cr.savepoint():
                self.move_id.action_scan_invoice_ai()
        except (UserError, ValidationError) as exc:
            scan_error = exc.args[0] if exc.args else str(exc)
            _logger.error('AI scan error for move %s: %s', self.move_id.id, scan_error)
        except Exception as exc:
            scan_error = '%s: %s' % (type(exc).__name__, exc)
            _logger.exception('AI scan unexpected error for move %s', self.move_id.id)

        if scan_error:
            self.write({'state': 'error', 'error_message': scan_error})
            # Also post the error to the invoice chatter for visibility
            try:
                self.move_id.message_post(
                    body=_('<strong>Error de escaneo IA:</strong><br/>%s') % scan_error,
                    message_type='comment',
                    subtype_xmlid='mail.mt_note',
                )
            except Exception:
                pass  # chatter post is best-effort
            return {
                'type': 'ir.actions.act_window',
                'name': _('Escanear Comprobante con IA'),
                'res_model': 'l10n_ar.ai.scan.wizard',
                'res_id': self.id,
                'view_mode': 'form',
                'target': 'new',
            }

        # Reload the move to get fresh data after the scan
        self.move_id.invalidate_recordset()

        # Check if scan ended in error (exception was caught internally)
        if self.move_id.ai_scan_state == 'error':
            last_log = self.env['l10n_ar.ai.scan.log'].sudo().search(
                [('move_id', '=', self.move_id.id)], order='id desc', limit=1,
            )
            err_msg = ''
            if last_log:
                err_msg = last_log.error_message or ''
                if not err_msg and last_log.raw_response:
                    err_msg = _('El modelo respondió pero no se pudo parsear.\n'
                                'Respuesta (primeros 500 chars):\n%s'
                                ) % last_log.raw_response[:500]
            if not err_msg:
                err_msg = _('Error desconocido durante el escaneo. '
                            'Revise los logs del servidor.')
            self.write({'state': 'error', 'error_message': err_msg})
            return {
                'type': 'ir.actions.act_window',
                'name': _('Escanear Comprobante con IA'),
                'res_model': 'l10n_ar.ai.scan.wizard',
                'res_id': self.id,
                'view_mode': 'form',
                'target': 'new',
            }

        # Reload extracted data into wizard fields
        raw = self.move_id.ai_scan_raw_result
        if raw:
            try:
                data = json.loads(raw)
                self._populate_from_data(data)
                self.write({'state': 'scanned', 'raw_json': raw, 'error_message': False})
            except (json.JSONDecodeError, Exception) as exc:
                self.write({'state': 'error', 'error_message': str(exc)})
        else:
            self.write({'state': 'error', 'error_message': _('El modelo no devolvió datos.')})

        # Return same wizard refreshed
        return {
            'type': 'ir.actions.act_window',
            'name': _('Escanear Comprobante con IA'),
            'res_model': 'l10n_ar.ai.scan.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_apply(self):
        """Apply the reviewed/edited AI data to the linked account.move."""
        self.ensure_one()
        if self.state != 'scanned':
            raise UserError(_('Primero debe escanear el comprobante antes de aplicar los datos.'))
        if not self.move_id:
            raise UserError(_('No hay un comprobante vinculado al asistente.'))

        move = self.move_id
        vals = {}
        applied_fields = []

        # Detect document type
        tipo_doc = ''
        if self.raw_json:
            try:
                _data = json.loads(self.raw_json)
                tipo_doc = (_data.get('tipo_documento') or '').upper()
            except (json.JSONDecodeError, TypeError):
                pass

        try:
            # --- 1. Partner ---
            if tipo_doc == 'SICOSS':
                partner = self._get_or_create_afip_partner('AFIP 931')
                vals['partner_id'] = partner.id
                applied_fields.append(_('Proveedor: %s') % partner.name)
            elif tipo_doc == 'IVA':
                partner = self._get_or_create_afip_partner('AFIP 731')
                vals['partner_id'] = partner.id
                applied_fields.append(_('Proveedor: %s') % partner.name)
            elif tipo_doc == 'CM':
                provincia = str(_data.get('provincia') or '').strip() if _data else ''
                partner_name = 'Convenio Multilateral - {}'.format(provincia) if provincia else 'Convenio Multilateral'
                partner = self._get_or_create_afip_partner(partner_name)
                vals['partner_id'] = partner.id
                applied_fields.append(_('Proveedor: %s') % partner.name)
            elif tipo_doc == 'RECIBO':
                nombre = str(_data.get('nombre_empleado') or '').strip() if _data else ''
                legajo = str(_data.get('legajo') or '').strip() if _data else ''
                partner_name = nombre or 'Empleado Leg. {}'.format(legajo)
                partner = self._get_or_create_employee_partner(partner_name, legajo)
                vals['partner_id'] = partner.id
                applied_fields.append(_('Proveedor: %s') % partner.name)
            else:
                # Regular invoice: search by CUIT
                partner_cuit = self.cuit_emisor if move.move_type in ('in_invoice', 'in_refund') else self.cuit_receptor
                if self.apply_cuit_emisor and partner_cuit and partner_cuit != '-':
                    self._apply_partner_from_cuit(partner_cuit, move, vals, applied_fields)

            # --- 2. Document type (l10n_latam_document_type_id) ---
            if self.apply_tipo_comprobante and self.tipo_comprobante:
                doc_type = self._find_document_type(self.tipo_comprobante)
                if doc_type and hasattr(move, 'l10n_latam_document_type_id'):
                    vals['l10n_latam_document_type_id'] = doc_type.id
                    applied_fields.append(_('Tipo: %s') % doc_type.name)

            # Write partner and doc type first (doc number depends on doc type)
            if vals:
                move.write(vals)
                vals = {}

            # --- 3. Document number ---
            if self.apply_numero and self.punto_de_venta and self.numero_comprobante:
                pdv = str(self.punto_de_venta).strip().zfill(5)
                num = str(self.numero_comprobante).strip().zfill(8)
                doc_number = '{}-{}'.format(pdv, num)
                vals['ai_scanned_doc_number'] = doc_number
                # Write the official Odoo AR document number field
                if hasattr(move, 'l10n_latam_document_number'):
                    vals['l10n_latam_document_number'] = doc_number
                applied_fields.append(_('Número: %s') % doc_number)

            # --- 4. CAE ---
            if self.apply_cae and self.cae:
                cae_clean = re.sub(r'[^0-9]', '', self.cae)
                if re.fullmatch(r'\d{14}', cae_clean):
                    vals['ai_scanned_cae'] = cae_clean
                    if hasattr(move, 'l10n_ar_afip_auth_code'):
                        field_def = move._fields.get('l10n_ar_afip_auth_code')
                        if field_def and not field_def.compute:
                            vals['l10n_ar_afip_auth_code'] = cae_clean
                    applied_fields.append(_('CAE: %s') % cae_clean)
                else:
                    raise ValidationError(
                        _('El CAE ingresado "%s" no es válido. Debe tener exactamente 14 dígitos.') % self.cae
                    )

            # --- 5. Invoice date ---
            if self.apply_fecha and self.fecha_emision:
                parsed_date = self._parse_date(self.fecha_emision)
                if parsed_date:
                    vals['invoice_date'] = parsed_date
                    applied_fields.append(_('Fecha: %s') % self.fecha_emision)

            # --- 6. Ref (for easy identification) ---
            ref_parts = []
            if self.tipo_comprobante:
                ref_parts.append(self.tipo_comprobante)
            if self.apply_numero and self.punto_de_venta and self.numero_comprobante:
                ref_parts.append('{}-{}'.format(
                    str(self.punto_de_venta).strip().zfill(5),
                    str(self.numero_comprobante).strip().zfill(8),
                ))
            if ref_parts and not move.ref:
                vals['ref'] = ' '.join(ref_parts)

            # --- 7. Confidence ---
            vals['ai_scan_confidence'] = self.confianza

            if vals:
                move.write(vals)

            # --- 8. Post amounts as chatter message ---
            if self.apply_amounts:
                self._apply_amounts_to_move(move)

        except (UserError, ValidationError):
            raise
        except Exception as exc:
            raise UserError(_(
                'Error al aplicar los datos a la factura: %s'
            ) % str(exc)) from exc

        # Show success notification
        if applied_fields:
            summary = ', '.join(applied_fields)
        else:
            summary = _('Solo se actualizaron campos internos de IA')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Datos aplicados correctamente'),
                'message': summary,
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def action_discard(self):
        """Close the wizard without applying any data."""
        self.ensure_one()
        return {'type': 'ir.actions.act_window_close'}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _populate_from_data(self, data):
        """Fill wizard fields from a parsed JSON dict."""
        tipo_doc = (data.get('tipo_documento') or 'FACTURA').upper()
        defaults = self._defaults_from_data(data)
        for field_name, value in defaults.items():
            if field_name == 'raw_json':
                continue
            if hasattr(self, field_name):
                setattr(self, field_name, value)
        self.raw_json = json.dumps(data, ensure_ascii=False, indent=2)

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
        """Find or create a partner for an employee (payroll receipt)."""
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

    def _apply_partner_from_cuit(self, partner_cuit, move, vals, applied_fields):
        """Search/create partner by CUIT and update vals dict."""
        valid, digits = _validate_cuit(partner_cuit)
        if valid and digits:
            formatted_cuit = '{}-{}-{}'.format(digits[:2], digits[2:10], digits[10])
            vals['ai_scanned_cuit_emisor'] = formatted_cuit
            ar_vat = 'AR' + digits
            partner = self.env['res.partner'].search([
                '|', '|', '|',
                ('vat', '=', ar_vat),
                ('vat', '=', digits),
                ('vat', '=', formatted_cuit),
                ('vat', 'ilike', digits),
            ], limit=1)
            if partner:
                vals['partner_id'] = partner.id
                applied_fields.append(_('Proveedor: %s') % partner.name)
            elif self.create_partner_if_missing:
                if move.move_type in ('in_invoice', 'in_refund'):
                    partner_name = self.razon_social_emisor
                else:
                    partner_name = self.razon_social_receptor
                if not partner_name or partner_name == '-':
                    partner_name = formatted_cuit

                afip_resp = False
                if hasattr(self.env['res.partner'], 'l10n_ar_afip_responsibility_type_id'):
                    tipo = (self.tipo_comprobante or '').upper()
                    if 'A' in tipo:
                        afip_resp = self.env.ref('l10n_ar.res_IVARI', raise_if_not_found=False)
                    elif 'B' in tipo:
                        afip_resp = self.env.ref('l10n_ar.res_CF', raise_if_not_found=False)
                    elif 'C' in tipo:
                        afip_resp = self.env.ref('l10n_ar.res_mono', raise_if_not_found=False)

                new_partner_vals = {
                    'name': partner_name,
                    'vat': digits,
                    'company_type': 'company',
                    'supplier_rank': 1 if move.move_type in ('in_invoice', 'in_refund') else 0,
                    'customer_rank': 1 if move.move_type in ('out_invoice', 'out_refund') else 0,
                    'country_id': self.env.ref('base.ar', raise_if_not_found=False).id or False,
                }
                if afip_resp:
                    new_partner_vals['l10n_ar_afip_responsibility_type_id'] = afip_resp.id

                partner = self.env['res.partner'].create(new_partner_vals)
                vals['partner_id'] = partner.id
                applied_fields.append(_('Proveedor CREADO: %s') % partner.name)
            else:
                applied_fields.append(_('CUIT %s no encontrado en contactos') % formatted_cuit)

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

        Returns dict with account recordsets or False per key.
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
        move = self.move_id
        company = move.company_id or self.env.company
        CmJur = self.env['l10n_ar.ai.scanner.cm.jurisdiction'].sudo()
        jur = CmJur.search([
            ('name', '=', provincia),
            ('company_id', '=', company.id),
        ], limit=1)
        if not jur:
            jur = CmJur.search([
                ('name', '=ilike', provincia),
                ('company_id', '=', company.id),
            ], limit=1)
        if not jur:
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

    def _parse_date(self, date_str):
        """Parse a DD/MM/YYYY date string to an odoo-compatible date string YYYY-MM-DD."""
        if not date_str:
            return False
        date_str = str(date_str).strip()
        # Try DD/MM/YYYY
        match = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})', date_str)
        if match:
            day, month, year = match.groups()
            try:
                from datetime import date as _date  # noqa: PLC0415
                parsed = _date(int(year), int(month), int(day))
                return parsed.strftime('%Y-%m-%d')
            except ValueError:
                return False
        # Try YYYY-MM-DD
        match2 = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', date_str)
        if match2:
            return date_str
        return False

    def _find_document_type(self, tipo_str):
        """Find a l10n_latam.document.type matching the AI-extracted type string.

        Maps common AI output strings to AFIP document type codes.
        Returns a recordset (possibly empty).
        """
        if not tipo_str:
            return self.env['l10n_latam.document.type'].browse()

        tipo = tipo_str.strip().upper()

        # Map AI output to AFIP internal_type + letter
        # AFIP codes: 1=FA A, 6=FA B, 11=FA C, 19=FA E, 51=FA M
        #             2=ND A, 7=ND B, 12=ND C, 20=ND E, 52=ND M
        #             3=NC A, 8=NC B, 13=NC C, 21=NC E, 53=NC M
        code_map = {
            'FACTURA A': '1', 'FACTURA B': '6', 'FACTURA C': '11',
            'FACTURA E': '19', 'FACTURA M': '51',
            'NOTA DE DEBITO A': '2', 'NOTA DE DEBITO B': '7',
            'NOTA DE DEBITO C': '12', 'NOTA DE DEBITO E': '20',
            'NOTA DE DEBITO M': '52',
            'NOTA DE CREDITO A': '3', 'NOTA DE CREDITO B': '8',
            'NOTA DE CREDITO C': '13', 'NOTA DE CREDITO E': '21',
            'NOTA DE CREDITO M': '53',
        }

        code = code_map.get(tipo)
        if code:
            doc_type = self.env['l10n_latam.document.type'].search([
                ('code', '=', code),
                ('country_id.code', '=', 'AR'),
            ], limit=1)
            if doc_type:
                return doc_type

        # Fallback: search by name similarity
        return self.env['l10n_latam.document.type'].search([
            ('name', 'ilike', tipo),
            ('country_id.code', '=', 'AR'),
        ], limit=1)

    def _apply_amounts_to_move(self, move):
        """Create invoice lines on the account.move with proper tax assignment.

        Detects document type: SICOSS F931, IVA F731, or regular invoice.
        """
        if move.state != 'draft':
            self._post_amounts_chatter(move)
            return

        from odoo import Command  # noqa: PLC0415
        import json

        new_lines = []

        # --- Detect document type ---
        tipo_doc = ''
        data = {}
        if self.raw_json:
            try:
                data = json.loads(self.raw_json)
                tipo_doc = (data.get('tipo_documento') or '').upper()
            except (json.JSONDecodeError, TypeError):
                pass

        if tipo_doc == 'SICOSS':
            # --- SICOSS F931: create grouped lines ---
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
                new_lines.append(Command.create(line_vals))

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
                new_lines.append(Command.create(line_vals))

        elif tipo_doc == 'IVA':
            # --- IVA F731: create lines for each concept ---
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

            # Débito fiscal → positive
            if debito > 0:
                line_vals = {
                    'name': 'Débito Fiscal IVA F731 {}'.format(periodo),
                    'price_unit': debito,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_debito:
                    line_vals['account_id'] = acct_debito.id
                new_lines.append(Command.create(line_vals))

            # Crédito fiscal → NEGATIVE
            if credito > 0:
                line_vals = {
                    'name': 'Crédito Fiscal IVA F731 {}'.format(periodo),
                    'price_unit': -credito,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_credito:
                    line_vals['account_id'] = acct_credito.id
                new_lines.append(Command.create(line_vals))

            # Saldo técnico anterior → positive (saldo a favor del período anterior)
            if saldo_tec_ant > 0:
                line_vals = {
                    'name': 'Saldo técnico anterior IVA F731 {}'.format(periodo),
                    'price_unit': saldo_tec_ant,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_debito:
                    line_vals['account_id'] = acct_debito.id
                new_lines.append(Command.create(line_vals))

            # Retenciones → NEGATIVE
            if ret_perc > 0:
                line_vals = {
                    'name': 'Retenciones IVA sufridas F731 {}'.format(periodo),
                    'price_unit': -ret_perc,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_retencion:
                    line_vals['account_id'] = acct_retencion.id
                new_lines.append(Command.create(line_vals))

            # Saldo de libre disponibilidad anterior → NEGATIVE
            if saldo_libre_ant > 0:
                line_vals = {
                    'name': 'Saldo libre disponibilidad anterior IVA F731 {}'.format(periodo),
                    'price_unit': -saldo_libre_ant,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_saldo_libre:
                    line_vals['account_id'] = acct_saldo_libre.id
                new_lines.append(Command.create(line_vals))

        elif tipo_doc == 'CM':
            # --- Convenio Multilateral CM03: create lines for each concept ---
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
                new_lines.append(Command.create(line_vals))

            if ret_bancarias > 0:
                line_vals = {
                    'name': 'Retenciones bancarias {}'.format(label_suffix),
                    'price_unit': -ret_bancarias,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_ret_bancarias:
                    line_vals['account_id'] = acct_ret_bancarias.id
                new_lines.append(Command.create(line_vals))

            if ret_iibb > 0:
                line_vals = {
                    'name': 'Retenciones IIBB {}'.format(label_suffix),
                    'price_unit': -ret_iibb,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_ret_iibb:
                    line_vals['account_id'] = acct_ret_iibb.id
                new_lines.append(Command.create(line_vals))

            if perc_iibb > 0:
                line_vals = {
                    'name': 'Percepciones IIBB {}'.format(label_suffix),
                    'price_unit': -perc_iibb,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_perc_iibb:
                    line_vals['account_id'] = acct_perc_iibb.id
                new_lines.append(Command.create(line_vals))

            if saldo_favor_ant > 0:
                line_vals = {
                    'name': 'Saldo a favor anterior {}'.format(label_suffix),
                    'price_unit': -saldo_favor_ant,
                    'quantity': 1,
                    'tax_ids': [Command.clear()],
                }
                if acct_saldo_favor:
                    line_vals['account_id'] = acct_saldo_favor.id
                new_lines.append(Command.create(line_vals))

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
                new_lines.append(Command.create(line_vals))

        else:
            # --- Regular invoice ---
            company = move.company_id or self.env.company
            tax_21 = self._find_tax(company, 21.0, move.move_type)
            tax_105 = self._find_tax(company, 10.5, move.move_type)
            tax_27 = self._find_tax(company, 27.0, move.move_type)

            tax_map = {
                21: tax_21, 21.0: tax_21,
                10.5: tax_105, 10: tax_105,
                27: tax_27, 27.0: tax_27,
            }

            # --- Try product-level lines first ---
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
                    line_vals = {
                        'name': desc,
                        'price_unit': price,
                        'quantity': qty,
                    }
                    tax = tax_map.get(iva_pct)
                    if tax:
                        line_vals['tax_ids'] = [Command.set(tax.ids)]
                    elif iva_pct == 0:
                        line_vals['tax_ids'] = [Command.clear()]
                    new_lines.append(Command.create(line_vals))

                # Add non-taxed amounts that aren't covered by product lines
                if self.neto_no_gravado:
                    new_lines.append(Command.create({
                        'name': _('No gravado (IA)'),
                        'price_unit': self.neto_no_gravado,
                        'quantity': 1,
                        'tax_ids': [Command.clear()],
                    }))
                if self.exento:
                    new_lines.append(Command.create({
                        'name': _('Exento (IA)'),
                        'price_unit': self.exento,
                        'quantity': 1,
                        'tax_ids': [Command.clear()],
                    }))
                if self.otros_tributos:
                    new_lines.append(Command.create({
                        'name': _('Otros tributos / Percepciones (IA)'),
                        'price_unit': self.otros_tributos,
                        'quantity': 1,
                        'tax_ids': [Command.clear()],
                    }))
            else:
                # --- Fallback: one line per tax category ---
                if self.neto_gravado_21:
                    line_vals = {
                        'name': _('Neto gravado 21%% (IA)'),
                        'price_unit': self.neto_gravado_21,
                        'quantity': 1,
                    }
                    if tax_21:
                        line_vals['tax_ids'] = [Command.set(tax_21.ids)]
                    new_lines.append(Command.create(line_vals))

                if self.neto_gravado_105:
                    line_vals = {
                        'name': _('Neto gravado 10.5%% (IA)'),
                        'price_unit': self.neto_gravado_105,
                        'quantity': 1,
                    }
                    if tax_105:
                        line_vals['tax_ids'] = [Command.set(tax_105.ids)]
                    new_lines.append(Command.create(line_vals))

                if self.neto_gravado_27:
                    line_vals = {
                        'name': _('Neto gravado 27%% (IA)'),
                        'price_unit': self.neto_gravado_27,
                        'quantity': 1,
                    }
                    if tax_27:
                        line_vals['tax_ids'] = [Command.set(tax_27.ids)]
                    new_lines.append(Command.create(line_vals))

                if self.neto_no_gravado:
                    new_lines.append(Command.create({
                        'name': _('No gravado (IA)'),
                        'price_unit': self.neto_no_gravado,
                        'quantity': 1,
                        'tax_ids': [Command.clear()],
                    }))
                if self.exento:
                    new_lines.append(Command.create({
                        'name': _('Exento (IA)'),
                        'price_unit': self.exento,
                        'quantity': 1,
                        'tax_ids': [Command.clear()],
                    }))
                if self.otros_tributos:
                    new_lines.append(Command.create({
                        'name': _('Otros tributos / Percepciones (IA)'),
                        'price_unit': self.otros_tributos,
                        'quantity': 1,
                        'tax_ids': [Command.clear()],
                    }))

        if new_lines:
            move.write({'invoice_line_ids': new_lines})

        self._post_amounts_chatter(move)

    def _find_tax(self, company, amount, move_type):
        """Find an IVA tax record for the given percentage and move type.

        For purchase invoices looks for purchase taxes, for sales looks for
        sale taxes.
        """
        tax_type = 'purchase' if move_type in ('in_invoice', 'in_refund') else 'sale'
        tax = self.env['account.tax'].search([
            ('company_id', '=', company.id),
            ('amount', '=', amount),
            ('type_tax_use', '=', tax_type),
            ('amount_type', '=', 'percent'),
            ('active', '=', True),
        ], limit=1)

        if not tax:
            # Fallback: search by name patterns common in AR localization
            name_patterns = {
                21.0: '%21%',
                10.5: '%10%5%',
                27.0: '%27%',
            }
            pattern = name_patterns.get(amount, '%%%s%%' % amount)
            tax = self.env['account.tax'].search([
                ('company_id', '=', company.id),
                ('name', 'ilike', pattern),
                ('type_tax_use', '=', tax_type),
                ('active', '=', True),
            ], limit=1)

        return tax

    def _post_amounts_chatter(self, move):
        """Post a summary of extracted amounts as a chatter note."""
        lines = []
        if self.neto_gravado_21:
            lines.append(_('Neto gravado 21%%: %.2f') % self.neto_gravado_21)
        if self.neto_gravado_105:
            lines.append(_('Neto gravado 10.5%%: %.2f') % self.neto_gravado_105)
        if self.neto_gravado_27:
            lines.append(_('Neto gravado 27%%: %.2f') % self.neto_gravado_27)
        if self.neto_no_gravado:
            lines.append(_('Neto no gravado: %.2f') % self.neto_no_gravado)
        if self.exento:
            lines.append(_('Exento: %.2f') % self.exento)
        if self.iva_21:
            lines.append(_('IVA 21%%: %.2f') % self.iva_21)
        if self.iva_105:
            lines.append(_('IVA 10.5%%: %.2f') % self.iva_105)
        if self.iva_27:
            lines.append(_('IVA 27%%: %.2f') % self.iva_27)
        if self.otros_tributos:
            lines.append(_('Otros tributos: %.2f') % self.otros_tributos)
        if self.importe_total:
            lines.append(_('<strong>Total: %.2f %s</strong>') % (self.importe_total, self.moneda or 'ARS'))

        if lines and hasattr(move, 'message_post'):
            body = _('<b>Datos extraídos por IA (%.1f%% confianza):</b><br/>') % self.confianza
            body += '<br/>'.join(lines)
            if self.observaciones:
                body += '<br/>' + _('<em>Observaciones: %s</em>') % self.observaciones
            move.message_post(body=body, subtype_xmlid='mail.mt_note')
