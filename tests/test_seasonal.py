import unittest
from datetime import date, datetime, timezone

from src.seasonal import (
    is_in_season,
    normalize_seasonal_rule,
    seasonal_defaults_for_recipe,
    seasonal_sort_name,
)


class SeasonalRuleTests(unittest.TestCase):
    def test_holiday_presets_cover_existing_seasonal_recipes(self):
        self.assertEqual(seasonal_defaults_for_recipe('Halloween Collection'), {
            'start': '10-01',
            'end': '11-01',
        })
        self.assertEqual(seasonal_defaults_for_recipe("Valentine's Day"), {
            'start': '02-01',
            'end': '02-15',
        })
        self.assertEqual(seasonal_defaults_for_recipe('Christmas'), {
            'start': '12-01',
            'end': '01-02',
        })
        self.assertEqual(seasonal_defaults_for_recipe('Thanksgiving'), {
            'start': '11-01',
            'end': '12-01',
        })
        disabled = normalize_seasonal_rule('Halloween', None)
        self.assertFalse(disabled['enabled'])
        self.assertEqual(disabled['start'], '10-01')

    def test_windows_are_annual_inclusive_and_can_cross_new_year(self):
        halloween = normalize_seasonal_rule('Halloween', {
            'enabled': True,
            'start': '10-01',
            'end': '11-01',
        })
        christmas = normalize_seasonal_rule('Christmas', {'enabled': True})

        self.assertTrue(is_in_season(halloween, date(2026, 10, 31)))
        self.assertFalse(is_in_season(halloween, date(2026, 11, 2)))
        self.assertTrue(is_in_season(christmas, date(2026, 12, 24)))
        self.assertTrue(is_in_season(christmas, date(2027, 1, 1)))
        self.assertFalse(is_in_season(christmas, date(2027, 1, 3)))

    def test_invalid_custom_dates_are_rejected_when_enabled(self):
        with self.assertRaises(ValueError):
            normalize_seasonal_rule('My Seasonal List', {
                'enabled': True,
                'start': '13-40',
                'end': '11-01',
            })

    def test_newer_seasonal_sort_key_sorts_before_older_one(self):
        older = seasonal_sort_name('Halloween', datetime(2026, 10, 1, tzinfo=timezone.utc))
        newer = seasonal_sort_name('Christmas', datetime(2026, 12, 1, tzinfo=timezone.utc))
        self.assertLess(newer, older)


if __name__ == '__main__':
    unittest.main()
