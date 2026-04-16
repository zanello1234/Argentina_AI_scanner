# -*- coding: utf-8 -*-
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

SCANNABLE_MIMETYPES = {
    'application/pdf',
    'image/jpeg',
    'image/jpg',
    'image/png',
    'image/gif',
    'image/webp',
}


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model_create_multi
    def create(self, vals_list):
        attachments = super().create(vals_list)

        # Check if auto-scan is enabled
        auto_scan = (
            self.env['ir.config_parameter']
            .sudo()
            .get_param('l10n_ar_ai_scanner.auto_scan', default='False')
        )
        if auto_scan not in ('True', 'true', '1'):
            return attachments

        # Find attachments linked to account.move with scannable mimetypes
        for att in attachments:
            if (
                att.res_model == 'account.move'
                and att.res_id
                and (att.mimetype or '') in SCANNABLE_MIMETYPES
            ):
                move = self.env['account.move'].browse(att.res_id)
                if (
                    move.exists()
                    and move.move_type in ('in_invoice', 'in_refund', 'out_invoice', 'out_refund')
                    and move.ai_scan_state in ('draft', 'error')
                ):
                    try:
                        move.action_scan_invoice_ai()
                        _logger.info(
                            'Auto-scan triggered for move %s (attachment %s)',
                            move.id, att.id,
                        )
                    except Exception:
                        _logger.warning(
                            'Auto-scan failed for move %s (attachment %s)',
                            move.id, att.id, exc_info=True,
                        )

        return attachments
