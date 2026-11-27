# Copyright 2026 Coopdevs
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
import logging
import math
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.http import request as http_request
from odoo.tools import SQL

from .ai_tool import approved_tool, running_tool

_logger = logging.getLogger(__name__)

DEFAULT_EXPIRATION_HOURS = 24.0


class AiToolRequest(models.Model):
    _name = "ai.tool.request"
    _description = "AI Tool Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"
    # Requester and approver only have read access: let them use the chatter.
    # Security: posting or handling activities here never answers a request,
    # only action_approve and action_reject do.
    _mail_post_access = "read"

    # Security: users have no write or create access on this model, and every
    # field is readonly: the frozen call cannot be changed after the request
    # is made. Only the methods below write on it, as superuser, after their
    # own checks.
    name = fields.Char(required=True, readonly=True)
    # Audit: if the tool is removed, the request keeps its name
    tool_id = fields.Many2one("ai.tool", readonly=True, ondelete="set null")
    tool_name = fields.Char(required=True, readonly=True)
    tool_description = fields.Text(related="tool_id.description")
    arguments = fields.Text(
        readonly=True, help="Arguments of the tool call, frozen as JSON."
    )
    res_model = fields.Char(string="Model", readonly=True, index=True)
    res_id = fields.Many2oneReference(
        string="Record ID", model_field="res_model", readonly=True, index=True
    )
    record_ref = fields.Reference(
        selection="_selection_record_ref",
        string="Record",
        compute="_compute_record_ref",
    )
    requester_id = fields.Many2one(
        "res.users", required=True, readonly=True, index=True
    )
    approver_id = fields.Many2one(
        "res.users", required=True, readonly=True, index=True, tracking=True
    )
    origin = fields.Char(
        readonly=True, help="Integration that asked for the tool call."
    )
    expiration_date = fields.Datetime(
        readonly=True, help="Empty when the request never expires."
    )
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("done", "Executed"),
            ("rejected", "Rejected"),
            ("expired", "Expired"),
            ("error", "Failed"),
        ],
        required=True,
        default="pending",
        readonly=True,
        tracking=True,
    )
    answer_date = fields.Datetime(readonly=True)
    result = fields.Text(readonly=True)
    error = fields.Text(readonly=True)
    can_answer = fields.Boolean(compute="_compute_can_answer")

    @api.depends("res_model", "res_id")
    def _compute_record_ref(self):
        for request in self:
            if request.res_model in self.env and request.res_id:
                request.record_ref = f"{request.res_model},{request.res_id}"
            else:
                request.record_ref = False

    @api.depends("state", "approver_id")
    @api.depends_context("uid")
    def _compute_can_answer(self):
        for request in self:
            request.can_answer = (
                request.state == "pending" and request.approver_id == self.env.user
            )

    @api.model
    def _selection_record_ref(self):
        # From the registry: no need to read ir.model (and no sudo)
        return [
            (name, self.env[name]._description or name) for name in sorted(self.env)
        ]

    def action_approve(self):
        self.ensure_one()
        self._check_can_answer()
        self._execute()
        return True

    def action_reject(self):
        self.ensure_one()
        self._check_can_answer()
        self._write_answer({"state": "rejected", "answer_date": fields.Datetime.now()})
        self._close_activity()
        self._post_on_record(self.env._("%(user)s rejected the AI action %(name)s."))
        return True

    # Creation

    @api.model
    def _create_from_tool_call(self, tool, arguments, record=None):
        if record is not None and len(record) > 1:
            raise ValueError("AI tool requests accept a single record")
        try:
            # Security: no default= serializer. What is stored must be
            # exactly what will run: never a str() of an unknown object.
            frozen_arguments = json.dumps(arguments)
        except (TypeError, ValueError) as e:
            raise ValueError(
                "The arguments of a tool that requires confirmation must be JSON"
            ) from e
        approver = self._get_approver(record)
        # Security: sudo, because nobody can create requests directly: it is
        # what keeps them from being forged or altered. Only here, after the
        # checks above.
        request = self.sudo().create(
            {
                "name": self._get_request_name(tool, record),
                "tool_id": tool.id,
                "tool_name": tool.name,
                "arguments": frozen_arguments,
                "res_model": record._name if record else False,
                "res_id": record.id if record else False,
                "requester_id": self.env.user.id,
                "approver_id": approver.id,
                "origin": self.env.context.get("ai_tool_origin"),
                "expiration_date": self._get_expiration_date(),
            }
        )
        request._schedule_confirmation_activity()
        return request.sudo(self.env.su)

    @api.model
    def _get_config_param(self, key, default=None):
        # Security: sudo, as Odoo itself does: only administrators can read
        # ir.config_parameter. It reads this module's settings only.
        return self.env["ir.config_parameter"].sudo().get_param(key, default)

    @api.model
    def _get_request_name(self, tool, record):
        if not record:
            return tool.name
        return f"{tool.name}: {record.display_name}"

    @api.model
    def _is_valid_approver(self, user):
        # Security: only active internal users answer requests. An archived
        # user would leave requests stuck; an external one must not decide.
        return bool(user) and user.active and user._is_internal()

    @api.model
    def _get_approver(self, record=None):
        """Whoever executes the tool approves it. Requests run as OdooBot,
        with no person behind (scheduled actions, job queues, webhooks), go
        to the responsible user of the record, or else to the configured
        fallback approver."""
        user = self.env.user
        if not user._is_superuser():
            # Security: portal and public users cannot make AI tool requests.
            # Otherwise an external user could get an internal one to execute
            # actions with internal access rights.
            if not self._is_valid_approver(user):
                raise AccessError(
                    self.env._("Only internal users can request AI actions.")
                )
            return user
        # No person behind: a scheduled action, a job queue, a webhook...
        # This already runs as superuser: no sudo needed.
        if record and "user_id" in record._fields:
            field = record._fields["user_id"]
            responsible = (
                field.comodel_name == "res.users" and record.with_env(self.env).user_id
            )
            if self._is_valid_approver(responsible):
                return responsible
        fallback_id = self._get_config_param("ai_tool_confirm.fallback_approver_id")
        fallback = self.env["res.users"].browse(int(fallback_id or 0)).exists()
        if self._is_valid_approver(fallback):
            return fallback
        raise UserError(
            self.env._(
                "Nobody can confirm this AI action: no person executes it, the "
                "record has no responsible user and no valid fallback approver "
                "is configured."
            )
        )

    @api.model
    def _get_expiration_date(self):
        """The caller may set the expiration of the request through the
        context; otherwise the instance default applies. 0 means never."""
        hours = self.env.context.get("ai_tool_confirm_expiration_hours")
        if hours is None:
            hours = self._get_config_param(
                "ai_tool_confirm.expiration_hours", DEFAULT_EXPIRATION_HOURS
            )
        hours = float(hours)
        # Security: reject inf, nan and negative values instead of failing
        # later in an unexpected way.
        if not math.isfinite(hours) or hours < 0:
            raise ValueError("The expiration of an AI tool request must be >= 0")
        if not hours:
            return False
        return fields.Datetime.now() + timedelta(hours=hours)

    def _schedule_confirmation_activity(self):
        for request in self:
            request.activity_schedule(
                "ai_tool_confirm.mail_activity_type_ai_tool_confirm",
                date_deadline=request.expiration_date
                and fields.Date.to_date(request.expiration_date),
                user_id=request.approver_id.id,
                summary=self.env._("Confirm AI action: %s", request.name),
            )

    def _get_pending_response(self):
        """What the caller of the tool (an LLM, an MCP client) receives."""
        self.ensure_one()
        return {
            "status": "pending_confirmation",
            "request_id": self.id,
            "message": (
                "The action has not been executed yet. It is waiting for "
                f"{self.approver_id.name} to confirm it in Odoo."
            ),
        }

    # Answer

    def _is_interactive_session(self):
        # Security: answers must come from a person using the web client.
        # XML-RPC and JSON-RPC calls (API keys, scripts, AI agents holding the
        # user's credentials) have no authenticated web session, and Odoo does
        # not open web sessions with API keys.
        return bool(http_request) and http_request.session.uid == self.env.uid

    def _check_can_answer(self):
        # Security: an AI tool must never answer a request. Otherwise an LLM
        # with a tool able to call methods could approve its own actions.
        if running_tool.get():
            raise AccessError(self.env._("AI tools cannot answer AI tool requests."))
        if not self._is_interactive_session():
            raise AccessError(
                self.env._("AI tool requests can only be answered from Odoo.")
            )
        # Security: lock the requests before checking their state, so two
        # simultaneous answers cannot both see them pending and run the tool
        # twice (the second one waits for a retry and finds them answered).
        self.env.cr.execute(
            SQL(
                "SELECT id FROM %s WHERE id IN %s FOR UPDATE NOWAIT",
                SQL.identifier(self._table),
                tuple(self.ids),
            )
        )
        self.invalidate_recordset(["state"])
        for request in self:
            if request.state != "pending":
                raise UserError(
                    self.env._("The AI request %s is not pending.", request.name)
                )
            # Security: only the approver answers, not even the superuser on
            # their behalf.
            if request.approver_id != self.env.user:
                raise UserError(
                    self.env._(
                        "Only %(user)s can answer the AI request %(name)s.",
                        user=request.approver_id.name,
                        name=request.name,
                    )
                )
            # Security: checked here too, not only by the cron, which may not
            # have run yet.
            if request.expiration_date and (
                request.expiration_date < fields.Datetime.now()
            ):
                raise UserError(
                    self.env._("The AI request %s has expired.", request.name)
                )

    def _write_answer(self, vals):
        # Security: sudo, because nobody can write on requests directly: it
        # is what keeps them from being altered. Only after _check_can_answer.
        self.sudo().write(vals)

    def _execute(self):
        """Run the tool with the access rights of the approver."""
        self.ensure_one()
        # Security: never as superuser, even if this method is reached from
        # a sudo() environment: the approver's own rights apply.
        env = self.env(su=False)
        tool = env["ai.tool"].browse(self.tool_id.id)
        record = None
        if self.res_model:
            record = env[self.res_model].browse(self.res_id).exists()
        arguments = json.loads(self.arguments or "{}")
        vals = {"answer_date": fields.Datetime.now()}
        # Security: only this tool, and only during this call, skips the
        # confirmation.
        token = approved_tool.set(tool.id)
        try:
            if not tool:
                raise UserError(self.env._("The AI tool no longer exists."))
            if record is not None and not record:
                raise UserError(self.env._("The record no longer exists."))
            with self.env.cr.savepoint():
                result = tool._execute_tool(record=record, **arguments)
        except Exception as e:
            _logger.info("AI tool request %s failed: %s", self.id, e)
            vals.update({"state": "error", "error": str(e)})
            message = self.env._(
                "%(user)s approved the AI action %(name)s, but it failed."
            )
        else:
            vals.update({"state": "done", "result": json.dumps(result, default=str)})
            message = self.env._(
                "%(user)s approved and executed the AI action %(name)s."
            )
        finally:
            approved_tool.reset(token)
        self._write_answer(vals)
        self._close_activity()
        self._post_on_record(message)

    def _close_activity(self):
        # No sudo: the activity is the approver's, and the request accepts
        # messages from those who can read it (_mail_post_access).
        self.activity_feedback(["ai_tool_confirm.mail_activity_type_ai_tool_confirm"])

    def _post_on_record(self, message):
        """Leave a trace on the chatter of the affected record."""
        self.ensure_one()
        if not self.res_model:
            return
        if not self.env[self.res_model].browse(self.res_id).exists():
            return
        # Security: a plain string body is escaped by _message_log, so user
        # and tool names cannot inject HTML.
        body = message % {"user": self.env.user.name, "name": self.tool_name}
        self._log_audit_note(self.res_model, self.res_id, body, self.env.user)

    # Audit trail

    def _log_audit_note(self, model, res_id, body, author):
        """An internal note on a document, for the audit trail.

        _message_log is Odoo's API for log notes: internal, it notifies
        nobody and subscribes nobody, and it does not depend on the rights of
        the user (it creates the message as superuser itself). The audit
        trail must not depend on whether the approver can post on the
        document.
        """
        record = self.env[model].browse(res_id)
        if hasattr(record, "_message_log"):
            record._message_log(body=body, author_id=author.partner_id.id)

    # Expiration

    @api.model
    def _cron_expire_requests(self):
        requests = self.search(
            [
                ("state", "=", "pending"),
                ("expiration_date", "!=", False),
                ("expiration_date", "<", fields.Datetime.now()),
            ]
        )
        requests.write({"state": "expired"})
        requests.activity_unlink(["ai_tool_confirm.mail_activity_type_ai_tool_confirm"])
