class BusinessLogicError(Exception):
    def __init__(self, message: str, code: str = "business_error"):
        self.message = message
        self.code = code


class TenantAccessError(Exception):
    pass
