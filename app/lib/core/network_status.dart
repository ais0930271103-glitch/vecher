import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'api.dart';
import 'network_endpoints.dart';
import 'theme.dart';

/// A dedicated row never covers messages or depends on another network asset.
/// It reports reachability, not delivery of a pending message or background push.
class NetworkStatus extends ConsumerStatefulWidget {
  const NetworkStatus({super.key, required this.child});
  final Widget child;

  @override
  ConsumerState<NetworkStatus> createState() => _NetworkStatusState();
}

class _NetworkStatusState extends ConsumerState<NetworkStatus> {
  bool _checking = false;

  Future<void> _retry() async {
    if (_checking) return;
    setState(() => _checking = true);
    try {
      // A safe read may select a configured fallback. Never replay a write here.
      await ref.read(dioProvider).get('/health');
    } catch (_) {
      // The shared client keeps the unavailable state; retry stays accessible.
    } finally {
      if (mounted) setState(() => _checking = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final endpoints = ref.watch(apiEndpointsProvider);
    return ValueListenableBuilder<ApiConnectionState>(
      valueListenable: endpoints.connection,
      builder: (context, connection, _) {
        final unavailable = connection == ApiConnectionState.unavailable;
        return Column(
          children: [
            if (unavailable)
              Material(
                color: VColors.card2,
                child: SafeArea(
                  bottom: false,
                  child: Padding(
                    padding: const EdgeInsets.only(left: 16, right: 8),
                    child: Row(
                      children: [
                        const Icon(
                          Icons.cloud_off_outlined,
                          size: 20,
                          color: VColors.muted,
                        ),
                        const SizedBox(width: 10),
                        const Expanded(
                          child: Text(
                            'Нет связи с сервером',
                            style: TextStyle(fontSize: 13),
                          ),
                        ),
                        TextButton(
                          onPressed: _checking ? null : _retry,
                          child: Text(_checking ? 'Проверяем…' : 'Повторить'),
                        ),
                      ],
                    ),
                  ),
                ),
              )
            else
              const SizedBox.shrink(),
            Expanded(
              child: MediaQuery.removePadding(
                context: context,
                removeTop: unavailable,
                child: widget.child,
              ),
            ),
          ],
        );
      },
    );
  }
}
