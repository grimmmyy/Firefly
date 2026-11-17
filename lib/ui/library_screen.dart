import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:permission_handler/permission_handler.dart';

import '../theme.dart';
import 'shell.dart';
import 'widgets.dart';

class LibraryScreen extends StatelessWidget {
  final AppState app;
  const LibraryScreen({super.key, required this.app});

  Future<void> _pick(BuildContext context) async {
    final c = app.cast;
    if (c.selected == null) {
      _snack(context, 'No receiver selected yet: check Settings → Receiver');
      return;
    }
    if (Platform.isAndroid) await Permission.audio.request();
    final res = await FilePicker.platform.pickFiles(type: FileType.audio, allowMultiple: true);
    if (res == null || res.files.isEmpty) return;
    final files = res.files.where((f) => f.path != null).map((f) => File(f.path!)).toList();
    if (files.isEmpty) return;
    if (app.spot.online && app.spot.playing) await app.spot.control('pause');
    try {
      await c.playQueue(files);
    } catch (e) {
      if (context.mounted) _snack(context, 'Couldn\'t play: $e');
    }
  }

  void _snack(BuildContext context, String m) =>
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(m)));

  @override
  Widget build(BuildContext context) {
    final c = app.cast;
    final recents = c.recents.where((p) => File(p).existsSync()).toList();
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: [
        const Text('Library', style: TextStyle(fontSize: 30, fontWeight: FontWeight.w800)),
        const SizedBox(height: 4),
        Text('Play music files from this device on the receiver',
            style: TextStyle(color: Colors.white.withValues(alpha: 0.55))),
        const SizedBox(height: 16),
        Card(
          color: FireflyTheme.amber.withValues(alpha: 0.14),
          child: InkWell(
            borderRadius: BorderRadius.circular(20),
            onTap: () => _pick(context),
            child: const Padding(
              padding: EdgeInsets.all(20),
              child: Row(children: [
                Icon(Icons.add_circle_rounded, color: FireflyTheme.amber, size: 36),
                SizedBox(width: 14),
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text('Choose songs', style: TextStyle(fontSize: 17, fontWeight: FontWeight.w700)),
                    Text('MP3, FLAC, WAV, AAC/M4A · pick several to queue them'),
                  ]),
                ),
              ]),
            ),
          ),
        ),
        if (c.queue.isNotEmpty) ...[
          SectionLabel('Queue · ${c.queueIndex + 1} of ${c.queue.length}',
              trailing: TextButton(onPressed: c.clearQueue, child: const Text('Clear'))),
          for (var i = 0; i < c.queue.length; i++)
            Card(
              child: ListTile(
                leading: Icon(
                  i == c.queueIndex ? Icons.graphic_eq : Icons.music_note,
                  color: i == c.queueIndex ? FireflyTheme.amber : null,
                ),
                title: Text(c.basename(c.queue[i].path),
                    maxLines: 1, overflow: TextOverflow.ellipsis),
                onTap: () => c.playQueueIndex(i),
              ),
            ),
        ],
        if (recents.isNotEmpty) ...[
          SectionLabel('Recently played',
              trailing: TextButton(onPressed: c.clearRecents, child: const Text('Clear'))),
          for (final p in recents)
            Card(
              child: ListTile(
                leading: const Icon(Icons.history),
                title: Text(c.basename(p), maxLines: 1, overflow: TextOverflow.ellipsis),
                onTap: () async {
                  if (app.spot.online && app.spot.playing) await app.spot.control('pause');
                  await c.playQueue([File(p)]);
                },
              ),
            ),
        ],
        if (c.queue.isEmpty && recents.isEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 40),
            child: Column(children: [
              Icon(Icons.library_music_outlined,
                  size: 56, color: Colors.white.withValues(alpha: 0.25)),
              const SizedBox(height: 8),
              Text('Nothing here yet',
                  style: TextStyle(color: Colors.white.withValues(alpha: 0.5))),
            ]),
          ),
        if (c.selected != null) ...[
          const SectionLabel('Receiver volume'),
          ReceiverVolume(spot: app.spot),
        ],
      ],
    );
  }
}
