import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

/// Client for the spotcast bridge running on the home Pi (port 8090).
class SpotcastApi {
  static const _candidates = ['192.168.1.110', '192.168.1.115'];
  String? host;

  Future<String?> locate() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString('spotcastHost');
    for (final h in [if (saved != null) saved, ..._candidates]) {
      try {
        final r = await http
            .get(Uri.parse('http://$h:8090/api/status'))
            .timeout(const Duration(seconds: 2));
        if (r.statusCode == 200) {
          host = h;
          await prefs.setString('spotcastHost', h);
          return h;
        }
      } catch (_) {}
    }
    return host = null;
  }

  Future<void> setHost(String h) async {
    host = h.trim();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('spotcastHost', host!);
  }

  Future<Map<String, dynamic>> status() async {
    final r = await http
        .get(Uri.parse('http://$host:8090/api/status'))
        .timeout(const Duration(seconds: 4));
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  Future<void> call(String path, [Map<String, String>? q]) async {
    await http
        .get(Uri.http('$host:8090', '/api/$path', q))
        .timeout(const Duration(seconds: 6));
  }
}

class SpotifyTab extends StatefulWidget {
  const SpotifyTab({super.key});

  @override
  State<SpotifyTab> createState() => _SpotifyTabState();
}

class _SpotifyTabState extends State<SpotifyTab>
    with AutomaticKeepAliveClientMixin {
  final api = SpotcastApi();
  Map<String, dynamic>? st;
  String? error;
  Timer? _timer;
  bool _busy = false;

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
    _start();
  }

  Future<void> _start() async {
    final h = await api.locate();
    if (!mounted) return;
    if (h == null) {
      setState(() => error = 'Speaker bridge not found on this network.');
      return;
    }
    await _refresh();
    _timer = Timer.periodic(const Duration(seconds: 3), (_) => _refresh());
  }

  Future<void> _refresh() async {
    try {
      final s = await api.status();
      if (mounted) setState(() {
        st = s;
        error = null;
      });
    } catch (e) {
      if (mounted) setState(() => error = 'Lost connection to the speaker bridge.');
    }
  }

  Future<void> _do(String path, [Map<String, String>? q]) async {
    setState(() => _busy = true);
    try {
      await api.call(path, q);
      await Future.delayed(const Duration(milliseconds: 400));
      await _refresh();
    } catch (_) {
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _editHost() async {
    final ctrl = TextEditingController(text: api.host ?? '192.168.1.110');
    final h = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Speaker bridge address'),
        content: TextField(controller: ctrl, autofocus: true),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(ctx, ctrl.text), child: const Text('Save')),
        ],
      ),
    );
    if (h != null && h.trim().isNotEmpty) {
      await api.setHost(h);
      _timer?.cancel();
      setState(() => error = null);
      await _refresh();
      _timer = Timer.periodic(const Duration(seconds: 3), (_) => _refresh());
    }
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    final theme = Theme.of(context);
    if (st == null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            if (error == null) const CircularProgressIndicator() else ...[
              const Icon(Icons.cloud_off, size: 48),
              const SizedBox(height: 12),
              Text(error!, textAlign: TextAlign.center),
              const SizedBox(height: 16),
              FilledButton(onPressed: _start, child: const Text('Try again')),
              TextButton(onPressed: _editHost, child: const Text('Set address')),
            ],
          ]),
        ),
      );
    }
    final s = st!;
    final rx = (s['receiver'] ?? {}) as Map;
    final np = s['now_playing'] as Map?;
    final playing = s['playing'] == true;
    final lat = (s['latency'] ?? {}) as Map;
    final health = (s['health'] ?? {}) as Map;
    final devices = (s['devices'] ?? {}) as Map;
    final unknown = (devices['unknown'] ?? {}) as Map;
    final addLeft = (s['add_me_visible_s'] ?? 0) as int;

    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (error != null)
            Card(
              color: theme.colorScheme.errorContainer,
              child: ListTile(leading: const Icon(Icons.warning), title: Text(error!)),
            ),
          // ---- now playing
          Card(
            clipBehavior: Clip.antiAlias,
            child: Column(children: [
              if (np != null && np['art'] != null)
                AspectRatio(
                  aspectRatio: 1,
                  child: Image.network(np['art'], fit: BoxFit.cover,
                      errorBuilder: (_, __, ___) => const SizedBox()),
                ),
              ListTile(
                leading: Icon(playing ? Icons.graphic_eq : Icons.pause_circle_outline,
                    color: playing ? Colors.green : null),
                title: Text(np?['title'] ?? (playing ? 'Playing' : 'Nothing playing')),
                subtitle: Text(np != null && (np['artists'] as List?)?.isNotEmpty == true
                    ? (np['artists'] as List).join(', ')
                    : 'Pick "Living Room (RX-V671)" in Spotify to play here'),
              ),
              Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                IconButton(
                    iconSize: 36,
                    onPressed: _busy ? null : () => _do('control', {'cmd': 'prev'}),
                    icon: const Icon(Icons.skip_previous)),
                IconButton(
                    iconSize: 48,
                    onPressed: _busy
                        ? null
                        : () => _do('control', {'cmd': playing ? 'pause' : 'play'}),
                    icon: Icon(playing ? Icons.pause_circle : Icons.play_circle)),
                IconButton(
                    iconSize: 36,
                    onPressed: _busy ? null : () => _do('control', {'cmd': 'next'}),
                    icon: const Icon(Icons.skip_next)),
              ]),
              const SizedBox(height: 8),
            ]),
          ),
          // ---- receiver
          Card(
            child: Column(children: [
              SwitchListTile(
                secondary: const Icon(Icons.power_settings_new),
                title: const Text('Receiver power'),
                subtitle: Text(rx['reachable'] == false
                    ? 'Unreachable'
                    : 'Input: ${rx['input'] ?? '?'}'),
                value: rx['power'] == 'On',
                onChanged: _busy
                    ? null
                    : (v) => _do('receiver/power', {'on': v ? '1' : '0'}),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
                child: Wrap(spacing: 8, runSpacing: 8, children: [
                  for (final inp in const ['PC', 'HDMI1', 'HDMI2', 'HDMI3', 'AV1', 'TUNER'])
                    ChoiceChip(
                      label: Text(inp == 'PC' ? 'Spotify (PC)' : inp),
                      selected: rx['input'] == inp,
                      onSelected: _busy
                          ? null
                          : (_) => _do('receiver/input', {'input': inp}),
                    ),
                ]),
              ),
            ]),
          ),
          // ---- people
          Card(
            child: Column(children: [
              ListTile(
                leading: const Icon(Icons.group),
                title: Text('${s['accounts']} Spotify account(s) linked'),
                subtitle: Text(addLeft > 0
                    ? '"Add me" visible for ${addLeft ~/ 60}:${(addLeft % 60).toString().padLeft(2, '0')}'
                    : '"Add me" hidden'),
                trailing: TextButton(
                  onPressed: _busy ? null : () => _do('add'),
                  child: const Text('Show Add me'),
                ),
              ),
              for (final e in unknown.entries)
                ListTile(
                  dense: true,
                  leading: const Icon(Icons.devices_other),
                  title: Text('Unregistered: ${(e.value as Map)['ip'] ?? e.key}'),
                  subtitle: Text(e.key),
                  trailing: TextButton(
                    onPressed: _busy ? null : () => _do('ignore', {'mac': e.key}),
                    child: const Text('Ignore'),
                  ),
                ),
            ]),
          ),
          // ---- health
          Card(
            child: ListTile(
              leading: Icon(Icons.monitor_heart,
                  color: rx['reachable'] == false ? Colors.red : Colors.green),
              title: Text('Delay ≈ ${lat['receiver_buffer_s'] ?? '–'} s receiver + '
                  '${lat['pi_queue_ms'] ?? '–'} ms Pi'),
              subtitle: Text('Stalls: ${health['stalls'] ?? 0} · '
                  'Reconnects: ${health['reconnects'] ?? 0} · '
                  'Spotify player update due in ${health['soloist_days_left'] ?? '?'} days'),
              onLongPress: _editHost,
            ),
          ),
        ],
      ),
    );
  }
}
