from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from applyd.apply.browser import brightdata_cdp_url


class BrowserConfigTests(unittest.TestCase):
    def test_direct_brightdata_cdp_url_takes_precedence(self) -> None:
        direct = "wss://example:secret@brd.superproxy.io:9222"
        with patch.dict(os.environ, {"BRIGHTDATA_CDP_URL": direct}, clear=True):
            self.assertEqual(brightdata_cdp_url(), direct)

    def test_brightdata_cdp_url_can_be_built_from_zone_credentials(self) -> None:
        with patch.dict(os.environ, {
            "BRIGHTDATA_CUSTOMER_ID": "customer",
            "BRIGHTDATA_ZONE": "applyd",
            "BRIGHTDATA_ZONE_PASSWORD": "secret",
            "BRIGHTDATA_COUNTRY": "ca",
        }, clear=True):
            self.assertEqual(
                brightdata_cdp_url(),
                "wss://brd-customer-customer-zone-applyd-country-ca:secret@"
                "brd.superproxy.io:9222",
            )


if __name__ == "__main__":
    unittest.main()
