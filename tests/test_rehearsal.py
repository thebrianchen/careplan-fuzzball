"""The rehearsal endpoint proxy mints assertions the unmodified gateway accepts, and only those."""
import sys
import tempfile
import unittest
from pathlib import Path

from careplan import ROOT
from careplan.gateway import CallerVerifier, Denied

sys.path.insert(0, str(ROOT / "rehearsal"))
from identity_proxy import Proxy  # noqa: E402


class RehearsalProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jwks = Path(self.tmp.name) / "signing-keys.json"
        callers = {"t-priv": {"sub": "svc-private", "account_id": "zone-private", "routes": ["gateway"]},
                   "t-pub": {"sub": "svc-public", "account_id": "zone-public", "routes": ["gateway"]}}
        routes = {"gateway": {"upstream": "http://gateway:8090", "endpoint_id": "ep-gw"},
                  "card": {"upstream": "http://planner:8080", "endpoint_id": "ep-card"}}
        self.p = Proxy(callers, routes, self.jwks)
        self.v = CallerVerifier(str(self.jwks), ["endpoint:ep-gw"], {"svc-private"}, set())

    def tearDown(self):
        self.tmp.cleanup()

    def test_gateway_accepts_private_rejects_public_and_wrong_endpoint(self):
        self.assertEqual(self.v.verify(self.p.assertion(self.p.callers["t-priv"], "ep-gw"))["sub"], "svc-private")
        with self.assertRaises(Denied) as d:
            self.v.verify(self.p.assertion(self.p.callers["t-pub"], "ep-gw"))
        self.assertEqual(d.exception.status, 403)
        with self.assertRaises(Denied) as d:
            self.v.verify(self.p.assertion(self.p.callers["t-priv"], "ep-card"))   # minted for another endpoint
        self.assertEqual(d.exception.status, 401)

    def test_routes(self):
        self.assertEqual(self.p.route_for("/gateway/find_resources"), ("gateway", "/find_resources"))
        self.assertEqual(self.p.route_for("/card/D001"), ("card", "/card/D001"))
        self.assertIsNone(self.p.route_for("/admin"))


if __name__ == "__main__":
    unittest.main()
