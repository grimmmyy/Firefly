import 'package:flutter/material.dart';

import 'shell.dart';

/// The receiver's own sources: FM/AM tuner, front USB port, media-server
/// client, and renaming its inputs. All go through the Pi (/api/x/...).

class TunerCard extends StatefulWidget {
  final AppState app;
  const TunerCard({super.key, required this.app});

  @override
  State<TunerCard> createState() => _TunerCardState();
}

class _TunerCardState extends State<TunerCard> {
  Map t = {};
  bool busy = false;

  @override
  void initState() {
    super.initState();
    _do(null);
  }

  Future<void> _do(String? action, [String? value, String? band]) async {
    setState(() => busy = true);
    final q = <String, String>{
      if (action != null) 'action': action,
      if (value != null) 'value': value,
      if (band != null) 'band': band,
    };
    final r = await widget.app.spot.x('tuner', q.isEmpty ? null : q);
    if (!mounted) return;
    setState(() {
      busy = false;
      if (r != null && r['error'] == null) t = r;
    });
    if (r?['error'] != null) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('${r!['error']}')));
    }
  }

  Future<void> _enterFreq() async {
    final band = (t['band'] as String?) ?? 'FM';
    final c = TextEditingController(text: t['freq']?.toString() ?? '');
    final v = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Tune $band'),
        content: TextField(
          controller: c,
          autofocus: true,
          keyboardType: const TextInputType.numberWithOptions(decimal: true),
          decoration: InputDecoration(
              helperText: band == 'FM' ? '87.5 – 107.9 MHz' : '530 – 1710 kHz'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, c.text), child: const Text('Tune')),
        ],
      ),
    );
    if (v != null && v.trim().isNotEmpty) _do('freq', v.trim(), band);
  }

  @override
  Widget build(BuildContext context) {
    final band = (t['band'] as String?) ?? 'FM';
    final f = t['freq'] as num?;
    final presets = ((t['presets'] as List?) ?? []).cast<Map>();
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      ListTile(
        leading: const Icon(Icons.radio),
        title: Text(f == null
            ? 'FM / AM radio'
            : '$band ${band == 'FM' ? f.toStringAsFixed(1) : f.toInt()} ${t['unit'] ?? ''}'),
        subtitle: Text(t['tuned'] == true
            ? (t['stereo'] == true ? 'Tuned · stereo' : 'Tuned')
            : 'Tap a preset or tune to listen on the receiver'),
        trailing: busy
            ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
            : null,
      ),
      Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: Wrap(spacing: 8, runSpacing: 4, crossAxisAlignment: WrapCrossAlignment.center, children: [
          SegmentedButton<String>(
            segments: const [
              ButtonSegment(value: 'FM', label: Text('FM')),
              ButtonSegment(value: 'AM', label: Text('AM')),
            ],
            selected: {band},
            onSelectionChanged: (s) => _do('band', s.first),
          ),
          IconButton(
              tooltip: 'Scan down', icon: const Icon(Icons.fast_rewind), onPressed: () => _do('scan_down')),
          IconButton(
              tooltip: 'Scan up', icon: const Icon(Icons.fast_forward), onPressed: () => _do('scan_up')),
          OutlinedButton(onPressed: _enterFreq, child: const Text('Tune…')),
        ]),
      ),
      if (presets.isNotEmpty)
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
          child: Wrap(spacing: 8, runSpacing: 4, children: [
            for (final p in presets)
              ChoiceChip(
                label: Text('${p['n']} · ${p['title']}'),
                selected: '${p['n']}' == '${t['preset']}',
                onSelected: (_) => _do('preset', '${p['n']}'),
              ),
          ]),
        ),
    ]);
  }
}

/// Folder browser for the receiver's USB port or media-server client.
class ReceiverBrowsePage extends StatefulWidget {
  final AppState app;
  final String source; // 'usb' or 'server'
  const ReceiverBrowsePage({super.key, required this.app, required this.source});

  @override
  State<ReceiverBrowsePage> createState() => _ReceiverBrowsePageState();
}

class _ReceiverBrowsePageState extends State<ReceiverBrowsePage> {
  Map s = {};
  bool busy = true;

  String get title => widget.source == 'usb' ? 'USB' : 'Media server';

  @override
  void initState() {
    super.initState();
    _do(null);              // look first; the receiver switches when you open or play something
  }

  Future<void> _do(String? action, [String? value]) async {
    setState(() => busy = true);
    final r = await widget.app.spot.x(widget.source, {
      if (action != null) 'action': action,
      if (value != null) 'value': value,
    });
    if (!mounted) return;
    setState(() {
      busy = false;
      if (r != null && r['error'] == null) s = r;
    });
  }

  @override
  Widget build(BuildContext context) {
    final lines = ((s['lines'] as List?) ?? []).cast<Map>();
    final first = (s['first'] as num?)?.toInt() ?? 1;
    final total = (s['total'] as num?)?.toInt() ?? 0;
    final song = (s['song'] as String?) ?? '';
    return Scaffold(
      appBar: AppBar(
        title: Text((s['menu'] as String?)?.isNotEmpty == true ? s['menu'] : title),
        actions: [
          IconButton(tooltip: 'Top', icon: const Icon(Icons.home_outlined), onPressed: () => _do('home')),
          IconButton(tooltip: 'Refresh', icon: const Icon(Icons.refresh), onPressed: () => _do(null)),
        ],
      ),
      body: Column(children: [
        if (busy) const LinearProgressIndicator(),
        if (!busy && s['ready'] != true)
          Padding(
            padding: const EdgeInsets.all(24),
            child: Text(widget.source == 'usb'
                ? 'No USB stick found. Plug one into the receiver\'s front USB port.'
                : 'The receiver can\'t see a media server on the network.'),
          ),
        if ((s['layer'] as num? ?? 1) > 1)
          ListTile(
            leading: const Icon(Icons.arrow_upward),
            title: const Text('Back'),
            onTap: () => _do('back'),
          ),
        Expanded(
          child: ListView(children: [
            for (final l in lines)
              ListTile(
                leading: Icon(l['kind'] == 'Container' ? Icons.folder_outlined : Icons.music_note),
                title: Text('${l['text']}'),
                onTap: () => _do('open', '${l['line']}'),
              ),
            if (total > 8)
              Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                IconButton(
                  icon: const Icon(Icons.chevron_left),
                  onPressed: first > 1 ? () => _do('page', '${first - 8}') : null,
                ),
                Text('${first}–${(first + 7).clamp(1, total)} of $total'),
                IconButton(
                  icon: const Icon(Icons.chevron_right),
                  onPressed: first + 8 <= total ? () => _do('page', '${first + 8}') : null,
                ),
              ]),
          ]),
        ),
        if (song.isNotEmpty || s['playing'] == true)
          Material(
            elevation: 4,
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              child: Column(children: [
                Text(song, maxLines: 1, overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w600)),
                Text([s['artist'], s['album']].where((v) => '$v'.isNotEmpty).join(' · '),
                    maxLines: 1, overflow: TextOverflow.ellipsis),
                Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                  IconButton(
                    icon: Icon(Icons.shuffle,
                        color: s['shuffle'] == 'On' ? Theme.of(context).colorScheme.primary : null),
                    onPressed: () => _do('shuffle', s['shuffle'] == 'On' ? 'Off' : 'On'),
                  ),
                  IconButton(icon: const Icon(Icons.skip_previous), onPressed: () => _do('Skip Rev')),
                  IconButton(
                    iconSize: 36,
                    icon: Icon(s['playing'] == true ? Icons.pause_circle : Icons.play_circle),
                    onPressed: () => _do(s['playing'] == true ? 'Pause' : 'Play'),
                  ),
                  IconButton(icon: const Icon(Icons.skip_next), onPressed: () => _do('Skip Fwd')),
                  IconButton(
                    icon: Icon(s['repeat'] == 'Single' ? Icons.repeat_one : Icons.repeat,
                        color: s['repeat'] != 'Off' && s['repeat'] != null
                            ? Theme.of(context).colorScheme.primary
                            : null),
                    onPressed: () => _do('repeat',
                        const {'Off': 'All', 'All': 'Single', 'Single': 'Off'}[s['repeat']] ?? 'All'),
                  ),
                ]),
              ]),
            ),
          ),
      ]),
    );
  }
}

/// Rename the receiver's inputs (shown on its front display and in the app).
class InputNamesPage extends StatefulWidget {
  final AppState app;
  const InputNamesPage({super.key, required this.app});

  @override
  State<InputNamesPage> createState() => _InputNamesPageState();
}

class _InputNamesPageState extends State<InputNamesPage> {
  List<Map> inputs = [];

  @override
  void initState() {
    super.initState();
    _load(null);
  }

  Future<void> _load(Map<String, String>? q) async {
    final r = await widget.app.spot.x('input_names', q);
    if (!mounted) return;
    if (r?['error'] != null) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('${r!['error']}')));
      return;
    }
    setState(() => inputs = ((r?['inputs'] as List?) ?? []).cast<Map>());
  }

  Future<void> _rename(Map i) async {
    final c = TextEditingController(text: '${i['name']}');
    final v = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Rename ${i['id']}'),
        content: TextField(
          controller: c,
          autofocus: true,
          maxLength: 9,
          decoration: const InputDecoration(helperText: 'Up to 9 characters, e.g. Fire TV'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, '${i['id']}'), child: const Text('Reset')),
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, c.text), child: const Text('Save')),
        ],
      ),
    );
    if (v != null) _load({'id': '${i['id']}', 'name': v});
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Input names')),
        body: inputs.isEmpty
            ? const Center(child: CircularProgressIndicator())
            : ListView(children: [
                for (final i in inputs)
                  ListTile(
                    title: Text('${i['name']}'),
                    subtitle: i['name'] != i['id'] ? Text('${i['id']}') : null,
                    trailing: const Icon(Icons.edit_outlined),
                    onTap: () => _rename(i),
                  ),
              ]),
      );
}
