import 'dart:io';

import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../theme.dart';
import 'shell.dart';

Future<void> openSetup(BuildContext context, AppState app) => Navigator.of(context)
    .push(MaterialPageRoute(builder: (_) => SetupScreen(app: app)));

/// Shows the wizard once on first launch.
Future<void> maybeFirstRunSetup(BuildContext context, AppState app) async {
  final prefs = await SharedPreferences.getInstance();
  if (prefs.getBool('setupDone') ?? false) return;
  if (context.mounted) await openSetup(context, app);
}

enum _Step { pending, running, ok, failed }

/// Find bridge → find receiver → live health check → test tone.
class SetupScreen extends StatefulWidget {
  final AppState app;
  const SetupScreen({super.key, required this.app});

  @override
  State<SetupScreen> createState() => _SetupScreenState();
}

class _SetupScreenState extends State<SetupScreen> {
  _Step bridge = _Step.pending, receiver = _Step.pending, checks = _Step.pending;
  _Step tone = _Step.pending;
  List<Map> results = [];
  String? toneError;

  AppState get a => widget.app;

  @override
  void initState() {
    super.initState();
    _runAll();
  }

  Future<void> _runAll() async {
    setState(() {
      bridge = receiver = checks = _Step.running;
      tone = _Step.pending;
      results = [];
    });
    await a.spot.start();
    if (!mounted) return;
    setState(() => bridge = a.spot.host != null ? _Step.ok : _Step.failed);

    // the bridge knows the receiver's address: seed discovery with it
    final rx = a.spot.receiverIp;
    if (rx != null && a.cast.renderers.every((r) => r.location.host != rx)) {
      await a.cast.addReceiverByIp(rx);
    } else if (a.cast.renderers.isEmpty) {
      await a.cast.discover();
    }
    if (!mounted) return;
    setState(() => receiver = a.cast.selected != null ? _Step.ok : _Step.failed);

    if (a.spot.host != null) {
      final r = await a.spot.diagnose();
      if (!mounted) return;
      setState(() {
        results = r;
        // only hard failures block; power/input/ethernet/remote are advisories
        const hard = {'receiver', 'upnp', 'stream'};
        checks = r.isEmpty
            ? _Step.failed
            : r.any((c) => c['ok'] != true && hard.contains(c['id']))
                ? _Step.failed
                : _Step.ok;
      });
    } else {
      setState(() => checks = _Step.failed);
    }
  }

  Future<void> _playTone() async {
    final url = a.spot.toneUrl;
    setState(() {
      tone = _Step.running;
      toneError = null;
    });
    try {
      if (url == null) throw StateError('Speaker bridge not connected');
      if (a.spot.playing) await a.spot.control('pause');
      await a.cast.playUrl(url, title: 'Firefly test tone', mime: 'audio/wav');
    } catch (e) {
      setState(() {
        tone = _Step.failed;
        toneError = '$e';
      });
    }
  }

  Future<void> _heard(bool yes) async {
    setState(() {
      tone = yes ? _Step.ok : _Step.failed;
      toneError = yes
          ? null
          : 'Check the receiver volume and that its input shows PC, then try again.';
    });
    if (yes) {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool('setupDone', true);
    }
  }

  Future<void> _pair() async {
    final mobile = Platform.isAndroid || Platform.isIOS;
    final link = mobile
        ? await Navigator.of(context).push<String>(
            MaterialPageRoute(builder: (_) => const _ScanPage()))
        : await _pasteDialog();
    if (link == null || !mounted) return;
    final ok = await a.spot.pair(link);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(ok ? 'Paired with the speaker bridge' : 'That isn\'t a Firefly pairing code')));
    if (ok) _runAll();
  }

  Future<String?> _pasteDialog() {
    final ctrl = TextEditingController();
    return showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Paste pairing link'),
        content: TextField(
          controller: ctrl,
          autofocus: true,
          decoration: const InputDecoration(hintText: 'firefly://pair?…'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, ctrl.text), child: const Text('Pair')),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final s = a.spot;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Set up & check'),
        actions: [
          IconButton(tooltip: 'Run again', icon: const Icon(Icons.refresh), onPressed: _runAll),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
        children: [
          _StepCard(
            n: 1,
            title: 'Speaker bridge (Raspberry Pi)',
            state: bridge,
            detail: s.host == null
                ? 'Not found. Be on home Wi-Fi, or pair below to use it away from home.'
                : '${s.remote ? 'Via Tailscale' : 'Found'} at ${s.host}',
          ),
          _StepCard(
            n: 2,
            title: 'Receiver',
            state: receiver,
            detail: a.cast.selected == null
                ? 'No receiver found. Is it plugged in and Network Standby on?'
                : '${a.cast.selected!.friendlyName} at ${a.cast.selected!.location.host}',
            action: receiver == _Step.failed && s.host != null
                ? TextButton(
                    onPressed: () async {
                      final ok = await s.rediscoverReceiver();
                      if (ok) _runAll();
                    },
                    child: const Text('Search again'))
                : null,
          ),
          _StepCard(
            n: 3,
            title: 'Health check',
            state: checks,
            detail: results.isEmpty && checks == _Step.running ? 'Checking…' : null,
            children: [
              for (final c in results)
                ListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  leading: Icon(
                    c['ok'] == true ? Icons.check_circle : Icons.error_outline,
                    color: c['ok'] == true ? FireflyTheme.spotify : Colors.orangeAccent,
                  ),
                  title: Text('${c['detail']}'),
                  subtitle: (c['fix'] as String? ?? '').isEmpty ? null : Text('${c['fix']}'),
                ),
            ],
          ),
          _StepCard(
            n: 4,
            title: 'Test tone',
            state: tone,
            detail: toneError ??
                (tone == _Step.running
                    ? 'Playing a 2-second beep on the receiver. Did you hear it?'
                    : tone == _Step.ok
                        ? 'All set.'
                        : 'Plays a short beep through the receiver.'),
            action: tone == _Step.running
                ? Row(mainAxisSize: MainAxisSize.min, children: [
                    TextButton(onPressed: () => _heard(false), child: const Text('No')),
                    FilledButton(onPressed: () => _heard(true), child: const Text('Yes')),
                  ])
                : FilledButton.tonal(
                    onPressed: s.toneUrl == null || a.cast.selected == null ? null : _playTone,
                    child: const Text('Play test tone')),
          ),
          const SizedBox(height: 16),
          Card(
            child: Column(children: [
              ListTile(
                leading: const Icon(Icons.qr_code_scanner),
                title: Text(s.paired ? 'Paired' : 'Pair this device'),
                subtitle: Text(s.paired
                    ? (s.remoteHost != null
                        ? 'Works away from home via Tailscale ${s.remoteHost}'
                        : 'Install Tailscale on the Pi and this phone to use it away from home')
                    : 'Scan the code on the bridge page (http://<pi>:8090) to connect '
                        'instantly and enable remote control'),
                trailing: TextButton(
                  onPressed: _pair,
                  child: Text(Platform.isAndroid || Platform.isIOS ? 'Scan' : 'Paste'),
                ),
              ),
              ListTile(
                leading: const Icon(Icons.notifications_active_outlined),
                title: const Text('Problem alerts'),
                subtitle: Text(_alertsText(s.alerts)),
              ),
            ]),
          ),
          const SizedBox(height: 16),
          FilledButton(
            onPressed: () async {
              final prefs = await SharedPreferences.getInstance();
              await prefs.setBool('setupDone', true);
              if (context.mounted) Navigator.pop(context);
            },
            child: const Text('Done'),
          ),
        ],
      ),
    );
  }

  String _alertsText(Map al) {
    final on = [
      if (al['ntfy'] == true) 'ntfy',
      if (al['discord'] == true) 'Discord',
    ];
    final hb = al['heartbeat'] == true
        ? 'Pi-offline alerts on (heartbeat).'
        : 'Set HEARTBEAT_URL on the Pi to be told when it goes offline.';
    return on.isEmpty
        ? 'Off. Set NTFY_TOPIC or DISCORD_WEBHOOK when installing the Pi. $hb'
        : 'Receiver down, network changes and expiring Spotify build go to ${on.join(' + ')}. $hb';
  }
}

class _StepCard extends StatelessWidget {
  final int n;
  final String title;
  final _Step state;
  final String? detail;
  final Widget? action;
  final List<Widget> children;
  const _StepCard({
    required this.n,
    required this.title,
    required this.state,
    this.detail,
    this.action,
    this.children = const [],
  });

  @override
  Widget build(BuildContext context) {
    final Widget icon = switch (state) {
      _Step.running => const SizedBox(
          width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2.5)),
      _Step.ok => const Icon(Icons.check_circle, color: FireflyTheme.spotify),
      _Step.failed => const Icon(Icons.cancel, color: Colors.redAccent),
      _Step.pending => CircleAvatar(radius: 11, child: Text('$n', style: const TextStyle(fontSize: 12))),
    };
    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            icon,
            const SizedBox(width: 12),
            Expanded(
                child: Text(title,
                    style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 16))),
            if (action != null) action!,
          ]),
          if (detail != null) ...[
            const SizedBox(height: 6),
            Text(detail!, style: TextStyle(color: Colors.white.withValues(alpha: 0.7))),
          ],
          ...children,
        ]),
      ),
    );
  }
}

class _ScanPage extends StatefulWidget {
  const _ScanPage();

  @override
  State<_ScanPage> createState() => _ScanPageState();
}

class _ScanPageState extends State<_ScanPage> {
  bool _done = false;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Scan pairing code')),
      body: MobileScanner(
        onDetect: (capture) {
          if (_done) return;
          for (final b in capture.barcodes) {
            final v = b.rawValue;
            if (v != null && v.startsWith('firefly://')) {
              _done = true;
              Navigator.pop(context, v);
              return;
            }
          }
        },
      ),
    );
  }
}
