import 'dart:async';

import 'package:flutter/material.dart';

import '../services/cast_controller.dart';
import '../services/radio_browser.dart';
import '../theme.dart';
import 'shell.dart';

class RadioScreen extends StatefulWidget {
  final AppState app;
  const RadioScreen({super.key, required this.app});

  @override
  State<RadioScreen> createState() => _RadioScreenState();
}

class _RadioScreenState extends State<RadioScreen> {
  final _rb = RadioBrowser();
  final _q = TextEditingController();
  Timer? _debounce;
  List<RadioStation> _results = [];
  bool _loading = false;
  String _heading = 'Popular in the US';

  static const _genres = [
    'jazz', 'hip-hop', 'rnb', 'chill', 'lofi', 'classical',
    'rock', 'pop', 'news', 'country', 'electronic', 'gospel',
  ];

  @override
  void initState() {
    super.initState();
    _load(() => _rb.top(), 'Popular in the US');
  }

  Future<void> _load(Future<List<RadioStation>> Function() f, String heading) async {
    setState(() {
      _loading = true;
      _heading = heading;
    });
    final r = await f();
    if (mounted) {
      setState(() {
        _results = r;
        _loading = false;
      });
    }
  }

  void _onSearch(String v) {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 450), () {
      if (v.trim().isEmpty) {
        _load(() => _rb.top(), 'Popular in the US');
      } else {
        _load(() => _rb.search(v.trim()), 'Results for “${v.trim()}”');
      }
    });
  }

  Future<void> _play(String name, String url, String mime) async {
    final a = widget.app;
    if (a.cast.selected == null && !(a.spot.online && !a.spot.remote)) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('No receiver selected yet: check Settings → Receiver')));
      return;
    }
    // Through the bridge when it's reachable: the Pi plays the station, so it
    // takes part in last-source-wins and the room EQ.
    if (a.spot.online && !a.spot.remote) {
      if (await a.spot.playRadioOnBridge(url, name)) return;
    }
    // Spotify and Firefly share the receiver: pause Spotify first.
    if (a.spot.online && a.spot.playing) await a.spot.control('pause');
    try {
      await a.cast.playUrl(url, title: name, mime: mime);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('Couldn\'t play: $e')));
      }
    }
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _q.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final c = widget.app.cast;
    final favUrls = c.stations.map((s) => s.url).toSet();
    return CustomScrollView(slivers: [
      SliverPadding(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
        sliver: SliverList.list(children: [
          const Text('Radio', style: TextStyle(fontSize: 30, fontWeight: FontWeight.w800)),
          const SizedBox(height: 12),
          SearchBar(
            controller: _q,
            hintText: 'Search 40,000+ stations',
            leading: const Icon(Icons.search),
            onChanged: _onSearch,
            elevation: const WidgetStatePropertyAll(0),
            backgroundColor: const WidgetStatePropertyAll(FireflyTheme.surfaceHigh),
          ),
          const SizedBox(height: 12),
          SizedBox(
            height: 38,
            child: ListView(scrollDirection: Axis.horizontal, children: [
              for (final g in _genres)
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ActionChip(
                    label: Text(g),
                    onPressed: () => _load(() => _rb.byTag(g), 'Genre: $g'),
                  ),
                ),
            ]),
          ),
          if (c.stations.isNotEmpty) ...[
            const SectionLabel('Favourites'),
            for (final s in c.stations)
              _StationTile(
                name: s.name,
                subtitle: 'Saved',
                fav: true,
                onTap: () => _play(s.name, s.url, s.mime),
                onFav: () => c.removeStation(s),
              ),
          ],
          SectionLabel(_heading),
          if (_loading)
            const Padding(
                padding: EdgeInsets.all(24),
                child: Center(child: CircularProgressIndicator())),
          if (!_loading && _results.isEmpty)
            Padding(
              padding: const EdgeInsets.all(24),
              child: Text('No playable stations found.',
                  style: TextStyle(color: Colors.white.withValues(alpha: 0.6))),
            ),
        ]),
      ),
      if (!_loading)
        SliverPadding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
          sliver: SliverList.builder(
            itemCount: _results.length,
            itemBuilder: (_, i) {
              final s = _results[i];
              final meta = [
                if (s.country.isNotEmpty) s.country,
                if (s.bitrate > 0) '${s.bitrate} kbps',
                if (s.tags.isNotEmpty) s.tags.split(',').take(2).join(', '),
              ].join(' · ');
              return _StationTile(
                name: s.name,
                subtitle: meta,
                favicon: s.favicon,
                fav: favUrls.contains(s.url),
                onTap: () => _play(s.name, s.url, s.mime),
                onFav: () => favUrls.contains(s.url)
                    ? c.removeStation(c.stations.firstWhere((x) => x.url == s.url))
                    : c.addStation(SavedStation(s.name, s.url, s.mime)),
              );
            },
          ),
        ),
    ]);
  }
}

class _StationTile extends StatelessWidget {
  final String name;
  final String subtitle;
  final String favicon;
  final bool fav;
  final VoidCallback onTap;
  final VoidCallback onFav;
  const _StationTile({
    required this.name,
    required this.subtitle,
    this.favicon = '',
    required this.fav,
    required this.onTap,
    required this.onFav,
  });

  @override
  Widget build(BuildContext context) {
    final fallback = Container(
      width: 48,
      height: 48,
      decoration: BoxDecoration(
          color: FireflyTheme.surfaceHigh, borderRadius: BorderRadius.circular(12)),
      child: const Icon(Icons.radio, color: FireflyTheme.amber),
    );
    return Card(
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
        leading: favicon.isEmpty
            ? fallback
            : ClipRRect(
                borderRadius: BorderRadius.circular(12),
                child: Image.network(favicon,
                    width: 48, height: 48, fit: BoxFit.cover,
                    errorBuilder: (_, __, ___) => fallback)),
        title: Text(name, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(subtitle, maxLines: 1, overflow: TextOverflow.ellipsis),
        trailing: IconButton(
          icon: Icon(fav ? Icons.favorite : Icons.favorite_border,
              color: fav ? FireflyTheme.amber : null),
          onPressed: onFav,
        ),
        onTap: onTap,
      ),
    );
  }
}
