"""abu-decomp-64: the decompilation dev cycle and its seat."""

__version__ = "0.1.0"


class DecompError(Exception):
    """A named refusal that the public command reports without guessing a cause."""

    def __init__(self, key: str, detail: str) -> None:
        super().__init__(detail)
        self.key = key
        self.detail = detail

    def finding(self) -> dict[str, object]:
        return {"key": self.key, "message": self.detail, "blocking": True}
