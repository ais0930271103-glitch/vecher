/// Даты по-русски без лишних пакетов.
const _weekdays = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
const _months = ['янв.', 'февр.', 'марта', 'апр.', 'мая', 'июня', 'июля', 'авг.', 'сент.', 'окт.', 'нояб.', 'дек.'];

String _two(int n) => n.toString().padLeft(2, '0');

/// «сб, 18 окт., 19:00» — в местном времени устройства.
String formatWhen(DateTime dt) {
  final d = dt.toLocal();
  final now = DateTime.now();
  final today = DateTime(now.year, now.month, now.day);
  final day = DateTime(d.year, d.month, d.day);
  final diff = day.difference(today).inDays;
  final time = '${_two(d.hour)}:${_two(d.minute)}';
  if (diff == 0) return 'сегодня, $time';
  if (diff == 1) return 'завтра, $time';
  return '${_weekdays[d.weekday - 1]}, ${d.day} ${_months[d.month - 1]}, $time';
}

/// ISO-строка с часовым поясом устройства: 2026-10-18T19:00:00+03:00.
/// (DateTime.toIso8601String() для местного времени пояс не пишет.)
String isoWithOffset(DateTime local) {
  final off = local.timeZoneOffset;
  final sign = off.isNegative ? '-' : '+';
  final h = _two(off.inHours.abs());
  final m = _two(off.inMinutes.abs() % 60);
  return '${local.year}-${_two(local.month)}-${_two(local.day)}'
      'T${_two(local.hour)}:${_two(local.minute)}:00$sign$h:$m';
}
