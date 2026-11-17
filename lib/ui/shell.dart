import 'package:flutter/material.dart';

import '../services/cast_controller.dart';
import '../services/spotcast.dart';
import '../theme.dart';
import 'home_screen.dart';
import 'library_screen.dart';
import 'now_playing_sheet.dart';
import 'radio_screen.dart';
import 'settings_screen.dart';

/// Bundles the two controllers so every screen gets both.
class AppState {
  final CastController cast;
  final SpotcastController spot;
  AppState(this.cast, this.spot);
}

class Shell extends StatefulWidget {
  final AppState app;
  const Shell({super.key, required this.app});

  @override
  State<Shell> createState() => _ShellState();
}

class _ShellState extends State<Shell> {
  int _tab = 0;

  @override
  void initState() {
    super.initState();
    final a = widget.app;
    a.cast.addListener(_changed);
    a.spot.addListener(_changed);
    a.cast.loadStations();
    a.cast.loadRecents();
    a.cast.discover();
    a.spot.start();
  }

  @override
  void dispose() {
    widget.app.cast.removeListener(_changed);
    widget.app.spot.removeListener(_changed);
    super.dispose();
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final a = widget.app;
    final screens = [
      HomeScreen(app: a),
      RadioScreen(app: a),
      LibraryScreen(app: a),
      SettingsScreen(app: a),
    ];
    return Scaffold(
      body: SafeArea(
        bottom: false,
        child: IndexedStack(index: _tab, children: screens),
      ),
      bottomNavigationBar: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          MiniPlayer(app: a),
          NavigationBar(
            selectedIndex: _tab,
            onDestinationSelected: (i) => setState(() => _tab = i),
            destinations: const [
              NavigationDestination(
                  icon: Icon(Icons.home_outlined),
                  selectedIcon: Icon(Icons.home),
                  label: 'Home'),
              NavigationDestination(
                  icon: Icon(Icons.radio_outlined),
                  selectedIcon: Icon(Icons.radio),
                  label: 'Radio'),
              NavigationDestination(
                  icon: Icon(Icons.library_music_outlined),
                  selectedIcon: Icon(Icons.library_music),
                  label: 'Library'),
              NavigationDestination(
                  icon: Icon(Icons.tune_outlined),
                  selectedIcon: Icon(Icons.tune),
                  label: 'Settings'),
            ],
          ),
        ],
      ),
    );
  }
}

/// What the receiver is playing right now, from either source.
class NowPlayingInfo {
  final String source; // 'spotify' | 'firefly' | ''
  final String title;
  final String subtitle;
  final String? art;
  final bool playing;
  const NowPlayingInfo(this.source, this.title, this.subtitle, this.art, this.playing);

  static NowPlayingInfo of(AppState a) {
    final s = a.spot;
    if (s.online && s.input == 'PC' && (s.playing || s.title != null)) {
      return NowPlayingInfo('spotify', s.title ?? 'Spotify', s.artists, s.art, s.playing);
    }
    final c = a.cast;
    if (c.nowPlayingTitle != null && c.state != PlayState.stopped) {
      return NowPlayingInfo('firefly', c.nowPlayingTitle!,
          c.selected?.friendlyName ?? 'Receiver', null, c.state == PlayState.playing);
    }
    return const NowPlayingInfo('', '', '', null, false);
  }
}

class MiniPlayer extends StatelessWidget {
  final AppState app;
  const MiniPlayer({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    final np = NowPlayingInfo.of(app);
    if (np.source.isEmpty) return const SizedBox.shrink();
    final accent = np.source == 'spotify' ? FireflyTheme.spotify : FireflyTheme.amber;
    return Material(
      color: FireflyTheme.surfaceHigh,
      child: InkWell(
        onTap: () => showNowPlaying(context, app),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 4, 8),
          child: Row(children: [
            ArtThumb(url: np.art, size: 44, accent: accent, source: np.source),
            const SizedBox(width: 12),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(np.title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w600)),
                Text(np.subtitle.isEmpty ? 'Living Room' : np.subtitle,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: Colors.white.withValues(alpha: 0.6), fontSize: 13)),
              ]),
            ),
            IconButton(
              icon: Icon(np.playing ? Icons.pause_rounded : Icons.play_arrow_rounded, size: 32),
              onPressed: () => togglePlayback(app, np),
            ),
            if (np.source == 'spotify')
              IconButton(
                icon: const Icon(Icons.skip_next_rounded),
                onPressed: () => app.spot.control('next'),
              ),
          ]),
        ),
      ),
    );
  }
}

Future<void> togglePlayback(AppState a, NowPlayingInfo np) async {
  if (np.source == 'spotify') return a.spot.playPause();
  if (np.playing) return a.cast.pause();
  return a.cast.resume();
}

class ArtThumb extends StatelessWidget {
  final String? url;
  final double size;
  final Color accent;
  final String source;
  const ArtThumb(
      {super.key, this.url, required this.size, required this.accent, this.source = ''});

  @override
  Widget build(BuildContext context) {
    final fallback = Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        gradient: LinearGradient(
          colors: [accent.withValues(alpha: 0.8), accent.withValues(alpha: 0.25)],
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
        ),
        borderRadius: BorderRadius.circular(size * 0.18),
      ),
      child: Icon(source == 'spotify' ? Icons.music_note : Icons.radio,
          color: Colors.black87, size: size * 0.5),
    );
    if (url == null || url!.isEmpty) return fallback;
    return ClipRRect(
      borderRadius: BorderRadius.circular(size * 0.18),
      child: Image.network(url!,
          width: size, height: size, fit: BoxFit.cover, errorBuilder: (_, __, ___) => fallback),
    );
  }
}
