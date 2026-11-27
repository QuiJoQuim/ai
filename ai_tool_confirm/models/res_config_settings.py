# Copyright 2026 Coopdevs
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import math

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .ai_tool_request import DEFAULT_EXPIRATION_HOURS


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ai_tool_confirm_expiration_hours = fields.Float(
        string="AI Confirmation Expiration (hours)",
        config_parameter="ai_tool_confirm.expiration_hours",
        default=DEFAULT_EXPIRATION_HOURS,
        help="Hours a pending AI tool request can wait for confirmation. "
        "Use 0 for requests that never expire.",
    )
    ai_tool_confirm_fallback_approver_id = fields.Many2one(
        "res.users",
        string="AI Confirmation Fallback Approver",
        config_parameter="ai_tool_confirm.fallback_approver_id",
        domain=[("share", "=", False)],
        help="Approver of the AI tool requests with no person behind "
        "(scheduled actions, job queues, webhooks) on records without a "
        "responsible user.",
    )

    @api.constrains("ai_tool_confirm_expiration_hours")
    def _check_ai_tool_confirm_expiration_hours(self):
        # A wrong value would make every AI tool that modifies data fail
        for settings in self:
            hours = settings.ai_tool_confirm_expiration_hours
            if not math.isfinite(hours) or hours < 0:
                raise ValidationError(
                    self.env._("The AI confirmation expiration must be 0 or more.")
                )
