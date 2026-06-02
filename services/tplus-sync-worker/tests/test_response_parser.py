import unittest

from tplus_datahub.chanjet.response_parser import extract_rows


class ResponseParserTests(unittest.TestCase):
    def test_extracts_rows_from_common_nested_shapes(self):
        cases = [
            {"Result": {"Rows": [{"code": "BOM001"}]}},
            {"Data": {"items": [{"code": "BOM002"}]}},
            {"Value": [{"code": "BOM003"}]},
            {"result": {"data": [{"code": "BOM004"}]}},
        ]

        results = [extract_rows(case) for case in cases]

        self.assertEqual(results, [[{"code": "BOM001"}], [{"code": "BOM002"}], [{"code": "BOM003"}], [{"code": "BOM004"}]])

    def test_returns_empty_list_when_no_rows_exist(self):
        self.assertEqual(extract_rows({"Result": {"Rows": []}}), [])
        self.assertEqual(extract_rows({"message": "ok"}), [])


if __name__ == "__main__":
    unittest.main()
