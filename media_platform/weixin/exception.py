"""WeChat page parsing and public availability failures."""


class ParseError(RuntimeError):
    pass


class ArticleUnavailable(RuntimeError):
    pass


class ArticleSkipped(RuntimeError):
    """A user-authorized page category that must not be followed."""
