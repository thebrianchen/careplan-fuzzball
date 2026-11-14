"""The gateway enforces the Fuzzball caller-identity rules and never logs rejected values."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from careplan.cases import load_case
from careplan.data import FIXTURE_RESOURCES, FIXTURE_TRACTS, ResourceTable, TractTable
from careplan.gateway import HEADER, CallerVerifier, Denied, http_gateway, make_server
from careplan.tools import ToolError, Toolbox
from careplan import ROOT

FX_TRACT = "17031999901"
ENDPOINT = "ep-gateway-123"
SVC_PRIVATE, SVC_PUBLIC = "user-svc-private", "user-svc-public"
GOOD_BODY = {"tract_id": FX_TRACT, "need": "food"}


def _jwks(key, kid):
    jwk = ECAlgorithm.to_jwk(key.public_key(), as_dict=True) | {"kid": kid, "alg": "ES256", "use": "sig"}
    return {"keys": [jwk]}


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cluster_key = ec.generate_private_key(ec.SECP256R1())
        cls.attacker_key = ec.generate_private_key(ec.SECP256R1())
        keys = Path(cls.tmp.name) / "signing-keys.json"
        keys.write_text(json.dumps(_jwks(cls.cluster_key, "cluster-1")))
        cls.log = []
        verifier = CallerVerifier(str(keys), [f"endpoint:{ENDPOINT}"], {SVC_PRIVATE}, set())
        cls.srv = make_server(TractTable.load(FIXTURE_TRACTS), ResourceTable.load(FIXTURE_RESOURCES), verifier,
                              cls.log.append, host="127.0.0.1", port=0, per_minute=20)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def assertion(self, sub=SVC_PRIVATE, aud=f"endpoint:{ENDPOINT}", exp_in=120, key=None, kid="cluster-1"):
        claims = {"sub": sub, "aud": aud, "exp": int(time.time()) + exp_in, "account_id": "grp-zone-private",
                  "endpoint_id": ENDPOINT, "iss": "https://attacker.example/keys"}
        return jwt.encode(claims, key or self.cluster_key, algorithm="ES256", headers={"kid": kid})

    def post(self, body=GOOD_BODY, identity=None):
        headers = {HEADER: identity} if identity else {}
        return requests.post(self.url + "/find_resources", json=body, headers=headers, timeout=10)

    def test_valid_private_caller_gets_records_and_is_logged(self):
        r = self.post(identity=self.assertion())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["records"])
        last = self.log[-1]
        self.assertEqual((last["decision"], last["sub"], last["request"]), ("allowed", SVC_PRIVATE, GOOD_BODY))

    def test_identity_attacks_are_refused(self):
        cases = {
            "no header": (None, 401),
            "forged signature": (self.assertion(key=self.attacker_key), 401),
            "forged kid": (self.assertion(key=self.attacker_key, kid="attacker"), 401),
            "endpoint token reused as identity (wrong aud)": (self.assertion(aud="fuzzball"), 401),
            "assertion for another endpoint": (self.assertion(aud="endpoint:ep-card-999"), 401),
            "expired": (self.assertion(exp_in=-5), 401),
            "public-zone caller": (self.assertion(sub=SVC_PUBLIC), 403),
            "not a JWT": ("Bearer abc", 401),
        }
        for name, (identity, status) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.post(identity=identity).status_code, status)
                self.assertEqual(self.log[-1]["decision"], "denied")

    def test_free_text_is_rejected_and_never_logged_or_echoed(self):
        secret = "Raymond Ellis 1968-03-14 MRN-K7Q2XW3D4M"
        for body in ({"tract_id": secret, "need": "food"}, {**GOOD_BODY, "note": secret},
                     {**GOOD_BODY, "start": secret}, {**GOOD_BODY, "days": [secret]}):
            with self.subTest(body=list(body)):
                r = self.post(body=body, identity=self.assertion())
                self.assertEqual(r.status_code, 400)
                self.assertNotIn("Raymond", r.text)
                self.assertNotIn("Raymond", json.dumps(self.log[-1]))
                self.assertNotIn("K7Q2", json.dumps(self.log[-1]))

    def test_rate_limit_per_caller(self):
        verifier = CallerVerifier(str(Path(self.tmp.name) / "signing-keys.json"), [f"endpoint:{ENDPOINT}"], {SVC_PRIVATE}, set())
        srv = make_server(TractTable.load(FIXTURE_TRACTS), ResourceTable.load(FIXTURE_RESOURCES), verifier,
                          lambda e: None, host="127.0.0.1", port=0, per_minute=2)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/find_resources"
        codes = [requests.post(url, json=GOOD_BODY, headers={HEADER: self.assertion()}, timeout=10).status_code
                 for _ in range(3)]
        srv.shutdown()
        srv.server_close()
        self.assertEqual(codes, [200, 200, 429])

    def test_unconfigured_gateway_fails_closed(self):
        with self.assertRaises(Denied) as ctx:
            CallerVerifier("/nonexistent", [], set(), set()).verify(self.assertion())
        self.assertEqual(ctx.exception.status, 503)

    def test_toolbox_reaches_gateway_over_http(self):
        # Box 2's view. In the cluster the endpoint proxy adds the identity header; here the test does.
        case = load_case(ROOT / "cases" / "dev" / "D001-dialysis.yaml")
        tables = TractTable.load(FIXTURE_TRACTS), ResourceTable.load(FIXTURE_RESOURCES)
        ok = Toolbox(case, *tables, gateway=http_gateway(self.url, headers={HEADER: self.assertion()}))
        self.assertTrue(ok.call("find_resources", GOOD_BODY))
        bad = Toolbox(case, *tables, gateway=http_gateway(self.url))
        with self.assertRaises(ToolError):
            bad.call("find_resources", GOOD_BODY)

    def test_batch_runs_cross_through_the_gateway_when_its_url_is_set(self):
        import os
        from unittest import mock
        from careplan.planner import _env_gateway
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CAREPLAN_GATEWAY_URL", None)
            self.assertIsNone(_env_gateway())
        with mock.patch.dict(os.environ, {"CAREPLAN_GATEWAY_URL": self.url}):
            with self.assertRaises(ToolError):          # reaches the service; no identity header here, so it is refused
                _env_gateway()(GOOD_BODY)


if __name__ == "__main__":
    unittest.main()
