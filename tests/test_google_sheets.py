import unittest
import re

from bot.services.google_sheets import (
    CatalogIdTarget,
    GoogleSheetsCatalog,
    SheetsError,
)


class Request:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def execute(self, **kwargs):
        if self.error:
            raise self.error
        return self.result or {}


class ValuesAPI:
    def __init__(self, sheets, fail_updates=False):
        self.sheets = sheets
        self.fail_updates = fail_updates
        self.batch_bodies = []
        self.update_calls = []

    def get(self, **kwargs):
        name = kwargs["range"].strip("'").split("'!")[0]
        return Request({"values": self.sheets.get(name, [])})

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        self._apply(kwargs["range"], kwargs["body"]["values"])
        return Request({})

    def append(self, **kwargs):
        name = kwargs["range"].split("'!")[0].strip("'")
        assert kwargs["valueInputOption"] == "RAW"
        self.sheets[name].extend(kwargs["body"]["values"])
        return Request({})

    def batchUpdate(self, **kwargs):
        self.batch_bodies.append(kwargs["body"])
        if not self.fail_updates:
            for item in kwargs["body"].get("data", []):
                self._apply(item["range"], item["values"])
        return Request(error=RuntimeError("offline") if self.fail_updates else None)

    def _apply(self, range_name, values):
        match = re.fullmatch(r"'([^']+)'!(?:(\d+):(\d+)|([A-Z]+)(\d+))", range_name)
        if not match:
            raise AssertionError(f"unsupported fake range: {range_name}")
        sheet_name = match.group(1)
        rows = self.sheets.setdefault(sheet_name, [])
        if match.group(2):
            row_index = int(match.group(2)) - 1
            while len(rows) <= row_index:
                rows.append([])
            rows[row_index] = list(values[0])
            return
        letters, row_text = match.group(4), match.group(5)
        column = 0
        for letter in letters:
            column = column * 26 + ord(letter) - 64
        row_index, column_index = int(row_text) - 1, column - 1
        while len(rows) <= row_index:
            rows.append([])
        while len(rows[row_index]) <= column_index:
            rows[row_index].append("")
        rows[row_index][column_index] = values[0][0]


class Service:
    def __init__(self, values):
        self.values_api = values

    def spreadsheets(self):
        return self

    def values(self):
        return self.values_api


class GoogleSheetsTests(unittest.TestCase):
    def sheet_data(self):
        headers = ["File Name", "Size", "Path", "Category", "Source Type",
                   "File ID", "Telegram Link", "Telegram Status"]
        return {"softwares": [headers, ["Editor", "2 MB", "/x", "Tools", "exe",
                                                "sw_1", "", "unavailable"]],
                "courses": [headers]}

    def test_load_and_update_use_stable_id_and_only_target_cells(self):
        values = ValuesAPI(self.sheet_data())
        catalog = GoogleSheetsCatalog("sheet", service=Service(values))
        entries = catalog.load_entries()
        self.assertEqual("sw_1", entries[0].file_id)
        catalog.update_registration("sw_1", "https://t.me/bot?start=f_token", "available")
        data = values.batch_bodies[-1]["data"]
        self.assertEqual(2, len(data))
        self.assertTrue(all(item["range"].endswith("2") for item in data))
        self.assertNotIn("Path", repr(data))

    def test_google_write_failure_is_wrapped_without_credentials_leak(self):
        values = ValuesAPI(self.sheet_data(), fail_updates=True)
        catalog = GoogleSheetsCatalog("sheet", service=Service(values))
        with self.assertRaises(SheetsError) as caught:
            catalog.update_registration("sw_1", "link", "available")
        self.assertNotIn("offline", str(caught.exception))

    def test_batch_registration_sync_updates_only_exact_target_cells(self):
        headers = ["File Name", "Source Type", "File ID", "Telegram Link",
                   "Telegram Status"]
        data = {
            "softwares": [
                headers,
                ["Chrome", "source-1", "software-chrome", "", ""],
                ["Untouched", "youtube", "", "", ""],
                ["Opera", "source-2", "software-opera", "", ""],
            ],
            "courses": [headers],
        }
        values = ValuesAPI(data)
        catalog = GoogleSheetsCatalog("sheet", service=Service(values))
        result = catalog.update_registrations({
            "software-chrome": ("https://t.me/bot?start=f_chrome_token_value", "available"),
            "software-opera": ("https://t.me/bot?start=f_opera_token_value", "available"),
        })
        self.assertEqual(["software-chrome", "software-opera"],
                         [entry.file_id for entry in result])
        self.assertEqual(["Untouched", "youtube", "", "", ""],
                         data["softwares"][2])
        written_ranges = [item["range"] for item in values.batch_bodies[-1]["data"]]
        self.assertEqual(
            ["'softwares'!D2", "'softwares'!E2",
             "'softwares'!D4", "'softwares'!E4"],
            written_ranges,
        )

    def test_missing_id_initialization_requires_explicit_approval(self):
        catalog = GoogleSheetsCatalog("sheet", service=Service(ValuesAPI(self.sheet_data())))
        with self.assertRaises(ValueError):
            catalog.initialize_missing_file_ids()

    def test_load_normalizes_headers_and_keeps_source_link_rows_without_ids(self):
        headers = [" File Name ", "Size", "Category", "Source Type ",
                   "Details "]
        data = {
            "softwares": [
                headers,
                ["Chrome", "127 MB", "Browsers",
                 "https://t.me/Hyper_Vault/84/133", "browser"],
                ["Course", "1 MB", "Courses", "https://youtu.be/example", "video"],
            ],
            "courses": [headers],
        }
        entries = GoogleSheetsCatalog(
            "sheet", service=Service(ValuesAPI(data))
        ).load_entries()
        self.assertEqual(2, len(entries))
        self.assertIsNone(entries[0].file_id)
        self.assertEqual("https://t.me/Hyper_Vault/84/133",
                         entries[0].source_type)

    def test_existing_whitespace_variant_columns_are_not_duplicated(self):
        headers = ["File Name", " File ID ", "Telegram Link ",
                   " Telegram Status"]
        values = ValuesAPI({"softwares": [headers, ["Editor", "", "", ""]],
                            "courses": [headers]})
        catalog = GoogleSheetsCatalog("sheet", service=Service(values))
        self.assertEqual(1, catalog.initialize_missing_file_ids(approved=True))
        self.assertEqual([], [body for body in values.batch_bodies
                              if any(item.get("range", "").endswith("1:1")
                                     for item in body.get("data", []))])

    def test_single_row_id_initialization_changes_only_target_and_is_idempotent(self):
        data = {
            "softwares": [
                ["File Name", "Size", "Path", "Category", "Source Type", "Details "],
                ["Chrome", "180MB", "Browsers", "browsers",
                 "https://t.me/Hyper_Vault/84/133"],
                ["Opera", "51.9MB", "Browsers", "browsers",
                 "https://t.me/Hyper_Vault/84/134"],
            ],
            "courses": [["File Name", "Source Type "], ["Course", "youtube"]],
        }
        values = ValuesAPI(data)
        catalog = GoogleSheetsCatalog("sheet", service=Service(values))
        entry = catalog.ensure_entry_file_id(
            sheet_name="softwares", row_number=2,
            requested_file_id="software-chrome", expected_title="Chrome",
            expected_source_url="https://t.me/Hyper_Vault/84/133", approved=True,
        )
        self.assertEqual("software-chrome", entry.file_id)
        self.assertEqual("Opera", data["softwares"][2][0])
        self.assertEqual(5, len(data["softwares"][2]))
        calls_after_first = len(values.update_calls)
        repeated = catalog.ensure_entry_file_id(
            sheet_name="softwares", row_number=2,
            requested_file_id="replacement-must-not-win", expected_title="Chrome",
            expected_source_url="https://t.me/Hyper_Vault/84/133", approved=True,
        )
        self.assertEqual("software-chrome", repeated.file_id)
        self.assertEqual(calls_after_first, len(values.update_calls))

    def test_single_row_id_initialization_refuses_identity_drift(self):
        values = ValuesAPI(self.sheet_data())
        catalog = GoogleSheetsCatalog("sheet", service=Service(values))
        with self.assertRaisesRegex(SheetsError, "identity changed"):
            catalog.ensure_entry_file_id(
                sheet_name="softwares", row_number=2,
                requested_file_id="software-chrome", expected_title="Chrome",
                expected_source_url="https://t.me/Hyper_Vault/84/133", approved=True,
            )

    def test_targeted_batch_initializes_only_verified_rows(self):
        headers = ["File Name", "Source Type", "File ID", "Telegram Link",
                   "Telegram Status"]
        data = {
            "softwares": [
                headers,
                ["Chrome", "https://t.me/Hyper_Vault/84/133", "software-chrome"],
                ["Opera", "https://t.me/Hyper_Vault/84/134"],
                ["YouTube only", "https://youtube.com/watch?v=x"],
                ["VLC", "https://t.me/Hyper_Vault/126/132"],
            ],
            "courses": [headers],
        }
        values = ValuesAPI(data)
        catalog = GoogleSheetsCatalog("sheet", service=Service(values))
        entries = catalog.ensure_entries_file_ids(
            sheet_name="softwares",
            targets=(
                CatalogIdTarget(2, "must-not-replace", "Chrome",
                                "https://t.me/Hyper_Vault/84/133"),
                CatalogIdTarget(3, "software-opera", "Opera",
                                "https://t.me/Hyper_Vault/84/134"),
                CatalogIdTarget(5, "software-vlc", "VLC",
                                "https://t.me/Hyper_Vault/126/132"),
            ),
            approved=True,
        )
        self.assertEqual(
            ["software-chrome", "software-opera", "software-vlc"],
            [entry.file_id for entry in entries],
        )
        self.assertEqual(2, len(data["softwares"][3]))
        self.assertEqual(1, len(values.batch_bodies))
        self.assertEqual(2, len(values.batch_bodies[0]["data"]))


if __name__ == "__main__":
    unittest.main()


class CatalogOnlyTests(unittest.TestCase):
    def test_append_preserves_existing_rows_and_retries_by_id(self):
        headers = ["File Name", "Size", "Path", "Category", "Source Type", "Details "]
        data = {"softwares": [headers[:], ["Existing", "", "", "", "", ""]],
                "courses": [headers[:]]}
        catalog = GoogleSheetsCatalog("sheet", service=Service(ValuesAPI(data)))
        args = dict(file_id="sw_test", title="New app", size="1 MB", path="Tools",
                    category="Tools", source_url="https://t.me/Hyper_Vault/10/11",
                    details="=not a formula", pending=True)
        first = catalog.append_catalog_only("softwares", **args)
        second = catalog.append_catalog_only("softwares", **args)
        self.assertEqual(first.file_id, second.file_id)
        self.assertEqual(3, len(data["softwares"]))
        self.assertEqual("Existing", data["softwares"][1][0])
        self.assertEqual("=not a formula", data["softwares"][2][5])
        self.assertEqual("pending_review", first.telegram_status)
        self.assertIsNone(first.telegram_link)
        with self.assertRaises(ValueError):
            catalog.append_catalog_only("softwares", **{**args, "file_id": "sw_other",
                                      "source_url": args["source_url"]+"?single"})

    def test_blank_source_course_is_not_pending(self):
        headers = ["File Name", "Size", "Path", "Category", "Source Type", "Details "]
        catalog = GoogleSheetsCatalog("sheet", service=Service(ValuesAPI(
            {"softwares": [headers[:]], "courses": [headers[:]]})))
        entry = catalog.append_catalog_only("courses", file_id="course_test", title="Course",
                    size="", path="", category="", source_url="", details="", pending=False)
        self.assertIsNone(entry.telegram_link)
        self.assertIsNone(entry.telegram_status)
