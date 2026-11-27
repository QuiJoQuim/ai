# Copyright 2026 Coopdevs
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.ai_tool_confirm.models.ai_tool import running_tool
from odoo.addons.base.tests.common import BaseCommon


class AiToolConfirmCase(BaseCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        groups = "base.group_user,base.group_partner_manager"
        cls.requester = new_test_user(cls.env, "ai_requester", groups=groups)
        cls.other_user = new_test_user(cls.env, "ai_other", groups=groups)
        cls.fallback = new_test_user(cls.env, "ai_fallback", groups=groups)
        cls.admin = new_test_user(
            cls.env, "ai_admin", groups="base.group_user,base.group_system"
        )
        cls.partner = cls.env["res.partner"].create({"name": "AI Partner"})
        cls.post_message = cls.env.ref("ai_tool.post_message")
        cls.get_date = cls.env.ref("ai_tool.current_date")
        cls.Request = cls.env["ai.tool.request"]

    def _call_post_message(self, user=None, **context):
        env = self.env(user=user) if user else self.env
        return (
            self.post_message.with_env(env)
            .with_context(**context)
            ._execute_tool(message="Hello AI", record=self.partner.with_env(env))
        )

    def _request(self, result, user=None):
        request = self.Request.browse(result["request_id"])
        return request.with_user(user) if user else request

    def _partner_messages(self, text):
        return self.partner.message_ids.filtered(lambda m: text in (m.body or ""))


class TestAiToolConfirm(AiToolConfirmCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Tests run without an HTTP request: act as if answers came from the
        # web client. TestAiToolConfirmSession checks the real behaviour.
        cls.startClassPatcher(
            patch.object(
                type(cls.Request), "_is_interactive_session", return_value=True
            )
        )

    def test_read_only_tool_is_executed(self):
        result = self.get_date.with_user(self.requester)._execute_tool()
        self.assertIn("date", result)
        self.assertFalse(self.Request.search([]))

    def test_tool_call_creates_request(self):
        result = self._call_post_message(self.requester, ai_tool_origin="test")
        self.assertEqual(result["status"], "pending_confirmation")
        self.assertFalse(self._partner_messages("Hello AI"))
        request = self._request(result)
        self.assertEqual(request.state, "pending")
        self.assertEqual(request.tool_id, self.post_message)
        self.assertEqual(request.tool_name, "post_message")
        self.assertEqual(request.record_ref, self.partner)
        self.assertEqual(request.requester_id, self.requester)
        self.assertEqual(request.approver_id, self.requester)
        self.assertEqual(request.origin, "test")
        self.assertIn("Hello AI", request.arguments)
        self.assertEqual(request.activity_ids.user_id, self.requester)

    def test_approve(self):
        request = self._request(self._call_post_message(self.requester), self.requester)
        self.assertTrue(request.can_answer)
        request.action_approve()
        self.assertEqual(request.state, "done")
        self.assertTrue(request.answer_date)
        self.assertFalse(request.activity_ids)
        message = self._partner_messages("Hello AI")
        self.assertEqual(len(message), 1)
        self.assertEqual(message.create_uid, self.requester)
        self.assertTrue(self._partner_messages("approved and executed"))

    def test_approve_never_as_superuser(self):
        """Even when approving from a sudo() environment, the tool runs with
        the rights of the approver."""
        request = self._request(self._call_post_message(self.requester), self.requester)
        executed_as_su = []
        AiTool = type(self.env["ai.tool"])
        with patch.object(
            AiTool,
            "_ai_post_message",
            autospec=True,
            side_effect=lambda tool, **kw: executed_as_su.append(tool.env.su) or {},
        ):
            request.sudo().action_approve()
        self.assertEqual(executed_as_su, [False])
        self.assertEqual(request.state, "done")

    def _create_partner_tool(self):
        """A generic tool (no record) that creates a new record."""
        executed = []

        def _ai_create_partner(tool, name=None, fail=False):
            executed.append(tool.env.su)
            partner = tool.env["res.partner"].create({"name": name})
            if fail:
                raise ValueError("Failing after the creation")
            return {"id": partner.id}

        self.startPatcher(
            patch.object(
                type(self.env["ai.tool"]),
                "_ai_create_partner",
                _ai_create_partner,
                create=True,
            )
        )
        tool = self.env["ai.tool"].create(
            {
                "name": "create_partner",
                "description": "Create a contact.",
                "model_id": self.env.ref("ai_tool.model_ai_tool").id,
                "function_name": "_ai_create_partner",
                "kind": "generic",
            }
        )
        return tool, executed

    def test_approve_without_record(self):
        """A tool that creates a record: the request has no record, and the
        new record is created by the approver once approved."""
        tool, executed = self._create_partner_tool()
        result = tool.with_user(self.requester)._execute_tool(name="AI New Contact")
        self.assertEqual(result["status"], "pending_confirmation")
        Partner = self.env["res.partner"]
        self.assertFalse(Partner.search([("name", "=", "AI New Contact")]))
        request = self._request(result, self.requester)
        self.assertFalse(request.res_model)
        self.assertFalse(request.record_ref)
        self.assertEqual(request.name, "create_partner")
        self.assertEqual(request.approver_id, self.requester)
        request.action_approve()
        self.assertEqual(request.state, "done")
        self.assertEqual(executed, [False])
        new_partner = Partner.search([("name", "=", "AI New Contact")])
        self.assertEqual(len(new_partner), 1)
        self.assertEqual(new_partner.create_uid, self.requester)
        self.assertIn(str(new_partner.id), request.result)

    def test_approval_is_single_use(self):
        """If the approved tool runs an LLM that calls it again, that call
        needs its own confirmation."""
        tool, executed = self._create_partner_tool()
        inner_results = []
        original = type(self.env["ai.tool"])._ai_create_partner

        def create_twice(tool_model, name=None, fail=False):
            if name == "AI Outer":
                inner_results.append(
                    tool.with_env(tool_model.env)._execute_tool(name="AI Inner")
                )
            return original(tool_model, name=name, fail=fail)

        with patch.object(
            type(self.env["ai.tool"]), "_ai_create_partner", create_twice
        ):
            result = tool.with_user(self.requester)._execute_tool(name="AI Outer")
            self._request(result, self.requester).action_approve()
        Partner = self.env["res.partner"]
        self.assertTrue(Partner.search([("name", "=", "AI Outer")]))
        self.assertFalse(Partner.search([("name", "=", "AI Inner")]))
        self.assertEqual(inner_results[0]["status"], "pending_confirmation")

    def test_tool_changed_since_request(self):
        request = self._request(self._call_post_message(self.requester), self.requester)
        self.post_message.function_name = "_ai_get_date"
        request.action_approve()
        self.assertEqual(request.state, "error")
        self.assertIn("changed", request.error)

    def test_runs_in_requested_company(self):
        company_b = self.env["res.company"].create({"name": "AI Company B"})
        self.requester.company_ids |= company_b
        tool, executed = self._create_partner_tool()
        companies = []
        original = type(self.env["ai.tool"])._ai_create_partner

        def record_company(tool_model, name=None, fail=False):
            companies.append(tool_model.env.companies)
            return original(tool_model, name=name, fail=fail)

        result = (
            tool.with_user(self.requester)
            .with_company(company_b)
            ._execute_tool(name="AI Company Contact")
        )
        request = self._request(result, self.requester)
        self.assertEqual(request.company_id, company_b)
        with patch.object(
            type(self.env["ai.tool"]), "_ai_create_partner", record_company
        ):
            # Approved with another company active: it runs in company B only
            request.with_company(self.env.company).action_approve()
        self.assertEqual(request.state, "done", request.error)
        self.assertEqual(companies, [company_b])

    def test_approver_without_company_access(self):
        company_b = self.env["res.company"].create({"name": "AI Company B"})
        self.requester.company_ids |= company_b
        result = (
            self.post_message.with_user(self.requester)
            .with_company(company_b)
            ._execute_tool(message="Hello AI", record=self.partner)
        )
        request = self._request(result, self.requester)
        self.requester.company_ids -= company_b
        request.action_approve()
        self.assertEqual(request.state, "error")
        self.assertIn("You do not have access to the company", request.error)
        self.assertFalse(self._partner_messages("Hello AI"))

    def test_approver_context_defaults_ignored(self):
        """default_* keys of the approver's context do not fill the records
        the tool creates."""
        tool, executed = self._create_partner_tool()
        result = tool.with_user(self.requester)._execute_tool(name="AI Clean")
        request = self._request(result, self.requester)
        request.with_context(default_comment="Injected").action_approve()
        new_partner = self.env["res.partner"].search([("name", "=", "AI Clean")])
        self.assertTrue(new_partner)
        self.assertFalse(new_partner.comment)

    def test_automation_without_record_goes_to_fallback(self):
        """With no person behind nor a record, only the fallback
        approver can confirm, and the record is created with their rights."""
        tool, executed = self._create_partner_tool()
        with self.assertRaises(UserError):
            tool._execute_tool(name="AI Automated Contact")
        self.env["ir.config_parameter"].set_param(
            "ai_tool_confirm.fallback_approver_id", str(self.fallback.id)
        )
        request = self._request(tool._execute_tool(name="AI Automated Contact"))
        self.assertEqual(request.approver_id, self.fallback)
        request.with_user(self.fallback).action_approve()
        self.assertEqual(request.state, "done")
        new_partner = self.env["res.partner"].search(
            [("name", "=", "AI Automated Contact")]
        )
        self.assertEqual(new_partner.create_uid, self.fallback)

    def test_reject(self):
        request = self._request(self._call_post_message(self.requester), self.requester)
        request.action_reject()
        self.assertEqual(request.state, "rejected")
        self.assertFalse(request.activity_ids)
        self.assertFalse(self._partner_messages("Hello AI"))
        self.assertTrue(self._partner_messages("rejected"))

    def test_only_approver_can_answer(self):
        request = self._request(self._call_post_message(self.requester))
        # Other users cannot even see the request
        with self.assertRaises(AccessError):
            request.with_user(self.other_user).action_approve()
        # Not even the superuser answers on behalf of the approver
        with self.assertRaises(UserError):
            request.action_approve()
        self.assertEqual(request.state, "pending")

    def test_ai_tool_cannot_answer(self):
        """An AI tool able to call methods must not approve its own request."""
        request = self._request(self._call_post_message(self.requester), self.requester)
        token = running_tool.set(True)
        try:
            with self.assertRaises(AccessError):
                request.action_approve()
        finally:
            running_tool.reset(token)
        self.assertEqual(request.state, "pending")

    def test_context_does_not_skip_confirmation(self):
        """The Odoo context comes from RPC clients: it must not bypass."""
        result = self._call_post_message(self.requester, ai_tool_confirm_execute=True)
        self.assertEqual(result["status"], "pending_confirmation")
        self.assertFalse(self._partner_messages("Hello AI"))

    def test_cannot_answer_twice(self):
        request = self._request(self._call_post_message(self.requester), self.requester)
        request.action_reject()
        with self.assertRaises(UserError):
            request.action_approve()

    def test_requests_cannot_be_modified(self):
        request = self._request(self._call_post_message(self.requester), self.requester)
        with self.assertRaises(AccessError):
            request.write({"arguments": '{"message": "Something else"}'})
        with self.assertRaises(AccessError):
            request.with_user(self.admin).unlink()

    def test_failed_execution(self):
        request = self._request(self._call_post_message(self.requester), self.requester)
        self.partner.unlink()
        request.action_approve()
        self.assertEqual(request.state, "error")
        self.assertTrue(request.error)

    def test_removed_tool(self):
        tool = self.post_message.copy({"name": "temporary_post_message"})
        result = tool.with_user(self.requester)._execute_tool(
            message="Hello AI", record=self.partner.with_user(self.requester)
        )
        request = self._request(result, self.requester)
        tool.unlink()
        self.assertEqual(request.tool_name, "temporary_post_message")
        request.action_approve()
        self.assertEqual(request.state, "error")

    def test_expiration_from_context(self):
        now = fields.Datetime.now()
        with freeze_time(now):
            result = self._call_post_message(
                self.requester, ai_tool_confirm_expiration_hours=2
            )
        request = self._request(result)
        self.assertEqual(request.expiration_date, now + timedelta(hours=2))
        with freeze_time(now + timedelta(hours=3)):
            with self.assertRaises(UserError):
                request.with_user(self.requester).action_approve()
            self.Request._cron_expire_requests()
        self.assertEqual(request.state, "expired")
        self.assertFalse(request.activity_ids)

    def test_expiration_from_settings(self):
        self.env["ir.config_parameter"].set_param(
            "ai_tool_confirm.expiration_hours", "5"
        )
        now = fields.Datetime.now()
        with freeze_time(now):
            result = self._call_post_message(self.requester)
        request = self._request(result)
        self.assertEqual(request.expiration_date, now + timedelta(hours=5))

    def test_no_expiration(self):
        result = self._call_post_message(
            self.requester, ai_tool_confirm_expiration_hours=0
        )
        request = self._request(result)
        self.assertFalse(request.expiration_date)
        with freeze_time(fields.Datetime.now() + timedelta(days=365)):
            self.Request._cron_expire_requests()
        self.assertEqual(request.state, "pending")

    def test_invalid_expiration(self):
        for hours in (-1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self._call_post_message(
                    self.requester, ai_tool_confirm_expiration_hours=hours
                )

    def test_invalid_calls(self):
        tool = self.post_message.with_user(self.requester)
        # A record that is not a recordset (e.g. sent by an MCP client)
        with self.assertRaises(ValueError):
            tool._execute_tool(message="Hello AI", record=self.partner.id)
        # Arguments that cannot be frozen as JSON
        with self.assertRaises(ValueError):
            tool._execute_tool(message=object(), record=self.partner)
        self.assertFalse(self.Request.search([]))

    def test_portal_user_cannot_request(self):
        portal = new_test_user(self.env, "ai_portal", groups="base.group_portal")
        with self.assertRaises(AccessError):
            self.post_message.with_user(portal)._execute_tool(
                message="Hello AI", record=self.partner
            )

    def test_automation_goes_to_responsible(self):
        """With no person behind (self.env is the superuser), the
        request goes to the responsible user of the record."""
        self.partner.user_id = self.other_user
        request = self._request(self._call_post_message())
        self.assertEqual(request.requester_id, self.env.user)
        self.assertEqual(request.approver_id, self.other_user)
        request.with_user(self.other_user).action_approve()
        self.assertEqual(request.state, "done")
        self.assertEqual(self._partner_messages("Hello AI").create_uid, self.other_user)
        # The approver, not the requester, closes the activity: no sudo needed
        self.assertFalse(request.activity_ids)

    def test_automation_goes_to_fallback(self):
        self.env["ir.config_parameter"].set_param(
            "ai_tool_confirm.fallback_approver_id", str(self.fallback.id)
        )
        request = self._request(self._call_post_message())
        self.assertEqual(request.approver_id, self.fallback)

    def test_automation_skips_invalid_approvers(self):
        self.partner.user_id = self.other_user
        self.other_user.active = False
        self.env["ir.config_parameter"].set_param(
            "ai_tool_confirm.fallback_approver_id", str(self.fallback.id)
        )
        self.fallback.active = False
        with self.assertRaises(UserError):
            self._call_post_message()

    def test_superuser_in_web_request_of_internal_user(self):
        """Code running the tool as OdooBot within the web request of an
        internal user: that user executes it, and approves it."""
        self.partner.user_id = self.other_user
        with patch.object(
            type(self.Request), "_get_web_user", return_value=self.requester
        ):
            request = self._request(self._call_post_message())
        self.assertEqual(request.approver_id, self.requester)

    def test_superuser_in_web_request_of_external_user(self):
        """A website chatbot running the tool as OdooBot for a visitor or a
        portal user: refused, not sent to an internal user."""
        self.partner.user_id = self.other_user
        portal = new_test_user(self.env, "ai_portal2", groups="base.group_portal")
        for web_user in (self.env.ref("base.public_user"), portal):
            with patch.object(
                type(self.Request), "_get_web_user", return_value=web_user
            ):
                with self.assertRaises(AccessError):
                    self._call_post_message()
        self.assertFalse(self.Request.search([]))

    def test_invalid_expiration_setting(self):
        with self.assertRaises(ValidationError):
            self.env["res.config.settings"].create(
                {"ai_tool_confirm_expiration_hours": -1}
            )

    def test_audit_note_without_write_access(self):
        """The audit trail does not depend on whether the approver can post
        on the record: a user who can only read it still leaves the note."""
        reader = new_test_user(self.env, "ai_reader", groups="base.group_user")
        self.assertFalse(self.partner.with_user(reader).has_access("write"))
        self.partner.user_id = reader
        request = self._request(self._call_post_message())
        self.assertEqual(request.approver_id, reader)
        request.with_user(reader).action_reject()
        self.assertEqual(request.state, "rejected")
        self.assertTrue(self._partner_messages("rejected the AI action"))
        self.assertFalse(request.activity_ids)

    def test_automation_without_approver(self):
        with self.assertRaises(UserError):
            self._call_post_message()

    def test_confirmation_override(self):
        self.post_message.confirmation = "never"
        self.assertFalse(self.post_message.requires_confirmation)
        result = self._call_post_message(self.requester)
        self.assertEqual(result, {})
        self.assertTrue(self._partner_messages("Hello AI"))
        self.get_date.confirmation = "always"
        result = self.get_date.with_user(self.requester)._execute_tool()
        self.assertEqual(result["status"], "pending_confirmation")

    def test_admin_only_changes_confirmation(self):
        tool = self.post_message.with_user(self.admin)
        tool.write({"confirmation": "always"})
        self.assertEqual(self.post_message.confirmation, "always")
        for vals in (
            {"function_name": "unlink"},
            {"read_only": True},
            {"kind": "generic"},
        ):
            with self.assertRaises(AccessError):
                tool.write(vals)


class TestAiToolConfirmSession(AiToolConfirmCase):
    def test_answer_requires_web_session(self):
        """Without a web session (XML-RPC, JSON-RPC with an API key, scripts)
        requests cannot be answered."""
        request = self._request(self._call_post_message(self.requester), self.requester)
        with self.assertRaises(AccessError):
            request.action_approve()
        with self.assertRaises(AccessError):
            request.action_reject()
        self.assertEqual(request.state, "pending")
