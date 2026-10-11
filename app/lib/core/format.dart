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

/// Русское множественное число: plural(5, 'день', 'дня', 'дней') → «дней».
String plural(int n, String one, String few, String many) {
  final m10 = n.abs() % 10, m100 = n.abs() % 100;
  if (m10 == 1 && m100 != 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

/// «20 окт.» / «20 окт. 2027» (год — если не текущий).
String formatDay(DateTime d) {
  final now = DateTime.now();
  final base = '${d.day} ${_months[d.month - 1]}';
  return d.year == now.year ? base : '$base ${d.year}';
}

/// «через 8 дней», «сегодня», «завтра».
String daysLeftText(int days) {
  if (days == 0) return 'сегодня';
  if (days == 1) return 'завтра';
  if (days < 0) return 'прошло';
  return 'через $days ${plural(days, 'день', 'дня', 'дней')}';
}

/// Дата без времени для API: 2026-10-20.
String isoDate(DateTime d) => '${d.year}-${_two(d.month)}-${_two(d.day)}';
