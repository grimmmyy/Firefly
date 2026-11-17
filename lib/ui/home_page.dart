import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:permission_handler/permission_handler.dart';

import '../services/cast_controller.dart';
import '../services/ssdp.dart';

class HomePage extends StatefulWidget {
  final CastController controller;
  const HomePage({super.key, required this.controller});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_onChange);
    widget.controller.loadStations();
    widget.controller.discover();
  }

  @override
  void dispose() {
    widget.controller.removeListener(_onChange);
    super.dispose();
  }

  void _onChange() => setState(() {});

  @override
  Widget build(BuildContext context) {
    final c = widget.controller;
    return DefaultTabController(
      length: 3,
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Firefly'),
          actions: [
            IconButton(
              icon: c.discovering
                  ? const SizedBox(
                      height: 18,
                      width: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.refresh),
              onPressed: c.discovering ? null : c.discover,
              tooltip: 'Rescan for receivers',
            ),
          ],
          bottom: const TabBar(
            tabs: [
              Tab(icon: Icon(Icons.speaker), text: 'Receiver'),
              Tab(icon: Icon(Icons.radio), text: 'Stations'),
              Tab(icon: Icon(Icons.folder_open), text: 'Files'),
            ],
          ),
        ),
        body: Column(
          children: [
            _NowPlayingBar(controller: c),
            Expanded(
              child: TabBarView(
                children: [
                  _ReceiverTab(controller: c),
                  _StationsTab(controller: c),
                  _FilesTab(controller: c),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _NowPlayingBar extends StatelessWidget {
  final CastController controller;
  const _NowPlayingBar({required this.controller});

  @override
  Widget build(BuildContext context) {
    final c = controller;
    if (c.selected == null) {
      return Container(
        color: Theme.of(context).colorScheme.surfaceContainerHighest,
        padding: const EdgeInsets.all(12),
        width: double.infinity,
        child: const Text(
          'No receiver selected. Pick one on the Receiver tab.',
        ),
      );
    }
    return Material(
      color: Theme.of(context).colorScheme.surfaceContainer,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        child: Column(
          children: [
            Row(
              children: [
                const Icon(Icons.speaker, size: 20),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    '${c.selected!.friendlyName} · ${c.nowPlayingTitle ?? "—"}',
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                IconButton(
                  icon: Icon(
                    c.state == PlayState.playing
                        ? Icons.pause
                        : Icons.play_arrow,
                  ),
                  onPressed: c.nowPlayingTitle == null
                      ? null
                      : () => c.state == PlayState.playing
                          ? c.pause()
                          : c.resume(),
                ),
                IconButton(
                  icon: const Icon(Icons.stop),
                  onPressed: c.nowPlayingTitle == null ? null : c.stop,
                ),
              ],
            ),
            Row(
              children: [
                const Icon(Icons.volume_down, size: 18),
                Expanded(
                  child: Slider(
                    value: c.volume.toDouble(),
                    min: 0,
                    max: 100,
                    onChanged: (v) => c.setVolume(v.round()),
                  ),
                ),
                const Icon(Icons.volume_up, size: 18),
                SizedBox(
                  width: 36,
                  child: Text('${c.volume}', textAlign: TextAlign.end),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _ReceiverTab extends StatelessWidget {
  final CastController controller;
  const _ReceiverTab({required this.controller});

  @override
  Widget build(BuildContext context) {
    final c = controller;
    if (c.discovering && c.renderers.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    if (c.renderers.isEmpty) {
      return Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.wifi_off, size: 48),
            const SizedBox(height: 12),
            const Text(
              'No DLNA receivers found.\n\n'
              'Check that:\n'
              '• The receiver is powered on\n'
              '• Its NETWORK port is wired to your router\n'
              '• Your phone is on the same Wi-Fi (not guest/isolation)\n'
              '• Yamaha "Network Standby" is On',
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 16),
            FilledButton.icon(
              onPressed: c.discover,
              icon: const Icon(Icons.search),
              label: const Text('Scan again'),
            ),
          ],
        ),
      );
    }
    return ListView(
      children: [
        for (final r in c.renderers)
          RadioListTile<String>(
            value: r.uniqueKey,
            groupValue: c.selected?.uniqueKey,
            onChanged: (_) => c.select(r),
            title: Text(r.friendlyName),
            subtitle: Text(
              [
                r.manufacturer,
                r.modelName,
                r.location.host,
              ].where((s) => s.isNotEmpty).join(' · '),
            ),
          ),
      ],
    );
  }
}

class _StationsTab extends StatelessWidget {
  final CastController controller;
  const _StationsTab({required this.controller});

  @override
  Widget build(BuildContext context) {
    final c = controller;
    return Stack(
      children: [
        ListView.separated(
          padding: const EdgeInsets.only(bottom: 80),
          itemCount: c.stations.length,
          separatorBuilder: (_, __) => const Divider(height: 1),
          itemBuilder: (context, i) {
            final s = c.stations[i];
            return ListTile(
              leading: const Icon(Icons.radio),
              title: Text(s.name),
              subtitle: Text(
                s.url,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
              onTap: () => _play(context, s),
              trailing: IconButton(
                icon: const Icon(Icons.delete_outline),
                onPressed: () => c.removeStation(s),
              ),
            );
          },
        ),
        Positioned(
          right: 16,
          bottom: 16,
          child: FloatingActionButton.extended(
            onPressed: () => _showAddDialog(context),
            icon: const Icon(Icons.add),
            label: const Text('Add stream'),
          ),
        ),
      ],
    );
  }

  Future<void> _play(BuildContext context, SavedStation s) async {
    if (controller.selected == null) {
      _snack(context, 'Pick a receiver first.');
      return;
    }
    try {
      await controller.playUrl(s.url, title: s.name, mime: s.mime);
    } catch (e) {
      if (context.mounted) _snack(context, 'Failed: $e');
    }
  }

  Future<void> _showAddDialog(BuildContext context) async {
    final nameC = TextEditingController();
    final urlC = TextEditingController();
    final mimeC = TextEditingController(text: 'audio/mpeg');
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Add stream URL'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: nameC,
              decoration: const InputDecoration(labelText: 'Name'),
            ),
            TextField(
              controller: urlC,
              decoration: const InputDecoration(
                labelText: 'Stream URL (direct .mp3/.aac/HLS)',
              ),
            ),
            TextField(
              controller: mimeC,
              decoration: const InputDecoration(labelText: 'MIME'),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Save'),
          ),
        ],
      ),
    );
    if (ok == true && nameC.text.isNotEmpty && urlC.text.isNotEmpty) {
      await controller.addStation(
        SavedStation(nameC.text, urlC.text, mimeC.text),
      );
    }
  }
}

class _FilesTab extends StatelessWidget {
  final CastController controller;
  const _FilesTab({required this.controller});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(Icons.library_music, size: 48),
          const SizedBox(height: 12),
          const Text(
            'Pick an audio file from your phone. '
            'Firefly will start a tiny local web server and tell the '
            'receiver to stream it over Wi-Fi.',
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 20),
          FilledButton.icon(
            icon: const Icon(Icons.folder_open),
            label: const Text('Pick audio file'),
            onPressed: () => _pick(context),
          ),
        ],
      ),
    );
  }

  Future<void> _pick(BuildContext context) async {
    if (controller.selected == null) {
      _snack(context, 'Pick a receiver first.');
      return;
    }
    if (Platform.isAndroid) {
      await Permission.audio.request();
    }
    final result = await FilePicker.platform.pickFiles(
      type: FileType.audio,
      allowMultiple: false,
    );
    if (result == null || result.files.isEmpty) return;
    final path = result.files.single.path;
    if (path == null) return;
    try {
      await controller.playLocalFile(File(path));
    } catch (e) {
      if (context.mounted) _snack(context, 'Failed: $e');
    }
  }
}

void _snack(BuildContext context, String msg) {
  ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
}
