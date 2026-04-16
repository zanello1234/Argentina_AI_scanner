# -*- coding: utf-8 -*-
import base64
import logging

import json
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def _parse_amount(value):
    """Parse an amount string or number to float."""
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
    """Validate Argentine CUIT using mod-11. Returns (is_valid, digits_str)."""
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

SCANNABLE_MIMETYPES = {
    'application/pdf': 'application/pdf',
    'image/jpeg': 'image/jpeg',
    'image/jpg': 'image/jpeg',
    'image/png': 'image/png',
    'image/gif': 'image/gif',
    'image/webp': 'image/webp',
}


class BulkScanWizard(models.TransientModel):
    _name = 'l10n_ar.ai.bulk.scan.wizard'
    _description = 'Carga Masiva de Comprobantes con IA'

    move_type = fields.Selection(
        selection=[
            ('in_invoice', 'Factura de Proveedor'),
            ('in_refund', 'Nota de Crédito de Proveedor'),
            ('out_invoice', 'Factura de Cliente'),
            ('out_refund', 'Nota de Crédito de Cliente'),
        ],
        string='Tipo de Comprobante',
        default='in_invoice',
        required=True,
    )
    journal_id = fields.Many2one(
        comodel_name='account.journal',
        string='Diario',
        domain="[('type', '=', 'purchase')]",
    )
    file_ids = fields.Many2many(
        comodel_name='ir.attachment',
        relation='l10n_ar_bulk_scan_wizard_att_rel',
        column1='wizard_id',
        column2='attachment_id',
        string='Comprobantes',
        help='Seleccione hasta 10 archivos PDF o imágenes de comprobantes.',
    )
    auto_apply = fields.Boolean(
        string='Aplicar datos automáticamente',
        default=True,
        help='Si está activo, aplica automáticamente los datos extraídos a cada factura (proveedor, tipo, número, fecha, montos).',
    )
    create_partner = fields.Boolean(
        string='Crear proveedor si no existe',
        default=True,
    )

    # ---- Results ---------------------------------------------------------
    state = fields.Selection(
        selection=[
            ('draft', 'Seleccionar archivos'),
            ('processing', 'Procesando...'),
            ('done', 'Completado'),
        ],
        default='draft',
    )
    result_summary = fields.Html(string='Resultado', readonly=True)

    @api.onchange('move_type')
    def _onchange_move_type(self):
        if self.move_type in ('in_invoice', 'in_refund'):
            return {'domain': {'journal_id': [('type', '=', 'purchase')]}}
        return {'domain': {'journal_id': [('type', '=', 'sale')]}}

    def action_process(self):
        """Create moves, attach files, queue for background AI scanning."""
        self.ensure_one()

        if not self.file_ids:
            raise UserError(_('Seleccione al menos un archivo para procesar.'))

        self.write({'state': 'processing'})

        created_count = 0
        skipped = []

        for att in self.file_ids:
            mimetype = att.mimetype or ''
            if mimetype not in SCANNABLE_MIMETYPES:
                skipped.append(att.name)
                continue

            move_vals = {
                'move_type': self.move_type,
                'ai_scan_state': 'queued',
                'ai_scan_queue_auto_apply': self.auto_apply,
                'ai_scan_queue_create_partner': self.create_partner,
            }
            if self.journal_id:
                move_vals['journal_id'] = self.journal_id.id

            move = self.env['account.move'].create(move_vals)
            att.write({'res_model': 'account.move', 'res_id': move.id})
            created_count += 1

        # Trigger cron to start processing ASAP
        cron = self.env.ref(
            'l10n_ar_ai_invoice_scanner.ir_cron_process_ai_scans',
            raise_if_not_found=False,
        )
        if cron:
            cron.sudo()._trigger()

        # Build summary
        summary = (
            '<div class="alert alert-info">'
            '<i class="fa fa-clock-o"/> '
            '<strong>%d comprobantes</strong> creados y encolados para escaneo con IA.<br/>'
            'El procesamiento se realiza en segundo plano. '
            'Puede cerrar esta ventana y continuar trabajando.<br/>'
            'Las facturas aparecerán con estado "En cola" y pasarán a "Completado" '
            'a medida que se procesen.'
            '</div>'
        ) % created_count

        if skipped:
            summary += (
                '<div class="alert alert-warning mt-2">'
                '<i class="fa fa-exclamation-triangle"/> '
                '<strong>%d archivos omitidos</strong> (tipo no soportado): %s'
                '</div>'
            ) % (len(skipped), ', '.join(skipped))

        self.write({'state': 'done', 'result_summary': summary})

        return {
            'type': 'ir.actions.act_window',
            'name': _('Carga Masiva de Comprobantes'),
            'res_model': 'l10n_ar.ai.bulk.scan.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

