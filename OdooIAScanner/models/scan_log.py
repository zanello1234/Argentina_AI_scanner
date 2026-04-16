# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class L10nArAiScanLog(models.Model):
    """Audit trail for every AI invoice scan attempt."""
    _name = 'l10n_ar.ai.scan.log'
    _description = 'Registro de Escaneo IA de Comprobantes AR'
    _order = 'scan_date desc, id desc'
    # display_name is computed below; it inherits from the ORM automatically

    # ---- Relations --------------------------------------------------------
    move_id = fields.Many2one(
        comodel_name='account.move',
        string='Comprobante',
        ondelete='set null',
        index=True,
    )

    # ---- Scan metadata ----------------------------------------------------
    scan_date = fields.Datetime(
        string='Fecha de Escaneo',
        default=fields.Datetime.now,
        readonly=True,
    )
    ai_model = fields.Char(
        string='Modelo IA',
        readonly=True,
        help='Nombre del modelo de IA utilizado para el escaneo.',
    )
    tokens_used = fields.Integer(
        string='Tokens Utilizados',
        readonly=True,
        default=0,
    )
    scan_duration = fields.Float(
        string='Duración (s)',
        digits=(10, 3),
        readonly=True,
        default=0.0,
        help='Duración total del escaneo en segundos.',
    )
    confidence_score = fields.Float(
        string='Confianza (%)',
        digits=(5, 2),
        readonly=True,
        default=0.0,
    )

    # ---- Result -----------------------------------------------------------
    success = fields.Boolean(
        string='Exitoso',
        default=False,
        readonly=True,
    )
    error_message = fields.Text(
        string='Mensaje de Error',
        readonly=True,
    )

    # ---- Raw data (stored for debugging / reprocessing) ------------------
    raw_prompt = fields.Text(
        string='Prompt Enviado',
        readonly=True,
    )
    raw_response = fields.Text(
        string='Respuesta Cruda',
        readonly=True,
    )
    extracted_data = fields.Text(
        string='Datos Extraídos (JSON)',
        readonly=True,
        help='Objeto JSON con todos los campos extraídos por el modelo de IA.',
    )

    @api.depends('move_id', 'scan_date', 'success')
    def _compute_display_name(self):
        """Override base display_name to show scan status, move name and date."""
        for rec in self:
            date_str = fields.Datetime.to_string(rec.scan_date) if rec.scan_date else ''
            move_name = rec.move_id.name if rec.move_id else _('Sin comprobante')
            status = _('OK') if rec.success else _('Error')
            rec.display_name = '[{}] {} \u2013 {}'.format(status, move_name, date_str)
