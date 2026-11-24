import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

/// State + actions for the spotcast bridge on the home Pi (:8090/api).
class SpotcastController extends ChangeNotifier {
  /// Last-resort guesses; normally the saved host or a subnet scan finds it.
  static const _candidates = ['192.168.1.110', '192.168.1.115'];

  String? host;
  /// True when [host] is the Pi's Tailscale address (away from home).
  bool remote = false;
  Map<String, dynamic>? status;
  bool connecting = true;
  String? error;
  /// Consecutive failed polls; status is kept (shown as stale) meanwhile.
  int failures = 0;
  DateTime? lastOk;
  String? _token;
  String? _remoteHost;
  Timer? _timer;
  DateTime? _volumeHoldUntil;
  bool _searching = false;

  Map<String, String> get _headers =>
      _token == null ? const {} : {'X-Firefly-Token': _token!};

  // ---------------------------------------------------------------- lifecycle
  Future<void> start() async {
    connecting = true;
    error = null;
    notifyListeners();
    final prefs = await SharedPreferences.getInstance();
    _token = prefs.getString('spotcastToken');
    _remoteHost = prefs.getString('spotcastRemote');
    await _locate(prefs);
    connecting = false;
    if (host == null) {
      error = 'Speaker bridge not found on this network';
      notifyListeners();
      _schedule(const Duration(seconds: 15));
      return;
    }
    await refresh();
  }

  /// Saved host, known addresses, then a scan of the local /24 for :8090,
  /// then the Pi's Tailscale address (works away from home).
  Future<void> _locate(SharedPreferences prefs) async {
    final saved = prefs.getString('spotcastHost');
    host = null;
    remote = false;
    for (final h in {if (saved != null) saved, ..._candidates}) {
      if (await _probe(h)) {
        host = h;
        break;
      }
    }
    host ??= await _sweep();
    if (host == null && _remoteHost != null && await _probe(_remoteHost!)) {
      host = _remoteHost;
      remote = true;
    }
    if (host != null && !remote) await prefs.setString('spotcastHost', host!);
  }

  Future<bool> _probe(String h, {Duration timeout = const Duration(seconds: 2)}) async {
    try {
      final r = await http
          .get(Uri.parse('http://$h:8090/api/status'), headers: _headers)
          .timeout(timeout);
      return r.statusCode == 200 && r.body.contains('"receiver"');
    } catch (_) {
      return false;
    }
  }

  /// Find the bridge by scanning this device's /24 for port 8090.
  Future<String?> _sweep() async {
    String? prefix;
    try {
      for (final iface in await NetworkInterface.list(type: InternetAddressType.IPv4)) {
        for (final a in iface.addresses) {
          final ip = a.address;
          if (ip.startsWith('192.168.') || ip.startsWith('10.') || ip.startsWith('172.')) {
            prefix ??= ip.substring(0, ip.lastIndexOf('.') + 1);
          }
        }
      }
    } catch (_) {}
    if (prefix == null) return null;
    final open = <String>[];
    for (var start = 1; start < 255; start += 64) {
      await Future.wait([
        for (var i = start; i < start + 64 && i < 255; i++)
          Socket.connect('$prefix$i', 8090, timeout: const Duration(milliseconds: 600))
              .then((s) {
            s.destroy();
            open.add('$prefix$i');
          }).catchError((_) {}),
      ]);
    }
    for (final h in open) {
      if (await _probe(h)) return h;
    }
    return null;
  }

  Future<void> setHost(String h) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('spotcastHost', h.trim());
    await start();
  }

  /// Apply a pairing link from the Pi's status page QR code:
  /// firefly://pair?h=<lan ip>&t=<token>&ts=<tailscale ip>
  Future<bool> pair(String link) async {
    final u = Uri.tryParse(link.trim());
    if (u == null || u.scheme != 'firefly' || u.queryParameters['t'] == null) return false;
    final prefs = await SharedPreferences.getInstance();
    final q = u.queryParameters;
    await prefs.setString('spotcastToken', q['t']!);
    if (q['h'] != null) await prefs.setString('spotcastHost', q['h']!);
    if (q['ts'] != null) await prefs.setString('spotcastRemote', q['ts']!);
    await start();
    return host != null;
  }

  bool get paired => _token != null;
  String? get remoteHost => _remoteHost;

  void _schedule(Duration d) {
    _timer?.cancel();
    _timer = Timer(d, refresh);
  }

  /// Poll every 2 s while healthy; on failure keep the last status, back off
  /// 2/5/15/30 s, and after a few misses look for the bridge again.
  Future<void> refresh() async {
    if (host == null) {
      if (!_searching) await _research();
      if (host == null) {
        _schedule(const Duration(seconds: 30));
        return;
      }
    }
    try {
      final r = await http
          .get(Uri.parse('http://$host:8090/api/status'), headers: _headers)
          .timeout(const Duration(seconds: 4));
      if (r.statusCode == 401) throw const HttpException('pairing token required');
      final s = jsonDecode(r.body) as Map<String, dynamic>;
      // don't let a poll fight the volume slider while the user drags it
      if (_volumeHoldUntil != null && DateTime.now().isBefore(_volumeHoldUntil!)) {
        s['receiver'] = {...(s['receiver'] as Map), 'volume_db': volumeDb};
      }
      status = s;
      error = null;
      failures = 0;
      lastOk = DateTime.now();
      final ts = s['tailscale_ip'] as String?;
      if (ts != null && ts != _remoteHost) {
        _remoteHost = ts;
        unawaited(SharedPreferences.getInstance()
            .then((p) => p.setString('spotcastRemote', ts)));
      }
      _schedule(const Duration(seconds: 2));
    } catch (e) {
      failures++;
      error = e is HttpException
          ? 'Pair the app to use it away from home'
          : 'Reconnecting to the speaker bridge…';
      const steps = [2, 5, 15, 30];
      _schedule(Duration(seconds: steps[(failures - 1).clamp(0, steps.length - 1)]));
      if (failures == 3 || (failures > 3 && failures % 6 == 0)) unawaited(_research());
    }
    notifyListeners();
  }

  /// Address may have changed (DHCP, Wi-Fi <-> home, Ethernet <-> Wi-Fi).
  Future<void> _research() async {
    if (_searching) return;
    _searching = true;
    try {
      final prefs = await SharedPreferences.getInstance();
      final old = host;
      await _locate(prefs);
      host ??= old;
    } finally {
      _searching = false;
    }
  }

  Future<Map<String, dynamic>?> _get(String path, [Map<String, String>? q]) async {
    if (host == null) return null;
    try {
      final r = await http
          .get(Uri.http('$host:8090', '/api/$path', q), headers: _headers)
          .timeout(const Duration(seconds: 20));
      return jsonDecode(r.body) as Map<String, dynamic>;
    } catch (_) {
      return null;
    }
  }

  Future<void> _call(String path, [Map<String, String>? q]) async {
    await _get(path, q);
    await refresh();
  }

  /// Live checks from the Pi: [{id, ok, detail, fix}].
  Future<List<Map>> diagnose() async {
    final r = await _get('diagnose');
    return ((r?['checks'] as List?) ?? const []).cast<Map>();
  }

  /// Ask the Pi to search for the receiver (it moved to a new address).
  Future<bool> rediscoverReceiver() async {
    final r = await _get('rediscover');
    await refresh();
    return r?['ok'] == true;
  }

  /// A 2-second test tone served by the Pi, for the setup wizard.
  String? get toneUrl => host == null || remote ? null : 'http://$host:8090/tone.wav';

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  // ---------------------------------------------------------------- getters
  /// Showing data: live, or the last good status while reconnecting.
  bool get online => status != null && (error == null || failures < 3);
  bool get reconnecting => failures > 0 && status != null;
  bool get stale => failures >= 3;
  String? get receiverIp => receiver['ip'] as String?;
  Map get alerts => (status?['alerts'] ?? const {}) as Map;
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

  // ------------------------------------------------- receiver extras (/api/x)
  /// Sources, sound, Zone 2, DSP, scenes, schedules, history on the Pi.
  Map get extras => (status?['extras'] ?? const {}) as Map;
  String? get activeSource => extras['active'] as String?;
  String? get activeLabel => extras['label'] as String?;
  Map get extrasNowPlaying => (extras['now_playing'] ?? const {}) as Map;

  Future<Map<String, dynamic>?> x(String path, [Map<String, String>? q]) async {
    final r = await _get('x/$path', q);
    unawaited(refresh());
    return r;
  }

  Future<Map<String, dynamic>?> xPost(String path, Object body) async {
    if (host == null) return null;
    try {
      final r = await http
          .post(Uri.http('$host:8090', '/api/x/$path'),
              headers: {..._headers, 'Content-Type': 'application/json'},
              body: jsonEncode(body))
          .timeout(const Duration(seconds: 20));
      return jsonDecode(r.body) as Map<String, dynamic>;
    } catch (_) {
      return null;
    }
  }

  /// Internet radio played by the Pi (so it takes part in last-source-wins,
  /// schedules and the EQ).
  Future<bool> playRadioOnBridge(String url, String name) async =>
      (await x('radio', {'url': url, 'name': name}))?['radio'] != null;

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
