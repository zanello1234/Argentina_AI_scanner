# -*- coding: utf-8 -*-
import json
import logging

from odoo import http
from odoo.http import request
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)


class L10nArAiScannerController(http.Controller):
    """Optional JSON endpoint to trigger an AI scan from external systems."""

    @http.route(
        '/l10n_ar_ai_scanner/scan/<int:move_id>',
        type='json',
        auth='user',
        methods=['POST'],
        csrf=True,
    )
    def scan_invoice(self, move_id, **kwargs):
        """Trigger an AI scan for the given account.move id.

        Returns a JSON object with the extracted data or an error message.

        Request body (optional):
            {}  – no additional parameters required.

        Response:
            {
                "success": true/false,
                "data": { ... extracted fields ... },
                "error": "error message if success=false"
            }
        """
        move = request.env['account.move'].browse(move_id)
        if not move.exists():
            return {'success': False, 'error': 'Invoice not found.'}

        try:
            move.check_access('write')
        except AccessError as exc:
            return {'success': False, 'error': str(exc)}

        try:
            move.action_scan_invoice_ai()
        except UserError as exc:
            return {'success': False, 'error': str(exc)}
        except Exception as exc:
            _logger.exception('Unexpected error during API scan for move %s', move_id)
            return {'success': False, 'error': str(exc)}

        raw = move.ai_scan_raw_result
        extracted = {}
        if raw:
            try:
                extracted = json.loads(raw)
            except json.JSONDecodeError:
                extracted = {'raw': raw}

        return {
            'success': move.ai_scan_state == 'done',
            'state': move.ai_scan_state,
            'confidence': move.ai_scan_confidence,
            'cae': move.ai_scanned_cae,
            'cuit_emisor': move.ai_scanned_cuit_emisor,
            'doc_number': move.ai_scanned_doc_number,
            'data': extracted,
        }

    @http.route(
        '/l10n_ar_ai_scanner/status/<int:move_id>',
        type='json',
        auth='user',
        methods=['POST'],
        csrf=True,
    )
    def scan_status(self, move_id, **kwargs):
        """Return the current AI scan status for a given account.move id."""
        move = request.env['account.move'].browse(move_id)
        if not move.exists():
            return {'success': False, 'error': 'Invoice not found.'}

        try:
            move.check_access('read')
        except AccessError as exc:
            return {'success': False, 'error': str(exc)}

        return {
            'success': True,
            'move_id': move_id,
            'state': move.ai_scan_state,
            'confidence': move.ai_scan_confidence,
            'cae': move.ai_scanned_cae or '',
            'cuit_emisor': move.ai_scanned_cuit_emisor or '',
            'doc_number': move.ai_scanned_doc_number or '',
        }
