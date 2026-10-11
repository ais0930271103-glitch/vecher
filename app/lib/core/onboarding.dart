/// Куда вести после входа: согласие → имя → город → «Сегодня».
/// Принимает ответ /auth/me (или /auth/verify — там флаги называются has_*).
String nextOnboardingStep(Map me) {
  final consent = me['consent_ok'] ?? me['has_consent'];
  if (consent == false) return '/consent';
  final hasName = me.containsKey('has_name') ? me['has_name'] == true : me['display_name'] != null;
  if (!hasName) return '/name';
  if (me['has_city'] == false) return '/city';
  return '/today';
}
