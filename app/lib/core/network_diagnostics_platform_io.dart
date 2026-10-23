import 'dart:io';

bool isTlsFailure(Object? error) => error is TlsException;
bool isSocketFailure(Object? error) => error is SocketException;
