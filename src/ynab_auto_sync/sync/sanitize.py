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

# SpareBank1 confirmed live to append a trailing ", <COUNTRY>" (e.g. ", NOR")
# once a transaction ages from PENDING into BOOKED, where the PENDING
# observation had no country suffix at all - and the location token right
# before it can also change entirely (confirmed live: "...LG RD, NOR" ->
# "...AALGAARD, NOR" for the identical real purchase). Only the trailing
# country code is stripped here; the location token itself is deliberately
# left in place - see normalize_payee_for_fuzzy_match's docstring for why.
# Used only by the PENDING<->BOOKED fuzzy-matching helpers below, never by
# clean_bank_text.
_TRAILING_COUNTRY_SUFFIX = re.compile(r",\s*[A-Za-z]{2,4}\s*$")


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


def normalize_payee_for_fuzzy_match(payee: str) -> str:
    """Casefold + strip a trailing ', <location/country>' suffix (see
    _TRAILING_COUNTRY_SUFFIX above) - and nothing more. Not a general-
    purpose cleaning step - clean_bank_text already handles that; this is
    specifically for comparing two observations of what might be the same
    real-world transaction, used only by payees_plausibly_match/
    payee_similarity and sync/pending_match.py.

    Deliberately does NOT also truncate to the segment before the first
    comma, even though a merchant-name prefix is the part confirmed stable
    across the PENDING->BOOKED drift: doing so would throw away the
    location/branch text that follows, which is exactly what
    sync/pending_match.py's Levenshtein-based disambiguation step needs
    to rank two otherwise-plausible candidates (e.g. two branches of the
    same chain) against each other. payees_plausibly_match's prefix/
    shared-prefix check below already tolerates the drift case (a shared
    leading substring is enough), so nothing is lost by keeping the tail.
    """
    return _TRAILING_COUNTRY_SUFFIX.sub("", payee.strip().casefold())


def payees_plausibly_match(a: str, b: str, *, min_prefix_len: int = 6) -> bool:
    """True if two payee strings plausibly name the same merchant, after
    normalize_payee_for_fuzzy_match: exact match, one is a prefix of the
    other, or they share a common prefix of at least min_prefix_len
    characters. Deliberately loose - a caller must combine this with
    account+amount+date, never rely on it alone: two different same-day
    purchases at the same merchant chain can produce byte-identical
    normalized payees (confirmed live against real SpareBank1 data - two
    separate grocery-store visits, same day, same cleaned description,
    different amounts).
    """
    norm_a = normalize_payee_for_fuzzy_match(a)
    norm_b = normalize_payee_for_fuzzy_match(b)
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b or norm_a.startswith(norm_b) or norm_b.startswith(norm_a):
        return True
    prefix_len = min(len(norm_a), len(norm_b), min_prefix_len)
    return prefix_len >= min_prefix_len and norm_a[:prefix_len] == norm_b[:prefix_len]


def levenshtein_distance(a: str, b: str) -> int:
    """Classic edit-distance dynamic program (insert/delete/substitute, each
    cost 1), O(len(a)*len(b)) time, O(min(len(a),len(b))) space via
    row-swapping. No new dependency - deliberately not pulling in a library
    (e.g. python-Levenshtein/rapidfuzz) for something this small and
    self-contained.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) > len(b):
        a, b = b, a
    previous = list(range(len(a) + 1))
    for i, cb in enumerate(b, start=1):
        current = [i] + [0] * len(a)
        for j, ca in enumerate(a, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(
                previous[j] + 1,  # deletion
                current[j - 1] + 1,  # insertion
                previous[j - 1] + cost,  # substitution
            )
        previous = current
    return previous[-1]


def payee_similarity(a: str, b: str) -> float:
    """Normalized 0..1 similarity between two ALREADY-normalized payee
    strings (see normalize_payee_for_fuzzy_match): 1.0 = identical,
    0.0 = completely different, via
    1 - levenshtein_distance(a, b) / max(len(a), len(b), 1).

    Used only as a tie-breaker in sync/pending_match.py when amount + date
    + a coarse payees_plausibly_match check still leave more than one
    plausible candidate - never a substitute for the amount/date/account
    filters. A caller must still require both a minimum score AND a
    minimum margin over the runner-up before trusting the top result
    (see find_pending_match's disambiguation step) rather than blindly
    taking the argmax, which would silently turn a genuine ambiguity into
    a guess.
    """
    if not a and not b:
        return 1.0
    distance = levenshtein_distance(a, b)
    return 1.0 - distance / max(len(a), len(b), 1)
