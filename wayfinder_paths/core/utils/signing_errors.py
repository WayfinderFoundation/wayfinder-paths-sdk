class SessionExpiredError(RuntimeError):
    """Raised when the backend rejects a signature because the wallet's signing
    session has lapsed. Carries an agent-facing message telling the user how to
    renew, so the agent surfaces that instead of a raw HTTP error.

    Lives in its own import-free module so both the sign-callback path
    (wallets.py) and the sponsored-broadcast path (transaction.py) can raise it
    without the circular import those two modules have with each other."""


# The backend 404s a signature request once the session/policy TTL elapses. The
# agent reads this verbatim, so it's phrased as an instruction to the agent.
SESSION_EXPIRED_MESSAGE = (
    "Your wallet signing session has expired, so the transaction was not "
    "submitted. Do not retry automatically. Send the user a message with this "
    "link — https://wayfinder.ai/app/shells — asking them to open it, sign in, "
    "and renew their trading session, then try again once they confirm it's active."
)
