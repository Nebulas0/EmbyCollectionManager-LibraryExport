import re
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional


SEASONAL_PRESETS = {
    'halloween': ('10-01', '11-01'),
    'christmas': ('12-01', '01-02'),
    'valentine': ('02-01', '02-15'),
    'thanksgiving': ('11-01', '12-01'),
    'easter': ('03-15', '04-30'),
    'new year': ('12-26', '01-05'),
    'summer': ('06-01', '09-01'),
}


def seasonal_defaults_for_recipe(recipe_name: str) -> Optional[Dict[str, str]]:
    name = str(recipe_name or '').casefold()
    for key, (start, end) in SEASONAL_PRESETS.items():
        if key in name:
            return {'start': start, 'end': end}
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return bool(value)


def _valid_month_day(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r'\d{2}-\d{2}', value):
        return False
    try:
        datetime.strptime(f'2000-{value}', '%Y-%m-%d')
        return True
    except ValueError:
        return False


def normalize_seasonal_rule(recipe_name: str, value: Any) -> Dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    defaults = seasonal_defaults_for_recipe(recipe_name) or {}
    start = str(value.get('start') or defaults.get('start') or '').strip()
    end = str(value.get('end') or defaults.get('end') or '').strip()
    enabled = _as_bool(value.get('enabled', False))
    if enabled and (not _valid_month_day(start) or not _valid_month_day(end)):
        raise ValueError('Season dates must use valid MM-DD values, for example 10-01.')
    if start and not _valid_month_day(start):
        raise ValueError('Season start must be a valid MM-DD date.')
    if end and not _valid_month_day(end):
        raise ValueError('Season end must be a valid MM-DD date.')
    return {
        'enabled': enabled,
        'start': start or None,
        'end': end or None,
    }


def is_in_season(rule: Dict[str, Any], today: Optional[date] = None) -> bool:
    if not rule.get('enabled'):
        return True
    start, end = rule.get('start'), rule.get('end')
    if not _valid_month_day(start) or not _valid_month_day(end):
        return False
    today = today or datetime.now().date()
    current = (today.month, today.day)
    start_parts = tuple(int(part) for part in start.split('-'))
    end_parts = tuple(int(part) for part in end.split('-'))
    if start_parts <= end_parts:
        return start_parts <= current <= end_parts
    return current >= start_parts or current <= end_parts


def seasonal_sort_name(collection_name: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    inverted_timestamp = 9_999_999_999 - int(now.timestamp())
    return f'{inverted_timestamp:010d} {collection_name}'
