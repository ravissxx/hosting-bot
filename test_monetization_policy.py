import unittest
from types import SimpleNamespace

from telebot import types

from monetization_policy import (
    DISABLED_MONETIZATION_FEATURE_FLAGS,
    MAX_BOTS_PER_USER,
    filter_disabled_monetization_buttons,
    is_disabled_monetization_callback,
    is_disabled_monetization_flow,
)


class MonetizationPolicyTests(unittest.TestCase):
    def test_bot_limit_is_eight(self):
        self.assertEqual(MAX_BOTS_PER_USER, 8)

    def test_billing_and_plan_callbacks_are_disabled(self):
        blocked = (
            "menu_plans",
            "plan_view_pro",
            "pay_proof",
            "wallet_topup",
            "coupon_redeem",
            "adm_payments",
            "adm_ref_stats",
            "adm_goal_monthly",
            "adm_rate_plan_free",
            "adm_tmpl_edit_payment_received",
            "adm_ff_toggle_payment_system",
        )
        for callback in blocked:
            with self.subTest(callback=callback):
                self.assertTrue(is_disabled_monetization_callback(callback))

        for flag in DISABLED_MONETIZATION_FEATURE_FLAGS:
            self.assertTrue(is_disabled_monetization_callback(f"adm_ff_toggle_{flag}"))

    def test_upload_approval_and_regular_controls_remain_available(self):
        allowed = (
            "adm_pending",
            "adm_approval_toggle",
            "appr_ok_42",
            "appr_no_42",
            "menu_bots",
            "bot_start_abc123",
            "adm_refresh_status",
            "adm_ff_toggle_approval_system",
            "adm_ff_reset_all",
        )
        for callback in allowed:
            with self.subTest(callback=callback):
                self.assertFalse(is_disabled_monetization_callback(callback))

    def test_billing_input_flows_are_disabled(self):
        blocked = (
            {"flow": "await_payment_proof"},
            {"flow": "await_topup_proof"},
            {"flow": "await_coupon_admin"},
            {"flow": "await_gift_confirm"},
            {"flow": "await_adm_wallet_adjust"},
            {"flow": "await_adm_goal_set"},
            {"flow": "await_adm_bc_set", "bc_key": "min_payment_amount"},
        )
        for state in blocked:
            with self.subTest(state=state):
                self.assertTrue(is_disabled_monetization_flow(state))

        self.assertFalse(is_disabled_monetization_flow({"flow": "await_upload"}))
        self.assertFalse(
            is_disabled_monetization_flow(
                {"flow": "await_adm_bc_set", "bc_key": "maintenance_message"}
            )
        )

    def test_keyboard_removes_billing_buttons_without_touching_upload_approval(self):
        markup = SimpleNamespace(
            keyboard=[
                [
                    SimpleNamespace(text="Payments", callback_data="adm_payments"),
                    SimpleNamespace(text="Pending uploads", callback_data="adm_pending"),
                ],
                [SimpleNamespace(text="Top up", callback_data="wallet_topup")],
                [SimpleNamespace(text="Approve bot", callback_data="appr_ok_42")],
            ]
        )

        result = filter_disabled_monetization_buttons(markup)
        self.assertIs(result, markup)
        self.assertEqual(
            [[button.callback_data for button in row] for row in result.keyboard],
            [["adm_pending"], ["appr_ok_42"]],
        )

    def test_empty_billing_keyboard_is_removed(self):
        markup = SimpleNamespace(
            keyboard=[
                [SimpleNamespace(text="Plans", callback_data="menu_plans")],
            ]
        )
        self.assertIsNone(filter_disabled_monetization_buttons(markup))

    def test_telegram_keyboard_serializes_only_remaining_buttons(self):
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("Payments", callback_data="adm_payments"),
            types.InlineKeyboardButton("Pending uploads", callback_data="adm_pending"),
        )

        result = filter_disabled_monetization_buttons(markup)
        self.assertIs(result, markup)
        serialized = result.to_dict()["inline_keyboard"]
        self.assertEqual(
            [[button.get("callback_data") for button in row] for row in serialized],
            [["adm_pending"]],
        )


if __name__ == "__main__":
    unittest.main()
