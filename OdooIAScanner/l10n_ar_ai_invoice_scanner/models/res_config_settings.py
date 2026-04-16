# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ---- Proveedor de IA --------------------------------------------------
    l10n_ar_ai_scanner_provider = fields.Selection(
        selection=[
            ('anthropic', 'Anthropic (Claude)'),
            ('gemini', 'Google (Gemini)'),
        ],
        string='Proveedor de IA',
        config_parameter='l10n_ar_ai_scanner.provider',
        default='anthropic',
        help='Seleccione el proveedor de IA a utilizar para el escaneo de comprobantes.',
    )

    # ---- API Keys ---------------------------------------------------------
    l10n_ar_ai_scanner_api_key = fields.Char(
        string='Clave API de Anthropic',
        config_parameter='l10n_ar_ai_scanner.api_key',
        help=(
            'Clave de API de Anthropic para acceder a Claude Vision. '
            'Obtenga su clave en https://console.anthropic.com/'
        ),
    )

    l10n_ar_ai_scanner_gemini_api_key = fields.Char(
        string='Clave API de Google Gemini',
        config_parameter='l10n_ar_ai_scanner.gemini_api_key',
        help=(
            'Clave de API de Google para acceder a Gemini. '
            'Obtenga su clave en https://aistudio.google.com/apikey'
        ),
    )

    # ---- Model selection --------------------------------------------------
    l10n_ar_ai_scanner_model = fields.Selection(
        selection=[
            ('claude-opus-4-6', 'Anthropic: Claude Opus 4.6 (Mayor precisión)'),
            ('claude-sonnet-4-5', 'Anthropic: Claude Sonnet 4.5 (Recomendado)'),
            ('claude-opus-4-5', 'Anthropic: Claude Opus 4.5'),
            ('claude-haiku-3-5', 'Anthropic: Claude Haiku 3.5 (Más rápido)'),
            ('gemini-3.1-pro', 'Google: Gemini 3.1 Pro (Mayor precisión)'),
            ('gemini-3.0-pro', 'Google: Gemini 3.0 Pro'),
            ('gemini-2.5-pro', 'Google: Gemini 2.5 Pro'),
            ('gemini-2.5-flash', 'Google: Gemini 2.5 Flash (Recomendado)'),
        ],
        string='Modelo de IA',
        config_parameter='l10n_ar_ai_scanner.model',
        default='claude-sonnet-4-5',
        help='Modelo de IA a utilizar para el escaneo de comprobantes.',
    )

    # ---- Auto-scan --------------------------------------------------------
    l10n_ar_ai_scanner_auto_scan = fields.Boolean(
        string='Escaneo Automático al Adjuntar',
        config_parameter='l10n_ar_ai_scanner.auto_scan',
        default=False,
        help=(
            'Si está activado, el módulo escaneará automáticamente el comprobante '
            'cuando se adjunte un PDF o imagen a una factura de proveedor.'
        ),
    )

    # ---- Account mapping: IVA F731 ----------------------------------------
    l10n_ar_ai_scanner_account_iva_debito = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta IVA Débito Fiscal',
        config_parameter='l10n_ar_ai_scanner.account_iva_debito',
        help='Cuenta contable para el débito fiscal en DDJJ IVA F731.',
    )
    l10n_ar_ai_scanner_account_iva_credito = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta IVA Crédito Fiscal',
        config_parameter='l10n_ar_ai_scanner.account_iva_credito',
        help='Cuenta contable para el crédito fiscal en DDJJ IVA F731.',
    )
    l10n_ar_ai_scanner_account_iva_saldo_libre = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta IVA Saldo Libre Disponibilidad',
        config_parameter='l10n_ar_ai_scanner.account_iva_saldo_libre',
        help='Cuenta contable para el saldo de libre disponibilidad en DDJJ IVA F731.',
    )
    l10n_ar_ai_scanner_account_iva_retencion = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta Retención IVA Sufrida',
        config_parameter='l10n_ar_ai_scanner.account_iva_retencion',
        help='Cuenta contable para retenciones IVA sufridas en DDJJ IVA F731.',
    )
    l10n_ar_ai_scanner_account_iva_percepcion = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta Percepción IVA Sufrida',
        config_parameter='l10n_ar_ai_scanner.account_iva_percepcion',
        help='Cuenta contable para percepciones IVA sufridas en DDJJ IVA F731.',
    )

    # ---- Account mapping: SICOSS F931 -------------------------------------
    l10n_ar_ai_scanner_account_contribuciones = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta Contribuciones Patronales',
        config_parameter='l10n_ar_ai_scanner.account_contribuciones',
        help='Cuenta contable para contribuciones patronales en DDJJ SICOSS F931.',
    )
    l10n_ar_ai_scanner_account_aportes = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta Aportes Empleados',
        config_parameter='l10n_ar_ai_scanner.account_aportes',
        help='Cuenta contable para aportes de empleados en DDJJ SICOSS F931.',
    )

    # ---- Convenio Multilateral CM03: mapeo por jurisdicción ---------------
    # Las cuentas CM se configuran por jurisdicción en el modelo
    # l10n_ar.ai.scanner.cm.jurisdiction (ver botón en la vista de ajustes).

    # ---- Account mapping: Recibo de Sueldo --------------------------------
    l10n_ar_ai_scanner_account_recibo_neto = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta Neto de Sueldo',
        config_parameter='l10n_ar_ai_scanner.account_recibo_neto',
        help='Cuenta contable para el neto de bolsillo en recibos de sueldo.',
    )
