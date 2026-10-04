import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import sync


class SyncTests(unittest.TestCase):
    def extract_fixture(self, xml):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "20261004T180828.780868Z"
            output.mkdir()
            (root / "performance.xml").write_text(xml)
            (output / "run.json").write_text(json.dumps({"target_url": sync.TARGET_URL}))
            (output / "network.json").write_text(json.dumps([{
                "url": "https://teatrodiroma.vivaticket.it/wmsbackend.php?cmd=getMapImage&perfid=14459254&roomid=tl016248&tickSystem=undefined",
                "status": 200, "xml_file": "performance.xml",
                "received_at": "2026-10-04T18:08:28+00:00",
            }]))
            with patch("sync.ROOT", root):
                return sync.extract_snapshot(output)

    fixture = '''<reply><performance id="14322117" title="Example" roomName="TEATRO ARGENTINA"
        rcode="tl016248" timeStart="202702182000"><reductions>
        <reduction id="13190" description="INTERO"/>
        <reduction id="14910" description="RIDUZIONE OVER 65"/>
        <reduction id="SUBSTICKET" description="Bigl. Abb."/>
        </reductions><zones><zone id="330534" code="PT" name="Platea" avail="63">
        <price price="4400" presale="0" commission="453"/>
        <price price="3500" presale="0" commission="361"/>
        <price price="0" presale="0" commission="0"/>
        </zone></zones></performance></reply>'''

    def test_prices_and_identifiers(self):
        snapshot = self.extract_fixture(self.fixture)
        self.assertEqual(snapshot["request_performance_id"], "14459254")
        self.assertEqual(snapshot["performance_id"], "14322117")
        self.assertEqual(snapshot["starts_at"], "2027-02-18T20:00:00+01:00")
        self.assertEqual(snapshot["available_total"], 63)
        platea = snapshot["zones"][0]
        self.assertEqual(platea["tariffs"][0]["total_cents"], 4853)
        self.assertEqual(platea["tariffs"][1]["label"], "RIDUZIONE OVER 65")
        self.assertTrue(platea["tariffs"][2]["is_subscription"])

    def test_unknown_price_mapping_is_rejected(self):
        xml = self.fixture.replace('<price price="0" presale="0" commission="0"/>', '')
        with self.assertRaisesRegex(ValueError, "associate prices"):
            self.extract_fixture(xml)

    def test_wrong_room_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "different room"):
            self.extract_fixture(self.fixture.replace('rcode="tl016248"', 'rcode="different"'))

    def test_no_api_does_not_become_zero_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "run.json").write_text(json.dumps({"target_url": sync.TARGET_URL}))
            (output / "network.json").write_text("[]")
            with self.assertRaisesRegex(ValueError, "No fresh performance XML"):
                sync.extract_snapshot(output)

    def test_secret_key_headers_and_retry_id(self):
        with patch("sync.urlopen") as open_url:
            open_url.return_value.__enter__.return_value.status = 201
            snapshot = {"id": "same-id"}
            sync.upload_snapshot(snapshot, "https://example.supabase.co", "sb_secret_test")
            request = open_url.call_args.args[0]
            self.assertEqual(request.get_header("Apikey"), "sb_secret_test")
            self.assertIsNone(request.get_header("Authorization"))
            self.assertEqual(json.loads(request.data), snapshot)
            self.assertIn("on_conflict=id", request.full_url)
            self.assertIn("merge-duplicates", request.get_header("Prefer"))

    def test_legacy_key_authorization(self):
        with patch("sync.urlopen") as open_url:
            open_url.return_value.__enter__.return_value.status = 201
            sync.upload_snapshot({"id": "same-id"}, "https://example.supabase.co", "eyJ-test")
            self.assertEqual(open_url.call_args.args[0].get_header("Authorization"), "Bearer eyJ-test")

    def test_failed_upload_is_reported_without_secret(self):
        error = HTTPError("https://example.supabase.co", 403, "Forbidden", {}, io.BytesIO(b"secret"))
        with patch("sync.urlopen", side_effect=error):
            with self.assertRaisesRegex(ValueError, "Supabase HTTP 403") as caught:
                sync.upload_snapshot({"id": "same-id"}, "https://example.supabase.co", "sb_secret_test")
            self.assertNotIn("sb_secret_test", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
