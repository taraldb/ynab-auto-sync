from __future__ import annotations

import re

# Ported from the sibling project ../ynab-auto-bank's
# app/description_processing.py::clean_bank_description, which cleans raw
# bank description/text fields before they become a YNAB payee/memo. Kept
# to the rules confirmed relevant to real data this project has actually
# seen (SpareBank1's raw `description`/`text` field, and the Norwegian Bank
# xlsx export's `Text` column - both confirmed live to contain
# merchant-processor-prefixed strings like "Zettle_*Micro Kaffi AS,
# Stavanger", "PAYPAL *JAGEX LTD", and "Vipps*Odeon kino Stavange").
#
# Deliberately NOT ported: ynab-auto-bank's "strip standalone CRV marker"
# rule - that's specific to an EU payment-scheme code its aggregator
# (Enable Banking) surfaces, never observed in any SpareBank1 payload here.
# Porting an unconfirmed pattern risks stripping a legitimate substring from
# a real merchant name for no benefit - see CLAUDE.md's standing rule on
# live-verifying assumptions before trusting them.
_LEADING_EQUALS = re.compile(r"^=+")
_STAR_SEPARATOR = re.compile(r"\*")
_PARENTHESIZED_ACCOUNT_NUMBER = re.compile(r"\(\s*\d{4}(?:[.\s]?\d{2})(?:[.\s]?\d{5})\s*\)")
_WHITESPACE = re.compile(r"\s+")


def clean_bank_text(text: str | None) -> str | None:
    """Clean a raw bank payee/description/memo string for display: strips a
    leading run of '=' characters (an occasional bank-export artifact),
    replaces '*' with a space (banks commonly use it as a word separator
    between a payment processor's name and the actual merchant, e.g.
    "Zettle_*Micro Kaffi AS" -> "Zettle_ Micro Kaffi AS"), removes an
    account number written in parentheses, and collapses repeated
    whitespace. Returns None for empty/whitespace-only input or output,
    matching this project's existing convention for "no memo" (see
    transform.py's memo derivation).
    """
    if not text:
        return None
    result = text.strip()
    result = _LEADING_EQUALS.sub("", result)
    result = _STAR_SEPARATOR.sub(" ", result)
    result = _PARENTHESIZED_ACCOUNT_NUMBER.sub(" ", result)
    result = _WHITESPACE.sub(" ", result).strip()
    return result or None
