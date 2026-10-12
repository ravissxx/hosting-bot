"""Rules for disabling the legacy billing and plan features."""

from typing import Any


MAX_BOTS_PER_USER = 8
BILLING_DISABLED_MESSAGE = "Payment and plan features have been removed."

DISABLED_MONETIZATION_FEATURE_FLAGS = frozenset(
    {
        "payment_system",
        "coupon_system",
        "referral_system",
        "wallet_topup",
        "gift_plan",
        "trial_plan",
    }
)

_DISABLED_CALLBACKS = frozenset(
    {
        "menu_plans",
        "menu_buy",
        "menu_referral",
        "menu_wallet",
        "menu_trial",
        "menu_coupon",
        "adm_approve",       # Payment approvals; upload approvals use appr_ok_/appr_no_.
        "adm_giveplan",
        "adm_analytics",     # Contains revenue and plan-distribution reports.
    }
)

_DISABLED_CALLBACK_PREFIXES = (
    "pay_",
    "payapprove_",
    "payreject_",
    "plan_",
    "trial_",
    "coupon_",
    "wallet_",
    "adm_pay",
    "adm_payment",
    "adm_coupon",
    "adm_ref_",
    "adm_revenue",
    "adm_goal_",
    "adm_subs",
    "adm_sub_",
    "adm_wallet",
    "adm_gift",
    "adm_plan",
    "adm_set_plan",
    "adm_notify_plan_",
    "adm_lb_spenders",
    "adm_lb_referrals",
    "adm_rate_plan_",
)

_DISABLED_FLOW_PREFIXES = (
    "await_payment",
    "await_topup",
    "await_coupon",
    "await_gift",
    "await_giveplan",
    "await_adm_pay_",
    "await_adm_coupon",
    "await_adm_ref_",
    "await_adm_wallet",
    "await_adm_goal_",
    "await_adm_sub_",
)

_BILLING_TERMS = (
    "payment",
    "coupon",
    "referral",
    "revenue",
    "spender",
    "subscription",
    "wallet",
    "trial",
    "gift",
)


def is_disabled_monetization_callback(callback_data: Any) -> bool:
    """Return whether a callback belongs to a removed billing/plan feature."""
    if not isinstance(callback_data, str):
        return False

    route = callback_data.lower()
    if route in _DISABLED_CALLBACKS:
        return True
    if route.startswith(_DISABLED_CALLBACK_PREFIXES):
        return True

    # Catch legacy admin callbacks that don't follow a consistent prefix.
    if route.startswith("adm_") and (
        "plan" in route or any(term in route for term in _BILLING_TERMS)
    ):
        return True

    # Feature-flag buttons for removed systems must not be re-enabled.
    prefix = "adm_ff_toggle_"
    if route.startswith(prefix):
        return route[len(prefix):] in DISABLED_MONETIZATION_FEATURE_FLAGS
    return False


def is_disabled_monetization_flow(state: Any) -> bool:
    """Return whether a pending text/photo/document flow is billing-related."""
    if not isinstance(state, dict):
        return False

    flow = state.get("flow", "")
    if isinstance(flow, str):
        normalized = flow.lower()
        if normalized.startswith(_DISABLED_FLOW_PREFIXES) or "plan" in normalized:
            return True

    # Generic bot-config prompts can also be used to edit payment settings.
    config_key = state.get("bc_key", "")
    if isinstance(config_key, str):
        normalized_key = config_key.lower()
        if any(term in normalized_key for term in _BILLING_TERMS) or "plan" in normalized_key:
            return True
    return False


def filter_disabled_monetization_buttons(markup: Any) -> Any:
    """Remove legacy billing buttons from an inline keyboard, preserving others."""
    if markup is None:
        return None

    # pyTelegramBotAPI stores this field as ``inline_keyboard``; accept the
    # simpler ``keyboard`` alias too so the policy stays easy to unit-test.
    attribute = "inline_keyboard" if hasattr(markup, "inline_keyboard") else "keyboard"
    rows = getattr(markup, attribute, None)
    if not isinstance(rows, list):
        return markup

    filtered_rows = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        filtered_row = [
            button for button in row
            if not is_disabled_monetization_callback(
                getattr(button, "callback_data", None)
            )
        ]
        if filtered_row:
            filtered_rows.append(filtered_row)

    if filtered_rows == rows:
        return markup

    try:
        setattr(markup, attribute, filtered_rows)
    except Exception:
        return markup
    return markup if filtered_rows else None
