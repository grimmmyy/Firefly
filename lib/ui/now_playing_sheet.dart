import 'package:flutter/material.dart';

import '../theme.dart';
import 'shell.dart';
import 'widgets.dart';

void showNowPlaying(BuildContext context, AppState app) {
  showModalBottomSheet(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    backgroundColor: FireflyTheme.bg,
    builder: (_) => NowPlayingSheet(app: app),
  );
}

class NowPlayingSheet extends StatefulWidget {
  final AppState app;
  const NowPlayingSheet({super.key, required this.app});

  @override
  State<NowPlayingSheet> createState() => _NowPlayingSheetState();
}

class _NowPlayingSheetState extends State<NowPlayingSheet> {
  @override
  void initState() {
    super.initState();
    widget.app.spot.addListener(_c);
    widget.app.cast.addListener(_c);
  }

  @override
  void dispose() {
    widget.app.spot.removeListener(_c);
    widget.app.cast.removeListener(_c);
    super.dispose();
  }

  void _c() {
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final a = widget.app;
    final np = NowPlayingInfo.of(a);
    final spotify = np.source == 'spotify';
    final accent = spotify ? FireflyTheme.spotify : FireflyTheme.amber;
    final width = MediaQuery.of(context).size.width;
    final artSize = (width - 64).clamp(160.0, 420.0);

    return DecoratedBox(
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [accent.withValues(alpha: 0.22), FireflyTheme.bg],
        ),
      ),
      child: ListView(
        padding: const EdgeInsets.fromLTRB(32, 12, 32, 32),
        children: [
          Center(
            child: Container(
              width: 40,
              height: 4,
              decoration: BoxDecoration(
                  color: Colors.white24, borderRadius: BorderRadius.circular(2)),
            ),
          ),
          const SizedBox(height: 12),
          Row(children: [
            Icon(spotify ? Icons.queue_music : Icons.radio, size: 16, color: accent),
            const SizedBox(width: 6),
            Text(spotify ? 'Spotify · Living Room' : 'Firefly · Living Room',
                style: TextStyle(color: accent, fontWeight: FontWeight.w600)),
          ]),
          const SizedBox(height: 20),
          Center(
            child: ArtThumb(url: np.art, size: artSize, accent: accent, source: np.source),
          ),
          const SizedBox(height: 24),
          Text(np.title.isEmpty ? 'Nothing playing' : np.title,
              style: const TextStyle(fontSize: 24, fontWeight: FontWeight.w700)),
          const SizedBox(height: 4),
          Text(np.subtitle,
              style: TextStyle(fontSize: 16, color: Colors.white.withValues(alpha: 0.65))),
          const SizedBox(height: 20),
          Row(mainAxisAlignment: MainAxisAlignment.spaceEvenly, children: [
            if (spotify)
              IconButton(
                  iconSize: 40,
                  onPressed: () => a.spot.control('prev'),
                  icon: const Icon(Icons.skip_previous_rounded)),
            IconButton.filled(
              iconSize: 48,
              style: IconButton.styleFrom(
                  backgroundColor: Colors.white, foregroundColor: Colors.black),
              onPressed: np.source.isEmpty ? null : () => togglePlayback(a, np),
              icon: Icon(np.playing ? Icons.pause_rounded : Icons.play_arrow_rounded),
            ),
            if (spotify)
              IconButton(
                  iconSize: 40,
                  onPressed: () => a.spot.control('next'),
                  icon: const Icon(Icons.skip_next_rounded)),
            if (!spotify && np.source.isNotEmpty)
              IconButton(
                  iconSize: 36,
                  onPressed: a.cast.stop,
                  icon: const Icon(Icons.stop_rounded)),
          ]),
          const SizedBox(height: 16),
          ReceiverVolume(spot: a.spot),
          if (spotify && a.spot.queue.isNotEmpty) ...[
            const SectionLabel('Up next'),
            for (final q in a.spot.queue)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 4),
                child: Text('$q',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: Colors.white.withValues(alpha: 0.75))),
              ),
          ],
        ],
      ),
    );
  }
}
