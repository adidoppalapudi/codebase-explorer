def charge_card(amount_cents: int, token: str) -> str:
    """Charge a stored card and return the payment ID."""
    if amount_cents <= 0:
        raise ValueError("amount must be positive")
    return f"pay_{token}_{amount_cents}"


def refund(payment_id: str) -> None:
    """Refund a payment in full."""
    print(f"refunding {payment_id}")
