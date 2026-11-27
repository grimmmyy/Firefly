import 'dart:async';

import 'package:flutter/material.dart';

import '../theme.dart';
import 'freeze_history_page.dart';
import 'receiver_automations.dart';
import 'receiver_sources.dart';
import 'room_eq_page.dart';
import 'shell.dart';

const _days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const _eqLabels = ['50', '100', '156', '220', '311', '440', '622', '880', '1.2k', '1.7k',
  '2.5k', '3.5k', '5k', '10k', '20k'];

/// Everything that makes the RX-V671 behave like a current receiver:
/// sources, sound, scenes, Zone 2, room EQ, schedules and history.
class ReceiverScreen extends StatefulWidget {
  final AppState app;
  const ReceiverScreen({super.key, required this.app});

  @override
  State<ReceiverScreen> createState() => _ReceiverScreenState();
}

class _ReceiverScreenState extends State<ReceiverScreen> {
  Map sound = {};
  Map zone2 = {};
  List<Map> inputs = [];
  List<Map> scenes = [];
  List<String> programs = [];
  List<Map> schedules = [];
  Map bt = {};
  Map history = {};
  List<double> eq = List.filled(15, 0);
  bool loading = false;
  Timer? _eqDebounce;

  AppState get a => widget.app;

  // Sub-pages show cards built by this State; they rebuild whenever it does.
  final _rev = ValueNotifier(0);

  @override
  void setState(VoidCallback fn) {
    super.setState(fn);
    _rev.value++;
  }

  void _open(String title, List<Widget> Function() children) {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => Scaffold(
          appBar: AppBar(title: Text(title)),
          body: ValueListenableBuilder<int>(
            valueListenable: _rev,
            builder: (_, __, ___) => RefreshIndicator(
              onRefresh: _reload,
              child: ListView(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
                children: children(),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _more(IconData icon, String title, String subtitle, VoidCallback onTap) => Card(
        margin: const EdgeInsets.only(bottom: 8),
        child: ListTile(
          leading: Icon(icon),
          title: Text(title, style: const TextStyle(fontWeight: FontWeight.w600)),
          subtitle: Text(subtitle),
          trailing: const Icon(Icons.chevron_right),
          onTap: onTap,
        ),
      );

  @override
  void initState() {
    super.initState();
    a.spot.addListener(_onSpot);
    _reload();
  }

  @override
  void dispose() {
    a.spot.removeListener(_onSpot);
    _eqDebounce?.cancel();
    _rev.dispose();
    super.dispose();
  }

  void _onSpot() {
    if (mounted) setState(() {});
  }

  Future<void> _reload() async {
    if (a.spot.host == null) return;
    setState(() => loading = true);
    final s = a.spot;
    final r = await Future.wait([
      s.x('sound'), s.x('zone2'), s.x('inputs'), s.x('scenes'), s.x('schedules'),
      s.x('bt'), s.x('history'),
    ]);
    if (!mounted) return;
    setState(() {
      loading = false;
      sound = r[0] ?? {};
      zone2 = r[1] ?? {};
      inputs = ((r[2]?['inputs'] as List?) ?? []).cast<Map>();
      scenes = ((r[3]?['scenes'] as List?) ?? []).cast<Map>();
      programs = ((r[3]?['programs'] as List?) ?? []).cast<String>();
      schedules = ((r[4]?['schedules'] as List?) ?? []).cast<Map>();
      bt = r[5] ?? {};
      history = r[6] ?? {};
      final e = (a.spot.extras['dsp']?['eq'] as List?)?.cast<num>();
      if (e != null && e.length == 15) eq = e.map((v) => v.toDouble()).toList();
    });
  }

  Future<void> _sound(String key, Object value) async {
    final r = await a.spot.x('sound', {'key': key, 'value': '$value'});
    if (r != null && mounted) setState(() => sound = r);
  }

  Future<void> _zone2(Map<String, String> q) async {
    final r = await a.spot.x('zone2', q);
    if (r != null && mounted) setState(() => zone2 = r);
  }

  void _snack(String t) =>
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(t)));

  @override
  Widget build(BuildContext context) {
    final s = a.spot;
    if (s.host == null) {
      return const Center(
          child: Padding(
              padding: EdgeInsets.all(32),
              child: Text('Connect to the speaker bridge first (Settings → Set up & check).',
                  textAlign: TextAlign.center)));
    }
    final ex = s.extras;
    final src = (ex['sources'] ?? const {}) as Map;
    return RefreshIndicator(
      onRefresh: _reload,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 32),
        children: [
          Row(children: [
            const Expanded(
                child: Text('Receiver',
                    style: TextStyle(fontSize: 26, fontWeight: FontWeight.w800))),
            if (loading)
              const SizedBox(
                  width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2)),
          ]),
          const SizedBox(height: 8),
          // everyday: what's playing, how it sounds, one-tap scenes
          _nowPlaying(ex),
          _soundCard(),
          _scenesCard(),
          const SizedBox(height: 8),
          _more(Icons.radio, 'FM, USB & media server',
              'Tuner presets, browse a USB stick, rename inputs',
              () => _open('On the receiver', () => [_receiverSourcesCard()])),
          _more(Icons.bedtime_outlined, 'Sleep, quiet hours & automations',
              'Sleep timer, volume cap at night, sound per source, call ducking',
              () => _open('Automations',
                  () => [_Section(title: 'Automations', children: [AutomationsCard(app: a)])])),
          _more(Icons.alarm, 'Schedules',
              schedules.isEmpty ? 'Wake-up, sleep and timed scenes' : '${schedules.length} set',
              () => _open('Schedules', () => [_schedulesCard()])),
          _more(Icons.cast_connected, 'AirPlay, Bluetooth & cast',
              'Turn sources on or off, pair a phone', () => _open('Sources', () => [_sources(src)])),
          _more(Icons.speaker_group_outlined, 'Zone 2', zone2['power'] == 'On' ? 'On' : 'Second room',
              () => _open('Zone 2', () => [_zone2Card()])),
          _more(Icons.graphic_eq, 'Room EQ & loudness', 'Measure your room, 15-band EQ',
              () => _open('Room EQ & loudness',
                  () => [_dspCard(((a.spot.extras['dsp'] ?? const {}) as Map))])),
          _more(Icons.history, 'Listening history', 'What played, and for how long',
              () => _open('History', () => [_historyCard()])),
          _more(Icons.monitor_heart_outlined, 'Freeze history',
              'Every receiver freeze, with what led up to it',
              () => Navigator.push(context,
                  MaterialPageRoute(builder: (_) => FreezeHistoryPage(app: a)))),
        ],
      ),
    );
  }

  // ---------------------------------------------------------------- now playing
  Widget _nowPlaying(Map ex) {
    final np = (ex['now_playing'] ?? const {}) as Map;
    final label = ex['label'] as String?;
    final title = [np['title'], np['artist']].where((v) => v != null && '$v'.isNotEmpty).join(' · ');
    final radio = ex['radio'] as Map?;
    return Card(
      child: ListTile(
        leading: Icon(_sourceIcon(ex['active'] as String?), color: FireflyTheme.amber),
        title: Text(label ?? 'Nothing playing'),
        subtitle: Text(title.isEmpty
            ? 'Last source to start wins: AirPlay, Spotify, Bluetooth, casts and radio'
            : title),
        trailing: radio != null
            ? TextButton(onPressed: () => a.spot.x('radio', {'stop': '1'}), child: const Text('Stop'))
            : null,
      ),
    );
  }

  IconData _sourceIcon(String? k) => switch (k) {
        'spotify' => Icons.music_note,
        'airplay' => Icons.airplay,
        'bluetooth' => Icons.bluetooth_audio,
        'dlna' => Icons.cast,
        'radio' => Icons.radio,
        _ => Icons.speaker,
      };

  // ---------------------------------------------------------------- sources
  Widget _sources(Map src) {
    Future<void> set(String k, bool v) async {
      await a.spot.x('sources', {k: v ? '1' : '0'});
    }

    final devs = ((bt['devices'] as List?) ?? []).cast<Map>();
    final pairing = (bt['pairing_s'] as num?) ?? 0;
    return _Section(title: 'Ways to play', children: [
      SwitchListTile(
        secondary: const Icon(Icons.airplay),
        title: Text(src['airplay2'] == true ? 'AirPlay 2' : 'AirPlay'),
        subtitle: const Text('iPhone, iPad, Mac and Apple Music'),
        value: src['airplay'] == true,
        onChanged: (v) => set('airplay', v),
      ),
      SwitchListTile(
        secondary: const Icon(Icons.volume_up_outlined),
        title: const Text('AirPlay volume moves the receiver'),
        value: src['airplay_volume'] == true,
        onChanged: src['airplay'] == true ? (v) => set('airplay_volume', v) : null,
      ),
      SwitchListTile(
        secondary: const Icon(Icons.cast),
        title: const Text('Cast (DLNA)'),
        subtitle: const Text('Shows as "Living Room (Pi cast)" in apps that cast to DLNA'),
        value: src['dlna'] == true,
        onChanged: (v) => set('dlna', v),
      ),
      SwitchListTile(
        secondary: const Icon(Icons.bluetooth_audio),
        title: const Text('Bluetooth'),
        subtitle: Text('Pairs as "${bt['name'] ?? 'Living Room Speaker'}"'),
        value: src['bluetooth'] == true,
        onChanged: (v) => set('bluetooth', v),
      ),
      if (src['bluetooth'] == true) ...[
        for (final d in devs)
          ListTile(
            dense: true,
            leading: Icon(d['connected'] == true ? Icons.bluetooth_connected : Icons.bluetooth,
                size: 20),
            title: Text('${d['name']}'),
            subtitle: Text(d['connected'] == true ? 'Connected' : 'Paired'),
            trailing: IconButton(
              tooltip: 'Forget',
              icon: const Icon(Icons.delete_outline),
              onPressed: () async {
                final r = await a.spot.x('bt', {'forget': '${d['mac']}'});
                if (r != null && mounted) setState(() => bt = r);
              },
            ),
          ),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
          child: FilledButton.tonalIcon(
            icon: const Icon(Icons.add_link),
            label: Text(pairing > 0
                ? 'Visible for ${pairing ~/ 60}:${(pairing % 60).toString().padLeft(2, '0')}: pair now'
                : 'Pair a phone'),
            onPressed: () async {
              final r = await a.spot.x('bt', {'pair': '1'});
              if (r != null && mounted) setState(() => bt = r);
              _snack('Open Bluetooth on the phone and pick "${bt['name'] ?? 'Living Room Speaker'}"');
            },
          ),
        ),
      ],
    ]);
  }

  // ---------------------------------------------------------------- receiver's own sources
  Widget _receiverSourcesCard() => _Section(title: 'On the receiver', children: [
        TunerCard(app: a),
        ListTile(
          leading: const Icon(Icons.usb),
          title: const Text('USB stick'),
          subtitle: const Text('Browse and play from the front USB port'),
          trailing: const Icon(Icons.chevron_right),
          onTap: () => Navigator.push(context,
              MaterialPageRoute(builder: (_) => ReceiverBrowsePage(app: a, source: 'usb'))),
        ),
        ListTile(
          leading: const Icon(Icons.dns_outlined),
          title: const Text('Media server'),
          subtitle: const Text('Music shared from a computer or NAS'),
          trailing: const Icon(Icons.chevron_right),
          onTap: () => Navigator.push(context,
              MaterialPageRoute(builder: (_) => ReceiverBrowsePage(app: a, source: 'server'))),
        ),
        ListTile(
          leading: const Icon(Icons.label_outline),
          title: const Text('Input names'),
          subtitle: const Text('e.g. HDMI1 → Fire TV, shown on the receiver\'s display'),
          trailing: const Icon(Icons.chevron_right),
          onTap: () async {
            await Navigator.push(
                context, MaterialPageRoute(builder: (_) => InputNamesPage(app: a)));
            _reload();
          },
        ),
      ]);

  // ---------------------------------------------------------------- sound
  Widget _soundCard() {
    final prog = sound['program'] as String?;
    final list = ((sound['programs'] as List?) ?? programs).cast<String>();
    return _Section(title: 'Sound', children: [
      Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: DropdownButtonFormField<String>(
          value: list.contains(prog) ? prog : null,
          decoration: const InputDecoration(labelText: 'Sound program (DSP)'),
          items: [for (final p in list) DropdownMenuItem(value: p, child: Text(p))],
          onChanged: sound['straight'] == true ? null : (v) => v == null ? null : _sound('program', v),
        ),
      ),
      SwitchListTile(
        title: const Text('Straight'),
        subtitle: const Text('No surround processing'),
        value: sound['straight'] == true,
        onChanged: (v) => _sound('straight', v),
      ),
      SwitchListTile(
        title: const Text('Compressed Music Enhancer'),
        value: sound['enhancer'] == true,
        onChanged: (v) => _sound('enhancer', v),
      ),
      SwitchListTile(
        title: const Text('Night mode'),
        subtitle: const Text('Adaptive DRC: quieter peaks, clearer dialogue'),
        value: sound['drc'] == true,
        onChanged: (v) => _sound('drc', v),
      ),
      SwitchListTile(
        title: const Text('Pure Direct'),
        subtitle: const Text('Bypass all processing for the cleanest stereo'),
        value: sound['pure_direct'] == true,
        onChanged: (v) => _sound('pure_direct', v),
      ),
      SwitchListTile(
        title: const Text('CINEMA DSP 3D'),
        subtitle: const Text('Height effect for sound programs'),
        value: sound['cinema_3d'] == true,
        onChanged: (v) => _sound('cinema_3d', v),
      ),
      ListTile(
        title: Text('Dialogue Lift ${(sound['dialogue_lift'] as num?)?.toInt() ?? 0}'),
        subtitle: Slider(
          min: 0,
          max: 5,
          divisions: 5,
          value: ((sound['dialogue_lift'] as num?) ?? 0).toDouble().clamp(0, 5),
          onChanged: (n) => setState(() => sound = {...sound, 'dialogue_lift': n.round()}),
          onChangeEnd: (n) => _sound('dialogue_lift', n.round()),
        ),
      ),
      _toneSlider('Bass', 'bass', (sound['bass_db'] as num?)?.toDouble()),
      _toneSlider('Treble', 'treble', (sound['treble_db'] as num?)?.toDouble()),
      Padding(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
        child: Wrap(spacing: 8, runSpacing: 4, children: [
          for (var i = 1; i <= 4; i++)
            ActionChip(
              label: Text('Receiver scene $i'),
              onPressed: () => _sound('scene', 'Scene $i'),
            ),
        ]),
      ),
    ]);
  }

  Widget _toneSlider(String label, String key, double? v) => ListTile(
        title: Text('$label ${v == null ? '' : '${v > 0 ? '+' : ''}${v.toStringAsFixed(1)} dB'}'),
        subtitle: Slider(
          min: -6,
          max: 6,
          divisions: 24,
          value: (v ?? 0).clamp(-6, 6),
          onChanged: (n) => setState(() => sound = {...sound, '${key}_db': n}),
          onChangeEnd: (n) => _sound(key, n.toStringAsFixed(1)),
        ),
      );

  // ---------------------------------------------------------------- scenes
  Future<void> _saveScenes(List<Map> list) async {
    final r = await a.spot.xPost('scenes', list);
    if (r != null && mounted) {
      setState(() => scenes = ((r['scenes'] as List?) ?? []).cast<Map>());
    } else {
      _snack('Couldn\'t save scenes');
    }
  }

  Widget _scenesCard() => _Section(title: 'Scenes', children: [
        const Padding(
          padding: EdgeInsets.fromLTRB(16, 0, 16, 4),
          child: Text('One tap sets input, sound and volume. Each scene is also an Alexa '
              'device ("Alexa, turn on Movie") you can use in routines.',
              style: TextStyle(color: Colors.white60)),
        ),
        for (var i = 0; i < scenes.length; i++)
          ListTile(
            leading: const Icon(Icons.auto_awesome),
            title: Text('${scenes[i]['name']}'),
            subtitle: Text(_sceneSummary(scenes[i])),
            onTap: () => a.spot.x('scenes', {'apply': '${scenes[i]['name']}'}),
            trailing: PopupMenuButton<String>(
              onSelected: (v) async {
                if (v == 'edit') {
                  final e = await _editScene(scenes[i]);
                  if (e != null) _saveScenes([...scenes]..[i] = e);
                } else {
                  _saveScenes([...scenes]..removeAt(i));
                }
              },
              itemBuilder: (_) => const [
                PopupMenuItem(value: 'edit', child: Text('Edit')),
                PopupMenuItem(value: 'delete', child: Text('Delete')),
              ],
            ),
          ),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
          child: OutlinedButton.icon(
            icon: const Icon(Icons.add),
            label: const Text('New scene'),
            onPressed: () async {
              final e = await _editScene({'name': ''});
              if (e != null) _saveScenes([...scenes, e]);
            },
          ),
        ),
      ]);

  String _sceneSummary(Map s) => [
        if (s['input'] != null) _inputTitle('${s['input']}'),
        if (s['straight'] == true) 'Straight' else if (s['program'] != null) '${s['program']}',
        if (s['drc'] == true) 'night',
        if (s['volume_db'] != null) '${s['volume_db']} dB',
        if (s['zone2'] != null) 'Zone 2 ${s['zone2']}',
      ].join(' · ');

  String _inputTitle(String id) =>
      '${inputs.firstWhere((i) => i['id'] == id, orElse: () => {'title': id})['title']}';

  Future<Map?> _editScene(Map s) {
    final name = TextEditingController(text: '${s['name'] ?? ''}');
    String? input = s['input'] as String?;
    String? program = s['program'] as String?;
    bool straight = s['straight'] == true, drc = s['drc'] == true;
    bool setVol = s['volume_db'] != null;
    double vol = (s['volume_db'] as num?)?.toDouble() ?? -40;
    String? z2 = s['zone2'] as String?;
    return showDialog<Map>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, set) => AlertDialog(
          title: Text(s['name'] == '' ? 'New scene' : 'Edit scene'),
          content: SingleChildScrollView(
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              TextField(controller: name, decoration: const InputDecoration(labelText: 'Name')),
              DropdownButtonFormField<String?>(
                value: inputs.any((i) => i['id'] == input) ? input : null,
                decoration: const InputDecoration(labelText: 'Input'),
                items: [
                  const DropdownMenuItem(value: null, child: Text('Leave as is')),
                  for (final i in inputs)
                    DropdownMenuItem(value: '${i['id']}', child: Text('${i['title']}')),
                ],
                onChanged: (v) => set(() => input = v),
              ),
              DropdownButtonFormField<String?>(
                value: programs.contains(program) ? program : null,
                decoration: const InputDecoration(labelText: 'Sound program'),
                items: [
                  const DropdownMenuItem(value: null, child: Text('Leave as is')),
                  for (final p in programs) DropdownMenuItem(value: p, child: Text(p)),
                ],
                onChanged: straight ? null : (v) => set(() => program = v),
              ),
              SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Straight'),
                  value: straight,
                  onChanged: (v) => set(() => straight = v)),
              SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Night mode'),
                  value: drc,
                  onChanged: (v) => set(() => drc = v)),
              SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text(setVol ? 'Volume ${vol.toStringAsFixed(1)} dB' : 'Set volume'),
                  value: setVol,
                  onChanged: (v) => set(() => setVol = v)),
              if (setVol)
                Slider(
                    min: -80,
                    max: a.spot.maxDb,
                    divisions: ((a.spot.maxDb + 80) * 2).round(),
                    value: vol.clamp(-80, a.spot.maxDb),
                    onChanged: (v) => set(() => vol = v)),
              DropdownButtonFormField<String?>(
                value: z2,
                decoration: const InputDecoration(labelText: 'Zone 2'),
                items: const [
                  DropdownMenuItem(value: null, child: Text('Leave as is')),
                  DropdownMenuItem(value: 'party', child: Text('On, same source')),
                  DropdownMenuItem(value: 'on', child: Text('On')),
                  DropdownMenuItem(value: 'off', child: Text('Off')),
                ],
                onChanged: (v) => set(() => z2 = v),
              ),
            ]),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
            FilledButton(
              onPressed: () {
                if (name.text.trim().isEmpty) return;
                Navigator.pop(ctx, {
                  'name': name.text.trim(),
                  if (input != null) 'input': input,
                  if (!straight && program != null) 'program': program,
                  'straight': straight,
                  'drc': drc,
                  if (setVol) 'volume_db': double.parse(vol.toStringAsFixed(1)),
                  if (z2 != null) 'zone2': z2,
                });
              },
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
  }

  // ---------------------------------------------------------------- zone 2
  Widget _zone2Card() {
    if (zone2['reachable'] != true) return const SizedBox.shrink();
    final on = zone2['power'] == 'On';
    final db = (zone2['volume_db'] as num?)?.toDouble() ?? -40;
    final fixed = zone2['output'] == 'Fixed';
    return _Section(title: 'Zone 2 (second room)', children: [
      SwitchListTile(
        secondary: const Icon(Icons.meeting_room_outlined),
        title: const Text('Zone 2'),
        subtitle: const Text('Needs speakers wired to the Zone 2 terminals'),
        value: on,
        onChanged: (v) => _zone2({'power': v ? '1' : '0'}),
      ),
      if (on) ...[
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: DropdownButtonFormField<String>(
            value: inputs.any((i) => i['id'] == zone2['input']) ? '${zone2['input']}' : null,
            decoration: const InputDecoration(labelText: 'Input'),
            items: [
              for (final i in inputs)
                DropdownMenuItem(value: '${i['id']}', child: Text('${i['title']}')),
            ],
            onChanged: (v) => v == null ? null : _zone2({'input': v}),
          ),
        ),
        if (!fixed)
          ListTile(
            title: Text('Volume ${db.toStringAsFixed(1)} dB'),
            subtitle: Slider(
              min: -80,
              max: a.spot.maxDb,
              value: db.clamp(-80, a.spot.maxDb),
              onChanged: (v) => setState(() => zone2 = {...zone2, 'volume_db': v}),
              onChangeEnd: (v) => _zone2({'volume_db': v.toStringAsFixed(1)}),
            ),
          )
        else
          const ListTile(
              dense: true,
              title: Text('Zone 2 output is set to Fixed on the receiver, so volume is '
                  'controlled by the amplifier it feeds.')),
      ],
    ]);
  }

  // ---------------------------------------------------------------- DSP
  Widget _dspCard(Map dsp) {
    final mobile = Theme.of(context).platform == TargetPlatform.android ||
        Theme.of(context).platform == TargetPlatform.iOS;
    return _Section(title: 'Sound tuning', children: [
      SwitchListTile(
        secondary: const Icon(Icons.graphic_eq),
        title: const Text('Loudness levelling'),
        subtitle: const Text('Evens out quiet and loud songs and apps'),
        value: dsp['loudness'] == true,
        onChanged: (v) => a.spot.x('dsp', {'loudness': v ? '1' : '0'}),
      ),
      SwitchListTile(
        secondary: const Icon(Icons.equalizer),
        title: const Text('Room EQ'),
        value: dsp['eq_on'] == true,
        onChanged: (v) => a.spot.x('dsp', {'eq_on': v ? '1' : '0'}),
      ),
      SizedBox(
        height: 190,
        child: ListView.builder(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.symmetric(horizontal: 8),
          itemCount: 15,
          itemBuilder: (_, i) => SizedBox(
            width: 44,
            child: Column(children: [
              Text(eq[i] == 0 ? '0' : eq[i].toStringAsFixed(0),
                  style: const TextStyle(fontSize: 11)),
              Expanded(
                child: RotatedBox(
                  quarterTurns: 3,
                  child: Slider(
                    min: -12,
                    max: 12,
                    divisions: 48,
                    value: eq[i].clamp(-12, 12),
                    onChanged: (v) {
                      setState(() => eq[i] = v);
                      _eqDebounce?.cancel();
                      _eqDebounce = Timer(const Duration(milliseconds: 600), () {
                        a.spot.x('dsp', {
                          'eq': eq.map((g) => g.toStringAsFixed(1)).join(','),
                          'eq_on': '1',
                        });
                      });
                    },
                  ),
                ),
              ),
              Text(_eqLabels[i], style: const TextStyle(fontSize: 10, color: Colors.white60)),
            ]),
          ),
        ),
      ),
      Padding(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
        child: Wrap(spacing: 8, children: [
          FilledButton.tonalIcon(
            icon: const Icon(Icons.mic),
            label: const Text('Measure my room'),
            onPressed: mobile
                ? () async {
                    final res = await Navigator.of(context).push<List<double>>(
                        MaterialPageRoute(builder: (_) => RoomEqPage(app: a)));
                    if (res != null && mounted) setState(() => eq = res);
                  }
                : null,
          ),
          TextButton(
            onPressed: () {
              setState(() => eq = List.filled(15, 0));
              a.spot.x('dsp', {'eq': List.filled(15, '0').join(','), 'eq_on': '0'});
            },
            child: const Text('Flat'),
          ),
        ]),
      ),
    ]);
  }

  // ---------------------------------------------------------------- schedules
  Future<void> _saveSchedules(List<Map> list) async {
    final r = await a.spot.xPost('schedules', list);
    if (r != null && r['schedules'] != null && mounted) {
      setState(() => schedules = (r['schedules'] as List).cast<Map>());
    } else {
      _snack('Couldn\'t save: ${r?['error'] ?? 'bridge not reachable'}');
    }
  }

  Widget _schedulesCard() => _Section(title: 'Schedules', children: [
        for (var i = 0; i < schedules.length; i++)
          ListTile(
            leading: Icon(switch (schedules[i]['action']) {
              'wake' => Icons.alarm,
              'off' => Icons.power_settings_new,
              'sleep' => Icons.bedtime_outlined,
              _ => Icons.auto_awesome,
            }),
            title: Text('${schedules[i]['time']} · ${_schedTitle(schedules[i])}'),
            subtitle: Text(_daysText((schedules[i]['days'] as List?)?.cast<int>())),
            onTap: () async {
              final e = await _editSchedule(schedules[i]);
              if (e != null) _saveSchedules([...schedules]..[i] = e);
            },
            trailing: Row(mainAxisSize: MainAxisSize.min, children: [
              Switch(
                value: schedules[i]['enabled'] != false,
                onChanged: (v) =>
                    _saveSchedules([...schedules]..[i] = {...schedules[i], 'enabled': v}),
              ),
              IconButton(
                icon: const Icon(Icons.delete_outline),
                onPressed: () => _saveSchedules([...schedules]..removeAt(i)),
              ),
            ]),
          ),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
          child: OutlinedButton.icon(
            icon: const Icon(Icons.add_alarm),
            label: const Text('Add schedule'),
            onPressed: () async {
              final e = await _editSchedule({'action': 'wake', 'time': '07:00'});
              if (e != null) _saveSchedules([...schedules, e]);
            },
          ),
        ),
      ]);

  String _schedTitle(Map s) => switch (s['action']) {
        'wake' => 'Wake up with ${s['tuner_preset'] != null ? 'FM preset ${s['tuner_preset']}' : s['radio_name'] ?? (s['radio'] != null ? 'radio' : 'Spotify')}',
        'off' => 'Turn off',
        'sleep' => 'Fade out over ${s['fade_min'] ?? 20} min',
        _ => 'Scene: ${s['scene']}',
      };

  String _daysText(List<int>? d) {
    if (d == null || d.length == 7) return 'Every day';
    if (d.length == 5 && !d.contains(5) && !d.contains(6)) return 'Weekdays';
    if (d.length == 2 && d.contains(5) && d.contains(6)) return 'Weekends';
    return d.map((i) => _days[i]).join(', ');
  }

  Future<Map?> _editSchedule(Map s) {
    String action = '${s['action'] ?? 'wake'}';
    final parts = '${s['time'] ?? '07:00'}'.split(':');
    var time = TimeOfDay(hour: int.parse(parts[0]), minute: int.parse(parts[1]));
    final days = <int>{...((s['days'] as List?)?.cast<int>() ?? List.generate(7, (i) => i))};
    double vol = (s['volume_db'] as num?)?.toDouble() ?? -40;
    String? scene = s['scene'] as String? ?? (scenes.isEmpty ? null : '${scenes.first['name']}');
    final radio = TextEditingController(text: '${s['radio'] ?? ''}');
    final radioName = TextEditingController(text: '${s['radio_name'] ?? ''}');
    String wakeSrc = s['tuner_preset'] != null
        ? 'fm'
        : (s['radio'] != null ? 'stream' : 'spotify');
    final presetCtl = TextEditingController(text: '${s['tuner_preset'] ?? 1}');
    double fade = ((s['fade_min'] as num?) ?? (action == 'sleep' ? 20 : 10)).toDouble();
    return showDialog<Map>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, set) => AlertDialog(
          title: const Text('Schedule'),
          content: SingleChildScrollView(
            child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
              SegmentedButton<String>(
                segments: const [
                  ButtonSegment(value: 'wake', label: Text('Wake')),
                  ButtonSegment(value: 'sleep', label: Text('Sleep')),
                  ButtonSegment(value: 'scene', label: Text('Scene')),
                  ButtonSegment(value: 'off', label: Text('Off')),
                ],
                selected: {action},
                onSelectionChanged: (v) => set(() => action = v.first),
              ),
              const SizedBox(height: 8),
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(time.format(ctx)),
                trailing: const Icon(Icons.schedule),
                onTap: () async {
                  final t = await showTimePicker(context: ctx, initialTime: time);
                  if (t != null) set(() => time = t);
                },
              ),
              Wrap(spacing: 4, children: [
                for (var i = 0; i < 7; i++)
                  FilterChip(
                    label: Text(_days[i]),
                    selected: days.contains(i),
                    onSelected: (v) => set(() => v ? days.add(i) : days.remove(i)),
                  ),
              ]),
              if (action == 'wake') ...[
                const SizedBox(height: 8),
                Text('Volume ${vol.toStringAsFixed(1)} dB'),
                Slider(
                    min: -80,
                    max: a.spot.maxDb,
                    value: vol.clamp(-80, a.spot.maxDb),
                    onChanged: (v) => set(() => vol = v)),
                SegmentedButton<String>(
                  segments: const [
                    ButtonSegment(value: 'spotify', label: Text('Spotify')),
                    ButtonSegment(value: 'fm', label: Text('FM/AM')),
                    ButtonSegment(value: 'stream', label: Text('Stream')),
                  ],
                  selected: {wakeSrc},
                  onSelectionChanged: (v) => set(() => wakeSrc = v.first),
                ),
                if (wakeSrc == 'fm')
                  TextField(
                    controller: presetCtl,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(labelText: 'Receiver preset number'),
                  ),
                if (wakeSrc == 'stream') ...[
                  TextField(
                      controller: radio,
                      decoration: const InputDecoration(labelText: 'Radio stream URL')),
                  TextField(
                      controller: radioName,
                      decoration: const InputDecoration(labelText: 'Station name')),
                ],
              ],
              if (action == 'wake' || action == 'sleep') ...[
                const SizedBox(height: 8),
                Text(action == 'wake'
                    ? 'Fade up over ${fade.round()} min'
                    : 'Fade out over ${fade.round()} min, then standby'),
                Slider(
                    min: 1,
                    max: 60,
                    divisions: 59,
                    value: fade.clamp(1, 60),
                    onChanged: (v) => set(() => fade = v)),
              ],
              if (action == 'scene')
                DropdownButtonFormField<String>(
                  value: scene,
                  decoration: const InputDecoration(labelText: 'Scene'),
                  items: [
                    for (final sc in scenes)
                      DropdownMenuItem(value: '${sc['name']}', child: Text('${sc['name']}')),
                  ],
                  onChanged: (v) => set(() => scene = v),
                ),
            ]),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
            FilledButton(
              onPressed: days.isEmpty || (action == 'scene' && scene == null)
                  ? null
                  : () => Navigator.pop(ctx, {
                        if (s['id'] != null) 'id': s['id'],
                        'action': action,
                        'enabled': s['enabled'] ?? true,
                        'time': '${time.hour.toString().padLeft(2, '0')}:'
                            '${time.minute.toString().padLeft(2, '0')}',
                        'days': (days.toList()..sort()),
                        if (action == 'wake') 'volume_db': double.parse(vol.toStringAsFixed(1)),
                        if (action == 'wake' && wakeSrc == 'stream' && radio.text.trim().isNotEmpty)
                          'radio': radio.text.trim(),
                        if (action == 'wake' && wakeSrc == 'stream' && radioName.text.trim().isNotEmpty)
                          'radio_name': radioName.text.trim(),
                        if (action == 'wake' && wakeSrc == 'fm')
                          'tuner_preset': int.tryParse(presetCtl.text.trim()) ?? 1,
                        if (action == 'wake' || action == 'sleep') 'fade_min': fade.round(),
                        if (action == 'scene') 'scene': scene,
                      }),
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
  }

  // ---------------------------------------------------------------- history
  Widget _historyCard() {
    final mins = ((history['minutes'] ?? const {}) as Map);
    final daysSorted = mins.keys.map((k) => '$k').toList()..sort();
    final recent = ((history['recent'] as List?) ?? []).cast<Map>();
    final problems = ((history['problems'] as List?) ?? []).cast<Map>();
    const labels = {'spotify': 'Spotify', 'airplay': 'AirPlay', 'bluetooth': 'Bluetooth',
      'dlna': 'Cast', 'radio': 'Radio'};
    return _Section(title: 'Listening history (7 days)', children: [
      for (final d in daysSorted.reversed)
        ListTile(
          dense: true,
          title: Text(d),
          subtitle: Text((mins[d] as Map)
              .entries
              .map((e) => '${labels[e.key] ?? e.key} ${e.value} min')
              .join(' · ')),
        ),
      if (recent.isNotEmpty) const Divider(),
      for (final e in recent.take(15))
        ListTile(
          dense: true,
          leading: Icon(_sourceIcon(e['source'] as String?), size: 18),
          title: Text('${e['title'] ?? ''}'),
          subtitle: Text([
            if (e['artist'] != null) '${e['artist']}',
            _time(e['t'] as num?),
          ].join(' · ')),
        ),
      if (problems.isNotEmpty) ...[
        const Divider(),
        const Padding(
            padding: EdgeInsets.fromLTRB(16, 4, 16, 0),
            child: Text('Problems', style: TextStyle(fontWeight: FontWeight.w700))),
        for (final p in problems.take(8))
          ListTile(
            dense: true,
            leading: const Icon(Icons.warning_amber, size: 18, color: Colors.orangeAccent),
            title: Text('${p['text'] ?? p['kind']}'),
            subtitle: Text(_time(p['t'] as num?)),
          ),
      ],
      if (daysSorted.isEmpty && recent.isEmpty)
        const ListTile(dense: true, title: Text('Nothing yet')),
    ]);
  }

  String _time(num? t) {
    if (t == null) return '';
    final d = DateTime.fromMillisecondsSinceEpoch(t.toInt() * 1000);
    return '${d.month}/${d.day} ${d.hour.toString().padLeft(2, '0')}:'
        '${d.minute.toString().padLeft(2, '0')}';
  }
}

class _Section extends StatelessWidget {
  final String title;
  final List<Widget> children;
  const _Section({required this.title, required this.children});

  @override
  Widget build(BuildContext context) => Card(
        margin: const EdgeInsets.only(top: 12),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 8),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 4, 16, 4),
              child: Text(title,
                  style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 16)),
            ),
            ...children,
          ]),
        ),
      );
}
