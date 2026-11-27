import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../services/spotcast.dart';
import 'shell.dart';

/// Sleep timer, quiet hours, sound per source and receiver health.
class AutomationsCard extends StatefulWidget {
  final AppState app;
  const AutomationsCard({super.key, required this.app});

  @override
  State<AutomationsCard> createState() => _AutomationsCardState();
}

class _AutomationsCardState extends State<AutomationsCard> {
  int sleepS = 0;
  Map quiet = {};
  Map srcSound = {};
  List<Map> latency = [];
  Timer? _tick;
  bool duckCalls = false;
  static const _native = MethodChannel('firefly/native');

  SpotcastController get spot => widget.app.spot;

  @override
  void initState() {
    super.initState();
    _load();
    _tick = Timer.periodic(const Duration(seconds: 30), (_) async {
      if (sleepS > 0) {
        final r = await spot.x('sleep');
        if (mounted && r != null) setState(() => sleepS = (r['sleep_s'] as num?)?.toInt() ?? 0);
      }
    });
  }

  @override
  void dispose() {
    _tick?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    final prefs = await SharedPreferences.getInstance();
    duckCalls = prefs.getBool('duckCalls') ?? false;
    final r = await Future.wait(
        [spot.x('sleep'), spot.x('quiet'), spot.x('source_sound'), spot.x('health')]);
    if (!mounted) return;
    setState(() {
      sleepS = (r[0]?['sleep_s'] as num?)?.toInt() ?? 0;
      quiet = r[1] ?? {};
      srcSound = r[2] ?? {};
      latency = ((r[3]?['latency'] as List?) ?? []).cast<Map>();
    });
  }

  Future<void> _setDuck(bool v) async {
    if (v) {
      final ok = await _native.invokeMethod<bool>('phonePermission') ?? false;
      if (!ok) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
              content: Text('Firefly needs the Phone permission to notice calls.')));
        }
        return;
      }
    }
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool('duckCalls', v);
    if (mounted) setState(() => duckCalls = v);
  }

  Future<void> _sleep(int? minutes) async {
    final r = await spot.x('sleep', minutes == null ? {'cancel': '1'} : {'minutes': '$minutes'});
    if (mounted && r != null) setState(() => sleepS = (r['sleep_s'] as num?)?.toInt() ?? 0);
  }

  Future<void> _quiet(Map change) async {
    final r = await spot.xPost('quiet', change);
    if (mounted && r != null && r['error'] == null) setState(() => quiet = r);
  }

  Future<void> _srcSound(Map change) async {
    final r = await spot.xPost('source_sound', change);
    if (mounted && r != null && r['error'] == null) setState(() => srcSound = r);
  }

  Future<void> _pickTime(String key) async {
    final p = '${quiet[key] ?? '22:00'}'.split(':');
    final t = await showTimePicker(
        context: context, initialTime: TimeOfDay(hour: int.parse(p[0]), minute: int.parse(p[1])));
    if (t != null) {
      _quiet({key: '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}'});
    }
  }

  static const _srcLabels = {
    'spotify': 'Spotify',
    'airplay': 'AirPlay',
    'bluetooth': 'Bluetooth',
    'radio': 'Internet radio',
    'dlna': 'Cast (DLNA)',
    'TUNER': 'FM/AM',
    'USB': 'USB',
    'HDMI1': 'HDMI1',
    'HDMI2': 'HDMI2',
    'HDMI3': 'HDMI3',
    'HDMI4': 'HDMI4',
    'HDMI5': 'HDMI5',
    'AV1': 'AV1',
    'AV2': 'AV2',
    'AUDIO1': 'AUDIO1',
    'AUDIO2': 'AUDIO2',
  };

  String _profileText(Map? p) {
    if (p == null || p.isEmpty) return 'Leave as is';
    return [
      if (p['straight'] == true) 'Straight' else if (p['program'] != null) '${p['program']}',
      if (p['enhancer'] == true) 'Enhancer',
      if (p['drc'] == true) 'Night',
      if (p['pure_direct'] == true) 'Pure Direct',
      if ((p['dialogue_lift'] ?? 0) != 0) 'Dialogue ${p['dialogue_lift']}',
    ].join(' · ');
  }

  Future<void> _editProfile(String src) async {
    final profiles = Map<String, dynamic>.from((srcSound['profiles'] as Map?) ?? {});
    final p = Map<String, dynamic>.from((profiles[src] as Map?) ?? {});
    final programs = ((srcSound['programs'] as List?) ?? []).cast<String>();
    String mode = p['straight'] == true ? 'straight' : (p['program'] != null ? 'program' : 'keep');
    String? program = p['program'] as String? ?? (programs.isEmpty ? null : programs.first);
    bool enhancer = p['enhancer'] == true, drc = p['drc'] == true;
    double lift = ((p['dialogue_lift'] as num?) ?? 0).toDouble();
    final res = await showDialog<Map?>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, set) => AlertDialog(
          title: Text('Sound for ${_srcLabels[src] ?? src}'),
          content: SingleChildScrollView(
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              SegmentedButton<String>(
                segments: const [
                  ButtonSegment(value: 'keep', label: Text('Keep')),
                  ButtonSegment(value: 'straight', label: Text('Straight')),
                  ButtonSegment(value: 'program', label: Text('Program')),
                ],
                selected: {mode},
                onSelectionChanged: (v) => set(() => mode = v.first),
              ),
              if (mode == 'program')
                DropdownButtonFormField<String>(
                  value: programs.contains(program) ? program : null,
                  items: [for (final x in programs) DropdownMenuItem(value: x, child: Text(x))],
                  onChanged: (v) => set(() => program = v),
                ),
              SwitchListTile(
                  title: const Text('Enhancer'),
                  value: enhancer,
                  onChanged: (v) => set(() => enhancer = v)),
              SwitchListTile(
                  title: const Text('Night mode'), value: drc, onChanged: (v) => set(() => drc = v)),
              ListTile(
                title: Text('Dialogue Lift ${lift.round()}'),
                subtitle: Slider(
                    min: 0, max: 5, divisions: 5, value: lift, onChanged: (v) => set(() => lift = v)),
              ),
            ]),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx, <String, dynamic>{}), child: const Text('Clear')),
            TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
            FilledButton(
              onPressed: () => Navigator.pop(ctx, <String, dynamic>{
                if (mode == 'straight') 'straight': true,
                if (mode == 'program' && program != null) 'program': program,
                'enhancer': enhancer,
                'drc': drc,
                'dialogue_lift': lift.round(),
              }),
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
    if (res == null) return;
    if (res.isEmpty) {
      profiles.remove(src);
    } else {
      profiles[src] = res;
    }
    _srcSound({'profiles': profiles});
  }

  @override
  Widget build(BuildContext context) {
    final profiles = (srcSound['profiles'] as Map?) ?? {};
    final cap = ((quiet['max_db'] as num?) ?? -40).toDouble();
    final recent = latency.length > 6 ? latency.sublist(latency.length - 6) : latency;
    final avg = recent.isEmpty
        ? null
        : recent.map((h) => (h['avg_ms'] as num).toDouble()).reduce((a, b) => a + b) / recent.length;
    final slow = recent.fold<int>(0, (n, h) => n + ((h['slow'] as num?)?.toInt() ?? 0));
    final theme = Theme.of(context);
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      // ------------------------------------------------ sleep timer
      ListTile(
        leading: const Icon(Icons.bedtime_outlined),
        title: Text(sleepS > 0 ? 'Sleep timer: ${(sleepS / 60).ceil()} min left' : 'Sleep timer'),
        subtitle: const Text('Fades the music out gently, then turns the receiver off'),
      ),
      Padding(
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
        child: Wrap(spacing: 8, children: [
          for (final m in [15, 30, 60, 90]) ActionChip(label: Text('$m min'), onPressed: () => _sleep(m)),
          if (sleepS > 0) ActionChip(label: const Text('Cancel'), onPressed: () => _sleep(null)),
        ]),
      ),
      if (Platform.isAndroid)
        SwitchListTile(
          secondary: const Icon(Icons.phone_in_talk_outlined),
          title: const Text('Lower volume during calls'),
          subtitle: const Text('Turns the receiver down 20 dB while this phone rings or is on a call'),
          value: duckCalls,
          onChanged: _setDuck,
        ),
      const Divider(),
      // ------------------------------------------------ quiet hours
      SwitchListTile(
        secondary: const Icon(Icons.volume_down_outlined),
        title: const Text('Quiet hours'),
        subtitle: Text('Volume can\'t go above ${cap.toStringAsFixed(0)} dB between '
            '${quiet['from'] ?? '22:00'} and ${quiet['to'] ?? '07:00'}, even from the remote'),
        value: quiet['on'] == true,
        onChanged: (v) => _quiet({'on': v}),
      ),
      if (quiet['on'] == true) ...[
        Row(children: [
          const SizedBox(width: 16),
          OutlinedButton(onPressed: () => _pickTime('from'), child: Text('From ${quiet['from']}')),
          const SizedBox(width: 8),
          OutlinedButton(onPressed: () => _pickTime('to'), child: Text('To ${quiet['to']}')),
        ]),
        Slider(
          min: -70,
          max: spot.maxDb,
          divisions: ((spot.maxDb + 70) * 2).round(),
          value: cap.clamp(-70, spot.maxDb),
          label: '${cap.toStringAsFixed(1)} dB',
          onChanged: (v) => setState(() => quiet = {...quiet, 'max_db': v}),
          onChangeEnd: (v) => _quiet({'max_db': double.parse(v.toStringAsFixed(1))}),
        ),
      ],
      const Divider(),
      // ------------------------------------------------ sound per source
      SwitchListTile(
        secondary: const Icon(Icons.tune),
        title: const Text('Sound per source'),
        subtitle: const Text('Switch sound program automatically when the source changes'),
        value: srcSound['on'] == true,
        onChanged: (v) => _srcSound({'on': v}),
      ),
      if (srcSound['on'] == true)
        for (final src in _srcLabels.keys)
          if (profiles.containsKey(src) || const ['spotify', 'airplay', 'TUNER', 'HDMI1'].contains(src))
            ListTile(
              dense: true,
              contentPadding: const EdgeInsets.only(left: 72, right: 16),
              title: Text(_srcLabels[src]!),
              subtitle: Text(_profileText(profiles[src] as Map?)),
              trailing: const Icon(Icons.edit_outlined, size: 18),
              onTap: () => _editProfile(src),
            ),
      if (srcSound['on'] == true)
        Padding(
          padding: const EdgeInsets.only(left: 64),
          child: Align(
            alignment: Alignment.centerLeft,
            child: PopupMenuButton<String>(
              tooltip: 'Add a source',
              itemBuilder: (_) => [
                for (final e in _srcLabels.entries)
                  if (!profiles.containsKey(e.key)) PopupMenuItem(value: e.key, child: Text(e.value)),
              ],
              onSelected: _editProfile,
              child: const Padding(
                padding: EdgeInsets.all(8),
                child: Text('+ Add a source'),
              ),
            ),
          ),
        ),
      const Divider(),
      // ------------------------------------------------ health
      ListTile(
        leading: Icon(Icons.monitor_heart_outlined,
            color: slow > 5 || (avg ?? 0) > 500 ? theme.colorScheme.error : null),
        title: Text(avg == null
            ? 'Receiver health: collecting…'
            : 'Receiver answers in ${avg.round()} ms on average'),
        subtitle: Text(avg == null
            ? 'Response times appear after the bridge has talked to the receiver for a while'
            : slow == 0
                ? 'Healthy over the last ${recent.length} h'
                : '$slow slow answers in the last ${recent.length} h, an early sign of a network freeze'),
      ),
      if (latency.length > 1)
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
          child: SizedBox(
            height: 40,
            child: Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
              for (final h in latency)
                Expanded(
                  child: Tooltip(
                    message: '${h['avg_ms']} ms avg, ${h['max_ms']} ms max',
                    child: Container(
                      margin: const EdgeInsets.symmetric(horizontal: 1),
                      height: (4 + ((h['avg_ms'] as num) / 10)).clamp(4, 40).toDouble(),
                      color: ((h['slow'] as num?) ?? 0) > 0
                          ? theme.colorScheme.error
                          : theme.colorScheme.primary.withOpacity(0.6),
                    ),
                  ),
                ),
            ]),
          ),
        ),
    ]);
  }
}
