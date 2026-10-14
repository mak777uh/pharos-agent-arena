"""Offline tests (no network): action validation and Multicall3 encoding."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from eth_abi import decode as abi_decode  # noqa: E402

import batch  # noqa: E402

ASSETS = os.path.join(os.path.dirname(__file__), "..", "assets")
A = "0x1f3546a29eec2102c8d52e21a45114124014c85c"


def tmp(actions):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"actions": actions}, f)
    f.close()
    return f.name


class Load(unittest.TestCase):
    def test_sample_files(self):
        self.assertEqual(len(batch.load_actions(os.path.join(ASSETS, "keeper.json"))), 5)
        self.assertEqual(len(batch.load_actions(os.path.join(ASSETS, "payouts_with_bad.json"))), 5)

    def test_rejects(self):
        send = {"type": "send_pros", "to": A, "amount": "0.01"}
        cases = [[send, send],                                                   # < 3 actions
                 [dict(send, id="x"), dict(send, id="x"), send],                 # duplicate id
                 [send, send, dict(send, amount="-1")],                          # bad amount
                 [send, send, dict(send, to="0x12")],                            # bad address
                 [send, send, {"type": "morpho_accrue", "market_id": "0x12"}],   # bad market id
                 [send, send, {"type": "nuke"}]]
        for c in cases:
            with self.assertRaises(ValueError, msg=c):
                batch.load_actions(tmp(c))
        with self.assertRaises(ValueError) as e:
            batch.load_actions(os.path.join(ASSETS, "erc20_refused.json"))
        self.assertIn("Multicall3", str(e.exception))


class Encode(unittest.TestCase):
    def test_aggregate3value_roundtrip(self):
        acts = [{"to": A, "value": 10, "data": "0x"}, {"to": A, "value": 5, "data": "0xabcd"}]
        data, total = batch.encode_batch(acts, False)
        self.assertEqual(data[:10], "0x174dea71")
        self.assertEqual(total, 15)
        calls = abi_decode(["(address,bool,uint256,bytes)[]"], bytes.fromhex(data[10:]))[0]
        self.assertEqual([(c[0].lower(), c[1], c[2], c[3]) for c in calls],
                         [(A, False, 10, b""), (A, False, 5, b"\xab\xcd")])


if __name__ == "__main__":
    unittest.main()
