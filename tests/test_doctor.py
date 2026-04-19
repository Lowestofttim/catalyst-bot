"""Tests for doctor.py — preflight/readiness checks."""

import unittest
from unittest.mock import patch, MagicMock

from doctor import (
    DoctorCheck, DoctorReport, _check_db_health, _check_config_sanity, _check_cat_config,
)


class TestDoctorReport(unittest.TestCase):

    def test_can_start_with_no_failures(self):
        report = DoctorReport(checks=[
            DoctorCheck("a", "test", "pass", "ok", "info"),
            DoctorCheck("b", "test", "warn", "risky", "warning"),
        ])
        self.assertTrue(report.can_start)

    def test_cannot_start_with_failure(self):
        report = DoctorReport(checks=[
            DoctorCheck("a", "test", "pass", "ok", "info"),
            DoctorCheck("b", "test", "fail", "broken", "error"),
        ])
        self.assertFalse(report.can_start)

    def test_summary_blocked(self):
        report = DoctorReport(checks=[
            DoctorCheck("a", "test", "fail", "bad", "error"),
        ])
        self.assertIn("BLOCKED", report.summary)

    def test_summary_ok_with_warnings(self):
        report = DoctorReport(checks=[
            DoctorCheck("a", "test", "pass", "ok", "info"),
            DoctorCheck("b", "test", "warn", "risky", "warning"),
        ])
        self.assertIn("warning", report.summary)

    def test_summary_all_pass(self):
        report = DoctorReport(checks=[
            DoctorCheck("a", "test", "pass", "ok", "info"),
        ])
        self.assertIn("passed", report.summary)

    def test_to_dict(self):
        report = DoctorReport(checks=[
            DoctorCheck("test", "cat", "pass", "msg", "info"),
        ])
        d = report.to_dict()
        self.assertIn("can_start", d)
        self.assertIn("checks", d)
        self.assertEqual(len(d["checks"]), 1)
        self.assertEqual(d["checks"][0]["name"], "test")


class TestDoctorCheck_DB(unittest.TestCase):

    @patch("database.get_connection")
    def test_db_health_pass(self, mock_conn_fn):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {"name": "offers"}, {"name": "fills"},
            {"name": "events"}, {"name": "coins"},
        ]
        mock_conn_fn.return_value = mock_conn
        check = _check_db_health()
        self.assertEqual(check.status, "pass")

    @patch("database.get_connection")
    def test_db_health_missing_tables(self, mock_conn_fn):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {"name": "offers"},
        ]
        mock_conn_fn.return_value = mock_conn
        check = _check_db_health()
        self.assertEqual(check.status, "fail")

    @patch("database.get_connection", side_effect=Exception("DB locked"))
    def test_db_health_error(self, mock_conn_fn):
        check = _check_db_health()
        self.assertEqual(check.status, "fail")


class TestDoctorCheck_Config(unittest.TestCase):

    @patch("config_validator.validate_config")
    def test_config_pass(self, mock_validate):
        from config_validator import ValidationReport
        mock_validate.return_value = ValidationReport()
        check = _check_config_sanity()
        self.assertEqual(check.status, "pass")

    @patch("config_validator.validate_config")
    def test_config_with_errors(self, mock_validate):
        from config_validator import ValidationReport, ConfigIssue
        report = ValidationReport(errors=[
            ConfigIssue("X", "bad", "error"),
        ])
        mock_validate.return_value = report
        check = _check_config_sanity()
        self.assertEqual(check.status, "fail")


class TestDoctorCheck_CAT(unittest.TestCase):

    def test_cat_configured(self):
        with patch("config.cfg") as mock_cfg:
            mock_cfg.CAT_ASSET_ID = "abc123"
            mock_cfg.CAT_NAME = "TEST"
            mock_cfg.CAT_DECIMALS = 3
            check = _check_cat_config()
            self.assertEqual(check.status, "pass")

    def test_cat_missing(self):
        with patch("config.cfg") as mock_cfg:
            mock_cfg.CAT_ASSET_ID = ""
            check = _check_cat_config()
            self.assertEqual(check.status, "fail")


if __name__ == "__main__":
    unittest.main()
