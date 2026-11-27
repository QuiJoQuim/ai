# Copyright 2026 Coopdevs
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, models

from .ai_tool import executing_request


class MailThread(models.AbstractModel):
    _inherit = "mail.thread"

    # Audit: a document created by an approved AI action must say so, and
    # its request must list it. Only documents with a chatter: lines and
    # technical records would only add noise.

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        execution = executing_request.get()
        if execution:
            execution.request._log_created_records(records)
        return records
