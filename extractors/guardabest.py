from extractors.f16px import F16PxExtractor


class GuardabestExtractor(F16PxExtractor):
    """Guardabest/Byse embed extractor."""

    ERROR_PREFIX = "GUARDABEST"

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, bypass_warp=bypass_warp)
        self.extractor_name = "guardabest"
