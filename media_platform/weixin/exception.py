"""WeChat page parsing and public availability failures."""


class ParseError(RuntimeError):
    pass


class ArticleUnavailable(RuntimeError):
    pass
