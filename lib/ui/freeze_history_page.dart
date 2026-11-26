import 'package:flutter/material.dart';

import 'shell.dart';

/// Every receiver network freeze the bridge has recorded (pi/freezelog.py):
/// when, how long, and what led up to it.
class FreezeHistoryPage extends StatefulWidget {
  final AppState app;
  const FreezeHistoryPage({super.key, required this.app});

  @override
  State<FreezeHistoryPage> createState() => _FreezeHistoryPageState();
}

class _FreezeHistoryPageState extends State<FreezeHistoryPage> {
  List<Map>? freezes;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final r = await widget.app.spot.x('freezes', {'limit': '30'});
    if (!mounted) return;
    setState(() => freezes = ((r?['freezes'] as List?) ?? []).cast<Map>());
  }

  String _dur(num? s) {
    if (s == null) return 'still frozen, or not recorded';
    if (s < 120) return '${s.round()} s';
    if (s < 7200) return '${(s / 60).round()} min';
    return '${(s / 3600).toStringAsFixed(1)} h';
  }

  @override
  Widget build(BuildContext context) {
    final f = freezes;
    return Scaffold(
      appBar: AppBar(title: const Text('Freeze history')),
      body: f == null
          ? const Center(child: CircularProgressIndicator())
          : RefreshIndicator(
              onRefresh: _load,
              child: ListView(padding: const EdgeInsets.all(16), children: [
                const Text(
                  'When the receiver\'s network control stops answering, the bridge records '
                  'what was going on just before. Unplugging the receiver for 30 seconds '
                  'clears a freeze.',
                  style: TextStyle(color: Colors.white70),
                ),
                const SizedBox(height: 12),
                if (f.isEmpty)
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 32),
                    child: Center(child: Text('No freezes recorded yet.')),
                  ),
                for (final r in f)
                  Card(
                    margin: const EdgeInsets.only(bottom: 12),
                    child: ExpansionTile(
                      leading: const Icon(Icons.power_off, color: Colors.redAccent),
                      title: Text('${r['when']}'),
                      subtitle: Text([
                        'Down ${_dur(r['down_s'] as num?)}',
                        if (r['since_receiver_came_back_s'] != null)
                          '${((r['since_receiver_came_back_s'] as num) / 3600).toStringAsFixed(1)} h after it last came back',
                        if (r['playing'] == true) 'while playing',
                      ].join(' · ')),
                      childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
                      expandedCrossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('Likely causes',
                            style: TextStyle(fontWeight: FontWeight.w700)),
                        const SizedBox(height: 4),
                        for (final c in ((r['likely_causes'] as List?) ?? []))
                          Padding(
                            padding: const EdgeInsets.only(bottom: 4),
                            child: Text('• $c'),
                          ),
                        const SizedBox(height: 8),
                        if (r['capture'] is Map && (r['capture']['packets'] ?? 0) > 0) ...[
                          const Text('Network, last 10 minutes',
                              style: TextStyle(fontWeight: FontWeight.w700)),
                          const SizedBox(height: 4),
                          for (final e in ((r['capture']['ssdp_per_min_by_source'] as Map?) ?? {}).entries)
                            Text('${e.key}: ${e.value} announcements/min',
                                style: const TextStyle(fontSize: 13)),
                          if (r['capture']['last_receiver_reply_s_before'] != null)
                            Text('Last reply from the receiver '
                                '${r['capture']['last_receiver_reply_s_before']} s before',
                                style: const TextStyle(fontSize: 13)),
                          const SizedBox(height: 8),
                        ],
                        const Text('Last requests before the freeze',
                            style: TextStyle(fontWeight: FontWeight.w700)),
                        const SizedBox(height: 4),
                        for (final q in ((r['last_requests'] as List?) ?? []).reversed.take(8))
                          Text(
                            '${q['t']} s  ${q['ok'] == true ? 'ok  ' : 'FAIL'}  '
                            '${'${q['what']}'.replaceAll('YamahaRemoteControl:', '').replaceAll('<YAMAHA_AV cmd="', '')}',
                            style: const TextStyle(fontSize: 12, fontFamily: 'monospace'),
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                          ),
                      ],
                    ),
                  ),
              ]),
            ),
    );
  }
}
