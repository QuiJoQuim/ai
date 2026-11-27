# Copyright 2026 Coopdevs
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import new_test_user

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
