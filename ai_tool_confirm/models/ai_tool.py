# Copyright 2026 Coopdevs
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from contextvars import ContextVar

from odoo import api, fields, models

# Security: these flags live in context variables, not in the Odoo context.
# The Odoo context travels with every RPC call, so any client could set a
# context key; a context variable can only be set by server-side code.
#
# Id of the tool an approved request is executing right now: the only way to
# run a tool that requires confirmation.
approved_tool = ContextVar("ai_tool_confirm_approved_tool", default=None)
# True while any AI tool is running: an AI tool must never answer a request,
# or an LLM could approve its own actions.
running_tool = ContextVar("ai_tool_confirm_running_tool", default=False)


class AiTool(models.Model):
    _inherit = "ai.tool"

    confirmation = fields.Selection(
        [
            ("default", "When the tool modifies data"),
            ("always", "Always"),
            ("never", "Never"),
        ],
        string="Human Confirmation",
        required=True,
        default="default",
        help="When a human must confirm a call to this tool before it is "
        "executed. By default, every tool not flagged as read-only.",
    )
    requires_confirmation = fields.Boolean(compute="_compute_requires_confirmation")

    @api.depends("confirmation", "read_only")
    def _compute_requires_confirmation(self):
        for tool in self:
            if tool.confirmation == "default":
                tool.requires_confirmation = not tool.read_only
            else:
                tool.requires_confirmation = tool.confirmation == "always"

    def _execute_tool(self, *args, record=None, **kwargs):
        token = running_tool.set(True)
        try:
            if self.requires_confirmation and approved_tool.get() != self.id:
                return self._request_confirmation(args, kwargs, record)
            return super()._execute_tool(*args, record=record, **kwargs)
        finally:
            running_tool.reset(token)

    def _request_confirmation(self, args, kwargs, record):
        """Instead of executing the tool, ask a human to confirm the call."""
        self.ensure_one()
        if args:
            raise ValueError(
                "Tools that require confirmation only accept keyword arguments"
            )
        # Security: callers such as MCP clients pass their arguments straight
        # through; make sure "record" is a real record before trusting it.
        if record is not None and not isinstance(record, models.BaseModel):
            raise ValueError("The record of an AI tool call must be an Odoo record")
        request = self.env["ai.tool.request"]._create_from_tool_call(
            self, kwargs, record=record
        )
        return request._get_pending_response()
