import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

/// State + actions for the spotcast bridge on the home Pi (:8090/api).
class SpotcastController extends ChangeNotifier {
  static const _candidates = ['192.168.1.110', '192.168.1.115'];

  String? host;
  Map<String, dynamic>? status;
  bool connecting = true;
  String? error;
  Timer? _timer;
  DateTime? _volumeHoldUntil;

  // ---------------------------------------------------------------- lifecycle
  Future<void> start() async {
    connecting = true;
    error = null;
    notifyListeners();
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString('spotcastHost');
    host = null;
    for (final h in [if (saved != null) saved, ..._candidates]) {
      if (await _probe(h)) {
        host = h;
        await prefs.setString('spotcastHost', h);
        break;
      }
    }
    connecting = false;
    if (host == null) {
      error = 'Speaker bridge not found on this network';
      notifyListeners();
      return;
    }
    await refresh();
    _timer?.cancel();
    _timer = Timer.periodic(const Duration(seconds: 2), (_) => refresh());
  }

  Future<bool> _probe(String h) async {
    try {
      final r = await http
          .get(Uri.parse('http://$h:8090/api/status'))
          .timeout(const Duration(seconds: 2));
      return r.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  Future<void> setHost(String h) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('spotcastHost', h.trim());
    await start();
  }

  Future<void> refresh() async {
    if (host == null) return;
    try {
      final r = await http
          .get(Uri.parse('http://$host:8090/api/status'))
          .timeout(const Duration(seconds: 4));
      final s = jsonDecode(r.body) as Map<String, dynamic>;
      // don't let a poll fight the volume slider while the user drags it
      if (_volumeHoldUntil != null && DateTime.now().isBefore(_volumeHoldUntil!)) {
        s['receiver'] = {...(s['receiver'] as Map), 'volume_db': volumeDb};
      }
      status = s;
      error = null;
    } catch (_) {
      error = 'Lost connection to the speaker bridge';
    }
    notifyListeners();
  }

  Future<void> _call(String path, [Map<String, String>? q]) async {
    if (host == null) return;
    try {
      await http
          .get(Uri.http('$host:8090', '/api/$path', q))
          .timeout(const Duration(seconds: 8));
    } catch (_) {}
    await refresh();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  // ---------------------------------------------------------------- getters
  bool get online => status != null && error == null;
  Map get receiver => (status?['receiver'] ?? const {}) as Map;
  bool get receiverOn => receiver['power'] == 'On';
  String? get input => receiver['input'] as String?;
  double? get volumeDb => (receiver['volume_db'] as num?)?.toDouble();
  double get maxDb => (receiver['max_db'] as num?)?.toDouble() ?? -10;
  bool get muted => receiver['mute'] == true;
  bool get playing => status?['playing'] == true;
  Map? get nowPlaying => status?['now_playing'] as Map?;
  String? get title => nowPlaying?['title'] as String?;
  String get artists =>
      ((nowPlaying?['artists'] as List?) ?? const []).join(', ');
  String? get art => nowPlaying?['art'] as String?;
  List get queue => (status?['queue'] as List?) ?? const [];
  int get sleepSeconds => (status?['sleep_s'] as num?)?.toInt() ?? 0;
  int get accounts => (status?['accounts'] as num?)?.toInt() ?? 0;
  List<Map> get accountsList =>
      ((status?['accounts_list'] as List?) ?? const []).cast<Map>();

  /// The linked account currently playing on the receiver, if any.
  Map? get playingAccount {
    for (final a in accountsList) {
      if (a['on_receiver'] == true) return a;
    }
    return null;
  }

  String? displayNameFor(String? username) {
    if (username == null) return null;
    for (final a in accountsList) {
      if (a['username'] == username) return a['display_name'] as String?;
    }
    return null;
  }

  Map get follow => (status?['follow'] ?? const {}) as Map;
  int get addMeSeconds => (status?['add_me_visible_s'] as num?)?.toInt() ?? 0;
  Map get devices => (status?['devices'] ?? const {}) as Map;
  Map get latency => (status?['latency'] ?? const {}) as Map;
  Map get health => (status?['health'] ?? const {}) as Map;
  Map get update => (status?['update'] ?? const {}) as Map;
  num get idleHandbackMin => (status?['idle_handback_min'] as num?) ?? 20;

  // ---------------------------------------------------------------- actions
  Future<void> control(String cmd) => _call('control', {'cmd': cmd});
  Future<void> playPause() => control(playing ? 'pause' : 'play');
  Future<void> power(bool on) => _call('receiver/power', {'on': on ? '1' : '0'});
  Future<void> selectInput(String i) => _call('receiver/input', {'input': i});
  Future<void> mute(bool on) => _call('receiver/mute', {'on': on ? '1' : '0'});
  Future<void> showAddMe() => _call('add');
  Future<void> ignore(String mac) => _call('ignore', {'mac': mac});
  Future<void> forget(String mac) => _call('forget', {'mac': mac});
  Future<void> handBack() => _call('handback');
  Future<void> sleep(int minutes) => _call('sleep', {'min': '$minutes'});
  Future<void> updateNow() => _call('update');
  Future<void> unlinkAccount(int slot) => _call('account/remove', {'slot': '$slot'});
  Future<void> armFollow() => _call('follow', {'on': '1'});

  /// Optimistic local volume while dragging; sent when the drag settles.
  void previewVolume(double db) {
    _volumeHoldUntil = DateTime.now().add(const Duration(seconds: 3));
    status?['receiver'] = {...receiver, 'volume_db': db};
    notifyListeners();
  }

  Future<void> setVolume(double db) async {
    _volumeHoldUntil = DateTime.now().add(const Duration(seconds: 3));
    await _call('receiver/volume', {'db': db.toStringAsFixed(1)});
  }
}

/// Friendly names for the RX-V671 inputs we expose.
const receiverInputs = <String, String>{
  'PC': 'Spotify / Firefly',
  'HDMI1': 'HDMI 1',
  'HDMI2': 'HDMI 2',
  'HDMI3': 'HDMI 3',
  'HDMI4': 'HDMI 4',
  'AV1': 'AV 1',
  'AUDIO1': 'Audio 1',
  'TUNER': 'FM/AM Tuner',
  'NET RADIO': 'Net Radio',
};
