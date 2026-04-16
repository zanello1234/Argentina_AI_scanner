# -*- coding: utf-8 -*-
{
    'name': 'Argentina AI Invoice Scanner',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Localizations',
    'summary': 'Escaneo inteligente de facturas, tickets, DDJJ IVA, SICOSS, CM y recibos argentinos con IA (Claude / Gemini)',
    'description': """
Argentina AI Invoice Scanner
=============================
Módulo para escanear comprobantes fiscales argentinos utilizando inteligencia artificial
con visión por computadora (Anthropic Claude y Google Gemini).

Tipos de comprobantes soportados:
- Facturas A, B, C, E, M
- Notas de Débito y Crédito A, B, C, E, M
- Tickets Fiscales
- DDJJ IVA F731
- SICOSS F931 (Seguridad Social)
- Convenio Multilateral CM03/CM04/CM05
- Recibos de Sueldo

Funcionalidades principales:
- Extracción automática de datos AFIP (CAE, CUIT, tipo, montos, IVA)
- Escaneo individual con wizard de revisión interactiva
- Carga masiva de hasta 10 comprobantes en paralelo
- Procesamiento en segundo plano con cron dedicado
- Creación automática de proveedores por CUIT
- Registro de auditoría inmutable de cada escaneo
- Escaneo automático al adjuntar PDF/imagen
- Mapeo de cuentas contables para IVA, SICOSS, CM y Recibos
- API REST para integración con sistemas externos
- Soporte para PDF, JPEG, PNG, WebP y GIF
    """,
    'author': 'Martin Zanello',
    'website': 'https://github.com/zanello1234/agroproyect',
    'license': 'LGPL-3',
    'price': 300.00,
    'currency': 'USD',
    'images': ['static/description/icon.png'],
    'depends': [
        'account',
        'l10n_ar',
        'l10n_latam_invoice_document',
        'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/l10n_ar_document_types.xml',
        'data/ir_cron_data.xml',
        'views/account_move_views.xml',
        'views/scan_log_views.xml',
        'views/scan_wizard_views.xml',
        'views/bulk_scan_wizard_views.xml',
        'views/cm_jurisdiction_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'assets': {},
    'external_dependencies': {
        # All Python deps are imported lazily at runtime.
        # Install them via: pip install -r requirements.txt
        'python': [],
        'bin': [],
    },
    'installable': True,
    'auto_install': False,
    'application': True,
}
