import logging
import unittest
from pathlib import Path

from web.app import _SuccessfulLogPollFilter


class SuccessfulLogPollFilterTests(unittest.TestCase):
    def setUp(self):
        self.log_filter = _SuccessfulLogPollFilter()

    @staticmethod
    def make_record(message, logger_name='werkzeug'):
        return logging.LogRecord(logger_name, logging.INFO, __file__, 1, message, (), None)

    def test_suppresses_successful_log_viewer_polls_only(self):
        poll = self.make_record(
            '172.19.0.12 - - [17/Oct/2026 10:44:46] "GET /api/logs?lines=200 HTTP/1.1" 200 -'
        )
        normal_request = self.make_record(
            '172.19.0.12 - - [17/Oct/2026 10:44:46] "GET /most-watched HTTP/1.1" 200 -'
        )
        failed_poll = self.make_record(
            '172.19.0.12 - - [17/Oct/2026 10:44:46] "GET /api/logs?lines=200 HTTP/1.1" 500 -'
        )
        application_log = self.make_record(
            'GET /api/logs?lines=200 returned an error', logger_name='WebUI'
        )

        self.assertFalse(self.log_filter.filter(poll))
        self.assertTrue(self.log_filter.filter(normal_request))
        self.assertTrue(self.log_filter.filter(failed_poll))
        self.assertTrue(self.log_filter.filter(application_log))

    def test_logs_page_refresh_interval_is_ten_seconds(self):
        template = Path(__file__).parent.parent / 'web' / 'templates' / 'logs.html'
        self.assertIn('setInterval(refreshLogs,10000)', template.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
