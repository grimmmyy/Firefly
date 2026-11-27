import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'media_server.dart';
import 'ssdp.dart';
import 'upnp_client.dart';

enum PlayState { idle, playing, paused, stopped }

/// Orchestrates discovery, the local media server, and the UPnP client.
class CastController extends ChangeNotifier {
  final SsdpDiscovery _ssdp = SsdpDiscovery();
  final MediaServer _server = MediaServer();

  List<Renderer> renderers = [];
  Renderer? selected;
  UpnpClient? _client;

  bool discovering = false;
  PlayState state = PlayState.idle;
  String? nowPlayingTitle;
  int volume = 30;

  List<SavedStation> stations = [];

  // ------------------------------------------------------------ recents + queue
  List<String> recents = [];
  List<File> queue = [];
  int queueIndex = 0;
  Timer? _advance;
  bool _sawPlaying = false;

  Future<void> loadRecents() async {
    final prefs = await SharedPreferences.getInstance();
    recents = prefs.getStringList('recents') ?? [];
    notifyListeners();
  }

  Future<void> _addRecent(String path) async {
    recents = [path, ...recents.where((p) => p != path)].take(25).toList();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setStringList('recents', recents);
  }

  Future<void> clearRecents() async {
    recents = [];
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('recents');
    notifyListeners();
  }

  /// Play files in order, advancing when the receiver finishes each one.
  Future<void> playQueue(List<File> files) async {
    queue = files;
    await playQueueIndex(0);
  }

  Future<void> playQueueIndex(int i) async {
    if (i < 0 || i >= queue.length) return;
    queueIndex = i;
    _sawPlaying = false;
    await playLocalFile(queue[i]);
    _advance?.cancel();
    _advance = Timer.periodic(const Duration(seconds: 2), (_) => _checkAdvance());
  }

  Future<void> _checkAdvance() async {
    final client = _client;
    if (client == null || queue.isEmpty) return;
    try {
      final st = (await client.getTransportInfo())['state'];
      if (st == 'PLAYING') _sawPlaying = true;
      if (_sawPlaying && st == 'STOPPED' && state == PlayState.playing) {
        if (queueIndex + 1 < queue.length) {
          await playQueueIndex(queueIndex + 1);
        } else {
          _advance?.cancel();
          state = PlayState.stopped;
          notifyListeners();
        }
      }
    } catch (_) {}
  }

  void clearQueue() {
    _advance?.cancel();
    queue = [];
    queueIndex = 0;
    notifyListeners();
  }

  String basename(String path) => _basename(path);

  Future<void> loadStations() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getStringList('stations') ?? _defaultStations;
    stations = raw.map(SavedStation.decode).whereType<SavedStation>().toList();
    notifyListeners();
  }

  Future<void> addStation(SavedStation s) async {
    stations = [...stations, s];
    await _persistStations();
    notifyListeners();
  }

  Future<void> removeStation(SavedStation s) async {
    stations = stations.where((e) => e.url != s.url).toList();
    await _persistStations();
    notifyListeners();
  }

  Future<void> _persistStations() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setStringList(
      'stations',
      stations.map((s) => s.encode()).toList(),
    );
  }

  Future<void> discover({bool sweep = false}) async {
    discovering = true;
    notifyListeners();
    try {
      final prefs = await SharedPreferences.getInstance();
      _ssdp.extraLocations = {
        ...(prefs.getStringList('receiverUrls') ?? const []),
        // Default seed for the home RX-V671.
        ...SsdpDiscovery.candidatesFor('192.168.1.7'),
      };
      renderers = await _ssdp.discover(sweep: sweep);
      // Remember every working description URL for next time.
      await prefs.setStringList('receiverUrls',
          {...renderers.map((r) => r.location.toString())}.toList());
      if (selected == null && renderers.length == 1) select(renderers.first);
      if (selected == null) {
        final yamaha = renderers.where((r) =>
            r.modelName.toUpperCase().startsWith('RX-'));
        if (yamaha.isNotEmpty) select(yamaha.first);
      }
      if (selected != null &&
          !renderers.any((r) => r.uniqueKey == selected!.uniqueKey)) {
        selected = null;
        _client = null;
      }
    } finally {
      discovering = false;
      notifyListeners();
    }
  }

  /// Add a receiver by IP address (or full description URL) and rescan.
  Future<void> addReceiverByIp(String ip) async {
    final prefs = await SharedPreferences.getInstance();
    final existing = prefs.getStringList('receiverUrls') ?? [];
    await prefs.setStringList('receiverUrls',
        {...existing, ...SsdpDiscovery.candidatesFor(ip)}.toList());
    await discover();
  }

  void select(Renderer r) {
    selected = r;
    _client = UpnpClient(r);
    unawaited(_refreshVolume());
    notifyListeners();
  }

  Future<void> _refreshVolume() async {
    try {
      final v = await _client?.getVolume();
      if (v != null) {
        volume = v;
        notifyListeners();
      }
    } catch (_) {}
  }

  Future<void> playLocalFile(File file, {String? title}) async {
    final client = _client;
    if (client == null) throw StateError('No renderer selected');
    final url = await _server.publish(file);
    unawaited(_addRecent(file.path));
    await client.setAvTransportUri(url, title: title ?? _basename(file.path));
    await client.play();
    state = PlayState.playing;
    nowPlayingTitle = title ?? _basename(file.path);
    notifyListeners();
  }

  Future<void> playUrl(String url, {String? title, String? mime}) async {
    final client = _client;
    if (client == null) throw StateError('No renderer selected');
    await client.setAvTransportUri(url, title: title ?? url, mime: mime);
    await client.play();
    state = PlayState.playing;
    nowPlayingTitle = title ?? url;
    notifyListeners();
  }

  Future<void> pause() async {
    await _client?.pause();
    state = PlayState.paused;
    notifyListeners();
  }

  Future<void> resume() async {
    await _client?.play();
    state = PlayState.playing;
    notifyListeners();
  }

  Future<void> stop() async {
    _advance?.cancel();
    await _client?.stop();
    state = PlayState.stopped;
    notifyListeners();
  }

  Future<void> setVolume(int v) async {
    volume = v.clamp(0, 100);
    notifyListeners();
    await _client?.setVolume(volume);
  }

  @override
  void dispose() {
    _advance?.cancel();
    _server.stop();
    super.dispose();
  }

  String _basename(String path) {
    final i = path.replaceAll('\\', '/').lastIndexOf('/');
    return i < 0 ? path : path.substring(i + 1);
  }

  static const _defaultStations = <String>[
    'SomaFM Groove Salad|https://ice1.somafm.com/groovesalad-128-mp3|audio/mpeg',
    'SomaFM Drone Zone|https://ice1.somafm.com/dronezone-128-mp3|audio/mpeg',
    'BBC Radio 1|http://stream.live.vc.bbcmedia.co.uk/bbc_radio_one|audio/mpeg',
    'KEXP 90.3 Seattle|https://kexp-mp3-128.streamguys1.com/kexp128.mp3|audio/mpeg',
  ];
}

class SavedStation {
  final String name;
  final String url;
  final String mime;
  SavedStation(this.name, this.url, this.mime);

  String encode() => '$name|$url|$mime';
  static SavedStation? decode(String s) {
    final parts = s.split('|');
    if (parts.length < 2) return null;
    return SavedStation(
      parts[0],
      parts[1],
      parts.length > 2 ? parts[2] : 'audio/mpeg',
    );
  }
}
