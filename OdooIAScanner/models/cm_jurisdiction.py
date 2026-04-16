# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

ARGENTINA_JURISDICTIONS = [
    ('buenos_aires', 'Buenos Aires'),
    ('caba', 'Ciudad Autónoma de Buenos Aires'),
    ('catamarca', 'Catamarca'),
    ('chaco', 'Chaco'),
    ('chubut', 'Chubut'),
    ('cordoba', 'Córdoba'),
    ('corrientes', 'Corrientes'),
    ('entre_rios', 'Entre Ríos'),
    ('formosa', 'Formosa'),
    ('jujuy', 'Jujuy'),
    ('la_pampa', 'La Pampa'),
    ('la_rioja', 'La Rioja'),
    ('mendoza', 'Mendoza'),
    ('misiones', 'Misiones'),
    ('neuquen', 'Neuquén'),
    ('rio_negro', 'Río Negro'),
    ('salta', 'Salta'),
    ('san_juan', 'San Juan'),
    ('san_luis', 'San Luis'),
    ('santa_cruz', 'Santa Cruz'),
    ('santa_fe', 'Santa Fe'),
    ('santiago_del_estero', 'Santiago del Estero'),
    ('tierra_del_fuego', 'Tierra del Fuego'),
    ('tucuman', 'Tucumán'),
]


class L10nArAiScannerCmJurisdiction(models.Model):
    _name = 'l10n_ar.ai.scanner.cm.jurisdiction'
    _description = 'Mapeo de Cuentas CM por Jurisdicción'
    _order = 'name'

    name = fields.Char(
        string='Jurisdicción',
        required=True,
        help='Nombre de la jurisdicción/provincia tal como aparece en el CM03.',
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Compañía',
        default=lambda self: self.env.company,
        required=True,
    )
    account_cm_impuesto = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta Impuesto Determinado',
        help='Cuenta contable para el impuesto determinado CM03 de esta jurisdicción.',
    )
    account_cm_ret_bancarias = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta Retenciones Bancarias',
        help='Cuenta contable para retenciones bancarias CM03 de esta jurisdicción.',
    )
    account_cm_ret_iibb = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta Retenciones IIBB',
        help='Cuenta contable para retenciones de Ingresos Brutos CM03 de esta jurisdicción.',
    )
    account_cm_perc_iibb = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta Percepciones IIBB',
        help='Cuenta contable para percepciones de Ingresos Brutos CM03 de esta jurisdicción.',
    )
    account_cm_saldo_favor = fields.Many2one(
        comodel_name='account.account',
        string='Cuenta Saldo a Favor Anterior',
        help='Cuenta contable para el saldo a favor del período anterior CM03 de esta jurisdicción.',
    )

    _sql_constraints = [
        ('name_company_uniq', 'unique(name, company_id)',
         'Ya existe un mapeo para esta jurisdicción en esta compañía.'),
    ]
