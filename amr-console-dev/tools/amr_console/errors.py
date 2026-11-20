"""Structured, user-facing errors shared by the console modules."""


class ConsoleError(Exception):
    def __init__(self, status, code, reason):
        self.status = int(status)
        self.code = str(code)
        self.reason = str(reason)
        super().__init__(self.reason)

    def as_dict(self):
        return {"error": self.code, "reason": self.reason}
