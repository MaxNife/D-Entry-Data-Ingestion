"""Turn an exception into one line a person can act on."""


def readable(e: BaseException, limit: int = 400) -> str:
    """The driver's own message (not SQLAlchemy's wrapper), on one line.

    Some errors carry no message at all (a bare ``NotImplementedError``); their type name
    is shown instead, so an error is never blank.
    """
    inner = getattr(e, "orig", None) or e
    text = " ".join(str(inner).split())
    return (text or type(inner).__name__)[:limit]
