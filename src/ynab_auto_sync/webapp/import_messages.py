from __future__ import annotations

import random

# A deliberate, explicit product decision (not a joke to skip): rejecting a
# file import that can't resolve to a real target gets a message with some
# personality, not a dry generic 400. Two distinct failure modes get two
# distinct message pools.

_UNRECOGNIZED_FORMAT_MESSAGES = (
    (
        "I don't recognize this file's shape at all - not the Norwegian Bank "
        "export I know, and not anything else registered."
    ),
    (
        "This isn't a format I speak. I'm fluent in exactly one bank's file "
        "dialect right now, and this wasn't it."
    ),
)

_NO_ACCOUNT_RESOLVED_MESSAGES = (
    (
        "I can read this file just fine - I have absolutely no idea which "
        "account it's supposed to land in, though. Pick one, or fix the "
        "config so I stop having to guess."
    ),
    (
        "Norwegian Bank format: recognized. Destination account: a complete "
        "mystery. I don't do psychic imports."
    ),
    (
        "This file parses beautifully. Sadly 'somewhere, probably' isn't a "
        "valid YNAB account ID."
    ),
    (
        "No account in config.yaml claims this file's format, and you didn't "
        "pick one either - I'd rather ask than throw these transactions at a "
        "random budget and hope for the best."
    ),
)

# Unlike the live-poll/scheduled path, a file import is a one-shot,
# user-initiated action with no backoff/retry loop behind it - if YNAB
# itself is unreachable or erroring, the only sane thing to do is tell the
# user plainly and let them retry the upload later, not keep the upload
# request hanging in a background retry.
_YNAB_UNAVAILABLE_MESSAGES = (
    (
        "YNAB itself isn't cooperating right now - this isn't your file's "
        "fault. Try the import again in a bit."
    ),
    (
        "I got your transactions ready to go, but YNAB's API just threw "
        "them back at me. Nothing was imported - try again later."
    ),
    (
        "YNAB is having a moment. Your file is fine, I just can't hand it "
        "off right now - please retry the import shortly."
    ),
)


def unrecognized_format() -> str:
    return random.choice(_UNRECOGNIZED_FORMAT_MESSAGES)


def no_account_resolved() -> str:
    return random.choice(_NO_ACCOUNT_RESOLVED_MESSAGES)


def ynab_unavailable() -> str:
    return random.choice(_YNAB_UNAVAILABLE_MESSAGES)
